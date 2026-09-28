#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW 2560 Trend Monitor FINAL 2026-09-28
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
    "0050":"0050.TW", "2330":"2330.TW", "2317":"2317.TW", "2454":"2454.TW", "2308":"2308.TW",
    "2881":"2881.TW", "2882":"2882.TW", "2891":"2891.TW",
    "1216":"1216.TW", "1301":"1301.TW", "1303":"1303.TW", "2002":"2002.TW",
    "2412":"2412.TW", "2603":"2603.TW", "1101":"1101.TW",
}

TW_NAMES = {
    "0050":"元大台灣50", "2330":"台積電", "2317":"鴻海", "2454":"聯發科", "2308":"台達電",
    "2881":"富邦金", "2882":"國泰金", "2891":"中信金",
    "1216":"統一", "1301":"台塑", "1303":"南亞", "2002":"中鋼",
    "2412":"中華電", "2603":"長榮", "1101":"台泥",
}

TW_TZ = ZoneInfo("Asia/Taipei")
TW_OPEN = dt_time(9, 0)
TW_CLOSE = dt_time(13, 30)

ONE_D = 86400
ONE_H = 3600
FIFTEEN_M = 900
FIVE_M = 300

MIN_NEW_ENTRY_SPACE_PCT = 2.0
TW_COMMISSION_RATE = float(os.getenv("TW2560_COMMISSION_RATE", "0.001425"))
TW_STOCK_SELL_TAX = float(os.getenv("TW2560_STOCK_SELL_TAX", "0.003"))
TW_ETF_SELL_TAX = float(os.getenv("TW2560_ETF_SELL_TAX", "0.001"))
def tw_sell_tax(code): return TW_ETF_SELL_TAX if code=="0050" else TW_STOCK_SELL_TAX
def tw_net_exit_price(entry,code,net_pct):
    return entry*(1+TW_COMMISSION_RATE)*(1+net_pct/100.0)/(1-TW_COMMISSION_RATE-tw_sell_tax(code)) if entry else None
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
    """日K只決定是否允許做多，量能不再是日K硬性封鎖。"""
    needed=[r.get("ma25"),r.get("ma25_prev"),r.get("ma20"),r.get("ma20_prev")]
    if any(x is None for x in needed): return False
    return (r["c"] >= r["ma25"]*0.98 and r["ma25"] >= r["ma25_prev"]*0.995 and r["ma20"] >= r["ma20_prev"]*0.995)


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
        r["c"] >= r["ma25"] * 0.985
        and r["ma20"] >= r["ma20_prev"] * 0.995
        and r["ma5"] >= r["ma10"] * 0.985
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


def recent_resistance(current_price, r1d, r1h):
    candidates = []

    for r in r1d[-60:]:
        if r["h"] > current_price:
            candidates.append(r["h"])

    for r in r1h[-80:]:
        if r["h"] > current_price:
            candidates.append(r["h"])

    return min(candidates) if candidates else None


def estimate_space(r1d, r1h, latest5):
    current=latest5["c"]
    latest1h=r1h[-1]
    resistance=recent_resistance(current,r1d,r1h)
    atr=latest1h.get("atr14")
    atr_pct=(atr/current*100) if (atr and current>0) else None
    day_highs=sorted({x["h"] for x in r1d[-90:] if x["h"]>current})
    hour_highs=sorted({x["h"] for x in r1h[-80:] if x["h"]>current})
    nearest=sorted(day_highs+hour_highs)[0] if (day_highs or hour_highs) else None
    major=sorted(day_highs+hour_highs)[-1] if (day_highs or hour_highs) else None
    atr_base=current+(atr or current*0.015)*1.5
    atr_high=current+(atr or current*0.015)*2.5
    base=max(current,min([x for x in (nearest,atr_base) if x is not None]))
    high=max(base,min([x for x in (major,atr_high) if x is not None]))
    high=min(high,current*1.095)  # 台股漲跌幅制度的異常值護欄，不是目標生成器
    effective=(base/current-1)*100
    return {
        "target":base,"target_low":base,"target_base":base,"target_high":high,
        "effective_space_pct":effective,"expected_high_pct":(high/current-1)*100,
        "resistance":resistance,"resistance_pct":((resistance/current-1)*100 if resistance else None),
        "major_resistance":major,"atr_pct":atr_pct,
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

    space = estimate_space(r1d, r1h, latest5)
    space["breakeven_after_cost"] = tw_net_exit_price(latest5["c"], code, 0.0)
    space["net_tp3"] = tw_net_exit_price(latest5["c"], code, 3.0)
    space["net_tp5"] = tw_net_exit_price(latest5["c"], code, 5.0)
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
        "1h_trend": h_trend,
        "1h_pullback": pullback,
        "15m_setup": m15,
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

    safe_title = str(Header(title, "utf-8"))

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title": safe_title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("NTFY:", resp.status, title)
            return True
    except Exception as e:
        print("NTFY ERROR:", title, str(e))
        return False


def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "symbols": {},
            "last_summary_utc": None,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        state.setdefault("symbols", {})
        state.setdefault("last_summary_utc", None)
        return state

    except Exception as e:
        print("STATE LOAD ERROR:", e)
        return {
            "symbols": {},
            "last_summary_utc": None,
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
        f"目前：{price_text(r.get('price'))}\n\n"

        f"1D 2560：{r.get('1d_2560')}\n"
        f"1H 趨勢：{r.get('1h_trend')}\n"
        f"1H 回踩：{r.get('1h_pullback')}\n"
        f"15m SETUP：{r.get('15m_setup')}\n"
        f"5m ENTRY：{r.get('5m_entry')}\n\n"

        f"最近壓力：{price_text(s.get('resistance'))}\n"
        f"壓力空間：{pct_text(s.get('resistance_pct'))}\n"
        f"1H ATR：{pct_text(s.get('atr_pct'))}\n"
        f"有效預估空間：{pct_text(s.get('effective_space_pct'))}\n"
        f"合理目標基準：{price_text(s.get('target_base'))}\n"
        f"合理目標上緣：{price_text(s.get('target_high'))}\n"
        f"上緣空間：{pct_text(s.get('expected_high_pct'))}\n"
        f"含成本損益兩平：{price_text(s.get('breakeven_after_cost'))}\n"
        f"淨利3%出場價：{price_text(s.get('net_tp3'))}\n"
        f"淨利5%出場價：{price_text(s.get('net_tp5'))}\n\n"

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
    print("TW 2560 Trend Monitor | FINAL 2026-09-28")
    print("UTC:", now_iso())
    print("TW market open:", tw_market_open())
    print("Manual run:", MANUAL_RUN)

    state = load_state()

    if not tw_market_open():
        print("TW2560 SKIP: outside 09:00-13:30 Taipei time")
        save_state(state)
        return

    results = []
    errors = []

    for code, yahoo_symbol in TW_STOCKS.items():
        try:
            r = analyze(code, yahoo_symbol)
            results.append(r)

            if r["status"] in ("WAIT_HISTORY", "STALE_DATA"):
                print(f"{code:<6} {r['status']}")
                continue

            s = r.get("space") or {}

            print(
                f"{code:<6} "
                f"{r['status']:<16} "
                f"price={price_text(r.get('price'))} "
                f"1D2560={r.get('1d_2560')} "
                f"1H={r.get('1h_trend')} "
                f"pullback={r.get('1h_pullback')} "
                f"15m={r.get('15m_setup')} "
                f"5m={r.get('5m_entry')} "
                f"space={pct_text(s.get('effective_space_pct'))}"
            )

            notify_status(r, state)

        except Exception as e:
            errors.append((code, str(e)))
            print(f"{code:<6} ERROR {e}")

        time.sleep(0.15)

    send_summary(
        results,
        state,
        len(errors),
        force=MANUAL_RUN,
    )

    save_state(state)

    print("\nERROR COUNT:", len(errors))
    print("STATE FILE:", STATE_FILE)


if __name__ == "__main__":
    main()
