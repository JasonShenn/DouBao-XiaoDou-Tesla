#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 模板说明：telemetry consumer（精简版）。
# 每秒读一次收流服务的 journal，把 V 数据包合并成最新快照 telemetry_latest.json；
# 一次开车记一行 telemetry_trips.jsonl（行程落盘是真实逻辑，不是占位）。
# 只读 journal，不唤醒车、不发车辆命令、不碰令牌与私钥。
# 关键坑：journalctl -o json 会丢大包，必须用 -o cat + --after-cursor 增量读。

import json
import math
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
MI_TO_KM = 1.60934
INVALID = "<invalid>"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "..", "state")
LATEST_PATH = os.path.join(STATE_DIR, "telemetry_latest.json")
TRIPS_PATH = os.path.join(STATE_DIR, "telemetry_trips.jsonl")
OPEN_TRIP_PATH = os.path.join(STATE_DIR, "telemetry_trip_open.json")
CURSOR_PATH = os.path.join(STATE_DIR, "journal_cursor.txt")
UNIT_NAME = "fleet-telemetry-doubao"  # 改成你自己的收流服务名
PARK_CLOSE_SECONDS = 300  # 停稳满 5 分钟，一趟行程才落盘
BATTERY_KWH = 57.0  # 电池可用容量（度），按自己车型改：Model 3 标准续航约 57
TRACE_INTERVAL_S = 10  # 轨迹采样间隔（秒），一趟最多记 3000 个点
MIN_TRIP_KM = 0.1  # 不足 0.1 公里且不足 1 分钟的行程丢弃
MIN_TRIP_SECONDS = 60


def now_iso():
    return datetime.now(timezone.utc).astimezone(CST).isoformat(timespec="seconds")


def iso_from_ts(ts):
    return datetime.fromtimestamp(ts, tz=CST).isoformat(timespec="seconds")


def num(v):
    if v is None or v == INVALID or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_json(path, data):
    # 临时文件名带进程号：watcher、推送脚本也写同一个目录，共用 .tmp 名会撞车
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def append_jsonl(path, record):
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if new_file:
        os.chmod(path, 0o600)


def read_new_entries(cursor):
    """用 --after-cursor 增量读 journal，返回（新游标，V 包列表）。"""
    cmd = ["journalctl", "-u", UNIT_NAME, "-o", "cat", "--no-pager"]
    if cursor:
        cmd += ["--after-cursor", cursor]
    else:
        cmd += ["--since", "30 minutes ago"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    entries, new_cursor = [], cursor
    for line in out.stdout.splitlines():
        if line.startswith("-- cursor:"):
            new_cursor = line.split(":", 1)[1].strip()
            continue
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return new_cursor, entries


# ---- 行程记录 ----
# 开 trip：挂 D/R 档，或车速 > 0.5 km/h。
# 关 trip：停稳满 PARK_CLOSE_SECONDS（300 秒）后落盘；end_time 取最后一次动的时间，
# 不是落盘时间，所以停车后等的 5 分钟不算进行程时长。
# 断点续记：进行中的 trip 每轮都写 telemetry_trip_open.json；
# 重启时读到它，停稳已满 5 分钟就按最后一次动的时间估算落盘（estimated=true），
# 否则接着记。地名不写进行程记录：起终点只存经纬度，发提醒时现查高德逆地理编码。

def extract_latlon(snap):
    # 位置可能是嵌套的 Location dict，也可能是 LocationLatitude/Longitude 平铺字段
    loc = snap.get("Location")
    if isinstance(loc, dict):
        lat, lon = num(loc.get("latitude")), num(loc.get("longitude"))
    else:
        lat, lon = num(snap.get("LocationLatitude")), num(snap.get("LocationLongitude"))
    if lat is None or lon is None:
        return None, None
    return lat, lon


class TripRecorder:
    def __init__(self):
        self.open_trip = load_json(OPEN_TRIP_PATH)

    def _new_trip(self, snap, now_ts):
        lat, lon = extract_latlon(snap)
        start_dt = datetime.fromtimestamp(now_ts, tz=CST)
        return {
            "id": "trip-" + start_dt.strftime("%Y%m%d-%H%M%S"),
            "start_ts": now_ts,
            "start_time": iso_from_ts(now_ts),
            "start_lat": lat,
            "start_lon": lon,
            "start_odometer_km": num(snap.get("odometer_km")),
            "start_soc": num(snap.get("Soc")),
            "last_move_ts": now_ts,
            "last_lat": lat,
            "last_lon": lon,
            "last_odometer_km": num(snap.get("odometer_km")),
            "last_soc": num(snap.get("Soc")),
            "max_speed_kmh": num(snap.get("speed_kmh")) or 0.0,
            "trace": [],
            "last_trace_ts": 0,
        }

    def _trace_point(self, snap, now_ts):
        lat, lon = extract_latlon(snap)
        if lat is None or lon is None:
            return None
        return {
            "t": iso_from_ts(now_ts),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "speed_kmh": num(snap.get("speed_kmh")),
        }

    def update(self, snap, now_ts):
        """每收到一个新快照调一次。"""
        speed = num(snap.get("speed_kmh"))
        gear = snap.get("Gear")
        moving = (speed is not None and speed > 0.5) or gear in ("D", "R")
        if self.open_trip is None:
            if moving:
                self.open_trip = self._new_trip(snap, now_ts)
                point = self._trace_point(snap, now_ts)
                if point:
                    self.open_trip["trace"].append(point)
                    self.open_trip["last_trace_ts"] = now_ts
                save_json(OPEN_TRIP_PATH, self.open_trip)
            return
        trip = self.open_trip
        if moving:
            trip["last_move_ts"] = now_ts
            lat, lon = extract_latlon(snap)
            if lat is not None and lon is not None:
                trip["last_lat"], trip["last_lon"] = lat, lon
        if num(snap.get("odometer_km")) is not None:
            trip["last_odometer_km"] = num(snap.get("odometer_km"))
        if num(snap.get("Soc")) is not None:
            trip["last_soc"] = num(snap.get("Soc"))
        if speed is not None and speed > trip["max_speed_kmh"]:
            trip["max_speed_kmh"] = speed
        if now_ts - trip["last_trace_ts"] >= TRACE_INTERVAL_S and len(trip["trace"]) < 3000:
            point = self._trace_point(snap, now_ts)
            if point:
                trip["trace"].append(point)
                trip["last_trace_ts"] = now_ts
        save_json(OPEN_TRIP_PATH, trip)

    def _close(self, trip, end_ts, estimated):
        start_ts = trip["start_ts"]
        duration = int(end_ts - start_ts)
        start_odo = trip.get("start_odometer_km")
        end_odo = trip.get("last_odometer_km")
        distance = None
        if start_odo is not None and end_odo is not None and end_odo >= start_odo:
            raw_km = end_odo - start_odo
            if raw_km < MIN_TRIP_KM and duration < MIN_TRIP_SECONDS:
                return False  # 太短的行程丢弃（用未四舍五入的里程判，避免 0.05 进位成 0.1）
            distance = round(raw_km, 1)
        start_soc, end_soc = trip.get("start_soc"), trip.get("last_soc")
        energy = None
        if start_soc is not None and end_soc is not None and start_soc > end_soc:
            energy = round((start_soc - end_soc) / 100.0 * BATTERY_KWH, 1)
        record = {
            "id": trip["id"],
            "start_time": trip["start_time"],
            "end_time": iso_from_ts(end_ts),
            "start_lat": trip.get("start_lat"),
            "start_lon": trip.get("start_lon"),
            "end_lat": trip.get("last_lat"),
            "end_lon": trip.get("last_lon"),
            "distance_km": distance,
            "duration_sec": duration,
            "avg_speed_kmh": round(distance / (duration / 3600.0), 1) if distance is not None and duration > 0 else None,
            "max_speed_kmh": round(trip["max_speed_kmh"], 1),
            "start_soc": start_soc,
            "end_soc": end_soc,
            "energy_kwh": energy,
            "estimated": estimated,
            "trace": trip["trace"],
        }
        append_jsonl(TRIPS_PATH, record)
        return True

    def maybe_close(self, now_ts):
        """每轮都调：停稳满 5 分钟就落盘。返回 True 表示刚落了一条。"""
        if self.open_trip is None:
            return False
        if now_ts - self.open_trip["last_move_ts"] < PARK_CLOSE_SECONDS:
            return False
        self._close(self.open_trip, self.open_trip["last_move_ts"], estimated=False)
        self.open_trip = None
        try:
            os.remove(OPEN_TRIP_PATH)
        except OSError:
            pass
        return True

    def recover_on_start(self, now_ts):
        """启动时调一次：上次没落盘就按最后一次动的时间估算落盘。"""
        if self.open_trip is None:
            return
        if now_ts - self.open_trip["last_move_ts"] >= PARK_CLOSE_SECONDS:
            self._close(self.open_trip, self.open_trip["last_move_ts"], estimated=True)
            self.open_trip = None
            try:
                os.remove(OPEN_TRIP_PATH)
            except OSError:
                pass


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    cursor = open(CURSOR_PATH).read().strip() if os.path.exists(CURSOR_PATH) else ""
    snap = {}
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, encoding="utf-8") as f:
            snap = json.load(f)
    trips = TripRecorder()
    trips.recover_on_start(time.time())
    while True:
        try:
            cursor, entries = read_new_entries(cursor)
            for e in entries:
                # V 包是增量形态：每包只带变化的字段，跨包合并才完整
                if e.get("type") == "V" or "data" in e:
                    for k, v in (e.get("data") or {}).items():
                        if v != INVALID:
                            snap[k] = v
                    snap["updated_at"] = now_iso()
                    # 距离类字段原始单位是英里，写出时同时给公里
                    if num(snap.get("EstBatteryRange")) is not None:
                        snap["range_km"] = num(snap["EstBatteryRange"]) * MI_TO_KM
                    if num(snap.get("Odometer")) is not None:
                        snap["odometer_km"] = num(snap["Odometer"]) * MI_TO_KM
                    if num(snap.get("VehicleSpeed")) is not None:
                        snap["speed_kmh"] = num(snap["VehicleSpeed"]) * MI_TO_KM
                    trips.update(snap, time.time())
            if entries:
                save_json(LATEST_PATH, snap)
            if cursor:
                with open(CURSOR_PATH, "w") as f:
                    f.write(cursor)
            trips.maybe_close(time.time())
        except Exception as exc:  # 单轮出错不退出，下一秒继续
            print(json.dumps({"error": str(exc), "at": now_iso()}, ensure_ascii=False), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
