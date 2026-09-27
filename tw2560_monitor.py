
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
    print("TW 2560 Trend Monitor | v2.0")
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
