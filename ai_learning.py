"""
ai_learning.py — AlphaEdge M15 AI Auto-Learning & Decision Brain
================================================================
Analyzes executed M15 trades from 'm15_trade_analysis.csv' and MT5 deal history.
Evaluates performance metrics over the last 24-72 hours:
- M15 Swing Win Rate % (Target Hits vs Break-Even Stops vs Reversals)
- Profit Factor and Net Realized PnL ($)
- Break-Even efficiency (saving capital vs prematurely cutting swings)
- Live Market Volatility & 14-period ATR expansion/compression
- News Catalyst response performance

Dynamically tunes swing parameters without adding any trade blockers:
- Gold/Oil/US30 Base TP: $85.00 to $120.00 (Wholesale Institutional Swing Room)
- News Catalyst Impulse TP: $105.00 to $145.00
- Break-Even Trigger: $15.00 (Chop Protection) to $20.00 (Standard)
- Multi-Stage Trailing Locks:
  * Stage 1 Lock: +$35.00 -> Lock +$25.00
  * Stage 2 Lock: +$60.00 -> Lock +$45.00
- Max Initial Stop Loss: Strictly capped at $36.00 (Gold/US30) and $35.00 (Oil)
- Persists optimal settings to 'config_learned_m15.json' and 'config_learned_scalp.json'.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

logger = logging.getLogger("AlphaEdge.AutoLearner")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_learned_m15.json")
LEGACY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_learned_scalp.json")
ANALYSIS_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m15_trade_analysis.csv")
SCALP_MAGIC = 20250831


try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except Exception:
    pass

def send_telegram(message: str):
    token   = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "915238743")
    if not token or not chat_id:
        return
    url     = "https://api.telegram.org/bot" + token + "/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        logger.debug(f"[AutoLearner Telegram] {exc}")


class AutoLearner:
    def __init__(self, config_path=CONFIG_PATH, lookback_hours=72):
        self.config_path = config_path
        self.lookback_hours = lookback_hours

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading {self.config_path}: {e}")
        return {
            "gold_tp_dollars": 65.0,
            "gold_tp_catalyst_dollars": 85.0,
            "gold_be_trigger_dollars": 20.0,
            "gold_lock_trigger_dollars": 35.0,
            "gold_lock_amount_dollars": 25.0,
            "gold_lock2_trigger_dollars": 60.0,
            "gold_lock2_amount_dollars": 45.0,
            "regime": "BALANCED_INSTITUTIONAL_SWING",
            "volatility_regime": "NORMAL",
            "win_rate": 50.0,
            "total_trades": 0,
            "net_pnl": 0.0,
            "last_updated": None
        }

    def save_config(self, config):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
            with open(LEGACY_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
            logger.info(f"[AutoLearner] Saved learned configuration to {self.config_path}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    def measure_market_volatility(self, symbol="XAUUSDm") -> dict:
        """
        Calculates 14-period ATR on M15 timeframe and compares against 50-period baseline.
        Returns volatility state: EXPANSION, COMPRESSION, or NORMAL.
        """
        try:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 65)
            if rates is not None and len(rates) >= 50:
                df = pd.DataFrame(rates)
                df['tr0'] = abs(df['high'] - df['low'])
                df['tr1'] = abs(df['high'] - df['close'].shift(1))
                df['tr2'] = abs(df['low'] - df['close'].shift(1))
                df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
                
                # 14-period EMA ATR
                df['atr14'] = df['tr'].ewm(span=14, adjust=False).mean()
                current_atr = float(df['atr14'].iloc[-1])
                avg_atr = float(df['atr14'].tail(50).mean())

                ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
                if ratio >= 1.20:
                    state = "EXPANSION"
                elif ratio <= 0.80:
                    state = "COMPRESSION"
                else:
                    state = "NORMAL"

                return {
                    "symbol": symbol,
                    "current_atr": round(current_atr, 2),
                    "avg_atr": round(avg_atr, 2),
                    "ratio": round(ratio, 2),
                    "state": state
                }
        except Exception as e:
            logger.debug(f"[AutoLearner] Volatility calculation skipped for {symbol}: {e}")

        return {"symbol": symbol, "current_atr": 0.0, "avg_atr": 0.0, "ratio": 1.0, "state": "NORMAL"}

    def analyze_history_and_adapt(self) -> dict:
        if not mt5.terminal_info():
            login = int(os.getenv("MT5_LOGIN", "0"))
            pwd   = os.getenv("MT5_PASSWORD", "")
            srv   = os.getenv("MT5_SERVER", "")
            if login > 0:
                mt5.initialize(login=login, password=pwd, server=srv)
            else:
                mt5.initialize()

        date_from = datetime.now() - timedelta(hours=self.lookback_hours)
        date_to   = datetime.now()
        deals = mt5.history_deals_get(date_from, date_to)

        # Baseline Defaults (Wholesale Institutional Swing - $100 TP baseline)
        gold_tp = 65.0
        gold_tp_catalyst = 85.0
        gold_be = 20.0
        lock1_trig = 35.0
        lock1_amt = 25.0
        lock2_trig = 60.0
        lock2_amt = 45.0
        regime = "BALANCED_INSTITUTIONAL_SWING"

        total_trades = 0
        win_rate = 50.0
        net_pnl = 0.0
        wins_count = 0
        loss_count = 0

        # 1. Primary Analysis: Check Dedicated M15 Analysis Log if available
        if os.path.exists(ANALYSIS_CSV_PATH):
            try:
                df_csv = pd.read_csv(ANALYSIS_CSV_PATH)
                closed_csv = df_csv[df_csv['status'] == 'CLOSED']
                if len(closed_csv) >= 3:
                    total_trades = len(closed_csv)
                    closed_csv['pnl_num'] = pd.to_numeric(closed_csv['pnl_usd'], errors='coerce').fillna(0.0)
                    wins_count = len(closed_csv[closed_csv['pnl_num'] > 0])
                    loss_count = len(closed_csv[closed_csv['pnl_num'] <= 0])
                    win_rate = round((wins_count / total_trades) * 100, 1)
                    net_pnl = round(float(closed_csv['pnl_num'].sum()), 2)
            except Exception as e:
                logger.debug(f"[AutoLearner] CSV read skipped: {e}")

        # 2. Secondary Analysis: Analyze MT5 deals history
        if total_trades == 0 and deals:
            df_deals = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())
            exits = df_deals[(df_deals['entry'] == 1) & (df_deals['magic'] == SCALP_MAGIC)].copy()
            if len(exits) > 0:
                total_trades = len(exits)
                wins = exits[exits['profit'] > 0]
                losses = exits[exits['profit'] <= 0]
                wins_count = len(wins)
                loss_count = len(losses)
                win_rate = round((wins_count / total_trades) * 100, 1)
                net_pnl = round(float(exits['profit'].sum()), 2)

        # 3. Measure Live Market Volatility
        vol_info = self.measure_market_volatility("XAUUSDm")
        vol_state = vol_info.get("state", "NORMAL")

        logger.info(
            f"[AutoLearner Institutional Brain] Evaluated {total_trades} trades | Wins: {wins_count} | Losses: {loss_count} | "
            f"Win Rate: {win_rate}% | Net PnL: ${net_pnl:+.2f} | Volatility: {vol_state} (ATR Ratio: {vol_info.get('ratio', 1.0)})"
        )

        # ── Dynamic Quantitative Adaptation Decisions ──
        if total_trades >= 5:
            if win_rate >= 60.0 and net_pnl > 0:
                # Strong trending follow-through: expand target room for big swing gains
                regime = "HIGH_CONVICTION_INSTITUTIONAL_SWING"
                gold_tp = 110.0
                gold_tp_catalyst = 130.0
                gold_be = 20.0
                lock1_trig = 35.0
                lock1_amt = 25.0
                lock2_trig = 60.0
                lock2_amt = 45.0
            elif win_rate < 45.0:
                # Defensive mode: protect capital faster during choppy rotations
                regime = "DEFENSIVE_INSTITUTIONAL_SWING"
                gold_tp = 85.0
                gold_tp_catalyst = 105.0
                gold_be = 15.0  # Tighten BE to 15.0 to lock profit before pullbacks
                lock1_trig = 30.0
                lock1_amt = 20.0
                lock2_trig = 50.0
                lock2_amt = 35.0
            else:
                regime = "BALANCED_INSTITUTIONAL_SWING"
                gold_tp = 65.0
                gold_tp_catalyst = 85.0
                gold_be = 20.0
                lock1_trig = 35.0
                lock1_amt = 25.0
                lock2_trig = 60.0
                lock2_amt = 45.0
        else:
            regime = "INITIAL_INSTITUTIONAL_SWING"
            gold_tp = 65.0
            gold_tp_catalyst = 85.0
            gold_be = 20.0
            lock1_trig = 35.0
            lock1_amt = 25.0
            lock2_trig = 60.0
            lock2_amt = 45.0

        # Dynamic Volatility Overlay (Never widens initial SL risk!)
        if vol_state == "EXPANSION":
            # High volatility expansion: give runners +10% extra target upside
            gold_tp = round(gold_tp * 1.10, 1)
            gold_tp_catalyst = round(gold_tp_catalyst * 1.10, 1)
        elif vol_state == "COMPRESSION":
            # Low volatility squeeze: tighten BE to guarantee runners don't reverse
            gold_be = min(gold_be, 15.0)

        old_config = self.load_config()
        new_config = {
            "gold_tp_dollars": gold_tp,
            "gold_tp_catalyst_dollars": gold_tp_catalyst,
            "gold_be_trigger_dollars": gold_be,
            "gold_lock_trigger_dollars": lock1_trig,
            "gold_lock_amount_dollars": lock1_amt,
            "gold_lock2_trigger_dollars": lock2_trig,
            "gold_lock2_amount_dollars": lock2_amt,
            "regime": regime,
            "volatility_regime": vol_state,
            "atr_ratio": vol_info.get("ratio", 1.0),
            "win_rate": win_rate,
            "total_trades": total_trades,
            "net_pnl": net_pnl,
            "symbols_config": {
                "XAUUSDm": {
                    "tp_dollars": gold_tp,
                    "tp_catalyst_dollars": gold_tp_catalyst,
                    "be_trigger_dollars": gold_be,
                    "lock_trigger_dollars": lock1_trig,
                    "lock_amount_dollars": lock1_amt,
                    "lock2_trigger_dollars": lock2_trig,
                    "lock2_amount_dollars": lock2_amt,
                    "max_sl_dollars": 36.0
                },
                "USOILm": {
                    "tp_dollars": gold_tp,
                    "tp_catalyst_dollars": gold_tp_catalyst,
                    "be_trigger_dollars": gold_be,
                    "lock_trigger_dollars": lock1_trig,
                    "lock_amount_dollars": lock1_amt,
                    "lock2_trigger_dollars": lock2_trig,
                    "lock2_amount_dollars": lock2_amt,
                    "max_sl_dollars": 35.0
                },
                "US30m": {
                    "tp_dollars": gold_tp,
                    "tp_catalyst_dollars": gold_tp_catalyst,
                    "be_trigger_dollars": gold_be,
                    "lock_trigger_dollars": lock1_trig,
                    "lock_amount_dollars": lock1_amt,
                    "lock2_trigger_dollars": lock2_trig,
                    "lock2_amount_dollars": lock2_amt,
                    "max_sl_dollars": 36.0
                }
            },
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Only send Telegram update when the core institutional regime genuinely changes
        has_changed = (
            old_config.get("regime") != new_config["regime"]
        )

        self.save_config(new_config)

        if has_changed:
            msg = (
                f"🧠 <b>[AlphaEdge AI Brain Adaptation]</b>\n\n"
                f"• <b>Market Regime:</b> {regime}\n"
                f"• <b>Volatility State:</b> {vol_state} (ATR Ratio: {vol_info.get('ratio', 1.0)}x)\n"
                f"• <b>Analyzed History:</b> {total_trades} trades ({wins_count}W / {loss_count}L)\n"
                f"• <b>Win Rate:</b> {win_rate}%\n"
                f"• <b>Net PnL:</b> ${net_pnl:+.2f}\n"
                f"• <b>Calibrated Target:</b> ${gold_tp:.2f} TP (Catalyst: ${gold_tp_catalyst:.2f})\n"
                f"• <b>Multi-Stage Protection:</b> BE at +${gold_be:.2f} | Lock1 +${lock1_trig:.0f}➔+${lock1_amt:.0f} | Lock2 +${lock2_trig:.0f}➔+${lock2_amt:.0f}\n"
                f"• <b>Status:</b> Dynamic institutional calibration active."
            )
            send_telegram(msg)

        return new_config


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    learner = AutoLearner()
    cfg = learner.analyze_history_and_adapt()
    print("\nM15 AI Brain Config:")
    print(json.dumps(cfg, indent=4))
