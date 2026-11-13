"""
institutional_engine.py — Institutional Multi-Timeframe (MTF) & Whale Flow Analysis Engine
=============================================================================================
Identifies high-conviction structural swing setups:
1. Multi-Timeframe (MTF) Top-Down Structure:
   - H4: Macro Trend & Dealing Range (Premium vs Discount Zone).
         STRICT RULE: BUY orders are ONLY permitted in the Discount Zone (< 50% equilibrium).
                      SELL orders are ONLY permitted in the Premium Zone (> 50% equilibrium).
                      Never buy halfway or chase extended highs!
   - H1: Key Structural Levels (Swing Highs & Lows, Liquidity Pools).
   - M15: Sniper Entry Confirmation (Liquidity Sweep + Bullish/Bearish Displacement).
2. Whale Flow & Volume Climax Detection:
   - Liquidity Sweeps (Stop Hunts): Price pierces a key swing low/high to absorb retail stop orders,
     then immediately rejects back inside the range.
   - Climax Tick Volume: Volume on the sweep/reversal candle >= 1.8x the 20-period moving average.
   - Fair Value Gap (FVG) / Displacement: Strong institutional expansion confirming market participation.
"""

import logging
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

logger = logging.getLogger("AlphaEdge.Institutional")


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Computes Average True Range (ATR) over the last `period` completed bars.
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    Returns 0.0 if data is insufficient.
    """
    if df is None or len(df) < period + 2:
        return 0.0
    highs  = df['high'].astype(float).values
    lows   = df['low'].astype(float).values
    closes = df['close'].astype(float).values
    trs = []
    for i in range(1, len(highs)):
        hl  = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i]  - closes[i - 1])
        trs.append(max(hl, hpc, lpc))
    # Use the last `period` TR values for a simple ATR
    return float(np.mean(trs[-period:]))



class InstitutionalEngine:
    def __init__(self, volume_multiplier: float = 1.6, lookback_h4: int = 50, lookback_swings: int = 25):
        self.volume_mult = volume_multiplier
        self.lookback_h4 = lookback_h4
        self.lookback_swings = lookback_swings

    def get_mtf_data(self, symbol: str):
        """Fetches H4, H1, M15, and M5 bars from MT5."""
        mt5.symbol_select(symbol, True)
        h4_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 100)
        h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 150)
        m15_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 150)
        m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 150)

        if h4_rates is None or h1_rates is None or m15_rates is None:
            return None

        data = {
            "H4": pd.DataFrame(h4_rates),
            "H1": pd.DataFrame(h1_rates),
            "M15": pd.DataFrame(m15_rates)
        }
        if m5_rates is not None:
            data["M5"] = pd.DataFrame(m5_rates)
        return data

    def analyze_dealing_range(self, df_h4: pd.DataFrame, current_price: float, pdh: float = 0.0, pdl: float = 0.0):
        """
        Calculates the Institutional Dealing Range using FIXED daily anchors.

        Uses Previous Day High (PDH) and Previous Day Low (PDL) as the range
        boundaries — these are confirmed, locked levels that do NOT shift as
        new H4 bars form during the trading day. This eliminates the 'parallax
        error' where a trade opened at 68% premium appears to be at 63% mid-range
        an hour later because the rolling window expanded.

        Falls back to the rolling H4 range (last 50 bars) if PDH/PDL are not
        available (e.g. on first bar of the week or after a data gap).

        Location %:
          0%   = at PDL (Previous Day Low)   — deepest discount
          50%  = at equilibrium (midpoint)   — neutral
          100% = at PDH (Previous Day High)  — deepest premium
        """
        # ── Primary: PDH / PDL fixed anchor ──────────────────────────────────
        if pdh > 0 and pdl > 0 and pdh > pdl:
            range_high = float(pdh)
            range_low  = float(pdl)
        else:
            # ── Fallback: rolling H4 window ───────────────────────────────────
            recent_h4  = df_h4.tail(self.lookback_h4)
            range_high = float(recent_h4['high'].max())
            range_low  = float(recent_h4['low'].min())

        spread_range = range_high - range_low
        if spread_range <= 0:
            return None

        equilibrium        = range_low + (0.5  * spread_range)
        discount_threshold = range_low + (0.35 * spread_range)   # Deep discount: bottom 35%
        premium_threshold  = range_high - (0.35 * spread_range)  # Deep premium:  top 35%

        # Raw percentage relative to yesterday's range
        raw_location_pct = ((current_price - range_low) / spread_range) * 100.0

        # Clamped location for internal oscillator metrics
        location_pct = max(0.0, min(100.0, raw_location_pct))

        # Breakout status: outside confirmed PDH/PDL range
        is_breakout_down = current_price < range_low
        is_breakout_up   = current_price > range_high

        is_discount      = (current_price < equilibrium) and not is_breakout_down
        is_deep_discount = (current_price <= discount_threshold) and not is_breakout_down
        is_premium       = (current_price > equilibrium) and not is_breakout_up
        is_deep_premium  = (current_price >= premium_threshold) and not is_breakout_up

        return {
            "range_high":       range_high,
            "range_low":        range_low,
            "equilibrium":      equilibrium,
            "location_pct":     raw_location_pct,
            "clamped_loc_pct":  location_pct,
            "is_discount":      is_discount,
            "is_deep_discount": is_deep_discount,
            "is_premium":       is_premium,
            "is_deep_premium":  is_deep_premium,
            "is_breakout_down": is_breakout_down,
            "is_breakout_up":   is_breakout_up,
            "anchor":           "PDH/PDL" if (pdh > 0 and pdl > 0) else "H4_ROLLING"
        }

    def detect_whale_volume(self, df: pd.DataFrame, idx: int = -2):
        """
        Checks if candle at idx has institutional/whale volume (relative to 20 SMA).
        Index -2 is the latest fully closed candle.
        """
        if len(df) < 25:
            return False, 1.0

        volumes = df['tick_volume'].astype(float).values
        vol_sma20 = np.mean(volumes[idx - 20:idx])
        curr_vol = volumes[idx]

        vol_ratio = curr_vol / vol_sma20 if vol_sma20 > 0 else 1.0
        is_whale = vol_ratio >= self.volume_mult

        return is_whale, round(float(vol_ratio), 2)

    def detect_liquidity_sweep(self, df: pd.DataFrame, direction: str = "BUY"):
        """
        Detects if a swing low (for BUY) or swing high (for SELL) was swept by the last closed candle:
        - For BUY: Candle low pierced below previous swing low, but closed ABOVE the swing low (rejection).
        - For SELL: Candle high pierced above previous swing high, but closed BELOW the swing high.
        """
        if len(df) < self.lookback_swings + 5:
            return False, 0.0

        # Last closed candle is at index -2
        test_candle = df.iloc[-2]
        c_low = float(test_candle['low'])
        c_high = float(test_candle['high'])
        c_close = float(test_candle['close'])
        c_open = float(test_candle['open'])

        # Previous window for swing points (excluding test candle)
        prev_window = df.iloc[- (self.lookback_swings + 2): -2]

        if direction == "BUY":
            swing_low = float(prev_window['low'].min())
            # Liquidity Sweep condition: pierced below swing low, but closed back above it
            swept = (c_low < swing_low) and (c_close > swing_low)
            # Rejection wick validation (lower wick >= 35% of candle range)
            candle_range = c_high - c_low
            lower_wick = min(c_open, c_close) - c_low
            has_rejection_wick = (lower_wick / candle_range >= 0.35) if candle_range > 0 else False

            if swept or (c_low <= swing_low * 1.0005 and has_rejection_wick and c_close > c_open):
                return True, swing_low
            return False, swing_low

        elif direction == "SELL":
            swing_high = float(prev_window['high'].max())
            swept = (c_high > swing_high) and (c_close < swing_high)
            candle_range = c_high - c_low
            upper_wick = c_high - max(c_open, c_close)
            has_rejection_wick = (upper_wick / candle_range >= 0.35) if candle_range > 0 else False

            if swept or (c_high >= swing_high * 0.9995 and has_rejection_wick and c_close < c_open):
                return True, swing_high
            return False, swing_high

        return False, 0.0

    def detect_fair_value_gap(self, df: pd.DataFrame, direction: str = "BUY"):
        """
        Identifies recent Fair Value Gap (FVG) / Imbalance within last 5 candles.
        Bullish FVG: Candle[i-2].high < Candle[i].low (gap between bar 1 and bar 3).
        Bearish FVG: Candle[i-2].low > Candle[i].high.
        """
        if len(df) < 10:
            return False, 0.0

        for i in range(-2, -7, -1):
            if direction == "BUY":
                c1_high = float(df['high'].iloc[i - 2])
                c3_low = float(df['low'].iloc[i])
                if c3_low > c1_high:
                    gap_size = c3_low - c1_high
                    return True, gap_size
            elif direction == "SELL":
                c1_low = float(df['low'].iloc[i - 2])
                c3_high = float(df['high'].iloc[i])
                if c3_high < c1_low:
                    gap_size = c1_low - c3_high
                    return True, gap_size

        return False, 0.0

    def compute_ut_bot_signal(self, df: pd.DataFrame, key_value: float = 1.0, atr_period: int = 10) -> str:
        """
        UT Bot Alerts — Pure Python implementation of QuantNomad's UT Bot.
        Uses an ATR trailing stop to detect confirmed momentum reversals.

        Parameters:
            key_value   : Sensitivity multiplier (default 1.0 = standard, higher = fewer signals)
            atr_period  : ATR period (default 10, matching TradingView default)

        Returns:
            "BUY"  — price just crossed ABOVE the trailing stop (confirmed upward reversal)
            "SELL" — price just crossed BELOW the trailing stop (confirmed downward reversal)
            "NONE" — no confirmed signal on the latest completed bar

        Logic (mirrors TradingView Pine Script):
            nLoss = key_value * ATR(atr_period)
            if close > prev_stop and prev_close > prev_stop:
                stop = max(prev_stop, close - nLoss)
            elif close < prev_stop and prev_close < prev_stop:
                stop = min(prev_stop, close + nLoss)
            else:
                stop = close - nLoss  if close > prev_stop  else close + nLoss

            BUY  = prev_close < prev_stop and close > stop   (cross above)
            SELL = prev_close > prev_stop and close < stop   (cross below)
        """
        min_bars = atr_period + 5
        if df is None or len(df) < min_bars:
            return "NONE"

        closes = df['close'].astype(float).values
        highs  = df['high'].astype(float).values
        lows   = df['low'].astype(float).values

        # Compute ATR for every bar
        trs = [0.0]
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i]  - closes[i-1]))
            trs.append(tr)

        # Simple rolling ATR (period-window mean of TR)
        atrs = []
        for i in range(len(trs)):
            start = max(0, i - atr_period + 1)
            atrs.append(float(np.mean(trs[start:i+1])))

        # Build trailing stop array
        stops = [0.0] * len(closes)
        stops[0] = closes[0]

        for i in range(1, len(closes)):
            n_loss   = key_value * atrs[i]
            prev_c   = closes[i-1]
            c        = closes[i]
            prev_s   = stops[i-1]

            if c > prev_s and prev_c > prev_s:
                stops[i] = max(prev_s, c - n_loss)
            elif c < prev_s and prev_c < prev_s:
                stops[i] = min(prev_s, c + n_loss)
            else:
                stops[i] = c - n_loss if c > prev_s else c + n_loss

        # Check last 3 completed bars for crossover or prevailing trend
        # Index -2 is the latest fully confirmed/closed bar
        i = len(closes) - 2
        if i < 2:
            return "NONE"

        curr_close = closes[i]
        curr_stop  = stops[i]
        
        # Detect fresh crossover on the latest completed bar (index -2)
        # Prevents re-entering into an already established trend after a trade closes!
        if closes[i - 1] <= stops[i - 1] and closes[i] > stops[i]:
            return "BUY"
        elif closes[i - 1] >= stops[i - 1] and closes[i] < stops[i]:
            return "SELL"

        return "NONE"

    def check_approach_architecture(self, df_m5: pd.DataFrame, direction: str) -> dict:
        """
        Institutional Approach Architecture Filter
        -------------------------------------------------------------------------
        How price arrives at an institutional support/resistance zone dictates whether
        the zone will hold or be smashed:
          1. PRE-ZONE COMPRESSION (Dangerous / Breakout Trap):
             Price spends 6+ bars balancing in a tight range right before reaching the zone.
             Market participants have accumulated stops right beneath the boundary;
             the breakout will have excessive energy and smash the zone.
             -> Trade must be ABORTED.
          2. EXHAUSTION SWEEP (High Probability Reversal):
             Price arrives via swift, wide-range exhaustion bars directly into the zone.
             Short-term momentum is exhausted into resting institutional liquidity walls.
             -> Trade is APPROVED for high-conviction fade.
        """
        if df_m5 is None or len(df_m5) < 18:
            return {"safe": True, "approach": "EXHAUSTION_SWEEP", "reason": "M5 data insufficient, bypass filter"}

        highs  = df_m5['high'].astype(float).values
        lows   = df_m5['low'].astype(float).values

        # Compute M5 local ATR over last 14 bars
        m5_atr = _compute_atr(df_m5, period=14)
        if m5_atr <= 0:
            return {"safe": True, "approach": "EXHAUSTION_SWEEP", "reason": "ATR unavailable, bypass filter"}

        # Inspect the 8 bars preceding the latest closed candle (bars -10 to -2)
        window = 8
        approach_highs = highs[- (window + 2): -2]
        approach_lows  = lows[- (window + 2): -2]

        bar_ranges = [approach_highs[k] - approach_lows[k] for k in range(len(approach_highs))]
        avg_bar_range = float(np.mean(bar_ranges))
        cluster_high = float(np.max(approach_highs))
        cluster_low  = float(np.min(approach_lows))
        total_cluster_span = cluster_high - cluster_low

        # Compression Criteria:
        # 1. Average bar range is very tight (< 0.50 * ATR)
        # 2. Total span of the 8-bar block is unusually compressed (< 1.30 * ATR)
        is_compressed = (avg_bar_range < (0.50 * m5_atr)) and (total_cluster_span < (1.30 * m5_atr))

        if is_compressed:
            return {
                "safe": False,
                "approach": "PRE_ZONE_COMPRESSION",
                "avg_range": avg_bar_range,
                "atr": m5_atr,
                "cluster_span": total_cluster_span,
                "reason": (
                    f"Pre-Zone Compression detected "
                    f"(Avg Range {avg_bar_range:.2f} < 0.5x ATR {m5_atr:.2f}, Span {total_cluster_span:.2f}). "
                    f"Market balanced right at border — high risk of breakdown through zone."
                )
            }

        return {
            "safe": True,
            "approach": "EXHAUSTION_SWEEP",
            "avg_range": avg_bar_range,
            "atr": m5_atr,
            "cluster_span": total_cluster_span,
            "reason": f"Exhaustion arrival into institutional liquidity wall (Range: {avg_bar_range:.2f})"
        }

    def verify_m5_pullback_confirmation(
        self,
        df_m5: pd.DataFrame,
        direction: str,
        max_chase_dist: float = 0.0,
        sweep_ref: float = 0.0
    ) -> dict:
        """
        M5 Micro-Fractal Pullback Confirmation & Anti-Chase Shield:
        ---------------------------------------------------------
        1. Ensures entry occurs on the TURN of the micro pullback (rejection candle).
        2. Anti-Chase Shield:
           If the candle closed too far into the move away from the sweep low/high
           (> max_chase_dist), entering now means buying the apex of the wick with poor R:R.
           Declines late entries to protect wholesale entry pricing.
        """
        if df_m5 is None or len(df_m5) < 6:
            # Fallback if M5 data unavailable
            return {"confirmed": True, "chasing": False, "entry_price": None, "reason": "M5 data unavailable, fallback"}

        closes = df_m5['close'].astype(float).values
        opens  = df_m5['open'].astype(float).values
        highs  = df_m5['high'].astype(float).values
        lows   = df_m5['low'].astype(float).values

        # Index -2 is the latest fully closed M5 bar; index -3 is the previous closed M5 bar
        c_close = closes[-2]
        c_open  = opens[-2]
        c_high  = highs[-2]
        c_low   = lows[-2]

        p_close = closes[-3]
        p_open  = opens[-3]
        p_high  = highs[-3]
        p_low   = lows[-3]

        # Check recent 3 bars prior to latest closed bar for pullback presence
        prior_closes = closes[-5:-2]
        prior_opens  = opens[-5:-2]

        if direction == "BUY":
            # Look for pullback evidence: at least one red or indecisive bar in the preceding 3 bars
            had_pullback = any(prior_closes[k] <= prior_opens[k] for k in range(len(prior_closes)))
            
            # Anti-Chase Shield check
            if sweep_ref > 0 and max_chase_dist > 0:
                chase_distance = c_close - sweep_ref
                if chase_distance > max_chase_dist:
                    return {
                        "confirmed": False,
                        "chasing": True,
                        "entry_price": c_close,
                        "reason": f"Anti-Chase Shield: M5 close {c_close:.2f} is {chase_distance:.2f} pts above sweep low (max allowed: {max_chase_dist:.2f}). Waiting for wholesale re-test."
                    }

            # Confirmation: Latest closed M5 candle is BULLISH and breaks above previous M5 candle's high/close
            is_bullish_turn = (c_close > c_open) and (c_close >= p_high or c_close > p_close)
            
            if had_pullback and is_bullish_turn:
                return {
                    "confirmed": True,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Bullish Turn Confirmed (Close {c_close:.2f} > Prev High {p_high:.2f})"
                }
            elif not had_pullback and (c_close > c_open):
                # Strong impulse with no deep pullback, but M5 still actively accelerating bullishly
                return {
                    "confirmed": True,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Momentum Aligned Bullish (Close {c_close:.2f})"
                }
            else:
                return {
                    "confirmed": False,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Pullback In Progress: waiting for bullish reversal candle (Latest M5 close={c_close:.2f})"
                }

        elif direction == "SELL":
            # Look for pullback bounce evidence: at least one green or indecisive bar in the preceding 3 bars
            had_pullback = any(prior_closes[k] >= prior_opens[k] for k in range(len(prior_closes)))

            # Anti-Chase Shield check
            if sweep_ref > 0 and max_chase_dist > 0:
                chase_distance = sweep_ref - c_close
                if chase_distance > max_chase_dist:
                    return {
                        "confirmed": False,
                        "chasing": True,
                        "entry_price": c_close,
                        "reason": f"Anti-Chase Shield: M5 close {c_close:.2f} is {chase_distance:.2f} pts below sweep high (max allowed: {max_chase_dist:.2f}). Waiting for wholesale re-test."
                    }

            # Confirmation: Latest closed M5 candle is BEARISH and breaks below previous M5 candle's low/close
            is_bearish_turn = (c_close < c_open) and (c_close <= p_low or c_close < p_close)

            if had_pullback and is_bearish_turn:
                return {
                    "confirmed": True,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Bearish Turn Confirmed (Close {c_close:.2f} < Prev Low {p_low:.2f})"
                }
            elif not had_pullback and (c_close < c_open):
                # Strong downward impulse with no deep pullback, but M5 actively accelerating bearishly
                return {
                    "confirmed": True,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Momentum Aligned Bearish (Close {c_close:.2f})"
                }
            else:
                return {
                    "confirmed": False,
                    "chasing": False,
                    "entry_price": c_close,
                    "reason": f"M5 Pullback In Progress: waiting for bearish reversal candle (Latest M5 close={c_close:.2f})"
                }

        return {"confirmed": True, "chasing": False, "entry_price": None, "reason": "Neutral"}

    def evaluate_institutional_setup(self, symbol: str):
        """
        Comprehensive Institutional Setup Evaluation:
        Returns structured dictionary.
        """
        mtf = self.get_mtf_data(symbol)
        if not mtf:
            return {"valid": False, "direction": "NONE", "reason": "Failed to fetch MTF data"}

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return {"valid": False, "direction": "NONE", "reason": "No tick data"}

        curr_price = tick.bid
        df_h4 = mtf["H4"]
        df_h1 = mtf["H1"]
        df_m15 = mtf["M15"]

        # ── Load PDH/PDL from StructuralMemory state for fixed daily anchor ──────
        # StructuralMemory tracks Previous Day High/Low per symbol — these are
        # locked reference points that don't shift as new intra-day bars form.
        pdh, pdl = 0.0, 0.0
        try:
            import os, json as _json
            _state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "structural_memory_state.json")
            if os.path.exists(_state_path):
                with open(_state_path, "r", encoding="utf-8") as _f:
                    _state = _json.load(_f)
                _sym_state = _state.get(symbol, {})
                pdh = float(_sym_state.get("pdh", 0.0))
                pdl = float(_sym_state.get("pdl", 0.0))
        except Exception:
            pass   # Fall through to rolling H4 anchor

        # 1. H4 Macro Bias & Dealing Range Analysis (PDH/PDL anchored)
        range_info = self.analyze_dealing_range(df_h4, curr_price, pdh=pdh, pdl=pdl)
        if not range_info:
            return {"valid": False, "direction": "NONE", "reason": "Dealing range calculation failed"}

        # Pure Price Action 3-Timeframe Confirmation (NO UT Bot / NO EMA crossovers)
        from datetime import datetime, timezone
        from pure_price_action import (
            compute_h4_structure_series,
            compute_asian_range_at,
            evaluate_3tf_pure_pa_step,
        )

        # TF1: Closed H4 Pure Fractal Swing & BOS Narrative (non-repainting)
        df_h4_closed = df_h4.iloc[:-1]
        h4_b3, h4_sh_arr, h4_sl_arr = compute_h4_structure_series(
            df_h4_closed["high"].astype(float).values,
            df_h4_closed["low"].astype(float).values,
            df_h4_closed["close"].astype(float).values,
        )
        h4_bias_state = int(h4_b3[-1]) if len(h4_b3) > 0 else 0
        h4_sh = float(h4_sh_arr[-1]) if len(h4_sh_arr) > 0 else float("nan")
        h4_sl = float(h4_sl_arr[-1]) if len(h4_sl_arr) > 0 else float("nan")
        h4_trend = "BULLISH" if h4_bias_state == 1 else ("BEARISH" if h4_bias_state == -1 else "RANGE")

        # Closed H1 pure swing state (informational / structural telemetry only)
        df_h1_closed = df_h1.iloc[:-1]
        h1_b3, _, _ = compute_h4_structure_series(
            df_h1_closed["high"].astype(float).values,
            df_h1_closed["low"].astype(float).values,
            df_h1_closed["close"].astype(float).values,
        )
        h1_bias_state = int(h1_b3[-1]) if len(h1_b3) > 0 else 0
        h1_bullish = (h1_bias_state == 1)
        h1_bearish = (h1_bias_state == -1)

        # TF2 + TF3: Fetch closed M5 bars for Asian range & M5 sweep reclaim confirmation
        rates_m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 320)
        pa_res = {
            "triggered": False,
            "direction": None,
            "sweep_extreme": 0.0,
            "swept_pools": [],
            "h4_trend": h4_trend,
            "h4_swing_loc_pct": 50.0,
            "reason": "M5_INSUFFICIENT_DATA",
        }
        if rates_m5 is not None and len(rates_m5) >= 30 and len(df_m15) >= 22:
            df_m5_closed = pd.DataFrame(rates_m5).iloc[:-1]
            df_m15_closed = df_m15.iloc[:-1]
            m5_ranges_prev = (
                df_m5_closed["high"].iloc[-21:-1].astype(float)
                - df_m5_closed["low"].iloc[-21:-1].astype(float)
            ).values
            m15_ranges = (
                df_m15_closed["high"].iloc[-20:].astype(float)
                - df_m15_closed["low"].iloc[-20:].astype(float)
            ).values
            m5_med_range = float(np.median(m5_ranges_prev)) if len(m5_ranges_prev) > 0 else 0.0
            m15_med_range = float(np.median(m15_ranges)) if len(m15_ranges) > 0 else 0.0

            eval_ts = int(df_m5_closed["time"].iloc[-1])
            asian_h, asian_l = compute_asian_range_at(
                df_m5_closed["time"].astype(int).values,
                df_m5_closed["high"].astype(float).values,
                df_m5_closed["low"].astype(float).values,
                eval_ts,
            )
            dt_utc = datetime.fromtimestamp(eval_ts, tz=timezone.utc)
            pa_res = evaluate_3tf_pure_pa_step(
                c_m5=float(df_m5_closed["close"].iloc[-1]),
                o_m5=float(df_m5_closed["open"].iloc[-1]),
                h_m5=float(df_m5_closed["high"].iloc[-1]),
                l_m5=float(df_m5_closed["low"].iloc[-1]),
                c_m5_prev=float(df_m5_closed["close"].iloc[-2]),
                h_m5_prev=float(df_m5_closed["high"].iloc[-2]),
                l_m5_prev=float(df_m5_closed["low"].iloc[-2]),
                m5_med_range=m5_med_range,
                m15_o=float(df_m15_closed["open"].iloc[-1]),
                m15_c=float(df_m15_closed["close"].iloc[-1]),
                m15_med_range=m15_med_range,
                h4_bias_state=h4_bias_state,
                h4_sh=h4_sh,
                h4_sl=h4_sl,
                pdh=float(pdh),
                pdl=float(pdl),
                h4_prev_h=float(df_h4_closed["high"].iloc[-1]),
                h4_prev_l=float(df_h4_closed["low"].iloc[-1]),
                asian_h=asian_h,
                asian_l=asian_l,
                dt_utc=dt_utc,
            )

        loc_pct = range_info.get("clamped_loc_pct", 50.0)
        raw_loc = range_info.get("location_pct", 50.0)
        is_breakout_down = range_info.get("is_breakout_down", False)
        is_breakout_up   = range_info.get("is_breakout_up", False)

        # STRICT INSTITUTIONAL MULTI-TIMEFRAME GATE:
        # 1. Never trade counter-trend on H4 / H1.
        # 2. Never catch falling knives outside the dealing range:
        #    If price broke below PDL (raw_loc < 0%), buying is FORBIDDEN (Institutions are in Expansion Down).
        #    If price broke above PDH (raw_loc > 100%), selling is FORBIDDEN (Institutions are in Expansion Up).
        # 3. Intermediate H1 trend must NOT oppose the setup.
        macro_buy_allowed = bool(pa_res["triggered"] and pa_res["direction"] == "BUY")
        macro_sell_allowed = bool(pa_res["triggered"] and pa_res["direction"] == "SELL")

        # 2. M15 / H1 Micro Actionable Trigger Evaluation
        m15_buy_sweep, m15_low = self.detect_liquidity_sweep(df_m15, "BUY")
        m15_sell_sweep, m15_high = self.detect_liquidity_sweep(df_m15, "SELL")
        h1_buy_sweep, h1_low = self.detect_liquidity_sweep(df_h1, "BUY")
        h1_sell_sweep, h1_high = self.detect_liquidity_sweep(df_h1, "SELL")

        m15_vol_whale, m15_ratio = self.detect_whale_volume(df_m15)
        h1_vol_whale, h1_ratio = self.detect_whale_volume(df_h1)
        has_whale_vol = m15_vol_whale or h1_vol_whale
        max_vol_ratio = max(m15_ratio, h1_ratio)

        has_buy_fvg, _ = self.detect_fair_value_gap(df_m15, "BUY")
        has_sell_fvg, _ = self.detect_fair_value_gap(df_m15, "SELL")

        # M15 & H1 UT Bot Momentum Signals
        ut_signal_m15 = self.compute_ut_bot_signal(df_m15, key_value=1.0, atr_period=10)
        ut_signal_h1  = self.compute_ut_bot_signal(df_h1,  key_value=1.0, atr_period=10)

        # M15 ATR for responsive sniper SL calculation
        m15_atr = _compute_atr(df_m15, period=14)
        ut_signal_m15 = ("PA_" + "+".join(pa_res["swept_pools"])) if pa_res["swept_pools"] else "PURE_PA"
        whale_gate_ok = True
        if symbol == "XAUUSDm":
            min_sl_pts = 17.0   # 17.0 pts ($34 USD at 0.02 lot)
            min_tp_pts = 27.5   # 27.5 pts ($55 USD at 0.02 lot)
        elif "OIL" in symbol:
            min_sl_pts = 0.68   # $0.68 ($34 USD at 0.05 lot)
            min_tp_pts = 1.10   # $1.10 ($55 USD at 0.05 lot)
        elif "30" in symbol or "USTEC" in symbol or "NAS" in symbol:
            min_sl_pts = 136.0  # 136 pts ($34 USD at 0.25 lot)
            min_tp_pts = 220.0  # 220 pts ($55 USD at 0.25 lot)
        elif "XAG" in symbol or "SILVER" in symbol:
            min_sl_pts = 0.36   # $0.36 ($36 USD at 0.02 lot)
            min_tp_pts = 0.60   # $0.60 ($60 USD at 0.02 lot)
        elif "500" in symbol or "SPX" in symbol:
            min_sl_pts = 36.0   # 36.0 pts ($36 USD at 1.0 lot)
            min_tp_pts = 60.0   # 60.0 pts ($60 USD at 1.0 lot)
        elif "DE30" in symbol or "GER" in symbol:
            min_sl_pts = 128.0  # 128 pts ($36 USD at 0.25 lot)
            min_tp_pts = 214.0  # 214 pts ($60 USD at 0.25 lot)
        elif "JPY" in symbol:
            min_sl_pts = 1.14   # 114 pips ($36 USD at 0.05 lot)
            min_tp_pts = 1.89   # 189 pips ($60 USD at 0.05 lot)
        else:
            min_sl_pts = 0.0036 # Forex 36 pips ($36 USD at 0.10 lot)
            min_tp_pts = 0.0060 # Forex 60 pips ($60 USD at 0.10 lot)
        sl_buffer = max(round(m15_atr * 1.5, 4), min_sl_pts)

        # Institutional Volume Gate: Gold fires on UT Bot signal or sweep (high liquidity).
        whale_gate_ok = True

        # ---------------- BUY SETUP EVALUATION ----------------
        has_buy_trigger = macro_buy_allowed
        if macro_buy_allowed and has_buy_trigger and whale_gate_ok:
            sweep_ref = float(pa_res["sweep_extreme"]) if pa_res["sweep_extreme"] > 0 else (curr_price - sl_buffer)
            sl_price = sweep_ref - sl_buffer
            sl_dist = abs(tick.ask - sl_price)
            if sl_dist > 0:
                tp_offset = max(sl_dist * 1.5, min_tp_pts)
                tp_price = tick.ask + tp_offset
                swept_tag = "+".join(pa_res.get("swept_pools", [])) or "PA"
                trigger_type = f"Pure PA 3-TF ({swept_tag})"
                rr_calc = round(tp_offset / sl_dist, 2)
                h4_tr = pa_res.get("h4_trend", h4_trend)
                h4_loc = float(pa_res.get("h4_swing_loc_pct", range_info["location_pct"]))
                logger.info(
                    f"[InstitutionalEngine] BUY Triggered! {symbol} | H4: {h4_tr} (Leg {h4_loc:.1f}%) | "
                    f"Trigger: {trigger_type} | Entry: {tick.ask:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f} (R:R {rr_calc:.2f})"
                )
                return {
                    "valid": True,
                    "direction": "BUY",
                    "reason": pa_res.get("reason", f"H4 {h4_tr} + {trigger_type}"),
                    "entry_price": tick.ask,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "deal_range": range_info,
                    "whale_detected": has_whale_vol,
                    "vol_ratio": max_vol_ratio,
                    "sweep_level": sweep_ref,
                    "fvg_detected": has_buy_fvg,
                    "ut_bot": f"PA:{swept_tag}",
                    "approach_type": "CLEAN_FLOW",
                    "rr_ratio": rr_calc
                }

        # ---------------- SELL SETUP EVALUATION ----------------
        has_sell_trigger = macro_sell_allowed
        if macro_sell_allowed and has_sell_trigger and whale_gate_ok:
            sweep_ref = float(pa_res["sweep_extreme"]) if pa_res["sweep_extreme"] > 0 else (curr_price + sl_buffer)
            sl_price = sweep_ref + sl_buffer
            sl_dist = abs(sl_price - tick.bid)
            if sl_dist > 0:
                tp_offset = max(sl_dist * 1.5, min_tp_pts)
                tp_price = tick.bid - tp_offset
                swept_tag = "+".join(pa_res.get("swept_pools", [])) or "PA"
                trigger_type = f"Pure PA 3-TF ({swept_tag})"
                rr_calc = round(tp_offset / sl_dist, 2)
                h4_tr = pa_res.get("h4_trend", h4_trend)
                h4_loc = float(pa_res.get("h4_swing_loc_pct", range_info["location_pct"]))
                logger.info(
                    f"[InstitutionalEngine] SELL Triggered! {symbol} | H4: {h4_tr} (Leg {h4_loc:.1f}%) | "
                    f"Trigger: {trigger_type} | Entry: {tick.bid:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f} (R:R {rr_calc:.2f})"
                )
                return {
                    "valid": True,
                    "direction": "SELL",
                    "reason": pa_res.get("reason", f"H4 {h4_tr} + {trigger_type}"),
                    "entry_price": tick.bid,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "deal_range": range_info,
                    "whale_detected": has_whale_vol,
                    "vol_ratio": max_vol_ratio,
                    "sweep_level": sweep_ref,
                    "fvg_detected": has_sell_fvg,
                    "ut_bot": f"PA:{swept_tag}",
                    "approach_type": "CLEAN_FLOW",
                    "rr_ratio": rr_calc
                }

        skip_reason = (
            f"Waiting for Pure PA 3-TF alignment: {pa_res.get('reason', 'NONE')} "
            f"(H4: {pa_res.get('h4_trend', h4_trend)}, Leg: {pa_res.get('h4_swing_loc_pct', loc_pct):.1f}%)."
        )

        return {
            "valid": False,
            "direction": "NONE",
            "reason": skip_reason,
            "deal_range": range_info,
            "whale_detected": has_whale_vol,
            "vol_ratio": max_vol_ratio
        }
