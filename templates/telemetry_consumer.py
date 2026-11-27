#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 模板说明：telemetry consumer（精简版）。
# 每秒读一次收流服务的 journal，把 V 数据包合并成最新快照 telemetry_latest.json；
# 一次开车记一行 telemetry_trips.jsonl，一次充电记一行 telemetry_charging.jsonl。
# 只读 journal，不唤醒车、不发车辆命令、不碰令牌与私钥。
# 关键坑：journalctl -o json 会丢大包，必须用 -o cat + --after-cursor 增量读。

import json
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
CHARGING_PATH = os.path.join(STATE_DIR, "telemetry_charging.jsonl")
CURSOR_PATH = os.path.join(STATE_DIR, "journal_cursor.txt")
UNIT_NAME = "fleet-telemetry-doubao"  # 改成你自己的收流服务名
PARK_CLOSE_SECONDS = 300  # 停稳满 5 分钟，一趟行程才落盘


def now_iso():
    return datetime.now(timezone.utc).astimezone(CST).isoformat(timespec="seconds")


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


def main():
    cursor = open(CURSOR_PATH).read().strip() if os.path.exists(CURSOR_PATH) else ""
    snap = {}
    if os.path.exists(LATEST_PATH):
        with open(LATEST_PATH, encoding="utf-8") as f:
            snap = json.load(f)
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
            if entries:
                save_json(LATEST_PATH, snap)
            if cursor:
                with open(CURSOR_PATH, "w") as f:
                    f.write(cursor)
            # 行程 / 充电落盘：按 PARK_CLOSE_SECONDS 与 ChargeState 判定，
            # 完整实现见 README 阶段 9，先跑通快照再补这两块
        except Exception as exc:  # 单轮出错不退出，下一秒继续
            print(json.dumps({"error": str(exc), "at": now_iso()}, ensure_ascii=False), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
