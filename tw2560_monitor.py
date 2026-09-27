#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW2560 Monitor v1.2
===================

台股版 2560

台股節奏：
30m -> 早期確認
60m -> 主趨勢 CORE
1D  -> 大方向確認

狀態：
NO_SIGNAL
WATCH
TREND_READY
PRE-STRICT
STRICT

STRICT：
60m CORE
- MA25 rising
- Close > MA25
- VolMA5 crosses above VolMA60

1D CONFIRM
- Close > MA25
- MA25 rising
- VolMA5 > VolMA60

20-bar dedup

v1.2：
- 移除 synthetic 4H
- 30m 取代原 1H 的早期角色
- 60m 取代原 4H CORE
- 排除台股 13:00~13:30 的非完整60m bar
- 保留階段價格
- 保留 Swing 壓力
- 保留 gap
- 保留 ntfy
- 保留 state
- 盤中不使用未完成K
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

YAHOO_BASE = (
    "https://query1.finance.yahoo.com/v8/finance/chart"
)

STATE_DIR = Path(".tw2560_state")
STATE_FILE = STATE_DIR / "state.json"

DEDUP_BARS = 20

THIRTY_M_SECONDS = 30 * 60
SIXTY_M_SECONDS = 60 * 60


# ============================================================
# 標的
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

    return datetime.now(
        timezone.utc
    )


def now_taipei():

    return datetime.now(
        TAIPEI
    )


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
# 顯示
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
# Yahoo
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
                        "Mozilla/5.0 TW2560-Monitor/1.2",

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

    timestamps = (
        result.get(
            "timestamp",
            []
        )
        or []
    )

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
# 日期 / 完成K
# ============================================================

def row_local_datetime(row):

    return (
        datetime
        .fromtimestamp(
            row["t"],
            timezone.utc
        )
        .astimezone(
            TAIPEI
        )
    )


def row_local_date(row):

    return (
        row_local_datetime(
            row
        )
        .date()
    )


def completed_intraday_rows(
    rows,
    seconds
):

    now_ts = int(
        now_utc().timestamp()
    )

    return [
        r
        for r in rows
        if (
            r["t"] + seconds
            <= now_ts
        )
    ]


# ============================================================
# 台股完整60m K
#
# 保留：
# 09:00
# 10:00
# 11:00
# 12:00
#
# 排除13:00那根，
# 因為實際只有13:00~13:30
# ============================================================

def clean_full_60m_rows(rows):

    cleaned = []

    for r in rows:

        local_dt = row_local_datetime(
            r
        )

        if local_dt.hour in (
            9,
            10,
            11,
            12,
        ):

            cleaned.append(r)

    return cleaned


# ============================================================
# 完成日線
# ============================================================

def completed_daily_rows(rows):

    if not rows:
        return []

    today = (
        now_taipei()
        .date()
    )

    if market_open_now():

        return [
            r
            for r in rows
            if (
                row_local_date(r)
                < today
            )
        ]

    return rows


# ============================================================
# 交易日資訊
# ============================================================

def group_rows_by_date(rows):

    groups = {}

    for r in rows:

        d = row_local_date(r)

        groups.setdefault(
            d,
            []
        ).append(r)

    for d in groups:

        groups[d].sort(
            key=lambda x:
                x["t"]
        )

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

    open_price = (
        day_rows[0]["o"]
        if day_rows
        else None
    )

    daily_by_date = {}

    for r in rd:

        d = row_local_date(r)

        daily_by_date[d] = r

    previous_dates = sorted([
        d
        for d in daily_by_date
        if d < latest_date
    ])

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
            and
            open_price is not None
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

    return (
        sum(
            values[
                i - n + 1:
                i + 1
            ]
        )
        / n
    )


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
            closes, 5, i
        )

        r["ma10"] = sma(
            closes, 10, i
        )

        r["ma20"] = sma(
            closes, 20, i
        )

        r["ma25"] = sma(
            closes, 25, i
        )

        r["ma60"] = sma(
            closes, 60, i
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
# 30m 早期確認
# ============================================================

def thirty_min_confirm(r):

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
# 60m 結構
# ============================================================

def sixty_min_structure(r):

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


def sixty_min_early(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    return (
        r["c"] > r["ma25"]

        or

        r["ma25"]
        >= r["ma25_prev"]
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
# 60m STRICT CORE
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
# 1D
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

        r["ma25"] > r["ma25_prev"]

        and

        r["vma5"] > r["vma60"]
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
# 對齊日線
# ============================================================

def daily_asof_intraday(
    daily_rows,
    intraday_row
):

    target_date = row_local_date(
        intraday_row
    )

    candidate = None

    for d in daily_rows:

        d_date = row_local_date(d)

        if d_date <= target_date:

            candidate = d

        else:

            break

    return candidate


# ============================================================
# STRICT raw
# ============================================================

def strict_raw_at(
    r60,
    daily_rows
):

    d = daily_asof_intraday(
        daily_rows,
        r60
    )

    return (
        core_ok(r60)

        and

        daily_confirm(d)
    )


# ============================================================
# 20-bar dedup
# ============================================================

def kept_strict(
    r60_rows,
    daily_rows
):

    raw = []

    for r in r60_rows:

        if strict_raw_at(
            r,
            daily_rows
        ):

            raw.append(r)

    kept = []

    last_i = -10**9

    for r in raw:

        if (
            r["i"] - last_i
            >= DEDUP_BARS
        ):

            kept.append(r)

            last_i = r["i"]

    return kept


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
    lookback=80
):

    subset = rows[
        -lookback:
    ]

    swings = find_swing_highs(
        subset,
        2,
        2
    )

    above = [
        s
        for s in swings
        if (
            s["price"] > current_price
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


    # ========================================================
    # 5m，目前價與交易日資訊
    # ========================================================

    r5, meta5 = fetch_chart(
        ticker,
        "5m",
        "5d"
    )


    # ========================================================
    # 30m
    # ========================================================

    r30_raw, _ = fetch_chart(
        ticker,
        "30m",
        "1mo"
    )

    r30 = completed_intraday_rows(
        r30_raw,
        THIRTY_M_SECONDS
    )


    # ========================================================
    # 60m
    # ========================================================

    r60_raw, _ = fetch_chart(
        ticker,
        "60m",
        "6mo"
    )

    r60_completed = (
        completed_intraday_rows(
            r60_raw,
            SIXTY_M_SECONDS
        )
    )

    r60 = clean_full_60m_rows(
        r60_completed
    )


    # ========================================================
    # 1D
    # ========================================================

    rd_raw, _ = fetch_chart(
        ticker,
        "1d",
        "1y"
    )

    rd = completed_daily_rows(
        rd_raw
    )


    if (
        len(r5) < 1
        or
        len(r30) < 65
        or
        len(r60) < 65
        or
        len(rd) < 65
    ):

        return {
            "code": code,
            "name": name,
            "ticker": ticker,
            "status": "WAIT_HISTORY",

            "bars_30m":
                len(r30),

            "bars_60m":
                len(r60),

            "bars_1d":
                len(rd),
        }


    add_indicators(r30)
    add_indicators(r60)
    add_indicators(rd)


    latest30 = r30[-1]
    previous30 = r30[-2]

    latest60 = r60[-1]

    latest_d = daily_asof_intraday(
        rd,
        latest60
    )


    # ========================================================
    # Current
    # ========================================================

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


    # ========================================================
    # 交易日
    # ========================================================

    trade_info = latest_trade_day_info(
        r5,
        rd_raw
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


    # ========================================================
    # 30m
    # ========================================================

    m30_now = thirty_min_confirm(
        latest30
    )

    m30_prev = thirty_min_confirm(
        previous30
    )

    m30_fresh = (
        m30_now
        and
        not m30_prev
    )


    # ========================================================
    # 60m
    # ========================================================

    m60_structure = (
        sixty_min_structure(
            latest60
        )
    )

    m60_early = (
        sixty_min_early(
            latest60
        )
    )

    relaxed_volume = (
        relaxed_volume_ok(
            latest60
        )
    )

    m60_core = core_ok(
        latest60
    )


    # ========================================================
    # 1D
    # ========================================================

    day_strict = daily_confirm(
        latest_d
    )

    day_soft = daily_soft_confirm(
        latest_d
    )


    # ========================================================
    # STRICT
    # ========================================================

    strict_raw = (
        m60_core
        and
        day_strict
    )

    strict_kept = kept_strict(
        r60,
        rd
    )

    strict_now = (
        bool(strict_kept)

        and

        strict_kept[-1]["t"]
        == latest60["t"]
    )


    # ========================================================
    # PRE-STRICT
    # ========================================================

    pre_strict = (
        not strict_now

        and

        m30_now

        and

        m60_structure

        and

        relaxed_volume

        and

        day_strict
    )


    # ========================================================
    # TREND_READY
    # ========================================================

    trend_ready = (
        not strict_now

        and

        not pre_strict

        and

        m30_now

        and

        m60_early

        and

        relaxed_volume

        and

        day_soft
    )


    # ========================================================
    # WATCH
    # ========================================================

    watch = (
        not strict_now

        and

        not pre_strict

        and

        not trend_ready

        and

        m30_now
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


    # ========================================================
    # Volume Ratio
    # ========================================================

    volume_ratio = None

    if (
        latest60.get("vma5")
        is not None

        and

        latest60.get("vma60")
        not in (
            None,
            0
        )
    ):

        volume_ratio = (
            latest60["vma5"]
            /
            latest60["vma60"]
        )


    # ========================================================
    # Swing Resistance
    # ========================================================

    swing60 = nearest_swing_resistance(
        r60,
        current_price,
        100
    )

    swing1d = nearest_swing_resistance(
        rd,
        current_price,
        90
    )

    resistance_60m = (
        swing60["price"]
        if swing60
        else None
    )

    resistance_1d = (
        swing1d["price"]
        if swing1d
        else None
    )

    resistance_60m_pct = (
        pct_change(
            current_price,
            resistance_60m
        )
        if resistance_60m
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
        "code": code,
        "name": name,
        "ticker": ticker,

        "status":
            status,

        "current_price":
            current_price,

        "latest_60m_close":
            latest60["c"],

        "30m_confirm":
            m30_now,

        "30m_fresh":
            m30_fresh,

        "60m_early":
            m60_early,

        "60m_structure":
            m60_structure,

        "60m_volume_relaxed":
            relaxed_volume,

        "60m_volume_ratio":
            volume_ratio,

        "60m_core":
            m60_core,

        "1d_soft":
            day_soft,

        "1d_confirm":
            day_strict,

        "strict_raw":
            strict_raw,

        "strict":
            strict_now,

        "resistance_60m":
            resistance_60m,

        "resistance_60m_pct":
            resistance_60m_pct,

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

        "bars_30m":
            len(r30),

        "bars_60m":
            len(r60),
    }


# ============================================================
# NTFY
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
            "Title": title,
            "Priority": priority,
            "Tags": tags,
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
# STATE
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
# 階段記憶
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
    price = r["current_price"]

    s["current_price"] = price
    s["last_seen_utc"] = now_iso()

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

        status in active_states
    ):

        reset_cycle(s)

        s["current_price"] = price


    if (
        status == "WATCH"

        and

        s.get(
            "watch_price"
        )
        is None
    ):

        s["watch_price"] = price
        s["watch_time"] = now_iso()


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

        s["strict_price"] = price
        s["strict_time"] = now_iso()


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

    return "\n".join(lines)


# ============================================================
# 壓力
# ============================================================

def resistance_block(r):

    if r["resistance_60m"] is None:

        r60 = (
            "60m Swing壓力：未找到"
        )

    else:

        r60 = (
            "60m Swing壓力："
            f"{price_text(r['resistance_60m'])} "
            f"({pct_text(r['resistance_60m_pct'])})"
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
        f"{r60}\n"
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

    stage = stage_block(s)

    resistance = (
        resistance_block(r)
    )

    common = (
        f"{r['code']} "
        f"{r['name']}\n\n"

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


    # ========================================================
    # STRICT
    # ========================================================

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

            + "60m CORE=True\n"
            + "1D=True\n"
            + "20-bar dedup=True\n\n"

            + "進入台股2560人工複核。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 STRICT {code}",
                message,
                "high",
                "chart_with_upwards_trend,bell"
            )


    # ========================================================
    # PRE-STRICT
    # ========================================================

    elif status == "PRE-STRICT":

        message = (
            common

            + "30m=True\n"
            + "60m structure=True\n"
            + "60m relaxed volume=True\n"
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


    # ========================================================
    # TREND_READY
    # ========================================================

    elif status == "TREND_READY":

        message = (
            common

            + "30m=True\n"
            + "60m early=True\n"
            + "60m relaxed volume=True\n"
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


    # ========================================================
    # WATCH
    # ========================================================

    elif status == "WATCH":

        message = (
            common

            + "30m多頭成立\n"
            + "等待60m與1D成熟。"
        )

        if market_open_now():

            send_ntfy(
                f"TW2560 WATCH {code}",
                message,
                "default",
                "eyes"
            )


    # ========================================================
    # INVALID
    # ========================================================

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

                + f"前一狀態："
                f"{previous}\n"

                + "候選環境失效。",

                "default",
                "warning"
            )


    s["status"] = status
    s["updated_utc"] = now_iso()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "TW2560 Monitor | v1.2"
    )

    print(
        "Model: 30m / 60m / 1D"
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

            results.append(r)


            if (
                r["status"]
                == "WAIT_HISTORY"
            ):

                print(
                    f"{code} "
                    f"{info['name']} "
                    f"WAIT_HISTORY "
                    f"30m={r.get('bars_30m')} "
                    f"60m={r.get('bars_60m')} "
                    f"1D={r.get('bars_1d')}"
                )

                continue


            ratio = r.get(
                "60m_volume_ratio"
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

                f"30m="
                f"{r['30m_confirm']} "

                f"60mEarly="
                f"{r['60m_early']} "

                f"60mStruct="
                f"{r['60m_structure']} "

                f"VolRelax="
                f"{r['60m_volume_relaxed']} "

                f"V5/V60="
                f"{ratio_text} "

                f"60mCore="
                f"{r['60m_core']} "

                f"1Dsoft="
                f"{r['1d_soft']} "

                f"1D="
                f"{r['1d_confirm']} "

                f"strictRaw="
                f"{r['strict_raw']} "

                f"strict="
                f"{r['strict']} "

                f"R60="
                f"{price_text(r['resistance_60m'])} "

                f"R1D="
                f"{price_text(r['resistance_1d'])} "

                f"gap="
                f"{pct_text(r['gap_pct'])} "

                f"bars30="
                f"{r['bars_30m']} "

                f"bars60="
                f"{r['bars_60m']}"
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
