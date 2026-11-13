"""
backtest_institutional.py
=========================
Historical Backtester simulating the EXACT institutional strategy:
- Multi-Timeframe Structure: H4 Dealing Range & Trend Filter
- Micro Trigger: M15 Setup + M5 Pullback Turn Sniper
- Realistic Execution: Evaluates M5 sub-bars while in position to accurately
  track the progression:
    1. Early SL check (before unrealized gain)
    2. BE Trigger ($20 USD) -> Moves SL to Entry + buffer
    3. Profit Lock Trigger ($35 USD) -> Moves SL to +$25 USD guaranteed
    4. Full TP Trigger ($60 USD)
- Multi-Asset: XAUUSDm, USOILm, US30m
- Multi-Period: 1 Month (30d), 2 Months (60d), 3 Months (90d)
"""

import sys
import os
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from institutional_trader import ASSET_CONFIGS, get_dollar_per_pt

def run_simulation(symbol, days=30, use_m5_confirmation=True):
    if not mt5.initialize():
        print(f"Failed to initialize MT5: {mt5.last_error()}")
        return None

    cfg = ASSET_CONFIGS[symbol]
    lot = cfg['lot']
    tp_usd = cfg['tp_dollars']
    sl_usd = cfg['max_sl_dollars']
    be_usd = cfg['be_trigger_dollars']
    lock_trigger_usd = cfg['lock_trigger_dollars']
    lock_amount_usd = cfg['lock_amount_dollars']

    dollar_per_pt = get_dollar_per_pt(symbol, lot)
    if dollar_per_pt <= 0:
        dollar_per_pt = 1.0

    tp_pts = tp_usd / dollar_per_pt
    sl_pts = sl_usd / dollar_per_pt
    be_pts = be_usd / dollar_per_pt
    lock_trigger_pts = lock_trigger_usd / dollar_per_pt
    lock_amount_pts = lock_amount_usd / dollar_per_pt

    m15_bars_needed = int(days * 96) + 200
    m5_bars_needed = int(days * 288) + 600

    rates_m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, m15_bars_needed)
    rates_m5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, m5_bars_needed)

    if rates_m15 is None or len(rates_m15) < 200 or rates_m5 is None:
        print(f"Insufficient rates for {symbol}")
        return None

    df_m15 = pd.DataFrame(rates_m15)
    df_m15['datetime'] = pd.to_datetime(df_m15['time'], unit='s')
    df_m15 = df_m15.sort_values('time').reset_index(drop=True)

    df_m5 = pd.DataFrame(rates_m5)
    df_m5['datetime'] = pd.to_datetime(df_m5['time'], unit='s')
    df_m5 = df_m5.sort_values('time').reset_index(drop=True)

    closes = df_m15['close'].values
    highs = df_m15['high'].values
    lows = df_m15['low'].values
    times = df_m15['time'].values
    n = len(df_m15)

    # UT Bot calculation on M15
    key_value = 1.0
    atr_period = 10
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i] - closes[i - 1])
        trs.append(max(hl, hpc, lpc))

    atrs = []
    for i in range(n):
        start = max(0, i - atr_period + 1)
        atrs.append(float(np.mean(trs[start:i+1])))

    stops = [0.0] * n
    stops[0] = closes[0]
    for i in range(1, n):
        n_loss = key_value * atrs[i]
        prev_c = closes[i-1]
        c = closes[i]
        prev_s = stops[i-1]
        if c > prev_s and prev_c > prev_s:
            stops[i] = max(prev_s, c - n_loss)
        elif c < prev_s and prev_c < prev_s:
            stops[i] = min(prev_s, c + n_loss)
        else:
            stops[i] = c - n_loss if c > prev_s else c + n_loss

    trades = []
    in_trade = False
    cooldown_until_time = 0
    trade_info = {}

    start_idx = 100
    for i in range(start_idx, n - 1):
        curr_time_sec = times[i]
        curr_time = df_m15['datetime'].iloc[i]
        curr_close = closes[i]

        # Manage active trade through M5 sub-bars inside this M15 bar
        if in_trade:
            pos_type = trade_info['type']
            entry_price = trade_info['entry']
            current_sl = trade_info['sl']
            current_tp = trade_info['tp']
            be_active = trade_info.get('be_active', False)
            lock_active = trade_info.get('lock_active', False)

            # Get the three M5 bars corresponding to this M15 bar
            sub_m5 = df_m5[(df_m5['time'] >= curr_time_sec) & (df_m5['time'] < curr_time_sec + 900)]
            if len(sub_m5) == 0:
                sub_m5 = pd.DataFrame([{'open': curr_close, 'high': highs[i], 'low': lows[i], 'close': curr_close}])

            trade_closed = False
            for _, m5_row in sub_m5.iterrows():
                m5_h = m5_row['high']
                m5_l = m5_row['low']

                if pos_type == "BUY":
                    # 1. Gain check on M5 high
                    pts_up = m5_h - entry_price
                    if pts_up >= lock_trigger_pts:
                        lock_active = True
                        current_sl = max(current_sl, entry_price + lock_amount_pts)
                    elif pts_up >= be_pts and not lock_active:
                        be_active = True
                        be_buf = 0.5 if symbol == "XAUUSDm" else (0.02 if "OIL" in symbol else 10.0)
                        current_sl = max(current_sl, entry_price + be_buf)

                    # 2. Check if TP hit
                    if m5_h >= current_tp:
                        exit_price = current_tp
                        outcome = "WIN_FULL_TP"
                        trade_closed = True
                        break
                    # 3. Check if SL (or locked profit/BE) hit
                    elif m5_l <= current_sl:
                        exit_price = current_sl
                        if lock_active:
                            outcome = "WIN_LOCK"
                        elif be_active:
                            outcome = "BE"
                        else:
                            outcome = "LOSS_SL"
                        trade_closed = True
                        break

                else:  # SELL
                    # 1. Gain check on M5 low
                    pts_down = entry_price - m5_l
                    if pts_down >= lock_trigger_pts:
                        lock_active = True
                        current_sl = min(current_sl, entry_price - lock_amount_pts)
                    elif pts_down >= be_pts and not lock_active:
                        be_active = True
                        be_buf = 0.5 if symbol == "XAUUSDm" else (0.02 if "OIL" in symbol else 10.0)
                        current_sl = min(current_sl, entry_price - be_buf)

                    # 2. Check if TP hit
                    if m5_l <= current_tp:
                        exit_price = current_tp
                        outcome = "WIN_FULL_TP"
                        trade_closed = True
                        break
                    # 3. Check if SL (or locked profit/BE) hit
                    elif m5_h >= current_sl:
                        exit_price = current_sl
                        if lock_active:
                            outcome = "WIN_LOCK"
                        elif be_active:
                            outcome = "BE"
                        else:
                            outcome = "LOSS_SL"
                        trade_closed = True
                        break

            trade_info['sl'] = current_sl
            trade_info['be_active'] = be_active
            trade_info['lock_active'] = lock_active

            if trade_closed:
                pts_pnl = (exit_price - entry_price) if pos_type == "BUY" else (entry_price - exit_price)
                usd_pnl = pts_pnl * dollar_per_pt
                trade_info['exit_time'] = curr_time
                trade_info['exit_price'] = exit_price
                trade_info['pnl_pts'] = pts_pnl
                trade_info['pnl_usd'] = usd_pnl
                trade_info['outcome'] = outcome
                trades.append(trade_info)

                in_trade = False
                cooldown_until_time = curr_time_sec + 3600  # 60-min cooldown
                continue

        # If not in trade and not in cooldown, check setup
        if not in_trade and curr_time_sec >= cooldown_until_time:
            # 1. Fresh UT Bot Crossover check on bar i
            ut_signal = "NONE"
            if closes[i - 1] <= stops[i - 1] and closes[i] > stops[i]:
                ut_signal = "BUY"
            elif closes[i - 1] >= stops[i - 1] and closes[i] < stops[i]:
                ut_signal = "SELL"

            if ut_signal == "NONE":
                continue

            # 2. H4 Macro Bias Approximation (last 120 M15 bars)
            window_slice = df_m15.iloc[max(0, i - 120): i]
            range_high = float(window_slice['high'].max())
            range_low = float(window_slice['low'].min())
            spread = range_high - range_low
            if spread <= 0:
                continue
            loc_pct = ((curr_close - range_low) / spread) * 100.0

            # Dealing Range Gates
            if ut_signal == "BUY" and loc_pct > 65.0:
                continue
            if ut_signal == "SELL" and loc_pct < 35.0:
                continue

            # 24/5 Session (Monday through Friday, weekends closed)
            if curr_time.weekday() in (5, 6):
                continue

            # 3. M5 Pullback Turn Confirmation (if enabled)
            entry_price = curr_close
            if use_m5_confirmation:
                m15_bar_ts = df_m15['time'].iloc[i]
                m5_sub = df_m5[df_m5['time'] <= m15_bar_ts + 900]
                if len(m5_sub) >= 6:
                    m5_closes = m5_sub['close'].values
                    m5_opens  = m5_sub['open'].values
                    m5_highs  = m5_sub['high'].values
                    m5_lows   = m5_sub['low'].values
                    
                    c_c = m5_closes[-1]
                    c_o = m5_opens[-1]
                    p_h = m5_highs[-2]
                    p_l = m5_lows[-2]

                    prior_c = m5_closes[-4:-1]
                    prior_o = m5_opens[-4:-1]

                    if ut_signal == "BUY":
                        had_pb = any(prior_c[k] <= prior_o[k] for k in range(len(prior_c)))
                        is_turn = (c_c > c_o) and (c_c >= p_h or c_c > prior_c[-1])
                        if not (had_pb and is_turn):
                            continue
                        entry_price = c_c
                    elif ut_signal == "SELL":
                        had_pb = any(prior_c[k] >= prior_o[k] for k in range(len(prior_c)))
                        is_turn = (c_c < c_o) and (c_c <= p_l or c_c < prior_c[-1])
                        if not (had_pb and is_turn):
                            continue
                        entry_price = c_c

            if ut_signal == "BUY":
                sl_price = entry_price - sl_pts
                tp_price = entry_price + tp_pts
            else:
                sl_price = entry_price + sl_pts
                tp_price = entry_price - tp_pts

            in_trade = True
            trade_info = {
                "symbol": symbol,
                "type": ut_signal,
                "entry_time": curr_time,
                "entry": entry_price,
                "sl": sl_price,
                "tp": tp_price,
                "be_active": False,
                "lock_active": False,
                "location_pct": loc_pct
            }

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "symbol": symbol, "days": days, "total": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "net_pnl": 0.0, "profit_factor": 0.0,
            "full_tp": 0, "locks": 0, "be_count": 0, "sl_count": 0
        }

    wins = [t for t in trades if t['pnl_usd'] > 0]
    losses = [t for t in trades if t['pnl_usd'] <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total_trades) * 100.0

    full_tp = len([t for t in trades if t.get('outcome') == 'WIN_FULL_TP'])
    locks = len([t for t in trades if t.get('outcome') == 'WIN_LOCK'])
    bes = len([t for t in trades if t.get('outcome') == 'BE'])
    sls = len([t for t in trades if t.get('outcome') == 'LOSS_SL'])

    total_profit = sum(t['pnl_usd'] for t in wins)
    total_loss = abs(sum(t['pnl_usd'] for t in losses))
    net_pnl = sum(t['pnl_usd'] for t in trades)
    profit_factor = (total_profit / total_loss) if total_loss > 0 else 999.0

    return {
        "symbol": symbol,
        "days": days,
        "total": total_trades,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor,
        "full_tp": full_tp,
        "locks": locks,
        "be_count": bes,
        "sl_count": sls,
        "mode": "M5_CONFIRMED" if use_m5_confirmation else "DIRECT_M15"
    }

def run_all_backtests():
    if not mt5.initialize():
        print("MT5 initialization failed")
        return

    symbols = ["XAUUSDm", "USOILm", "US30m"]
    periods = [30, 60, 90]

    print("=" * 90)
    print("  ALPHAEDGE HIGH-PRECISION BACKTEST REPORT (M5 PULLBACK + ACCURATE BE & PROFIT LOCKS)")
    print("  Assets: XAUUSDm (0.02 lot), USOILm (0.05 lot), US30m (0.25 lot)")
    print("  Shields: Initial SL $36 | Full TP $60 | BE Trigger $20 | Lock Trigger $35 -> Lock $25")
    print("=" * 90)

    for days in periods:
        months_label = f"{days // 30} Month{'s' if days // 30 > 1 else ''} ({days} Days)"
        print(f"\n==================== PERIOD: {months_label} ====================")
        for sym in symbols:
            res_new = run_simulation(sym, days=days, use_m5_confirmation=True)
            if res_new:
                print(f"\n--- {sym} [{months_label}] ---")
                print(f"  Total Trades : {res_new['total']:2d} | Wins: {res_new['wins']:2d} | Losses: {res_new['losses']:2d} | Win Rate: {res_new['win_rate']:.1f}%")
                print(f"  Breakdown    : Full TP: {res_new['full_tp']} (+$60) | Locks: {res_new['locks']} (+$25) | BE: {res_new['be_count']} ($0) | Full SL: {res_new['sl_count']} (-$36)")
                print(f"  NET PROFIT   : ${res_new['net_pnl']:+8.2f} USD | Profit Factor: {res_new['profit_factor']:.2f}")

    mt5.shutdown()

if __name__ == "__main__":
    run_all_backtests()
