#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resident Bark pusher for Doubao-owned Tesla alerts.

The script reads state/alert-outbox.json every 0.1 seconds, sends pending alerts
to Bark, and marks them sent. It only reads Doubao-owned snapshot/state
files; it never wakes the vehicle and never touches Muse files.

更新 2026-11-28：开车途中提醒（启动/超速/急刹/低续航）改 critical 级别 + alarm 提示音，
急刹再加 call 连响 30 秒；轮询间隔 0.5 秒改为 0.1 秒。

Bark device_key lives in credentials/bark_push.json. Leave
it empty until the user supplies it; --dry-run prepares messages without calling Bark.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "..", "state")
CRED_DIR = os.path.join(BASE_DIR, "..", "credentials")
OUTBOX_PATH = os.path.join(STATE_DIR, "alert-outbox.json")
SNAPSHOT_PATH = os.path.join(STATE_DIR, "telemetry_latest.json")
CONFIG_PATH = os.path.join(CRED_DIR, "bark_push.json")
SENT_PATH = os.path.join(STATE_DIR, "push_sent.json")
NIGHT_BUFFER_PATH = os.path.join(STATE_DIR, "bark_night_buffer.json")
MAIN_CHAT_QUEUE_PATH = os.path.join(STATE_DIR, "main_chat_queue.json")
TEST_PUSH_SWITCH_PATH = os.path.join(STATE_DIR, "allow_test_push")
BARK_TITLE = "<BARK_TITLE>"  #  Bark 通知标题，所有提醒共用，例如：小豆提醒
BARK_ICON_URL = "<AVATAR_URL>"  #  Bark 通知头像图片直链，须是公网能直接打开的图片地址
TIME_SENSITIVE_KINDS = {"start", "speeding", "harsh_brake", "low_range"}
# 2026-11-28 更新：开车途中会触发的提醒走 critical（穿透静音与勿扰，必响铃），急刹再加 call=1 连响 30 秒
CRITICAL_KINDS = {"start", "speeding", "harsh_brake", "low_range"}
CALL_KINDS = {"harsh_brake"}
ACTIVE_KINDS = {"park", "charge_start", "charge_complete", "charge_stop"}
NIGHT_START_MINUTES = 23 * 60 + 30
NIGHT_END_HOUR = 7
AMAP_KEY_PATH = "/home/<SERVER_USER>/amap.key"  # 高德 Web 服务 Key 所在文件（只存 Key 本身，600 权限）；没有 Key 就留空文件，查站功能会自动跳过
HOME_GCJ_LNG = 0.0  # 家附近经度（GCJ-02，高德坐标系，不是 WGS-84）；必填，见 README 占位符表
HOME_GCJ_LAT = 0.0  # 家附近纬度（GCJ-02）；必填，见 README 占位符表
HOME_AOI_NAME = "<HOME_AOI_NAME>"  # 家附近高德 AOI 名字，用于家充二次判定；不知道就留空字符串 ""
HOME_CHARGE_RADIUS_M = 300.0
BATTERY_USABLE_KWH = 57.0  # 示例：可用电量，按车型改
PEAK_PRICE = 0.60  # 示例电价：峰时每度 0.60 元，按当地实际电价改
VALLEY_PRICE = 0.30  # 示例电价：谷时每度 0.30 元，按当地实际电价改
POLL_INTERVAL_S = 0.1  # 2026-11-28 更新：0.5 秒改为 0.1 秒，提醒推出更快
RETRY_AFTER_SECONDS = 3.0
AMAP_TIMEOUT_S = 8.0
WEATHER_TIMEOUT_S = 4.0
RAIN_WMO_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
                  80, 81, 82, 95, 96, 99}
WMO_TEXT = {
    0: "晴", 1: "大致晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "大阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "冰雹雷阵雨", 99: "冰雹雷阵雨",
}


# ---------------------------- JSON / numeric helpers ----------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = str(path) + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def allow_test_push():
    return os.path.exists(TEST_PUSH_SWITCH_PATH)


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


def fmt_soc(v):
    n = num(v)
    return None if n is None else f"{n:.0f}"


def fmt_km(v):
    n = num(v)
    return None if n is None else f"{n:.0f}"


def fmt_kmh(v):
    n = num(v)
    return None if n is None else f"{n:.0f}"


def fmt_kwh(v, digits=1):
    n = num(v)
    return None if n is None else f"{n:.{digits}f}"


def fmt_price(v):
    n = num(v)
    return None if n is None else f"{n:.2f}"


# ---------------------------- Coordinates and Amap ----------------------------

def _out_of_china(lng, lat):
    return lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(lng, lat):
    ret = (-100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat
           + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng)))
    ret += ((20.0 * math.sin(6.0 * lng * math.pi)
             + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0) / 3.0
    ret += ((20.0 * math.sin(lat * math.pi)
             + 40.0 * math.sin((lat / 3.0) * math.pi)) * 2.0) / 3.0
    ret += ((160.0 * math.sin((lat / 12.0) * math.pi)
             + 320 * math.sin((lat * math.pi) / 30.0)) * 2.0) / 3.0
    return ret


def _transform_lng(lng, lat):
    ret = (300.0 + lng + 2.0 * lat + 0.1 * lng * lng
           + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng)))
    ret += ((20.0 * math.sin(6.0 * lng * math.pi)
             + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0) / 3.0
    ret += ((20.0 * math.sin(lng * math.pi)
             + 40.0 * math.sin((lng / 3.0) * math.pi)) * 2.0) / 3.0
    ret += ((150.0 * math.sin((lng / 12.0) * math.pi)
             + 300 * math.sin((lng / 30.0) * math.pi)) * 2.0) / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    if _out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = (lat / 180.0) * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    adj_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    adj_lng = (d_lng * 180.0) / ((a / sqrt_magic) * math.cos(rad_lat) * math.pi)
    return lng + adj_lng, lat + adj_lat


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def amap_key():
    try:
        with open(AMAP_KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def amap_get(path, params):
    key = amap_key()
    if not key:
        return None
    params = dict(params)
    params["key"] = key
    url = "https://restapi.amap.com" + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=AMAP_TIMEOUT_S) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("status") == "1" and data.get("infocode") == "10000":
            return data
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return None


def place_name(lat, lon):
    """Return readable Amap address/POI name; None when unavailable."""
    if lat is None or lon is None:
        return None
    gcj_lng, gcj_lat = wgs84_to_gcj02(lon, lat)
    data = amap_get("/v3/geocode/regeo", {"location": f"{gcj_lng:.6f},{gcj_lat:.6f}"})
    if not data:
        return None
    rc = data.get("regeocode") or {}
    aois = [a.get("name") for a in rc.get("aois") or [] if isinstance(a, dict) and a.get("name")]
    pois = [p.get("name") for p in rc.get("pois") or [] if isinstance(p, dict) and p.get("name")]
    if aois:
        return aois[0]
    if pois:
        return pois[0]
    return rc.get("formatted_address") or None


def charging_home(lat, lon, place):
    if lat is None or lon is None:
        return None
    gcj_lng, gcj_lat = wgs84_to_gcj02(lon, lat)
    dist_m = haversine_km(gcj_lat, gcj_lng, HOME_GCJ_LAT, HOME_GCJ_LNG) * 1000.0
    if dist_m <= HOME_CHARGE_RADIUS_M:
        return True
    return place == HOME_AOI_NAME


def nearby_stations(lat, lon, limit=3):
    if lat is None or lon is None:
        return []
    gcj_lng, gcj_lat = wgs84_to_gcj02(lon, lat)
    data = amap_get("/v3/place/around", {
        "location": f"{gcj_lng:.6f},{gcj_lat:.6f}",
        "types": "011100", "radius": 5000, "offset": 10,
    })
    if not data:
        return []
    out = []
    for poi in data.get("pois") or []:
        try:
            dist = int(float(poi.get("distance")))
        except (TypeError, ValueError):
            continue
        name = (poi.get("name") or "").strip()
        if name:
            out.append({"name": name, "distance_m": dist})
    out.sort(key=lambda x: x["distance_m"])
    return out[:limit]


# ---------------------------- Weather ----------------------------

def weather_for(lat, lon, outside_temp):
    """Return (weather_text, temp_c, rainy). Missing values return None."""
    if lat is not None and lon is not None:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,weather_code&timezone=Asia%2FShanghai")
        try:
            with urllib.request.urlopen(url, timeout=WEATHER_TIMEOUT_S) as r:
                data = json.loads(r.read().decode("utf-8"))
            cur = data.get("current") or {}
            temp = num(cur.get("temperature_2m"))
            code = cur.get("weather_code")
            text = WMO_TEXT.get(code)
            if temp is not None or text:
                return text, temp, code in RAIN_WMO_CODES
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            pass
    temp = num(outside_temp)
    return None, temp, None


def clothing_advice(temp_c, rainy):
    if temp_c is None:
        return None
    if temp_c < 10:
        advice = "穿厚点"
    elif temp_c < 17:
        advice = "薄外套加长袖"
    elif temp_c < 25:
        advice = "薄外套或长袖就行"
    elif temp_c <= 30:
        advice = "短袖防晒"
    else:
        advice = "注意防暑"
    if rainy:
        advice += "，记得带伞"
    return advice


# ---------------------------- Snapshot extraction ----------------------------

def power_kw(snap):
    values = [num(snap.get("ACChargingPower")), num(snap.get("DCChargingPower"))]
    values = [v for v in values if v is not None and v > 0]
    return max(values) if values else None


def snapshot_summary(snap):
    loc = snap.get("location") or snap.get("Location") or {}
    if not isinstance(loc, dict):
        loc = {}
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    soc = num(snap.get("Soc"))
    battery = num(snap.get("BatteryLevel"))
    return {
        "name": snap.get("VehicleName") or "Tesla",
        "vin": snap.get("Vin") or snap.get("vin") or "—",
        "soc": soc if soc is not None else battery,
        "range_km": num(snap.get("range_km") or snap.get("EstBatteryRange")),
        "odometer_km": num(snap.get("odometer_km") or snap.get("Odometer")),
        "speed_kmh": num(snap.get("speed_kmh")),
        "gear": snap.get("gear") or snap.get("Gear"),
        "lat": lat, "lon": lon,
        "limit_soc": num(snap.get("ChargeLimitSoc")),
        "power_kw": power_kw(snap),
        "ttfc": num(snap.get("TimeToFullCharge")),
        "energy_now": num(snap.get("DCChargingEnergyIn") or snap.get("ACChargingEnergyIn")),
        "outside_temp": num(snap.get("OutsideTemp") or snap.get("InsideTemp")),
    }


def charge_eta(s):
    if s["ttfc"] is not None and s["ttfc"] > 0:
        total_min = int(round(s["ttfc"] * 60))
        if total_min <= 0:
            return None, False
        h, m = divmod(total_min, 60)
        text = f"约 {h} 小时 {m} 分钟" if h and m else (f"约 {h} 小时" if h else f"约 {m} 分钟")
        return text, False
    if s["soc"] is not None and s["limit_soc"] is not None and s["power_kw"] and s["power_kw"] > 0 and s["limit_soc"] > s["soc"]:
        hours = (s["limit_soc"] - s["soc"]) / 100.0 * BATTERY_USABLE_KWH / s["power_kw"]
        total_min = int(round(hours * 60))
        h, m = divmod(total_min, 60)
        text = f"约 {h} 小时 {m} 分钟" if h and m else (f"约 {h} 小时" if h else f"约 {m} 分钟")
        return text, True
    return None, False


def charge_fee(kwh, started_at_iso, ended_at_iso):
    """Home TOU fee estimate. External station prices are looked up separately."""
    if kwh is None or kwh <= 0:
        return None
    start_dt = parse_iso(started_at_iso)
    end_dt = parse_iso(ended_at_iso)
    if not start_dt or not end_dt:
        return kwh * PEAK_PRICE, False
    peak_min = valley_min = 0.0
    cur = start_dt
    while cur < end_dt:
        nxt = min(end_dt, (cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        mins = (nxt - cur).total_seconds() / 60.0
        if 8 <= cur.hour < 22:
            peak_min += mins
        else:
            valley_min += mins
        cur = nxt
    total = (end_dt - start_dt).total_seconds() / 60.0
    if total <= 0:
        peak_kwh, valley_kwh = (kwh, 0.0) if 8 <= end_dt.hour < 22 else (0.0, kwh)
    else:
        peak_kwh = kwh * peak_min / total
        valley_kwh = kwh * valley_min / total
    fee = peak_kwh * PEAK_PRICE + valley_kwh * VALLEY_PRICE
    return round(fee, 2), True


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------- Message builders ----------------------------

def is_test_alert(alert):
    alert_id = str(alert.get("id") or "")
    alert_message = str(alert.get("message") or "")
    return alert_id.startswith("alert-test") or "测试" in alert_message


def build_text(alert, snap, state):
    if is_test_alert(alert):
        return str(alert.get("message") or "")
    kind = alert.get("kind")
    s = snapshot_summary(snap)
    data = alert.get("data") or {}
    place = place_name(s["lat"], s["lon"])
    weather, temp, rainy = weather_for(s["lat"], s["lon"], s["outside_temp"])

    if kind == "start":
        parts = ["出发啦 🚗"]
        if s["soc"] is not None:
            parts.append(f"电量 {s['soc']:.0f}%")
        if s["range_km"] is not None:
            parts.append(f"续航约 {fmt_km(s['range_km'])} 公里")
        if weather:
            parts.append(weather)
        if temp is not None:
            parts.append(f"{temp:.0f}°C")
        if place:
            parts.append(place)
        advice = clothing_advice(temp, rainy)
        text = "，".join(parts) + ("。" + advice if advice else "")
        return text

    if kind == "park":
    # Trip distance/time are retained in watcher state; here use snapshot delta only
        trip_km = None
        minutes = None
        parts = ["到啦 🏁"]
        if trip_km is not None:
            parts.append(f"本次行程 {trip_km:.1f} 公里")
        if minutes is not None:
            parts.append(f"用时 {minutes:.0f} 分钟")
        if s["soc"] is not None:
            parts.append(f"电量 {s['soc']:.0f}%")
        if s["range_km"] is not None:
            parts.append(f"续航约 {fmt_km(s['range_km'])} 公里")
        if place:
            parts.append(f"停在{place}")
        return "，".join(parts)

    if kind == "harsh_brake":
        old = data.get("old_speed") or num((alert.get("message") or "").split("从 ")[-1].split(" 降到")[0])
        new = data.get("new_speed") or num((alert.get("message") or "").split("降到 ")[-1].split(" km/h")[0])
        if old is not None and new is not None:
            return f"急刹车提醒 🛑 10 秒内从 {old:.0f} 降到 {new:.0f} km/h，注意安全。"
        return "急刹车提醒 🛑 注意安全。"

    if kind == "speeding":
        limit = state.get("speeding_limit") or 60
        speed = s["speed_kmh"]
        if speed is not None:
            return f"超速提醒 ⚠️ 现在 {speed:.0f} km/h，这段限速 {limit:.0f} km/h，慢一点。"
        return "超速提醒 ⚠️ 慢一点。"

    if kind == "low_range":
        parts = [f"电量低提醒 🔋 续航只剩约 {fmt_km(s['range_km'])} 公里，记得充电。"]
        stations = nearby_stations(s["lat"], s["lon"])
        if stations:
            station_text = "；".join(f"{x['name']}（{x['distance_m']} 米）" for x in stations)
            parts.append(f"附近充电站：{station_text}")
        else:
            parts.append("附近充电站：没查到")
        return "".join(parts)

    if kind == "charge_start":
        parts = ["开始充电 🔌"]
        if s["soc"] is not None:
            parts.append(f"电量 {s['soc']:.0f}%")
        if s["limit_soc"] is not None:
            parts.append(f"上限 {s['limit_soc']:.0f}%")
        if s["power_kw"] is not None:
            parts.append(f"功率 {s['power_kw']:.0f} kW")
        eta, estimated = charge_eta(s)
        if eta:
            parts.append(f"预计{eta}充满" + ("（估算）" if estimated else ""))
        loc = charging_home(s["lat"], s["lon"], place)
        if loc is True and place:
            parts.append(f"地点：{place}（家充）")
        elif loc is False and place:
            parts.append(f"地点：{place}（外桩）")
        return "，".join(parts)

    if kind in ("charge_complete", "charge_stop"):
        session = state.get("charge_session") or {}
        start_soc = num(session.get("started_soc"))
        end_soc = s["soc"]
        kwh = None
        kwh_est = False
        if s["energy_now"] is not None and session.get("started_energy") is not None:
            kwh = s["energy_now"] - num(session.get("started_energy"))
        elif start_soc is not None and end_soc is not None and end_soc > start_soc:
            kwh = (end_soc - start_soc) / 100.0 * BATTERY_USABLE_KWH
            kwh_est = True
        head = "充满啦 ✅" if kind == "charge_complete" else "充电停了 ⏸️"
        parts = [head]
        if kwh is not None:
            parts.append(f"这次充了 {kwh:.1f} 度" + ("（估算）" if kwh_est else ""))
        fee = None
        fee_home = charging_home(s["lat"], s["lon"], place)
        if fee_home and kwh is not None:
            fee, _ = charge_fee(kwh, session.get("started_at"), alert.get("created_at"))
        if fee is not None:
            parts.append(f"电费 ¥{fee:.2f}")
        return "，".join(parts)

    return alert.get("message") or "Tesla 提醒"


# ---------------------------- Bark ----------------------------

def bark_level(kind):
    if kind in CRITICAL_KINDS:
        return "critical"
    return "timeSensitive" if kind in TIME_SENSITIVE_KINDS else "active"


def is_night(now_dt):
    minutes = now_dt.hour * 60 + now_dt.minute
    return minutes >= NIGHT_START_MINUTES or now_dt.hour < NIGHT_END_HOUR


def bark_send(device_key, title, body, level, dry_run, sound="alarm", call=0):
    if dry_run:
        return {"dry_run": True, "title": title, "body": body, "level": level,
                "icon": BARK_ICON_URL, "sound": sound, "call": call}
    if not device_key:
        raise RuntimeError("Bark device_key is not configured")
    data = {
        "device_key": device_key,
        "title": title,
        "body": body,
        "level": level,
        "icon": BARK_ICON_URL,
        "sound": sound,
    }
    if call:
        data["call"] = 1
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        "https://api.day.app/push",
        data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def load_runtime_state():
    return load_json(SENT_PATH, {"sent_ids": []})


def process_once(dry_run):
    outbox = load_json(OUTBOX_PATH, {"alerts": [], "seq": 0})
    snap = load_json(SNAPSHOT_PATH, {})
    config = load_json(CONFIG_PATH, {"bark_key": ""})
    state = load_json(STATE_DIR and os.path.join(STATE_DIR, "watcher-state.json"), {})
    sent = load_runtime_state()
    sent_ids = set(sent.get("sent_ids", []))
    buffer_state = load_json(NIGHT_BUFFER_PATH, {"items": [], "night_start_sent": []})
    buffer_items = buffer_state.setdefault("items", [])
    night_start_sent = set(buffer_state.setdefault("night_start_sent", []))
    mirror_queue = load_json(MAIN_CHAT_QUEUE_PATH, {"items": []})
    mirror_items = mirror_queue.setdefault("items", [])
    now_dt = datetime.now(CST)
    night = is_night(now_dt)

    results = []
    pending = [a for a in outbox.get("alerts", []) if a.get("status") == "pending"]

    def mirror_to_main_chat(record):
        record = dict(record)
        record["main_chat_delivered"] = False
        record["queued_at"] = now_dt.isoformat(timespec="seconds")
        mirror_items.append(record)

    # Morning flush: release the previous night's complete/stopped charge alerts as
    # one active-level summary, instead of separate wake-up notifications.
    if not night and buffer_items:
        summary_text = "\n".join(item.get("text", "") for item in buffer_items if item.get("text"))
        level = "active"
        result = bark_send(config.get("bark_key", ""), "夜间充电汇总", summary_text, level, dry_run)
        summary_record = {"id": "night-summary", "kind": "night_summary",
                          "text": summary_text, "level": level,
                          "created_at": now_dt.isoformat(timespec="seconds")}
        mirror_to_main_chat(summary_record)
        for item in buffer_items:
            sent_ids.add(item.get("id"))
        results.append({
            "id": "night-summary", "kind": "night_summary",
            "text": summary_text, "level": level, "result": result,
        })
        buffer_items.clear()
        night_start_sent.clear()

    for alert in pending:
        aid = alert.get("id")
        kind = alert.get("kind") or "Tesla"
        if aid in sent_ids:
            alert["status"] = "already_sent"
            continue

        if is_test_alert(alert) and not allow_test_push():
            alert["status"] = "skipped"
            alert["skipped_at"] = now_dt.isoformat(timespec="seconds")
            print(f"跳过测试提醒 {aid}", flush=True)
            continue

        attempt_at = alert.get("last_attempt_at") or alert.get("pushed_at")
        if attempt_at:
            attempt_dt = parse_iso(attempt_at)
            if attempt_dt and now_dt - attempt_dt < timedelta(seconds=RETRY_AFTER_SECONDS):
                continue
        alert["last_attempt_at"] = now_dt.isoformat(timespec="seconds")
        text = build_text(alert, snap, state)

        if kind in ("charge_complete", "charge_stop") and night:
            alert["status"] = "buffered_night"
            alert["buffered_at"] = now_dt.isoformat(timespec="seconds")
            buffer_items.append({"id": aid, "kind": kind, "text": text,
                                 "created_at": alert.get("created_at")})
            continue

        if kind == "charge_start" and night:
            if aid in night_start_sent:
                alert["status"] = "already_sent"
                continue
            night_start_sent.add(aid)

        level = bark_level(kind)
        try:
            result = bark_send(config.get("bark_key", ""), BARK_TITLE, text, level, dry_run,
                               sound="alarm", call=1 if kind in CALL_KINDS else 0)
        except Exception as exc:
            alert["status"] = "pending"
            alert["last_error"] = str(exc)
            continue
        alert["status"] = "dry_run" if dry_run else "sent"
        alert["pushed_at"] = now_dt.isoformat(timespec="seconds")
        alert["level"] = level
        alert.pop("last_error", None)
        sent_ids.add(aid)
        if not is_test_alert(alert):
            mirror_to_main_chat({"id": aid, "kind": kind, "text": text, "level": level,
                                 "created_at": alert.get("created_at")})
        results.append({"id": aid, "kind": kind, "text": text, "level": level, "result": result})

    sent["sent_ids"] = sorted(sent_ids)
    buffer_state["night_start_sent"] = sorted(night_start_sent)
    save_json(OUTBOX_PATH, outbox)
    save_json(SENT_PATH, sent)
    save_json(NIGHT_BUFFER_PATH, buffer_state)
    save_json(MAIN_CHAT_QUEUE_PATH, mirror_queue)

    stale = False
    updated = snap.get("updated_at")
    if updated:
        updated_dt = parse_iso(updated)
        stale = updated_dt is None or datetime.now(CST) - updated_dt > timedelta(hours=24)
    else:
        stale = True
    return {"pending_count": len(pending), "sent_count": len(results),
            "buffered_night_count": len(buffer_items),
            "main_chat_queued_count": len(mirror_items), "night": night,
            "stale_snapshot": stale, "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="process one cycle and exit")
    args = parser.parse_args()

    if args.once or args.dry_run:
        print(json.dumps(process_once(args.dry_run), ensure_ascii=False, indent=2))
        return

    while True:
        try:
            report = process_once(False)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc), "at": datetime.now(timezone.utc).astimezone(CST).isoformat()},
                             ensure_ascii=False), flush=True)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
