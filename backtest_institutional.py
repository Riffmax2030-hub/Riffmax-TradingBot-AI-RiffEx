"""
backtest_institutional.py — Synchronized Pure Price Action 3-TF Multi-Asset Backtester
=====================================================================================
Chronological portfolio-level MT5 backtester sharing the exact single-source-of-truth
Pure Price Action evaluator (`pure_price_action.py`) used by the live engine:

  - TF1 (D1 + H4 Macro): H4 3-state fractal structure (NOT_COUNTER) & 75%/25% swing leg equilibrium.
  - TF2 (Key Pools + M15): PDH/PDL, Asian Session H/L (00-06 UTC), Closed H4 H/L sweeps + anti-crash guard.
  - TF3 (Execution M5): 2-bar sweep reclaim + directional engulf/turn + anti-climax range guard.
  - Live Execution Parity:
      * Mon-Fri London (06-09 UTC) & NY (14-19 UTC) Killzones
      * Calibrated Exits: SL=$34 | TP=$55 | BE=+$28 (+$2 buffer) | Lock1=+$44->+$32
      * Max 2 concurrent positions portfolio-wide
      * Strict -$50.00 daily drawdown hard stop (realized + floating PnL)
      * 60-minute post-entry & post-closure cooldown per symbol
      * Friday 20:45 UTC weekend-close protection
"""

from datetime import datetime, timezone
import numpy as np
import MetaTrader5 as mt5

from pure_price_action import (
    CALIBRATED_ASSET_SPECS,
    compute_asian_range_at,
    compute_h4_structure_series,
    evaluate_3tf_pure_pa_step,
)

SYMBOLS = list(CALIBRATED_ASSET_SPECS.keys())
MAX_CONCURRENT = 2
DAILY_DD_CAP_USD = 50.0
COOLDOWN_SEC = 3600


def _rolling_median_range(highs: np.ndarray, lows: np.ndarray, window: int = 20) -> np.ndarray:
    rng = highs - lows
    n = len(rng)
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window, n):
        out[i] = float(np.median(rng[i - window : i]))
    return out


def _get_dollar_per_pt(symbol: str, lot: float) -> float:
    info = mt5.symbol_info(symbol)
    if info and info.trade_tick_size > 0:
        return (1.0 / info.trade_tick_size) * info.trade_tick_value * lot
    fallback = {"XAUUSDm": 2.0, "USOILm": 50.0, "US30m": 0.25}
    return fallback.get(symbol, 1.0)


def load_symbol_history(symbol: str):
    mt5.symbol_select(symbol, True)
    m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 30000)
    m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 12000)
    h4 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 1500)
    d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 400)
    if m5 is None or m15 is None or h4 is None or d1 is None:
        return None

    m5_t = m5["time"].astype(np.int64)
    m5_o = m5["open"].astype(np.float64)
    m5_h = m5["high"].astype(np.float64)
    m5_l = m5["low"].astype(np.float64)
    m5_c = m5["close"].astype(np.float64)
    m5_med = _rolling_median_range(m5_h, m5_l, 20)

    m15_t = m15["time"].astype(np.int64)
    m15_o = m15["open"].astype(np.float64)
    m15_h = m15["high"].astype(np.float64)
    m15_l = m15["low"].astype(np.float64)
    m15_c = m15["close"].astype(np.float64)
    m15_med = _rolling_median_range(m15_h, m15_l, 20)

    h4_t = h4["time"].astype(np.int64)
    h4_h = h4["high"].astype(np.float64)
    h4_l = h4["low"].astype(np.float64)
    h4_c = h4["close"].astype(np.float64)
    h4_b3, h4_sh, h4_sl = compute_h4_structure_series(h4_h, h4_l, h4_c)

    d1_t = d1["time"].astype(np.int64)
    d1_h = d1["high"].astype(np.float64)
    d1_l = d1["low"].astype(np.float64)

    # Precompute latest CLOSED HTF index matching each M5 bar close (bar open + 300s)
    close_ts = m5_t + 300
    m15_idx = np.searchsorted(m15_t + 900, close_ts, side="right") - 1
    h4_idx = np.searchsorted(h4_t + 14400, close_ts, side="right") - 1
    d1_idx = np.searchsorted(d1_t + 86400, close_ts, side="right") - 1

    # Cache Asian H/L (00-06 UTC) per day bucket
    day_arr = m5_t // 86400
    hr_arr = (m5_t % 86400) // 3600
    asian_cache = {}
    for d in np.unique(day_arr):
        mask = (day_arr == d) & (hr_arr < 6)
        if np.any(mask):
            hi = float(np.max(m5_h[mask]))
            lo = float(np.min(m5_l[mask]))
            asian_cache[int(d)] = (hi, lo) if hi > lo else (np.nan, np.nan)
        else:
            asian_cache[int(d)] = (np.nan, np.nan)

    spec = CALIBRATED_ASSET_SPECS[symbol]
    dpp = _get_dollar_per_pt(symbol, spec["lot"])

    return {
        "symbol": symbol,
        "spec": spec,
        "dpp": dpp,
        "m5_t": m5_t, "m5_o": m5_o, "m5_h": m5_h, "m5_l": m5_l, "m5_c": m5_c, "m5_med": m5_med,
        "m15_o": m15_o, "m15_c": m15_c, "m15_med": m15_med, "m15_idx": m15_idx,
        "h4_h": h4_h, "h4_l": h4_l, "h4_b3": h4_b3, "h4_sh": h4_sh, "h4_sl": h4_sl, "h4_idx": h4_idx,
        "d1_h": d1_h, "d1_l": d1_l, "d1_idx": d1_idx,
        "asian_cache": asian_cache,
    }


def _check_bar_exit(pos: dict, hi: float, lo: float, cl: float, dt_utc: datetime, dpp: float, spread: float):
    """Evaluate intra-bar SL, BE, Lock1, TP, and Friday 20:45 UTC weekend close conservatively."""
    direction = pos["dir"]
    entry = pos["entry"]
    sl_px = pos["sl_px"]
    tp_px = pos["tp_px"]

    # Conservative SL-first check using bid/ask spread adjustment
    if direction == "BUY":
        if lo <= sl_px:
            pnl = (sl_px - entry) * dpp
            reason = "LOCK1" if sl_px > entry + pos["be_buf"] * 1.5 else ("BE" if sl_px >= entry else "SL")
            return True, pnl, reason
        if hi >= tp_px:
            return True, pos["tp_usd"], "TP"
        fav_usd = (hi - entry) * dpp
        if not pos["lock1"] and fav_usd >= pos["lock_trig_usd"]:
            pos["sl_px"] = entry + pos["lock_amt_usd"] / dpp
            pos["lock1"] = True
            pos["be"] = True
        elif not pos["be"] and fav_usd >= pos["be_trig_usd"]:
            pos["sl_px"] = entry + pos["be_buf"]
            pos["be"] = True
        exit_mkt_pnl = (cl - entry) * dpp
    else:
        ask_hi = hi + spread
        ask_lo = lo + spread
        if ask_hi >= sl_px:
            pnl = (entry - sl_px) * dpp
            reason = "LOCK1" if sl_px < entry - pos["be_buf"] * 1.5 else ("BE" if sl_px <= entry else "SL")
            return True, pnl, reason
        if ask_lo <= tp_px:
            return True, pos["tp_usd"], "TP"
        fav_usd = (entry - ask_lo) * dpp
        if not pos["lock1"] and fav_usd >= pos["lock_trig_usd"]:
            pos["sl_px"] = entry - pos["lock_amt_usd"] / dpp
            pos["lock1"] = True
            pos["be"] = True
        elif not pos["be"] and fav_usd >= pos["be_trig_usd"]:
            pos["sl_px"] = entry - pos["be_buf"]
            pos["be"] = True
        exit_mkt_pnl = (entry - (cl + spread)) * dpp

    # Friday >= 20:45 UTC weekend close guard
    if dt_utc.weekday() == 4 and (dt_utc.hour > 20 or (dt_utc.hour == 20 and dt_utc.minute >= 45)):
        return True, exit_mkt_pnl, "WEEKEND_CLOSE"

    return False, exit_mkt_pnl, None


def run_portfolio_backtest():
    if not mt5.initialize():
        print("MT5 initialization failed")
        return

    data_by_sym = {}
    for sym in SYMBOLS:
        hist = load_symbol_history(sym)
        if hist is None:
            print(f"Missing MT5 history for {sym}")
            return
        data_by_sym[sym] = hist

    # Build chronological event map across all M5 timestamps
    events = []
    for sym, d in data_by_sym.items():
        for i in range(25, len(d["m5_t"]) - 1):
            events.append((int(d["m5_t"][i]), sym, i))
    events.sort(key=lambda x: x[0])

    common_start = max(int(d["m5_t"][25]) for d in data_by_sym.values())
    end_ts = max(int(d["m5_t"][-1]) for d in data_by_sym.values())

    open_pos = {}
    last_entry_ts = {s: 0 for s in SYMBOLS}
    last_exit_ts = {s: 0 for s in SYMBOLS}
    day_realized = {}
    trades = []

    # Group events by exact timestamp so portfolio caps and floating PnL are synchronous
    idx = 0
    n_ev = len(events)
    while idx < n_ev:
        ts = events[idx][0]
        step_items = []
        while idx < n_ev and events[idx][0] == ts:
            step_items.append(events[idx])
            idx += 1

        if ts < common_start:
            continue

        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        eat_day = (ts + 10800) // 86400
        realized_today = day_realized.get(eat_day, 0.0)

        # 1. Exit / Trail Check on open positions & accumulate floating PnL
        floating_pnl = 0.0
        for _, sym, bar_i in step_items:
            if sym not in open_pos:
                continue
            d = data_by_sym[sym]
            pos = open_pos[sym]
            closed, pnl_now, reason = _check_bar_exit(
                pos,
                float(d["m5_h"][bar_i]),
                float(d["m5_l"][bar_i]),
                float(d["m5_c"][bar_i]),
                dt_utc,
                d["dpp"],
                d["spec"]["spread_pts"],
            )
            if closed:
                del open_pos[sym]
                last_exit_ts[sym] = ts
                realized_today += pnl_now
                day_realized[eat_day] = realized_today
                trades.append({
                    "symbol": sym,
                    "dir": pos["dir"],
                    "open_ts": pos["open_ts"],
                    "close_ts": ts,
                    "pnl": round(pnl_now, 2),
                    "reason": reason,
                    "pools": pos["pools"],
                })
            else:
                floating_pnl += pnl_now

        daily_total = realized_today + floating_pnl
        dd_blocked = daily_total <= -DAILY_DD_CAP_USD
        fri_late = dt_utc.weekday() == 4 and (dt_utc.hour > 20 or (dt_utc.hour == 20 and dt_utc.minute >= 30))

        if dd_blocked or fri_late:
            continue

        # 2. Pure PA 3-TF Entry Scan
        for _, sym, bar_i in step_items:
            if sym in open_pos or len(open_pos) >= MAX_CONCURRENT:
                continue
            if (ts - last_entry_ts[sym] < COOLDOWN_SEC) or (ts - last_exit_ts[sym] < COOLDOWN_SEC):
                continue

            d = data_by_sym[sym]
            mi = int(d["m15_idx"][bar_i])
            hi_4 = int(d["h4_idx"][bar_i])
            di = int(d["d1_idx"][bar_i])
            if mi < 20 or hi_4 < 20 or di < 2:
                continue

            day_bucket = ts // 86400
            hr_utc = (ts % 86400) // 3600
            asian_h, asian_l = d["asian_cache"].get(day_bucket, (np.nan, np.nan)) if hr_utc >= 6 else (np.nan, np.nan)

            res = evaluate_3tf_pure_pa_step(
                c_m5=float(d["m5_c"][bar_i]),
                o_m5=float(d["m5_o"][bar_i]),
                h_m5=float(d["m5_h"][bar_i]),
                l_m5=float(d["m5_l"][bar_i]),
                c_m5_prev=float(d["m5_c"][bar_i - 1]),
                h_m5_prev=float(d["m5_h"][bar_i - 1]),
                l_m5_prev=float(d["m5_l"][bar_i - 1]),
                m5_med_range=float(d["m5_med"][bar_i]),
                m15_o=float(d["m15_o"][mi]),
                m15_c=float(d["m15_c"][mi]),
                m15_med_range=float(d["m15_med"][mi]),
                h4_bias_state=int(d["h4_b3"][hi_4]),
                h4_sh=float(d["h4_sh"][hi_4]),
                h4_sl=float(d["h4_sl"][hi_4]),
                pdh=float(d["d1_h"][di]),
                pdl=float(d["d1_l"][di]),
                h4_prev_h=float(d["h4_h"][hi_4]),
                h4_prev_l=float(d["h4_l"][hi_4]),
                asian_h=asian_h,
                asian_l=asian_l,
                dt_utc=dt_utc,
            )
            if not res["triggered"]:
                continue

            spec = d["spec"]
            dpp = d["dpp"]
            spread = spec["spread_pts"]
            sl_dist = spec["max_sl_dollars"] / dpp
            tp_dist = spec["tp_dollars"] / dpp
            cl = float(d["m5_c"][bar_i])
            if res["direction"] == "BUY":
                entry = cl + spread
                sl_px = entry - sl_dist
                tp_px = entry + tp_dist
            else:
                entry = cl
                sl_px = entry + sl_dist
                tp_px = entry - tp_dist

            open_pos[sym] = {
                "dir": res["direction"],
                "entry": entry,
                "sl_px": sl_px,
                "tp_px": tp_px,
                "tp_usd": spec["tp_dollars"],
                "be_trig_usd": spec["be_trigger_dollars"],
                "be_buf": 2.0 / dpp,
                "lock_trig_usd": spec["lock_trigger_dollars"],
                "lock_amt_usd": spec["lock_amount_dollars"],
                "be": False,
                "lock1": False,
                "open_ts": ts,
                "pools": "+".join(res["swept_pools"]),
            }
            last_entry_ts[sym] = ts

    def summarize(label: str, subset: list[dict]):
        n = len(subset)
        if n == 0:
            print(f"{label:<22} | Trades:   0")
            return
        pnls = np.array([t["pnl"] for t in subset], dtype=np.float64)
        reasons = [t["reason"] for t in subset]
        be_cnt = sum(1 for r in reasons if r == "BE")
        wins = sum(1 for p, r in zip(pnls, reasons) if r != "BE" and p > 0)
        losses = sum(1 for p, r in zip(pnls, reasons) if r != "BE" and p <= 0)
        wr = (wins / max(1, wins + losses)) * 100.0
        gp = float(np.sum(pnls[pnls > 0]))
        gl = float(abs(np.sum(pnls[pnls < 0])))
        pf = gp / gl if gl > 0 else 99.0
        net = float(np.sum(pnls))
        eq = np.cumsum(pnls)
        peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
        max_dd = float(np.min(eq - peak))
        exp = net / n
        print(
            f"{label:<22} | N={n:>3} | W/L/BE={wins:>3}/{losses:>2}/{be_cnt:>2} | "
            f"WR={wr:>5.1f}% | PF={pf:>4.2f} | Net=${net:>+8.2f} | Exp=${exp:>+5.2f} | MaxDD=${max_dd:>+7.2f}"
        )

    print("=" * 105)
    print("ALPHAEDGE PURE PRICE ACTION 3-TF PORTFOLIO BACKTEST (SL=$34 | TP=$55 | BE=+$28 | Lock1=+$44->$32)")
    print(f"Window: {datetime.fromtimestamp(common_start, timezone.utc).strftime('%Y-%m-%d')} -> {datetime.fromtimestamp(end_ts, timezone.utc).strftime('%Y-%m-%d')} | Max Conc: {MAX_CONCURRENT} | Daily DD Cap: -${DAILY_DD_CAP_USD:.0f}")
    print("=" * 105)

    split_ts = int(common_start + (end_ts - common_start) * 0.58)
    summarize("ALL (Full ~150d)", trades)
    summarize("TRAIN (First 58%)", [t for t in trades if t["open_ts"] < split_ts])
    summarize("OOS   (Last 42%)", [t for t in trades if t["open_ts"] >= split_ts])
    summarize("Last 90 Days", [t for t in trades if t["open_ts"] >= end_ts - 90 * 86400])
    summarize("Last 60 Days", [t for t in trades if t["open_ts"] >= end_ts - 60 * 86400])
    summarize("Last 30 Days", [t for t in trades if t["open_ts"] >= end_ts - 30 * 86400])
    print("-" * 105)
    for sym in SYMBOLS:
        summarize(f"Asset: {sym}", [t for t in trades if t["symbol"] == sym])
    print("=" * 105)


if __name__ == "__main__":
    run_portfolio_backtest()
