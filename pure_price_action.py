"""
pure_price_action.py — Institutional 3-Timeframe Pure Price Action Core

Single-source-of-truth pure price action signal & structure evaluators shared
by both the live execution engine (institutional_engine.py) and the historical
multi-timeframe backtester (backtest_institutional.py).

Zero lagging indicator triggers (no UT Bot, no EMA crossovers):
  TF1 (Macro D1 + H4): Pure fractal swing structure + BOS state & 75%/25% swing equilibrium.
  TF2 (Key Pools & M15): Sweeps of PDH/PDL, Asian Session H/L (00-06 UTC), Closed H4 H/L
                         with M15 anti-crash blowout validation.
  TF3 (Execution M5): 2-bar sweep-and-reclaim + controlled directional reversal candle
                      breaking prior bar extreme + anti-climax range guard (<= 2.5x median).
"""

from datetime import datetime, timezone
import numpy as np


KILLZONE_HOURS_UTC = frozenset({6, 7, 8, 9, 14, 15, 16, 17, 18, 19})

CALIBRATED_ASSET_SPECS = {
    "XAUUSDm": {
        "lot": 0.02,
        "tp_dollars": 55.0,
        "tp_catalyst_dollars": 70.0,
        "be_trigger_dollars": 28.0,
        "lock_trigger_dollars": 44.0,
        "lock_amount_dollars": 32.0,
        "lock2_trigger_dollars": 56.0,
        "lock2_amount_dollars": 45.0,
        "max_sl_dollars": 34.0,
        "spread_pts": 0.24,
    },
    "USOILm": {
        "lot": 0.05,
        "tp_dollars": 55.0,
        "tp_catalyst_dollars": 70.0,
        "be_trigger_dollars": 28.0,
        "lock_trigger_dollars": 44.0,
        "lock_amount_dollars": 32.0,
        "lock2_trigger_dollars": 56.0,
        "lock2_amount_dollars": 45.0,
        "max_sl_dollars": 34.0,
        "spread_pts": 0.014,
    },
    "US30m": {
        "lot": 0.25,
        "tp_dollars": 55.0,
        "tp_catalyst_dollars": 70.0,
        "be_trigger_dollars": 28.0,
        "lock_trigger_dollars": 44.0,
        "lock_amount_dollars": 32.0,
        "lock2_trigger_dollars": 56.0,
        "lock2_amount_dollars": 45.0,
        "max_sl_dollars": 34.0,
        "spread_pts": 1.30,
    },
}


def is_killzone_active(dt_utc: datetime | None = None) -> bool:
    """
    Active Monday - Friday during institutional liquidity windows:
      - London Killzone : 06:00 - 09:59 UTC (09:00 - 12:59 EAT)
      - New York Session: 14:00 - 19:59 UTC (17:00 - 22:59 EAT)
    Blocks dead Asian midnight chop (00-05 UTC), London-lunch / US-open fakeouts
    (10-13 UTC), rollover spread widening (20-23 UTC), and weekends.
    """
    if dt_utc is None:
        dt_utc = datetime.now(timezone.utc)
    if dt_utc.weekday() in (5, 6):
        return False
    return dt_utc.hour in KILLZONE_HOURS_UTC


def detect_fractal_swings(highs: np.ndarray, lows: np.ndarray, left: int = 2, right: int = 2):
    """
    Detect non-repainting fractal pivot highs and lows.
    A pivot at index i is confirmed only at index i + right.
    """
    n = len(highs)
    sh_idx, sh_val, sh_conf = [], [], []
    sl_idx, sl_val, sl_conf = [], [], []
    for i in range(left, n - right):
        w_h = highs[i - left : i + right + 1]
        if highs[i] >= np.max(w_h) and highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            sh_idx.append(i)
            sh_val.append(float(highs[i]))
            sh_conf.append(i + right)
        w_l = lows[i - left : i + right + 1]
        if lows[i] <= np.min(w_l) and lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            sl_idx.append(i)
            sl_val.append(float(lows[i]))
            sl_conf.append(i + right)
    return (
        np.array(sh_idx, dtype=np.int32), np.array(sh_val, dtype=np.float64), np.array(sh_conf, dtype=np.int32),
        np.array(sl_idx, dtype=np.int32), np.array(sl_val, dtype=np.float64), np.array(sl_conf, dtype=np.int32),
    )


def compute_h4_structure_series(h4_highs: np.ndarray, h4_lows: np.ndarray, h4_closes: np.ndarray):
    """
    Precompute 3-state pure price action structure (+1 BULLISH, -1 BEARISH, 0 RANGE)
    and latest confirmed swing high/low arrays across all closed H4 bars.
    """
    n = len(h4_closes)
    _, sh_val, sh_conf, _, sl_val, sl_conf = detect_fractal_swings(h4_highs, h4_lows, left=2, right=2)
    b3 = np.zeros(n, dtype=np.int8)
    last_sh = np.full(n, np.nan, dtype=np.float64)
    last_sl = np.full(n, np.nan, dtype=np.float64)

    sh_p, sl_p = 0, 0
    r_sh, r_sl = [], []
    for k in range(n):
        while sh_p < len(sh_conf) and sh_conf[sh_p] <= k:
            r_sh.append(float(sh_val[sh_p]))
            sh_p += 1
        while sl_p < len(sl_conf) and sl_conf[sl_p] <= k:
            r_sl.append(float(sl_val[sl_p]))
            sl_p += 1
        if r_sh:
            last_sh[k] = r_sh[-1]
        if r_sl:
            last_sl[k] = r_sl[-1]
        if len(r_sh) >= 2 and len(r_sl) >= 2:
            c = float(h4_closes[k])
            if r_sh[-1] > r_sh[-2] and r_sl[-1] > r_sl[-2] and c > r_sl[-1]:
                b3[k] = 1
            elif r_sh[-1] < r_sh[-2] and r_sl[-1] < r_sl[-2] and c < r_sh[-1]:
                b3[k] = -1
            elif c > r_sh[-1]:
                b3[k] = 1
            elif c < r_sl[-1]:
                b3[k] = -1
            else:
                b3[k] = 0
    return b3, last_sh, last_sl


def compute_asian_range_at(m5_times: np.ndarray, m5_highs: np.ndarray, m5_lows: np.ndarray, ref_ts_sec: int):
    """
    Compute today's completed Asian session high/low (00:00 - 05:59 UTC).
    Returns (asian_high, asian_low) only when ref_ts_utc hour >= 6 and Asian bars exist.
    """
    day = int(ref_ts_sec) // 86400
    hr = (int(ref_ts_sec) % 86400) // 3600
    if hr < 6:
        return np.nan, np.nan
    day_arr = m5_times // 86400
    hr_arr = (m5_times % 86400) // 3600
    mask = (day_arr == day) & (hr_arr < 6)
    if not np.any(mask):
        return np.nan, np.nan
    hi = float(np.max(m5_highs[mask]))
    lo = float(np.min(m5_lows[mask]))
    return (hi, lo) if hi > lo else (np.nan, np.nan)


def evaluate_3tf_pure_pa_step(
    c_m5: float, o_m5: float, h_m5: float, l_m5: float,
    c_m5_prev: float, h_m5_prev: float, l_m5_prev: float,
    m5_med_range: float,
    m15_o: float, m15_c: float, m15_med_range: float,
    h4_bias_state: int, h4_sh: float, h4_sl: float,
    pdh: float, pdl: float,
    h4_prev_h: float, h4_prev_l: float,
    asian_h: float, asian_l: float,
    dt_utc: datetime,
):
    """
    Execute the exact calibrated 3-Timeframe Pure Price Action signal gate on closed bars.

    Returns dict with:
      triggered: bool
      direction: 'BUY' | 'SELL' | None
      sweep_extreme: float
      swept_pools: list[str]
      h4_trend: 'BULLISH' | 'BEARISH' | 'RANGE'
      h4_swing_loc_pct: float
      reason: str
    """
    h4_trend = "BULLISH" if h4_bias_state == 1 else ("BEARISH" if h4_bias_state == -1 else "RANGE")
    result = {
        "triggered": False,
        "direction": None,
        "sweep_extreme": 0.0,
        "swept_pools": [],
        "h4_trend": h4_trend,
        "h4_swing_loc_pct": 50.0,
        "reason": "NO_SETUP",
    }

    if not is_killzone_active(dt_utc):
        result["reason"] = "OUTSIDE_KILLZONE"
        return result

    rng = h_m5 - l_m5
    if rng <= 0.0 or m5_med_range <= 0.0 or np.isnan(m5_med_range):
        result["reason"] = "INVALID_RANGE"
        return result

    # TF3 Anti-Climax Guard: reject spike candles (> 2.5x median M5 range)
    if rng > 2.5 * m5_med_range:
        result["reason"] = "M5_CLIMAX_OVEREXTENDED"
        return result

    # TF1 H4 Active Swing Leg Equilibrium (Block top 25% chasing for BUY, bottom 25% for SELL)
    h4_eq_buy_ok = True
    h4_eq_sell_ok = True
    if not np.isnan(h4_sh) and not np.isnan(h4_sl) and h4_sh > h4_sl:
        h4_loc = (c_m5 - h4_sl) / (h4_sh - h4_sl) * 100.0
        result["h4_swing_loc_pct"] = round(h4_loc, 1)
        h4_eq_buy_ok = h4_loc <= 75.0
        h4_eq_sell_ok = h4_loc >= 25.0

    # Liquidity pools from completed sessions/higher timeframes
    buy_lvls = [("PDL", pdl), ("H4L", h4_prev_l)]
    sell_lvls = [("PDH", pdh), ("H4H", h4_prev_h)]
    if not np.isnan(asian_l):
        buy_lvls.append(("ASIAN_L", float(asian_l)))
    if not np.isnan(asian_h):
        sell_lvls.append(("ASIAN_H", float(asian_h)))

    lo2 = min(l_m5, l_m5_prev)
    hi2 = max(h_m5, h_m5_prev)

    hit_buy = [name for name, lvl in buy_lvls if lo2 < lvl and c_m5 > lvl and c_m5_prev <= lvl]
    hit_sell = [name for name, lvl in sell_lvls if hi2 > lvl and c_m5 < lvl and c_m5_prev >= lvl]

    # TF2 + TF3 BUY Evaluation
    if hit_buy and c_m5 > o_m5 and (c_m5 - o_m5) >= 0.40 * m5_med_range and (c_m5 - l_m5) / rng >= 0.55 and c_m5 > h_m5_prev:
        if h4_bias_state == -1:
            result["reason"] = f"H4_BEARISH_BLOCKS_BUY({','.join(hit_buy)})"
        elif not h4_eq_buy_ok:
            result["reason"] = f"H4_OVEREXTENDED_BUY({result['h4_swing_loc_pct']:.1f}%)"
        elif m15_med_range > 0 and (m15_o - m15_c) > 1.5 * m15_med_range:
            result["reason"] = f"M15_BEAR_CRASH_BLOCKS_BUY({','.join(hit_buy)})"
        else:
            result.update({
                "triggered": True,
                "direction": "BUY",
                "sweep_extreme": float(lo2),
                "swept_pools": hit_buy,
                "reason": f"3TF_PA_SWEEP_RECLAIM({'+'.join(hit_buy)})",
            })
            return result

    # TF2 + TF3 SELL Evaluation
    if hit_sell and c_m5 < o_m5 and (o_m5 - c_m5) >= 0.40 * m5_med_range and (h_m5 - c_m5) / rng >= 0.55 and c_m5 < l_m5_prev:
        if h4_bias_state == 1:
            result["reason"] = f"H4_BULLISH_BLOCKS_SELL({','.join(hit_sell)})"
        elif not h4_eq_sell_ok:
            result["reason"] = f"H4_OVEREXTENDED_SELL({result['h4_swing_loc_pct']:.1f}%)"
        elif m15_med_range > 0 and (m15_c - m15_o) > 1.5 * m15_med_range:
            result["reason"] = f"M15_BULL_BLOWOUT_BLOCKS_SELL({','.join(hit_sell)})"
        else:
            result.update({
                "triggered": True,
                "direction": "SELL",
                "sweep_extreme": float(hi2),
                "swept_pools": hit_sell,
                "reason": f"3TF_PA_SWEEP_RECLAIM({'+'.join(hit_sell)})",
            })
            return result

    return result
