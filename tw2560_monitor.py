#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW 2560 Trend Monitor v2.0
台股 2560 趨勢雷達

核心 2560：
- 25MA 向上
- 收盤站上 25MA
- 5期均量 > 60期均量

台股版週期：
- 1D：大趨勢
- 60m：回踩/承接
- 15m：SETUP
- 5m：ENTRY

狀態：
NO_TREND / WATCH / PULLBACK_READY / ENTRY / HOLD

原則：
- 趨勢成立 != 現在適合進場
- 空間不足只代表不追，不代表趨勢失效
- 只在台北時間 09:00~13:30 監控
- 不自動下單
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, time as dt_time
from email.header import Header
from pathlib import Path
from zoneinfo import ZoneInfo

TW_STOCKS = {
    "0050": "0050.TW",
    "2330": "2330.TW",
    "2317": "2317.TW",
    "2454": "2454.TW",
    "2382": "2382.TW",
    "3231": "3231.TW",
    "2308": "2308.TW",
}

TW_NAMES = {
    "0050": "元大台灣50",
    "2330": "台積電",
    "2317": "鴻海",
    "2454": "聯發科",
    "2382": "廣達",
    "3231": "緯創",
    "2308": "台達電",
}

TW_TZ = ZoneInfo("Asia/Taipei")
TW_OPEN = dt_time(9, 0)
TW_CLOSE = dt_time(13, 30)

ONE_D = 86400
ONE_H = 3600
FIFTEEN_M = 900
FIVE_M = 300

MIN_NEW_ENTRY_SPACE_PCT = 2.0
PULLBACK_BAND_PCT = 1.2
SUMMARY_INTERVAL = 1800

STATE_DIR = Path(".tw2560_state")
STATE_FILE = STATE_DIR / "state.json"

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (
    os.getenv("NTFY_TOPIC_TW2560", "").strip()
    or os.getenv("NTFY_TOPIC_SHORT35", "").strip()
)

GITHUB_EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "").strip()
MANUAL_RUN = GITHUB_EVENT_NAME == "workflow_dispatch"

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def price_text(v):
    if v is None:
        return "N/A"
