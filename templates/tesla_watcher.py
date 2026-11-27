#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 模板说明：特斯拉提醒 watcher（每秒轮询一次快照，按规则写提醒进 outbox）。
# 本文件逻辑与实测跑通的版本一致，只需按 README 第 10 步改阈值与文案。
# 敏感值：本文件不含任何密钥、域名、坐标、车名，照原样可用。
# 注意：save_json 用“路径 + 进程号”做临时文件名，两个进程写同一个 outbox 也不会撞车，
# 不要改回共用同一个 .tmp 文件名。
import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "..", "state")
SNAPSHOT_PATH = os.path.join(STATE_DIR, "telemetry_latest.json")
STATE_PATH = os.path.join(STATE_DIR, "watcher-state.json")
OUTBOX_PATH = os.path.join(STATE_DIR, "alert-outbox.json")
LOW_RANGE_MILES = 62.14
PARK_SECONDS = 300
HARD_BRAKE_DROP_KMH = 60.0
HARD_BRAKE_ACCEL = -4.0
HARD_BRAKE_WINDOW_S = 12
HARD_BRAKE_COOLDOWN_S = 60
SPEEDING_TOLERANCE_KMH = 5.0
SPEEDING_MIN_INTERVAL_S = 60
SPEEDING_REARM_KMH = 3.0
MOVING_MPH = 0.5
WATCH_INTERVAL_S = 1.0
# Fallback segment limits only; official per-road limits are not available.
LIMITS = {"highway": 120, "elevated": 80, "city": 60, "feeder": 40}


def now_iso():
    return datetime.now(timezone.utc).astimezone(CST).isoformat(timespec="seconds")


def num(v):
    if v is None or v == "<invalid>":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def snapshot_fields(snap):
    speed = num(snap.get("speed_kmh"))
    gear = snap.get("gear") or snap.get("Gear")
    brake = bool(snap.get("brake_pedal") or snap.get("BrakePedal"))
    accel = num(snap.get("longitudinal_accel") or snap.get("LongitudinalAcceleration"))
    range_km = num(snap.get("range_km") or snap.get("EstBatteryRange"))
    charge_state = snap.get("charge_state") or snap.get("ChargeState")
    detailed = snap.get("detailed_charge_state") or snap.get("DetailedChargeState")
    loc = snap.get("location") or snap.get("Location") or {}
    if isinstance(loc, dict):
        lat = loc.get("latitude")
        lon = loc.get("longitude")
    else:
        lat = lon = None
    return {
        "speed_kmh": speed, "gear": gear, "brake": brake, "accel": accel,
        "range_km": range_km, "charge_state": charge_state, "detailed": detailed,
        "lat": lat, "lon": lon,
    }


def init_state():
    return {
        "last_v_time": None, "started_trip": False, "park_alerted": True,
        "last_move_ts": None, "park_since": None, "last_hard_brake": 0,
        "last_speeding": 0, "low_range_armed": True, "charge_session": {},
        "speeding_active": False, "speeding_limit": None,
        "history": [], "pending": [], "claimed": [], "seq": 0,
    }


def hard_brake_window(history):
    """Return (from_speed, to_speed) when speed fell >=60 km/h inside window.

    history keeps at most 30 frames; each entry already has speed_kmh.
    """
    cutoff = time.time() - HARD_BRAKE_WINDOW_S
    recent = [h for h in history if h.get("speed_kmh") is not None]
    # Prefer the newest frame and the oldest frame still inside the window.
    recent_in_window = [h for h in recent if h.get("ts", time.time()) >= cutoff]
    candidates = recent_in_window or recent
    if len(candidates) < 2:
        return None
    old = candidates[0]
    new = candidates[-1]
    old_speed = num(old.get("speed_kmh"))
    new_speed = num(new.get("speed_kmh"))
    if old_speed is None or new_speed is None:
        return None
    if old_speed - new_speed >= HARD_BRAKE_DROP_KMH:
        return old_speed, new_speed
    return None


def evaluate(snap, state):
    f = snapshot_fields(snap)
    alerts = []
    now = time.time()
    history = state.setdefault("history", [])
    f["ts"] = now
    history.append(f)
    history[:] = history[-30:]

    moving = (f["speed_kmh"] or 0) > 0.5 or f["gear"] in ("D", "R")
    if moving:
        state["last_move_ts"] = now
        state["park_since"] = None
        if not state["started_trip"] and ((f["speed_kmh"] or 0) > 0.5 or f["gear"] in ("D", "R")):
            state["started_trip"] = True
            state["park_alerted"] = False
            alerts.append(("start", "车辆启动，已挂挡出发", f))
    else:
        if state["last_move_ts"]:
            state["park_since"] = state.get("park_since") or now
        if state["park_since"] and now - state["park_since"] >= PARK_SECONDS and not state["park_alerted"]:
            state["park_alerted"] = True
            state["started_trip"] = False
            alerts.append(("park", "车辆已停稳满 5 分钟", f))

    brake_window = hard_brake_window(history)
    accel_brake = f["brake"] and f["accel"] is not None and f["accel"] <= HARD_BRAKE_ACCEL
    if brake_window or accel_brake:
        if now - state["last_hard_brake"] >= HARD_BRAKE_COOLDOWN_S:
            state["last_hard_brake"] = now
            if brake_window:
                old_speed, new_speed = brake_window
                msg = f"10 秒内车速从 {old_speed:.0f} 降到 {new_speed:.0f} km/h"
            else:
                msg = "检测到急刹"
            alerts.append(("harsh_brake", msg, f))

    speed = f["speed_kmh"] or 0
    # Segment fallback: >=90 km/h is treated as highway limit 120; below that
    # the city fallback limit is 60. Road-specific limits are unavailable.
    limit = LIMITS["highway"] if speed >= 90 else LIMITS["city"]
    speeding_active = state.get("speeding_active", False)
    if not speeding_active and speed > limit + SPEEDING_TOLERANCE_KMH:
        if now - state["last_speeding"] >= SPEEDING_MIN_INTERVAL_S:
            state["last_speeding"] = now
            state["speeding_active"] = True
            state["speeding_limit"] = limit
            alerts.append(("speeding", f"车速 {speed:.0f} km/h，超过兜底限速 {limit} km/h", f))
    elif speeding_active and speed <= state.get("speeding_limit", limit) - SPEEDING_REARM_KMH:
        state["speeding_active"] = False

    if f["range_km"] is not None and f["range_km"] < 100:
        if state["low_range_armed"]:
            state["low_range_armed"] = False
            alerts.append(("low_range", f"续航剩余 {f['range_km']:.0f} km，低于 100 km", f))
    elif f["range_km"] is not None and f["range_km"] >= 100:
        state["low_range_armed"] = True

    charge = state.get("charge_session") or {}
    state["charge_session"] = charge
    charging = str(f["charge_state"]).lower() == "charging" or "charging" in str(f["detailed"]).lower()
    complete = "complete" in str(f["detailed"]).lower() or str(f["charge_state"]).lower() == "complete"
    stopped = str(f["charge_state"]).lower() in ("stopped", "disconnected", "idle")

    if charging and not charge.get("active"):
        charge.update(active=True, started_at=now, started_range=f["range_km"],
                      started_soc=num(snap.get("Soc")),
                      started_energy=num(snap.get("DCChargingEnergyIn") or snap.get("ACChargingEnergyIn")))
        alerts.append(("charge_start", "开始充电", f))
    elif complete and charge.get("active"):
        charge.update(active=False, ended_at=now)
        alerts.append(("charge_complete", "充电已充满", f))
    elif stopped and charge.get("active"):
        charge.update(active=False, ended_at=now)
        alerts.append(("charge_stop", "充电中途停止", f))

    return alerts


def run_once(args):
    state = load_json(args.state, init_state())
    if not state:
        state = init_state()
    snap = load_json(args.snapshot, {})
    alerts = evaluate(snap, state)

    outbox = load_json(args.outbox, {"alerts": [], "seq": state.get("seq", 0)})
    for kind, message, data in alerts:
        outbox["seq"] += 1
        outbox["alerts"].append({
            "id": f"alert-{outbox['seq']}", "kind": kind, "message": message,
            "data": data, "created_at": now_iso(), "status": "pending",
        })
    save_json(args.state, state)
    save_json(args.outbox, outbox)
    print(json.dumps(outbox, ensure_ascii=False, indent=2))
    return outbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--outbox", default=OUTBOX_PATH)
    parser.add_argument("--snapshot", default=SNAPSHOT_PATH)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    run_once(args)
    if args.once:
        return

    while True:
        time.sleep(WATCH_INTERVAL_S)
        try:
            run_once(args)
        except Exception as exc:  # keep the resident watcher alive across one bad cycle
            print(json.dumps({"error": str(exc), "at": now_iso()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
