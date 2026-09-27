#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW2560 Monitor v1.0
===================

台股版 2560 趨勢監控

狀態：
NO_SIGNAL
WATCH
TREND_READY
PRE-STRICT
STRICT

原始 STRICT 核心概念：
4H
- MA25 rising
- Close > MA25
- VolMA5 crosses above VolMA60

1D
- Close > MA25
- MA25 rising
- VolMA5 > VolMA60

台股版新增：
- WATCH / TREND_READY / PRE-STRICT / STRICT 階段價格記憶
- 各階段 -> 目前 漲跌幅
- PRE-STRICT -> STRICT 追價幅度
- 4H Swing 壓力
- 1D Swing 壓力
- 最近交易日前收 / 開盤 / 跳空 / 當日漲跌
- 休市時不亂發新的進場型通知
- ntfy 通知
- state 記憶

資料：
Yahoo Finance 公開圖表資料
不需 API Key
不自動下單
"""

import json
import os
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# 基本設定
# ============================================================

TAIPEI = ZoneInfo("Asia/Taipei")

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

STATE_DIR = Path(".tw2560_state")
STATE_FILE = STATE_DIR / "state.json"

DEDUP_BARS = 20


# ============================================================
# 第一批標的
# ============================================================

SYMBOLS = {
    "2330": {"name": "台積電", "ticker": "2330.TW"},
    "2317": {"name": "鴻海", "ticker": "2317.TW"},
    "2454": {"name": "聯發科", "ticker": "2454.TW"},
    "2382": {"name": "廣達", "ticker": "2382.TW"},
    "3231": {"name": "緯創", "ticker": "3231.TW"},
    "2308": {"name": "台達電", "ticker": "2308.TW"},
    "2345": {"name": "智邦", "ticker": "2345.TW"},
    "3661": {"name": "世芯-KY", "ticker": "3661.TW"},
    "3443": {"name": "創意", "ticker": "3443.TW"},
    "3017": {"name": "奇鋐", "ticker": "3017.TW"},
}


# ============================================================
# NTFY
# ============================================================

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

NTFY_TOPIC = os.getenv(
    "NTFY_TOPIC_TW2560",
    ""
).strip()


# ============================================================
# GitHub
# ============================================================

GITHUB_EVENT_NAME = os.getenv(
    "GITHUB_EVENT_NAME",
    ""
).strip()

MANUAL_RUN = (
    GITHUB_EVENT_NAME == "workflow_dispatch"
)


# ============================================================
# 時間
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_taipei():
    return datetime.now(TAIPEI)


def now_iso():
    return now_utc().isoformat()


def market_open_now():

    now = now_taipei()

    if now.weekday() >= 5:
        return False

    t = now.time()

    return (
        dt_time(9, 0)
        <= t
        <= dt_time(13, 30)
    )


# ============================================================
# 顯示工具
# ============================================================

def price_text(v):

    if v is None:
        return "N/A"

    if v >= 1000:
        return f"{v:.1f}"

    if v >= 100:
        return f"{v:.2f}"

    if v >= 10:
        return f"{v:.2f}"

    return f"{v:.3f}"


def pct_text(v):

    if v is None:
        return "N/A"

    return f"{v:+.2f}%"


def pct_change(start, end):

    if (
        start is None
        or end is None
        or start <= 0
    ):
        return None

    return (
        end / start - 1
    ) * 100


# ============================================================
# Yahoo API
# ============================================================

def yahoo_get(
    ticker,
    interval,
    range_,
    retries=4
):

    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_,
        "includePrePost": "false",
        "events": "div,splits",
    })

    url = (
        f"{YAHOO_BASE}/{ticker}"
        f"?{params}"
    )

    last_error = None

    for attempt in range(retries):

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 TW2560-Monitor/1.0",
                    "Accept":
                        "application/json",
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=25
            ) as resp:

                data = json.load(resp)

            result = (
                data
                .get("chart", {})
                .get("result")
            )

            if not result:
                raise RuntimeError(
                    f"No Yahoo data: {ticker}"
                )

            return result[0]

        except Exception as e:

            last_error = e

            time.sleep(
                min(
                    2 ** attempt,
                    6
                )
            )

    raise RuntimeError(
        f"Yahoo request failed "
        f"{ticker}: {last_error}"
    )


# ============================================================
# K線解析
# ============================================================

def parse_chart(result):

    timestamps = result.get(
        "timestamp",
        []
    ) or []

    indicators = result.get(
        "indicators",
        {}
    )

    quotes = indicators.get(
        "quote",
        []
    )

    if not quotes:
        return []

    q = quotes[0]

    opens = q.get("open", [])
    highs = q.get("high", [])
    lows = q.get("low", [])
    closes = q.get("close", [])
    volumes = q.get("volume", [])

    rows = []

    for i, ts in enumerate(timestamps):

        try:
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]
            v = volumes[i]

        except IndexError:
            continue

        if (
            o is None
            or h is None
            or l is None
            or c is None
        ):
            continue

        rows.append({
            "t": int(ts),
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": float(c),
            "v": float(v or 0),
        })

    rows.sort(
        key=lambda x: x["t"]
    )

    return rows


def fetch_chart(
    ticker,
    interval,
    range_
):

    result = yahoo_get(
        ticker,
        interval,
        range_
    )

    rows = parse_chart(
        result
    )

    meta = result.get(
        "meta",
        {}
    )

    return rows, meta


# ============================================================
# 交易日工具
# ============================================================

def row_local_date(row):

    return (
        datetime
        .fromtimestamp(
            row["t"],
            timezone.utc
        )
        .astimezone(
            TAIPEI
        )
        .date()
    )


def group_rows_by_date(rows):

    groups = {}

    for r in rows:

        d = row_local_date(r)

        groups.setdefault(
            d,
            []
        ).append(r)

    return groups


def latest_trade_day_info(
    r5,
    rd
):

    if not r5:
        return {
            "trade_date": None,
            "open_price": None,
            "previous_close": None,
            "gap_pct": None,
        }

    groups = group_rows_by_date(
        r5
    )

    dates = sorted(
        groups.keys()
    )

    if not dates:
        return {
            "trade_date": None,
            "open_price": None,
            "previous_close": None,
            "gap_pct": None,
        }

    latest_date = dates[-1]

    day_rows = groups[
        latest_date
    ]

    day_rows.sort(
        key=lambda x: x["t"]
    )

    open_price = (
        day_rows[0]["o"]
        if day_rows
        else None
    )

    daily_by_date = {}

    for r in rd:

        d = row_local_date(r)

        daily_by_date[d] = r

    previous_dates = sorted(
        [
            d
            for d in daily_by_date.keys()
            if d < latest_date
        ]
    )

    previous_close = None

    if previous_dates:

        prev_date = (
            previous_dates[-1]
        )

        previous_close = (
            daily_by_date[
                prev_date
            ]["c"]
        )

    gap_pct = (
        pct_change(
            previous_close,
            open_price
        )
        if (
            previous_close is not None
            and open_price is not None
        )
        else None
    )

    return {
        "trade_date":
            latest_date,

        "open_price":
            open_price,

        "previous_close":
            previous_close,

        "gap_pct":
            gap_pct,
    }


# ============================================================
# 均線
# ============================================================

def sma(
    values,
    n,
    i
):

    if i + 1 < n:
        return None

    return sum(
        values[
            i - n + 1:
            i + 1
        ]
    ) / n


def add_indicators(rows):

    closes = [
        r["c"]
        for r in rows
    ]

    volumes = [
        r["v"]
        for r in rows
    ]

    for i, r in enumerate(rows):

        r["i"] = i

        r["ma5"] = sma(
            closes,
            5,
            i
        )

        r["ma10"] = sma(
            closes,
            10,
            i
        )

        r["ma20"] = sma(
            closes,
            20,
            i
        )

        r["ma25"] = sma(
            closes,
            25,
            i
        )

        r["ma60"] = sma(
            closes,
            60,
            i
        )

        r["ma25_prev"] = (
            sma(
                closes,
                25,
                i - 1
            )
            if i >= 25
            else None
        )

        r["vma5"] = sma(
            volumes,
            5,
            i
        )

        r["vma60"] = sma(
            volumes,
            60,
            i
        )

        r["vma5_prev"] = (
            sma(
                volumes,
                5,
                i - 1
            )
            if i >= 5
            else None
        )

        r["vma60_prev"] = (
            sma(
                volumes,
                60,
                i - 1
            )
            if i >= 60
            else None
        )


# ============================================================
# 1H 多頭
# ============================================================

def one_hour_confirm(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] > r["ma20"]

        and

        r["ma5"] > r["ma10"]

        and

        r["ma10"] > r["ma20"]
    )


# ============================================================
# 4H 結構
# ============================================================

def four_hour_structure(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    return (
        r["ma25"]
        > r["ma25_prev"]

        and

        r["c"]
        > r["ma25"]
    )


def four_hour_early(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    return (
        r["c"] > r["ma25"]
        or
        r["ma25"] >= r["ma25_prev"]
    )


def relaxed_volume_ok(r):

    needed = [
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    near_long_volume = (
        r["vma5"]
        >= r["vma60"] * 0.90
    )

    volume_rising = (
        r["vma5"]
        > r["vma5_prev"] * 1.02
    )

    return (
        near_long_volume
        or
        volume_rising
    )


# ============================================================
# 原版 STRICT CORE
# ============================================================

def core_ok(r):

    needed = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
        r.get("vma60_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["ma25"]
        > r["ma25_prev"]

        and

        r["c"]
        > r["ma25"]

        and

        r["vma5_prev"]
        <= r["vma60_prev"]

        and

        r["vma5"]
        > r["vma60"]
    )


# ============================================================
# 1D Strict
# ============================================================

def daily_confirm(r):

    if r is None:
        return False

    needed = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] > r["ma25"]

        and

        r["ma25"]
        > r["ma25_prev"]

        and

        r["vma5"]
        > r["vma60"]
    )


def daily_soft_confirm(r):

    if r is None:
        return False

    if r.get("ma25") is None:
        return False

    return (
        r["c"]
        >= r["ma25"] * 0.97
    )


# ============================================================
# Swing High
# ============================================================

def find_swing_highs(
    rows,
    left=2,
    right=2
):

    swings = []

    if (
        len(rows)
        < left + right + 1
    ):
        return swings

    for i in range(
        left,
        len(rows) - right
    ):

        center = rows[i]

        high = center["h"]

        left_highs = [
            rows[j]["h"]
            for j in range(
                i - left,
                i
            )
        ]

        right_highs = [
            rows[j]["h"]
            for j in range(
                i + 1,
                i + right + 1
            )
        ]

        if (
            all(
                high > x
                for x in left_highs
            )

            and

            all(
                high >= x
                for x in right_highs
            )
        ):

            swings.append({
                "price": high,
                "time": center["t"],
            })

    return swings


def nearest_swing_resistance(
    rows,
    current_price,
    lookback=60
):

    subset = rows[
        -lookback:
    ]

    swings = (
        find_swing_highs(
            subset,
            2,
            2
        )
    )

    above = [
        s
        for s in swings
        if (
            s["price"]
            > current_price
        )
    ]

    if not above:
        return None

    return min(
        above,
        key=lambda s:
            s["price"]
    )


# ============================================================
# 單檔分析
# ============================================================

def analyze(
    code,
    info
):

    ticker = info["ticker"]

    name = info["name"]

    # 5m只拿來抓目前價與交易日資訊
    r5, meta5 = fetch_chart(
        ticker,
        "5m",
        "5d"
    )

    # Yahoo台股沒有原生4H
    # 先使用60m資料聚合概念替代
    r1, _ = fetch_chart(
        ticker,
        "60m",
        "3mo"
    )

    rd, _ = fetch_chart(
        ticker,
        "1d",
        "6mo"
    )

    if (
        len(r5) < 1
        or
        len(r1) < 65
        or
        len(rd) < 65
    ):

        return {
            "code": code,
            "name": name,
            "ticker": ticker,
            "status": "WAIT_HISTORY",
        }

    add_indicators(
        r1
    )

    add_indicators(
        rd
    )

    # --------------------------------------------------------
    # 台股4H：
    # 用60m資料，每4根聚合一根4H
    # --------------------------------------------------------

    r4 = []

    for i in range(
        0,
        len(r1),
        4
    ):

        chunk = r1[
            i:i + 4
        ]

        if len(chunk) < 4:
            continue

        r4.append({
            "t":
                chunk[0]["t"],

            "o":
                chunk[0]["o"],

            "h":
                max(
                    x["h"]
                    for x in chunk
                ),

            "l":
                min(
                    x["l"]
                    for x in chunk
                ),

            "c":
                chunk[-1]["c"],

            "v":
                sum(
                    x["v"]
                    for x in chunk
                ),
        })

    if len(r4) < 65:

        return {
            "code": code,
            "name": name,
            "ticker": ticker,
            "status": "WAIT_HISTORY",
        }

    add_indicators(
        r4
    )

    latest1 = r1[-1]
    previous1 = r1[-2]

    latest4 = r4[-1]
    latest_d = rd[-1]

    current_price = (
        meta5.get(
            "regularMarketPrice"
        )
    )

    if current_price is None:

        current_price = (
            r5[-1]["c"]
        )

    current_price = float(
        current_price
    )

    trade_info = (
        latest_trade_day_info(
            r5,
            rd
        )
    )

    previous_close = (
        trade_info[
            "previous_close"
        ]
    )

    first_open = (
        trade_info[
            "open_price"
        ]
    )

    gap_pct = (
        trade_info[
            "gap_pct"
        ]
    )

    day_change_pct = (
        pct_change(
            previous_close,
            current_price
        )
        if previous_close
        else None
    )

    oneh_now = (
        one_hour_confirm(
            latest1
        )
    )

    oneh_prev = (
        one_hour_confirm(
            previous1
        )
    )

    oneh_fresh = (
        oneh_now
        and
        not oneh_prev
    )

    h4_structure = (
        four_hour_structure(
            latest4
        )
    )

    h4_early = (
        four_hour_early(
            latest4
        )
    )

    relaxed_volume = (
        relaxed_volume_ok(
            latest4
        )
    )

    h4_core = (
        core_ok(
            latest4
        )
    )

    day_strict = (
        daily_confirm(
            latest_d
        )
    )

    day_soft = (
        daily_soft_confirm(
            latest_d
        )
    )

    strict_now = (
        h4_core
        and
        day_strict
    )

    pre_strict = (
        not strict_now

        and

        oneh_now

        and

        h4_structure

        and

        relaxed_volume

        and

        day_strict
    )

    trend_ready = (
        not strict_now

        and

        not pre_strict

        and

        oneh_now

        and

        h4_early

        and

        relaxed_volume

        and

        day_soft
    )

    watch = (
        not strict_now

        and

        not pre_strict

        and

        not trend_ready

        and

        oneh_now
    )

    if strict_now:

        status = "STRICT"

    elif pre_strict:

        status = "PRE-STRICT"

    elif trend_ready:

        status = "TREND_READY"

    elif watch:

        status = "WATCH"

    else:

        status = "NO_SIGNAL"

    volume_ratio = None

    if (
        latest4.get("vma5")
        is not None

        and

        latest4.get("vma60")
        not in (
            None,
            0
        )
    ):

        volume_ratio = (
            latest4["vma5"]
            /
            latest4["vma60"]
        )

    swing4 = (
        nearest_swing_resistance(
            r4,
            current_price,
            60
        )
    )

    swing1d = (
        nearest_swing_resistance(
            rd,
            current_price,
            90
        )
    )

    resistance_4h = (
        swing4["price"]
        if swing4
        else None
    )

    resistance_1d = (
        swing1d["price"]
        if swing1d
        else None
    )

    resistance_4h_pct = (
        pct_change(
            current_price,
            resistance_4h
        )
        if resistance_4h
        else None
    )

    resistance_1d_pct = (
        pct_change(
            current_price,
            resistance_1d
        )
        if resistance_1d
        else None
    )

    return {
        "code":
            code,

        "name":
            name,

        "ticker":
            ticker,

        "status":
            status,

        "current_price":
            current_price,

        "latest_4h_close":
            latest4["c"],

        "1h_confirm":
            oneh_now,

        "1h_fresh":
            oneh_fresh,

        "4h_early":
            h4_early,

        "4h_structure":
            h4_structure,

        "4h_volume_relaxed":
            relaxed_volume,

        "4h_volume_ratio":
            volume_ratio,

        "4h_core":
            h4_core,

        "1d_soft":
            day_soft,

        "1d_confirm":
            day_strict,

        "resistance_4h":
            resistance_4h,

        "resistance_4h_pct":
            resistance_4h_pct,

        "resistance_1d":
            resistance_1d,

        "resistance_1d_pct":
            resistance_1d_pct,

        "trade_date":
            str(
                trade_info[
                    "trade_date"
                ]
            )
            if trade_info[
                "trade_date"
            ]
            else None,

        "previous_close":
            previous_close,

        "first_open":
            first_open,

        "gap_pct":
            gap_pct,

        "day_change_pct":
            day_change_pct,

        "market_open":
            market_open_now(),
    }


# ============================================================
# ntfy
# ============================================================

def send_ntfy(
    title,
    message,
    priority="default",
    tags="bell"
):

    if not NTFY_TOPIC:

        print(
            "NTFY_TOPIC_TW2560 未設定"
        )

        return False

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Title":
                title,

            "Priority":
                priority,

            "Tags":
                tags,

            "Content-Type":
                "text/plain; charset=utf-8",
        }
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=20
        ) as resp:

            print(
                "NTFY:",
                resp.status,
                title
            )

            return True

    except Exception as e:

        print(
            "NTFY ERROR:",
            title,
            str(e)
        )

        return False


# ============================================================
# State
# ============================================================

def load_state():

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not STATE_FILE.exists():

        return {
            "symbols": {}
        }

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        state.setdefault(
            "symbols",
            {}
        )

        return state

    except Exception:

        return {
            "symbols": {}
        }


def save_state(state):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 階段價格記憶
# ============================================================

def reset_cycle(s):

    keys = [
        "watch_price",
        "watch_time",
        "trend_ready_price",
        "trend_ready_time",
        "pre_strict_price",
        "pre_strict_time",
        "strict_price",
        "strict_time",
    ]

    for key in keys:

        s.pop(
            key,
            None
        )


def update_stage_memory(
    r,
    s,
    previous
):

    status = r["status"]

    price = r[
        "current_price"
    ]

    s[
        "current_price"
    ] = price

    s[
        "last_seen_utc"
    ] = now_iso()

    active_states = (
        "WATCH",
        "TREND_READY",
        "PRE-STRICT",
        "STRICT",
    )

    if (
        previous
        in (
            "NO_SIGNAL",
            "UNKNOWN"
        )

        and

        status
        in active_states
    ):

        reset_cycle(s)

        s[
            "current_price"
        ] = price

    if (
        status == "WATCH"

        and

        s.get(
            "watch_price"
        )
        is None
    ):

        s[
            "watch_price"
        ] = price

        s[
            "watch_time"
        ] = now_iso()

    if (
        status == "TREND_READY"

        and

        s.get(
            "trend_ready_price"
        )
        is None
    ):

        s[
            "trend_ready_price"
        ] = price

        s[
            "trend_ready_time"
        ] = now_iso()

    if (
        status == "PRE-STRICT"

        and

        s.get(
            "pre_strict_price"
        )
        is None
    ):

        s[
            "pre_strict_price"
        ] = price

        s[
            "pre_strict_time"
        ] = now_iso()

    if (
        status == "STRICT"

        and

        s.get(
            "strict_price"
        )
        is None
    ):

        s[
            "strict_price"
        ] = price

        s[
            "strict_time"
        ] = now_iso()


# ============================================================
# 階段統計
# ============================================================

def stage_stats(s):

    current = s.get(
        "current_price"
    )

    watch = s.get(
        "watch_price"
    )

    trend = s.get(
        "trend_ready_price"
    )

    pre = s.get(
        "pre_strict_price"
    )

    strict = s.get(
        "strict_price"
    )

    return {
        "current":
            current,

        "watch":
            watch,

        "trend":
            trend,

        "pre":
            pre,

        "strict":
            strict,

        "watch_to_now":
            pct_change(
                watch,
                current
            ),

        "trend_to_now":
            pct_change(
                trend,
                current
            ),

        "pre_to_now":
            pct_change(
                pre,
                current
            ),

        "strict_to_now":
            pct_change(
                strict,
                current
            ),

        "pre_to_strict":
            pct_change(
                pre,
                strict
            ),
    }


def stage_block(s):

    x = stage_stats(s)

    lines = []

    if x["watch"] is not None:

        lines.append(
            "WATCH："
            + price_text(
                x["watch"]
            )
        )

    if x["trend"] is not None:

        lines.append(
            "TREND_READY："
            + price_text(
                x["trend"]
            )
        )

    if x["pre"] is not None:

        lines.append(
            "PRE-STRICT："
            + price_text(
                x["pre"]
            )
        )

    if x["strict"] is not None:

        lines.append(
            "STRICT："
            + price_text(
                x["strict"]
            )
        )

    lines.append(
        "目前："
        + price_text(
            x["current"]
        )
    )

    lines.append("")

    if x["watch_to_now"] is not None:

        lines.append(
            "WATCH→目前："
            + pct_text(
                x["watch_to_now"]
            )
        )

    if x["trend_to_now"] is not None:

        lines.append(
            "TREND_READY→目前："
            + pct_text(
                x["trend_to_now"]
            )
        )

    if x["pre_to_now"] is not None:

        lines.append(
            "PRE-STRICT→目前："
            + pct_text(
                x["pre_to_now"]
            )
        )

    if x["strict_to_now"] is not None:

        lines.append(
            "STRICT→目前："
            + pct_text(
                x["strict_to_now"]
            )
        )

    return "\n".join(
        lines
    )


# ============================================================
# 壓力顯示
# ============================================================

def resistance_block(r):

    if r["resistance_4h"] is None:

        r4 = (
            "4H Swing壓力：未找到"
        )

    else:

        r4 = (
            "4H Swing壓力："
            f"{price_text(r['resistance_4h'])} "
            f"({pct_text(r['resistance_4h_pct'])})"
        )

    if r["resistance_1d"] is None:

        d1 = (
            "1D Swing壓力：未找到"
        )

    else:

        d1 = (
            "1D Swing壓力："
            f"{price_text(r['resistance_1d'])} "
            f"({pct_text(r['resistance_1d_pct'])})"
        )

    return (
        f"{r4}\n"
        f"{d1}"
    )


# ============================================================
# 通知
# ============================================================

def notify_status(
    r,
    state
):

    code = r["code"]

    status = r["status"]

    s = (
        state
        .setdefault(
            "symbols",
            {}
        )
        .setdefault(
            code,
            {}
        )
    )

    previous = s.get(
        "status",
        "UNKNOWN"
    )

    print(
        f"{code} {r['name']}: "
        f"{previous} -> {status}"
    )

    update_stage_memory(
        r,
        s,
        previous
    )

    if status == previous:
        return

    stage = stage_block(
        s
    )

    resistance = (
        resistance_block(
            r
        )
    )

    common = (
        f"{r['code']} {r['name']}\n\n"

        f"{stage}\n\n"

        f"{resistance}\n\n"

        f"交易日："
        f"{r['trade_date']}\n"

        f"前收："
        f"{price_text(r['previous_close'])}\n"

        f"開盤："
        f"{price_text(r['first_open'])}\n"

        f"跳空："
        f"{pct_text(r['gap_pct'])}\n"

        f"當日漲跌："
        f"{pct_text(r['day_change_pct'])}\n\n"
    )

    if status == "STRICT":

        chase = (
            stage_stats(s)
            .get(
                "pre_to_strict"
            )
        )

        message = (
            common

            + f"PRE→STRICT追價幅度："
            f"{pct_text(chase)}\n\n"

            + "4H CORE=True\n"
            + "1D=True\n\n"
            + "進入台股2560人工複核。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 STRICT {code}",
                message,
                "high",
                "chart_with_upwards_trend,bell"
            )

    elif status == "PRE-STRICT":

        message = (
            common

            + "1H=True\n"
            + "4H structure=True\n"
            + "4H relaxed volume=True\n"
            + "1D=True\n\n"
            + "等待STRICT。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 PRE {code}",
                message,
                "high",
                "eyes"
            )

    elif status == "TREND_READY":

        message = (
            common

            + "1H=True\n"
            + "4H early=True\n"
            + "4H relaxed volume=True\n"
            + "1D soft=True\n\n"
            + "趨勢正在形成。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 TREND {code}",
                message,
                "default",
                "eyes"
            )

    elif status == "WATCH":

        message = (
            common

            + "1H多頭成立\n"
            + "等待4H與1D成熟。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 WATCH {code}",
                message,
                "default",
                "eyes"
            )

    elif status == "NO_SIGNAL":

        if previous in (
            "WATCH",
            "TREND_READY",
            "PRE-STRICT",
            "STRICT"
        ):

            send_ntfy(
                f"TW2560 INVALID {code}",
                common
                + f"前一狀態：{previous}\n"
                + "候選環境失效。",
                "default",
                "warning"
            )

    s["status"] = status

    s["updated_utc"] = (
        now_iso()
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "TW2560 Monitor | v1.0"
    )

    print(
        "Taipei:",
        now_taipei().isoformat()
    )

    print(
        "Market open:",
        market_open_now()
    )

    print(
        "Manual run:",
        MANUAL_RUN
    )

    state = load_state()

    results = []

    errors = []

    for code, info in SYMBOLS.items():

        try:

            r = analyze(
                code,
                info
            )

            results.append(
                r
            )

            if (
                r["status"]
                == "WAIT_HISTORY"
            ):

                print(
                    f"{code} "
                    f"{info['name']} "
                    f"WAIT_HISTORY"
                )

                continue

            ratio = (
                r.get(
                    "4h_volume_ratio"
                )
            )

            ratio_text = (
                f"{ratio:.2f}"
                if ratio is not None
                else "N/A"
            )

            print(
                f"{code} "
                f"{r['name']:<8} "
                f"{r['status']:<12} "

                f"price="
                f"{price_text(r['current_price'])} "

                f"1H="
                f"{r['1h_confirm']} "

                f"4Hearly="
                f"{r['4h_early']} "

                f"4Hstruct="
                f"{r['4h_structure']} "

                f"VolRelax="
                f"{r['4h_volume_relaxed']} "

                f"V5/V60="
                f"{ratio_text} "

                f"4Hcore="
                f"{r['4h_core']} "

                f"1Dsoft="
                f"{r['1d_soft']} "

                f"1D="
                f"{r['1d_confirm']} "

                f"R4H="
                f"{price_text(r['resistance_4h'])} "

                f"R1D="
                f"{price_text(r['resistance_1d'])} "

                f"gap="
                f"{pct_text(r['gap_pct'])}"
            )

            notify_status(
                r,
                state
            )

        except Exception as e:

            errors.append(
                (
                    code,
                    str(e)
                )
            )

            print(
                f"{code} "
                f"{info['name']} "
                f"ERROR {e}"
            )

        time.sleep(
            0.3
        )

    save_state(
        state
    )

    counts = {}

    for r in results:

        status = r.get(
            "status",
            "UNKNOWN"
        )

        counts[status] = (
            counts.get(
                status,
                0
            )
            + 1
        )

    print(
        "\nSTATUS COUNTS:",
        counts
    )

    print(
        "ERROR COUNT:",
        len(errors)
    )

    print(
        "STATE FILE:",
        STATE_FILE
    )


if __name__ == "__main__":
    main()
