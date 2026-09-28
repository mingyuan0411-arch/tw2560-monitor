#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW 2560 Trend Monitor FINAL 2026-09-29
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

# 收盤後完整確認窗口
TW_CLOSE_CONFIRM_START = dt_time(13, 40)
TW_CLOSE_CONFIRM_END = dt_time(14, 10)

ONE_D = 86400
ONE_H = 3600
FIFTEEN_M = 900
FIVE_M = 300

MIN_NEW_ENTRY_SPACE_PCT = 2.0
PULLBACK_BAND_PCT = 1.2
SUMMARY_INTERVAL = 1800

STATE_DIR = Path(".tw2560_state")
STATE_FILE = STATE_DIR / "state.json"
RESULT_FILE = Path("tw2560_latest.json")

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
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 10:
        return f"{v:.3f}"
    return f"{v:.4f}"


def pct_text(v):
    if v is None:
        return "N/A"
    return f"{v:+.2f}%"


def money_twd(v):
    if v is None:
        return "N/A"
    if abs(v) >= 100_000_000:
        return f"{v / 100_000_000:.2f} 億"
    if abs(v) >= 10_000:
        return f"{v / 10_000:.2f} 萬"
    return f"{v:,.0f}"


def tw_market_open(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    tw_now = now_utc.astimezone(TW_TZ)
    if tw_now.weekday() >= 5:
        return False
    t = tw_now.time().replace(tzinfo=None)
    return TW_OPEN <= t < TW_CLOSE


def tw_trade_date(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(TW_TZ).date().isoformat()



def tw_now():
    return datetime.now(timezone.utc).astimezone(TW_TZ)


def tw_intraday_bucket(now_tw=None):
    """盤中每個新的15分鐘桶只重算一次。"""
    now_tw = now_tw or tw_now()

    if now_tw.weekday() >= 5:
        return None

    t = now_tw.time().replace(tzinfo=None)
    if not (TW_OPEN <= t < TW_CLOSE):
        return None

    minute = (now_tw.minute // 15) * 15
    bucket = now_tw.replace(minute=minute, second=0, microsecond=0)
    return bucket.strftime("%Y-%m-%d %H:%M")


def tw_scan_decision(state, now_tw=None):
    """
    TW2560 智慧掃描：
    - 盤中：新15m bucket才重算
    - 13:40~14:10：每天一次收盤確認
    - 其他時間：沿用上一輪
    - cache為空：任何時段允許一次 bootstrap
    """
    now_tw = now_tw or tw_now()
    meta = state.setdefault("scan_meta", {})
    date_key = now_tw.date().isoformat()

    cached = state.get("last_results")
    if not (isinstance(cached, list) and cached):
        return True, "BOOTSTRAP_EMPTY_CACHE", date_key

    if now_tw.weekday() >= 5:
        return False, "SKIP_WEEKEND", None

    bucket = tw_intraday_bucket(now_tw)
    if bucket is not None:
        if meta.get("last_intraday_bucket") == bucket:
            return False, "SKIP_SAME_15M", bucket
        return True, "INTRADAY_NEW_15M", bucket

    t = now_tw.time().replace(tzinfo=None)

    if TW_CLOSE_CONFIRM_START <= t < TW_CLOSE_CONFIRM_END:
        if meta.get("last_close_confirm_date") == date_key:
            return False, "SKIP_CLOSE_ALREADY_DONE", date_key
        return True, "CLOSE_CONFIRM", date_key

    return False, "SKIP_OFF_HOURS", None


def mark_scan_done(state, reason, token):
    meta = state.setdefault("scan_meta", {})
    if reason == "INTRADAY_NEW_15M":
        meta["last_intraday_bucket"] = token
    elif reason == "CLOSE_CONFIRM":
        meta["last_close_confirm_date"] = token
    elif reason == "BOOTSTRAP_EMPTY_CACHE":
        meta["last_bootstrap_date"] = token
    meta["last_scan_reason"] = reason
    meta["last_scan_utc"] = now_iso()


def cached_results(state):
    rows = state.get("last_results")
    return rows if isinstance(rows, list) else []


def yahoo_get(symbol, interval, range_text, retries=4):
    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_text,
        "includePrePost": "false",
        "events": "div,splits",
    })

    last_error = None

    for attempt in range(retries):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        url = (
            f"{host}/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?{params}"
        )
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)

            result = data.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(
                    f"Yahoo no result: {data.get('chart', {}).get('error')}"
                )
            return result[0]

        except Exception as e:
            last_error = e
            time.sleep(min(2 ** attempt, 6))

    raise RuntimeError(f"Yahoo request failed: {last_error}")


def fetch(symbol, interval, range_text):
    data = yahoo_get(symbol, interval, range_text)
    ts = data.get("timestamp") or []
    q = (data.get("indicators", {}).get("quote") or [{}])[0]

    o = q.get("open") or []
    h = q.get("high") or []
    l = q.get("low") or []
    c = q.get("close") or []
    v = q.get("volume") or []

    rows = []
    for i, t in enumerate(ts):
        try:
            if any(x is None for x in (o[i], h[i], l[i], c[i])):
                continue
            volume = float(v[i] or 0)
            close = float(c[i])
            rows.append({
                "t": int(t),
                "o": float(o[i]),
                "h": float(h[i]),
                "l": float(l[i]),
                "c": close,
                "v": volume,
                "amount": abs(volume * close),
            })
        except IndexError:
            continue

    rows.sort(key=lambda x: x["t"])
    return rows


def completed_only(rows, step, now_ts):
    return [r for r in rows if r["t"] + step <= now_ts]


def data_is_fresh(rows, max_age_seconds):
    if not rows:
        return False
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return now_ts - rows[-1]["t"] <= max_age_seconds


def sma(values, n, i):
    if i + 1 < n:
        return None
    return sum(values[i - n + 1:i + 1]) / n


def true_range(current, previous):
    if previous is None:
        return current["h"] - current["l"]
    return max(
        current["h"] - current["l"],
        abs(current["h"] - previous["c"]),
        abs(current["l"] - previous["c"]),
    )


def add_indicators(rows):
    closes = [r["c"] for r in rows]
    volumes = [r["v"] for r in rows]
    trs = []

    for i, r in enumerate(rows):
        prev = rows[i - 1] if i > 0 else None
        trs.append(true_range(r, prev))

    for i, r in enumerate(rows):
        for n in (5, 10, 20, 25, 60):
            r[f"ma{n}"] = sma(closes, n, i)

        r["ma25_prev"] = sma(closes, 25, i - 1) if i >= 25 else None
        r["ma20_prev"] = sma(closes, 20, i - 1) if i >= 20 else None
        r["vma5"] = sma(volumes, 5, i)
        r["vma20"] = sma(volumes, 20, i)
        r["vma60"] = sma(volumes, 60, i)
        r["atr14"] = sma(trs, 14, i)


def core_2560(r):
    needed = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
    ]
    if any(x is None for x in needed):
        return False

    return (
        r["ma25"] > r["ma25_prev"]
        and r["c"] > r["ma25"]
        and r["vma5"] > r["vma60"]
    )


def hourly_trend(r):
    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma25"),
        r.get("ma20_prev"),
    ]
    if any(x is None for x in needed):
        return False

    return (
        r["c"] > r["ma25"]
        and r["ma20"] >= r["ma20_prev"]
        and r["ma5"] >= r["ma10"] * 0.995
    )


def pullback_ready(r):
    needed = [
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma25"),
    ]
    if any(x is None for x in needed):
        return False

    dist = (r["c"] / r["ma20"] - 1) * 100

    return (
        abs(dist) <= PULLBACK_BAND_PCT
        and r["c"] >= r["ma25"] * 0.985
        and r["ma10"] >= r["ma20"] * 0.99
    )


def fifteen_setup(r):
    needed = [r.get("ma5"), r.get("ma10"), r.get("ma20")]
    if any(x is None for x in needed):
        return False

    return (
        r["c"] >= r["ma20"] * 0.995
        and r["ma5"] >= r["ma10"] * 0.995
    )


def five_entry(current, previous):
    needed = [
        current.get("ma5"),
        current.get("ma10"),
        current.get("ma20"),
        current.get("vma5"),
        current.get("vma20"),
        previous.get("ma5"),
        previous.get("ma10"),
    ]
    if any(x is None for x in needed):
        return False

    cross_up = (
        current["ma5"] > current["ma10"]
        and previous["ma5"] <= previous["ma10"]
    )

    already_strong = (
        current["ma5"] > current["ma10"]
        and current["c"] > current["ma20"]
    )

    volume_ok = current["vma5"] > current["vma20"]

    return (cross_up or already_strong) and volume_ok


def resistance_levels(current_price, r1d, r1h):
    highs = sorted({
        float(r["h"])
        for r in (r1h[-100:] + r1d[-120:])
        if r.get("h") is not None and r["h"] > current_price
    })

    near = highs[0] if highs else None

    major = None
    if highs:
        # 取明顯高於最近壓力的下一級壓力，避免兩個目標貼太近
        threshold = current_price * 1.01
        for x in highs:
            if x >= threshold and (near is None or x > near * 1.005):
                major = x
                break

    return near, major


def support_levels(current_price, r1d, r1h):
    lows = [
        float(r["l"])
        for r in (r1h[-100:] + r1d[-120:])
        if r.get("l") is not None and r["l"] < current_price
    ]
    if not lows:
        return None, None

    return max(lows), min(lows)


def estimate_space(r1d, r1h, latest5):
    current = latest5["c"]
    latest1h = r1h[-1]

    near_res, major_res = resistance_levels(current, r1d, r1h)
    near_sup, major_sup = support_levels(current, r1d, r1h)

    atr = latest1h.get("atr14")
    atr_pct = (atr / current * 100) if (atr and current > 0) else None

    atr_base = (
        current + atr * 1.5
        if atr is not None
        else current * 1.03
    )
    atr_high = (
        current + atr * 3.0
        if atr is not None
        else current * 1.06
    )

    # 目標下緣：最近有效壓力，若太近則至少給一個ATR延伸
    target_base_candidates = [atr_base]
    if near_res is not None:
        target_base_candidates.append(near_res)
    target_base = max(current, min(target_base_candidates))

    # 目標上緣：主要壓力與較大ATR延伸中取合理較高者，但不機械超過12%
    target_high_candidates = [atr_high, target_base]
    if major_res is not None:
        target_high_candidates.append(major_res)
    target_high = max(target_high_candidates)
    target_high = min(target_high, current * 1.12)

    base_pct = (target_base / current - 1) * 100 if current > 0 else None
    high_pct = (target_high / current - 1) * 100 if current > 0 else None

    return {
        "target": target_base,
        "target_base": target_base,
        "target_high": target_high,
        "effective_space_pct": base_pct,
        "expected_base_pct": base_pct,
        "expected_high_pct": high_pct,
        "resistance": near_res,
        "major_resistance": major_res,
        "support": near_sup,
        "major_support": major_sup,
        "resistance_pct": (
            (near_res / current - 1) * 100
            if near_res is not None and current > 0
            else None
        ),
        "atr_pct": atr_pct,
    }


def amount_5m(r5):
    return r5[-1].get("amount") if r5 else None


def amount_1h(r5):
    vals = [
        x.get("amount")
        for x in r5[-12:]
        if x.get("amount") is not None
    ]
    return sum(vals) if vals else None


def volume_ratio(r5):
    if not r5:
        return None
    last = r5[-1]
    if last.get("vma5") and last.get("vma20"):
        return last["vma5"] / last["vma20"]
    return None



def ensure_tw2560_space(space, current, latest1h):
    """
    TW2560 採 target-first：
    WATCH / HOLD / PULLBACK_READY / ENTRY 都先有合理目標區，
    再由趨勢條件決定狀態。
    """
    s = dict(space or {})
    if current is None or current <= 0:
        return s

    atr = latest1h.get("atr14") if latest1h else None
    step = atr if (atr is not None and atr > 0) else current * 0.02

    if s.get("target_base") is None:
        s["target_base"] = current + step * 1.5
        s["target"] = s["target_base"]

    if s.get("target_high") is None:
        s["target_high"] = max(
            s["target_base"],
            current + step * 3.0,
        )

    if s.get("expected_base_pct") is None:
        s["expected_base_pct"] = (
            s["target_base"] / current - 1
        ) * 100

    if s.get("expected_high_pct") is None:
        s["expected_high_pct"] = (
            s["target_high"] / current - 1
        ) * 100

    if s.get("effective_space_pct") is None:
        s["effective_space_pct"] = s["expected_base_pct"]

    return s


def analyze(code, yahoo_symbol):
    now_ts = int(datetime.now(timezone.utc).timestamp())

    r1d = completed_only(fetch(yahoo_symbol, "1d", "1y"), ONE_D, now_ts)
    r1h = completed_only(fetch(yahoo_symbol, "60m", "3mo"), ONE_H, now_ts)
    r15 = completed_only(fetch(yahoo_symbol, "15m", "60d"), FIFTEEN_M, now_ts)
    r5 = fetch(yahoo_symbol, "5m", "10d")

    if min(len(r1d), len(r1h), len(r15), len(r5)) < 70:
        return {
            "code": code,
            "name": TW_NAMES.get(code, code),
            "status": "WAIT_HISTORY",
        }

    add_indicators(r1d)
    add_indicators(r1h)
    add_indicators(r15)
    add_indicators(r5)

    latest1d = r1d[-1]
    latest1h = r1h[-1]
    latest15 = r15[-1]
    latest5 = r5[-1]
    prev5 = r5[-2]

    if tw_market_open() and not data_is_fresh(r5, 20 * 60):
        return {
            "code": code,
            "name": TW_NAMES.get(code, code),
            "status": "STALE_DATA",
            "price": latest5["c"],
        }

    d_trend = core_2560(latest1d)
    h_trend = hourly_trend(latest1h)
    pullback = pullback_ready(latest1h)
    m15 = fifteen_setup(latest15)
    m5 = five_entry(latest5, prev5)

    # TW2560 分工：1D戰略 / 1H波段 / 15m進場，5m只作進場輔助
    strategic_1d = d_trend
    wave_1h = h_trend
    timing_15m = m15

    # 先估合理目標區，再讓趨勢/進場條件決定狀態。
    space = ensure_tw2560_space(
        estimate_space(r1d, r1h, latest5),
        latest5["c"],
        latest1h,
    )
    effective_space = space.get("effective_space_pct")

    if not d_trend:
        status = "NO_TREND"

    elif (
        d_trend
        and h_trend
        and pullback
        and m15
        and m5
        and effective_space is not None
        and effective_space >= MIN_NEW_ENTRY_SPACE_PCT
    ):
        status = "ENTRY"

    elif d_trend and h_trend and pullback and m15:
        status = "PULLBACK_READY"

    elif d_trend and h_trend:
        status = "HOLD"

    else:
        status = "WATCH"

    return {
        "code": code,
        "name": TW_NAMES.get(code, code),
        "symbol": yahoo_symbol,
        "status": status,
        "price": latest5["c"],
        "1d_2560": d_trend,
        "1d_strategy": strategic_1d,
        "1h_trend": h_trend,
        "1h_wave": wave_1h,
        "1h_pullback": pullback,
        "15m_setup": m15,
        "15m_entry": timing_15m,
        "5m_entry": m5,
        "space": space,
        "amount_5m": amount_5m(r5),
        "amount_1h": amount_1h(r5),
        "volume_ratio": volume_ratio(r5),
    }


def send_ntfy(title, msg, priority="default", tags="bell"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC_TW2560 / NTFY_TOPIC_SHORT35 未設定")
        return False

    try:
        safe_title = Header(str(title), "utf-8").encode()

        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=str(msg).encode("utf-8"),
            method="POST",
            headers={
                "Title": safe_title,
                "Priority": str(priority),
                "Tags": str(tags),
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

        with urllib.request.urlopen(req, timeout=20) as resp:
            print("NTFY:", resp.status, title)
            return 200 <= resp.status < 300

    except Exception as e:
        print("NTFY WARN:", title, str(e))
        return False


def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "symbols": {},
            "last_summary_utc": None,
            "last_results": [],
            "scan_meta": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        state.setdefault("symbols", {})
        state.setdefault("last_summary_utc", None)
        state.setdefault("last_results", [])
        state.setdefault("scan_meta", {})
        return state

    except Exception as e:
        print("STATE LOAD ERROR:", e)
        return {
            "symbols": {},
            "last_summary_utc": None,
            "last_results": [],
            "scan_meta": {},
        }


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


def format_signal(r):
    s = r.get("space") or {}
    vr = r.get("volume_ratio")
    vr_text = f"{vr:.2f}x" if vr is not None else "N/A"

    return (
        f"股票：{r['code']} {r['name']}\n"
        f"目前：{price_text(r.get('price'))}\n"
        f"合理目標區：{price_text(s.get('target_base'))}"
        f"～{price_text(s.get('target_high'))}\n"
        f"預期空間：{pct_text(s.get('expected_base_pct'))}"
        f"～{pct_text(s.get('expected_high_pct'))}\n\n"

        f"1D戰略：{r.get('1d_strategy')}\n"
        f"1H波段：{r.get('1h_wave')}\n"
        f"1H回踩：{r.get('1h_pullback')}\n"
        f"15m進場：{r.get('15m_entry')}\n"
        f"5m輔助：{r.get('5m_entry')}\n\n"

        f"最近支撐：{price_text(s.get('support'))}\n"
        f"主要支撐：{price_text(s.get('major_support'))}\n"
        f"最近壓力：{price_text(s.get('resistance'))}\n"
        f"主要壓力：{price_text(s.get('major_resistance'))}\n"
        f"1H ATR：{pct_text(s.get('atr_pct'))}\n\n"

        f"5m成交額：約 NT${money_twd(r.get('amount_5m'))}\n"
        f"近1H成交額：約 NT${money_twd(r.get('amount_1h'))}\n"
        f"5m量能比：{vr_text}"
    )


def notify_status(r, state):
    code = r["code"]
    status = r["status"]

    symbol_state = (
        state
        .setdefault("symbols", {})
        .setdefault(code, {})
    )

    today = tw_trade_date()

    if symbol_state.get("tw_trade_date") != today:
        symbol_state.clear()
        symbol_state["tw_trade_date"] = today

    previous = symbol_state.get("status", "UNKNOWN")

    print(f"{code}: {previous} -> {status}")

    if status != previous:
        if status == "ENTRY":
            send_ntfy(
                f"台股2560 ENTRY {code}",
                format_signal(r)
                + "\n\n趨勢成立＋回踩完成＋短週期確認。"
                + "\n人工複核後再決定是否進場。",
                "high",
                "chart_with_upwards_trend,bell",
            )

        elif status == "PULLBACK_READY":
            send_ntfy(
                f"台股2560 PULLBACK READY {code}",
                format_signal(r)
                + "\n\n趨勢仍多，價格回到合理承接區。"
                + "\n等待5m重新轉強。",
                "default",
                "eyes",
            )

        elif status == "HOLD":
            send_ntfy(
                f"台股2560 HOLD {code}",
                format_signal(r)
                + "\n\n多頭趨勢仍成立，但目前不是理想新進場點。"
                + "\n已持有可續觀察，新單不追價。",
                "default",
                "eyes",
            )

        elif status == "WATCH":
            send_ntfy(
                f"台股2560 WATCH {code}",
                format_signal(r)
                + "\n\n日線2560仍成立，但1H尚未形成理想承接。",
                "default",
                "eyes",
            )

        elif status == "NO_TREND":
            if previous in ("WATCH", "PULLBACK_READY", "ENTRY", "HOLD"):
                send_ntfy(
                    f"台股2560 趨勢失效 {code}",
                    format_signal(r)
                    + "\n\n1D 2560 核心已不成立。",
                    "default",
                    "warning",
                )

    symbol_state["status"] = status
    symbol_state["updated_utc"] = now_iso()


def summary_due(state, force=False):
    if force:
        return True

    last = state.get("last_summary_utc")
    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last)
        return (
            datetime.now(timezone.utc) - last_dt
        ).total_seconds() >= SUMMARY_INTERVAL
    except Exception:
        return True


def send_summary(results, state, error_count, force=False):
    if not summary_due(state, force=force):
        return

    buckets = {
        "ENTRY": [],
        "PULLBACK_READY": [],
        "HOLD": [],
        "WATCH": [],
        "NO_TREND": [],
    }

    for r in results:
        status = r.get("status")
        if status not in buckets:
            continue

        s = r.get("space") or {}

        label = f"{r['code']} {r['name']}"
        if s.get("effective_space_pct") is not None:
            label += f"({s['effective_space_pct']:+.1f}%)"

        buckets[status].append(label)

    def show(items):
        return "、".join(items) if items else "無"

    msg = (
        f"[台股2560]\n"
        f"ENTRY：{show(buckets['ENTRY'])}\n"
        f"PULLBACK_READY：{show(buckets['PULLBACK_READY'])}\n"
        f"HOLD：{show(buckets['HOLD'])}\n"
        f"WATCH：{show(buckets['WATCH'])}\n"
        f"NO_TREND：{show(buckets['NO_TREND'])}\n\n"
        f"本輪錯誤：{error_count}"
    )

    if send_ntfy(
        "台股2560 Monitor Summary",
        msg,
        "default",
        "bar_chart",
    ):
        state["last_summary_utc"] = now_iso()


def main():
    print("TW 2560 Trend Monitor | FINAL 2026-09-29 TARGET-FIRST")
    print("UTC:", now_iso())
    print("Taipei:", tw_now().isoformat())
    print("Manual run:", MANUAL_RUN)

    state = load_state()

    should_scan, scan_reason, scan_token = tw_scan_decision(state)
    cached = cached_results(state)

    print(
        "TW2560 SMART SCAN:",
        f"scan={should_scan}",
        f"reason={scan_reason}",
        f"token={scan_token}",
        f"cached={len(cached)}",
    )

    results = []
    errors = []

    if should_scan:
        for code, yahoo_symbol in TW_STOCKS.items():
            try:
                r = analyze(code, yahoo_symbol)
                results.append(r)

                if r["status"] in ("WAIT_HISTORY", "STALE_DATA"):
                    print(f"{code:<6} {r['name']:<12} {r['status']}")
                    continue

                s = r.get("space") or {}

                if r["status"] == "NO_TREND":
                    print(
                        f"{code:<6} {r['name']:<12} "
                        f"NO_TREND close={price_text(r.get('price'))}"
                    )
                else:
                    print(
                        f"{code:<6} {r['name']:<12} "
                        f"{r['status']:<16} "
                        f"now={price_text(r.get('price'))} "
                        f"target={price_text(s.get('target_base'))}"
                        f"~{price_text(s.get('target_high'))} "
                        f"space={pct_text(s.get('expected_base_pct'))}"
                        f"~{pct_text(s.get('expected_high_pct'))} "
                        f"support={price_text(s.get('support'))} "
                        f"resist={price_text(s.get('resistance'))} "
                        f"1D={r.get('1d_strategy')} "
                        f"1H={r.get('1h_wave')} "
                        f"15m={r.get('15m_entry')} "
                        f"5m={r.get('5m_entry')}"
                    )

                notify_status(r, state)

            except Exception as e:
                errors.append((code, str(e)))
                print(f"{code:<6} ERROR {e}")

            time.sleep(0.15)

        mark_scan_done(state, scan_reason, scan_token)
        state["last_results"] = results
        state["last_results_updated_utc"] = now_iso()

        print(
            "TW2560 SCAN COMPLETE:",
            f"reason={scan_reason}",
            f"token={scan_token}",
            f"symbols={len(results)}",
        )

    else:
        results = cached
        print(
            "TW2560 SCAN SKIPPED:",
            scan_reason,
            f"| reused_results={len(results)}"
        )

    # 手動執行即使沿用 cache 也允許送摘要
    send_summary(
        results,
        state,
        len(errors),
        force=MANUAL_RUN,
    )

    payload = {
        "rule_version": "TW2560_FINAL_2026_09_29_SMART_SCAN",
        "generated_utc": now_iso(),
        "scan_reason": scan_reason,
        "rescanned_this_run": should_scan,
        "results": results,
        "error_count": len(errors),
    }
    RESULT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    save_state(state)

    print("\\nSYMBOL COUNT:", len(results))
    print("ERROR COUNT:", len(errors))
    print("STATE FILE:", STATE_FILE)
    print("RESULT FILE:", RESULT_FILE)


if __name__ == "__main__":
    main()
