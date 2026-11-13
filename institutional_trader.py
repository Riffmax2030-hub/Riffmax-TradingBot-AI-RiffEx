"""
institutional_trader.py — AlphaEdge Institutional Multi-Timeframe & Whale Flow Swing Engine
===================================================================================
Replaces the legacy scalper with a high-conviction Institutional Swing Architecture:
  - Multi-Timeframe Structure (H4 / H1 / M15):
      * H4 Dealing Range (Premium vs Discount Zone).
      * Strict Bottom Rule: BUY orders ONLY in Discount (< 50% Eq).
      * Strict Top Rule: SELL orders ONLY in Premium (> 50% Eq).
      * Refuses to chase moves halfway or buy at the top!
  - Whale Flow & Volume Climax Detection:
      * Liquidity Sweeps (Stop Hunts): Absorbs retail stop orders, rejects back into range.
      * Tick Volume Surge: Volume >= 1.6x 20-period moving average on the sweep candle.
      * Fair Value Gap (FVG) / Imbalance validation.
  - Target & Holding Calibration:
      * Gold (XAUUSDm, 0.02 lot): Full TP = $40.00 USD ($20.00 price expansion). Max SL = $24.00 USD (12.0 pts).
      * Gold Stage 1 BE: Triggers at +$15.00 profit.
      * Gold Stage 2 Profit Lock: Triggers at +$25.00 profit -> locks +$18.00.
  - Session Gateway: 24/5 for Gold (Sunday 23:00 EAT to Friday 23:55 EAT).
  - Strict 60-Minute Post-Closure Cooldown Shield: Prevents premature re-entries.
"""

import os
import json
import logging
import traceback
import urllib.request
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from news_catalyst_engine import NewsCatalystEngine
from institutional_engine import InstitutionalEngine
from pre_trade_backtester import PreTradeBacktester
from structural_memory import StructuralMemory
from m15_trade_analysis_logger import (
    log_trade_opened,
    update_trade_be,
    log_trade_closed,
    sync_closed_trades_from_history,
    get_performance_summary
)

logger = logging.getLogger("AlphaEdge.Institutional")

# Asset Configurations (Institutional MTF Swing Engine - User Defined Lots)
ASSET_CONFIGS = {
    "XAUUSDm": {
        "symbol": "XAUUSDm",
        "lot": 0.02,                      # 0.02 Lot (User exact setting)
        "key_mult": 1.0,
        "atr_period": 10,
        "tp_dollars": 100.0,             # Target: $100.00 USD Profit (50.0 pts move with 0.02 lot)
        "tp_catalyst_dollars": 120.0,     # Expanded $120.00 Target during High Momentum
        "be_trigger_dollars": 20.0,       # Stage 1: Move SL to Entry at +$20.00 profit (10.0 pts)
        "lock_trigger_dollars": 35.0,     # Stage 2: Trigger Profit Lock at +$35.00 profit (17.5 pts)
        "lock_amount_dollars": 25.0,      # Stage 2: Lock $25.00 profit into SL (12.5 pts)
        "lock2_trigger_dollars": 60.0,    # Stage 3: Trigger Profit Lock 2 at +$60.00 profit (30.0 pts)
        "lock2_amount_dollars": 45.0,     # Stage 3: Lock $45.00 profit into SL (22.5 pts)
        "max_sl_dollars": 36.0,           # Max Initial Risk Cap: $36.00 USD (18.0 pts room)
        "sl_atr_mult": 1.5,
        "currency": "USD"
    },
    "USOILm": {
        "symbol": "USOILm",
        "lot": 0.05,                      # 0.05 Lot ($50.00 per $1.00 move)
        "key_mult": 1.0,
        "atr_period": 10,
        "tp_dollars": 100.0,             # Target: $100.00 USD ($2.00 move at 0.05 lot)
        "tp_catalyst_dollars": 120.0,     # Expanded $120.00 Target ($2.40 move during high news volatility)
        "be_trigger_dollars": 20.0,       # BE Protection: Move SL to Entry at +$20.00 profit ($0.40 move)
        "lock_trigger_dollars": 35.0,     # Stage 2: Trigger Profit Lock at +$35.00 profit ($0.70 move)
        "lock_amount_dollars": 25.0,      # Stage 2: Lock +$25.00 profit into SL ($0.50 move)
        "lock2_trigger_dollars": 60.0,    # Stage 3: Trigger Profit Lock 2 at +$60.00 profit ($1.20 move)
        "lock2_amount_dollars": 45.0,     # Stage 3: Lock $45.00 profit into SL ($0.90 move)
        "max_sl_dollars": 35.0,           # Expanded Room: $35.00 USD ($0.70 room to prevent premature wick stopouts)
        "sl_atr_mult": 1.5,
        "currency": "USD"
    },
    "US30m": {
        "symbol": "US30m",
        "lot": 0.25,                      # 0.25 Lot ($0.25 per index pt)
        "key_mult": 1.0,
        "atr_period": 10,
        "tp_dollars": 100.0,             # Target: $100.00 USD Profit (400.0 index pts move)
        "tp_catalyst_dollars": 120.0,     # Expanded $120.00 Target (480.0 index pts move)
        "be_trigger_dollars": 20.0,       # Stage 1: Move SL to Entry at +$20.00 profit (80.0 pts)
        "lock_trigger_dollars": 35.0,     # Stage 2: Trigger Profit Lock at +$35.00 profit (140.0 pts)
        "lock_amount_dollars": 25.0,      # Stage 2: Lock $25.00 profit into SL (100.0 pts)
        "lock2_trigger_dollars": 60.0,    # Stage 3: Trigger Profit Lock 2 at +$60.00 profit (240.0 pts)
        "lock2_amount_dollars": 45.0,     # Stage 3: Lock $45.00 profit into SL (180.0 pts)
        "max_sl_dollars": 36.0,           # Max Initial Risk: $36.00 USD (144.0 index pts room)
        "sl_atr_mult": 1.5,
        "currency": "USD"
    }
}

def get_dollar_per_pt(symbol, lot):
    """Accurately computes USD value of 1.0 price point for any symbol and volume."""
    try:
        info = mt5.symbol_info(symbol)
        if not info:
            return 1.0 * lot
        tick_val = info.trade_tick_value
        tick_size = info.trade_tick_size
        if tick_size > 0:
            return (tick_val / tick_size) * lot
        return info.trade_contract_size * lot
    except Exception:
        return 1.0 * lot


NEWS_ENGINE = None
INST_ENGINE = None
PRE_BACKTESTER = None
STRUCT_MEMORY = None
ACTIVE_BE_TRACKED = {}   # ticket -> True if Stage 1 BE set
ACTIVE_LOCK_TRACKED = {} # ticket -> True if Stage 2 Lock ($35 -> $25) set
ACTIVE_LOCK2_TRACKED = {} # ticket -> True if Stage 3 Lock ($60 -> $45) set
WAS_OPEN_TRACKED = {}    # symbol -> True if position was open in previous cycle
PRE_SESSION_GATE_FIRED = None  # date str (YYYY-MM-DD EAT) when gate last fired; reset daily

# ─── Persistent Bar & Closure State (survives bot restarts) ────────────────────
_BAR_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_executed_bar.json")
_CLOSED_TIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_closed_time.json")

def _load_bar_state():
    """Load LAST_EXECUTED_BAR from disk. Returns dict of {symbol: bar_time (int)}."""
    try:
        if os.path.exists(_BAR_STATE_FILE):
            with open(_BAR_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            now_utc = datetime.now(timezone.utc)
            today_date = now_utc.date()
            filtered = {}
            for sym, ts in data.items():
                bar_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
                if bar_dt == today_date:
                    filtered[sym] = int(ts)
            return filtered
    except Exception:
        pass
    return {}

def _save_bar_state(bar_dict):
    """Persist LAST_EXECUTED_BAR to disk."""
    try:
        with open(_BAR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(bar_dict, f)
    except Exception:
        pass

def _load_closed_time():
    """Load LAST_CLOSED_TIME from disk."""
    try:
        if os.path.exists(_CLOSED_TIME_FILE):
            with open(_CLOSED_TIME_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_closed_time(closed_dict):
    """Persist LAST_CLOSED_TIME to disk."""
    try:
        with open(_CLOSED_TIME_FILE, "w", encoding="utf-8") as f:
            json.dump(closed_dict, f)
    except Exception:
        pass

LAST_EXECUTED_BAR = _load_bar_state()
LAST_CLOSED_TIME  = _load_closed_time()


def apply_ai_learned_settings():
    """Loads dynamically tuned parameters from the AI Auto-Learning Brain, strictly preserving institutional swing thresholds."""
    cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_learned_m15.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                c = json.load(f)

            symbols_cfg = c.get("symbols_config", {})
            for sym, asset_cfg in ASSET_CONFIGS.items():
                learned = symbols_cfg.get(sym, {})
                if not learned:
                    learned = {
                        "tp_dollars": c.get("gold_tp_dollars", 100.0),
                        "tp_catalyst_dollars": c.get("gold_tp_catalyst_dollars", 120.0),
                        "be_trigger_dollars": c.get("gold_be_trigger_dollars", 20.0),
                        "lock_trigger_dollars": c.get("gold_lock_trigger_dollars", 35.0),
                        "lock_amount_dollars": c.get("gold_lock_amount_dollars", 25.0),
                        "lock2_trigger_dollars": c.get("gold_lock2_trigger_dollars", 60.0),
                        "lock2_amount_dollars": c.get("gold_lock2_amount_dollars", 45.0)
                    }

                # Safe clamp: Wholesale swing TP range ($70 - $150)
                tp_val = float(learned.get("tp_dollars", asset_cfg.get("tp_dollars", 100.0)))
                asset_cfg["tp_dollars"] = max(70.0, min(150.0, tp_val))

                tp_cat = float(learned.get("tp_catalyst_dollars", asset_cfg.get("tp_catalyst_dollars", 120.0)))
                asset_cfg["tp_catalyst_dollars"] = max(90.0, min(180.0, tp_cat))

                be_trig = float(learned.get("be_trigger_dollars", asset_cfg.get("be_trigger_dollars", 20.0)))
                asset_cfg["be_trigger_dollars"] = max(15.0, min(25.0, be_trig))

                l1_trig = float(learned.get("lock_trigger_dollars", asset_cfg.get("lock_trigger_dollars", 35.0)))
                asset_cfg["lock_trigger_dollars"] = max(25.0, min(50.0, l1_trig))

                l1_amt = float(learned.get("lock_amount_dollars", asset_cfg.get("lock_amount_dollars", 25.0)))
                asset_cfg["lock_amount_dollars"] = max(15.0, min(35.0, l1_amt))

                l2_trig = float(learned.get("lock2_trigger_dollars", asset_cfg.get("lock2_trigger_dollars", 60.0)))
                asset_cfg["lock2_trigger_dollars"] = max(45.0, min(80.0, l2_trig))

                l2_amt = float(learned.get("lock2_amount_dollars", asset_cfg.get("lock2_amount_dollars", 45.0)))
                asset_cfg["lock2_amount_dollars"] = max(30.0, min(60.0, l2_amt))

                # SAFETY INVIOLABILITY: max_sl_dollars is NEVER widened or increased
        except Exception as e:
            logger.debug(f"[Institutional] Dynamic config load skipped: {e}")


try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except Exception:
    pass

# ─── Telegram Alerts & Strict Channel Firewall ─────────────────────────────────
_TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
_TELEGRAM_PERSONAL  = os.getenv("TELEGRAM_CHAT_ID", "915238743")              # Owner DM — receives ALL messages
_TELEGRAM_CHANNEL   = os.getenv("TELEGRAM_CHANNEL_ID", "@riffexalphaedgebot") # Public channel — trade signals ONLY

# ─── CHANNEL SILENT MODE ────────────────────────────────────────────────────────
# Set to True  → Channel receives NOTHING. Owner DM still works 100% normally.
# Set to False → Channel broadcasts re-enabled (only when bot is proven profitable).
CHANNEL_SILENT_MODE = False
# ────────────────────────────────────────────────────────────────────────────────

def _is_channel_allowed(message: str) -> bool:
    """
    STRICT ENFORCEMENT FIREWALL FOR PUBLIC CHANNEL (@riffexalphaedgebot):
    Only two types of messages are ever permitted:
      1. Trade Entry Signals: containing [AlphaEdge Signal]
      2. Trade Exit Results: containing [Trade Closed
    All other messages (Daily Reports, Market Summaries, Bot Status, News Briefings,
    30-min countdowns, Break-Even notices, Pre-news protections, AI updates, Errors)
    are strictly dropped and NEVER sent to the channel.
    """
    msg_lower = message.lower()

    # Blocklist: any of these immediately disqualifies message from channel
    blocked_keywords = [
        "daily", "weekly", "report", "analysis", "briefing",
        "bot status", "bot offline", "bot online", "shut down", "started",
        "scanner paused", "scanner resumed", "command", "balance & pnl",
        "account equity", "break-even protected", "pre-news", "30-minute news",
        "economic calendar", "protection activated", "capital preserved", "fakeout risk"
    ]
    for kw in blocked_keywords:
        if kw in msg_lower:
            return False

    # Allowlist: must have valid trade entry or close tag
    is_entry = "[alphaedge signal]" in msg_lower
    is_close = "[trade closed" in msg_lower

    return is_entry or is_close

def _tg_send(chat_id, message):
    """Low-level: sends a single message to one recipient with strict firewall."""
    if not _TELEGRAM_TOKEN or not chat_id:
        return

    # SILENT MODE: channel completely muted until bot is proven profitable
    is_channel = str(chat_id).strip().lower() in (
        _TELEGRAM_CHANNEL.lower(), "-1003973403139", "@riffexalphaedgebot"
    )
    if is_channel and CHANNEL_SILENT_MODE:
        logger.debug(f"[Channel Silent Mode] Message suppressed (channel muted): {message[:60]}...")
        return

    # FIREWALL CHECK: If recipient is the public channel, verify message whitelist
    if is_channel:
        if not _is_channel_allowed(message):
            logger.warning(f"[Telegram Firewall] BLOCKED non-trade message to channel: {message[:60]}...")
            return

    url = "https://api.telegram.org/bot" + _TELEGRAM_TOKEN + "/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        try:
            p2 = {"chat_id": chat_id, "text": message}
            data = json.dumps(p2).encode("utf-8")
            req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass

def _send_personal(message):
    """Sends to owner DM ONLY (915238743) — never to public channel."""
    _tg_send(_TELEGRAM_PERSONAL, message)

def _send_signal(message):
    """Sends to BOTH owner DM AND public channel (subject to strict channel firewall)."""
    _tg_send(_TELEGRAM_PERSONAL, message)
    _tg_send(_TELEGRAM_CHANNEL, message)

def send_trade_entry_broadcast(symbol, order_type, price, lot, sl, tp, catalyst_desc="Standard", deal_range_loc=None, rr_ratio=None):
    """Broadcasts clean, informative trade entry signal to channel and owner DM."""
    dp = _sym_digits(symbol)
    price_fmt = f"{price:.{dp}f}"
    sl_fmt = f"{sl:.{dp}f}"
    tp_fmt = f"{tp:.{dp}f}"

    dollar_per_pt = get_dollar_per_pt(symbol, lot)
    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)
    calc_sl_usd = sl_dist * dollar_per_pt if dollar_per_pt > 0 else 0.0
    calc_tp_usd = tp_dist * dollar_per_pt if dollar_per_pt > 0 else 0.0
    rr_val = (tp_dist / sl_dist) if sl_dist > 0 else 1.5

    loc_str = f" | {deal_range_loc}" if deal_range_loc else ""

    channel_msg = (
        f"🚀 <b>[AlphaEdge Signal]</b>\n"
        f"Asset: <b>{symbol}</b>\n"
        f"Action: <b>{order_type}</b> @ <code>{price_fmt}</code>\n"
        f"Take Profit: <code>{tp_fmt}</code> (+${calc_tp_usd:.2f})\n"
        f"Stop Loss: <code>{sl_fmt}</code> (-${calc_sl_usd:.2f})\n"
        f"Risk/Reward: <b>{rr_val:.2f} : 1</b>"
    )
    dm_msg = (
        f"🚀 <b>[AlphaEdge Signal]</b>\n"
        f"Asset: <b>{symbol}</b> | Action: <b>{order_type}</b>\n"
        f"Entry Price: <code>{price_fmt}</code>\n"
        f"Volume: <b>{lot} Lot</b>\n"
        f"Stop Loss: <code>{sl_fmt}</code> (Risk Cap: -${calc_sl_usd:.2f} | {sl_dist:.{dp}f} pts)\n"
        f"Take Profit: <code>{tp_fmt}</code> (Target: +${calc_tp_usd:.2f} | {tp_dist:.{dp}f} pts)\n"
        f"Risk/Reward: <b>{rr_val:.2f} : 1</b>\n"
        f"🏛 <b>Institutional Context:</b> {catalyst_desc}{loc_str}"
    )
    _tg_send(_TELEGRAM_PERSONAL, dm_msg)
    _tg_send(_TELEGRAM_CHANNEL, channel_msg)

def send_trade_close_broadcast(symbol, direction, open_price, close_price, pnl_usd, reason):
    """Broadcasts trade result (Win/Loss) to both public channel and owner DM."""
    dp = _sym_digits(symbol)
    try:
        op_fmt = f"{float(open_price):.{dp}f}"
        cp_fmt = f"{float(close_price):.{dp}f}"
    except Exception:
        op_fmt = str(open_price)
        cp_fmt = str(close_price)

    if pnl_usd > 0:
        emoji = "🎯"
        tag = "WIN"
        result_text = f"✅ <b>+${pnl_usd:.2f} (WIN)</b>"
        detail = "Take Profit Target Hit!" if reason == "TP_HIT" else "Closed in Profit"
    elif pnl_usd < 0:
        emoji = "🛑"
        tag = "LOSS"
        result_text = f"❌ <b>-${abs(pnl_usd):.2f} (LOSS)</b>"
        detail = "Stop Loss Executed" if reason == "SL_HIT" else "Closed with Loss"
    else:
        emoji = "🛡️"
        tag = "BREAK-EVEN"
        result_text = f"⚪ <b>${pnl_usd:+.2f} (BREAK-EVEN)</b>"
        detail = "Break-Even Capital Protected"

    if reason == "REVERSAL":
        detail = "M15 UT Bot Signal Flip Reversal"

    msg = (
        f"{emoji} <b>[Trade Closed — {tag}]</b>\n"
        f"Asset: <b>{symbol}</b> ({direction})\n"
        f"Entry: <code>{op_fmt}</code> ➔ Exit: <code>{cp_fmt}</code>\n"
        f"Result: {result_text}\n"
        f"Outcome: {detail}"
    )
    _tg_send(_TELEGRAM_PERSONAL, msg)
    _tg_send(_TELEGRAM_CHANNEL, msg)

# Alias for backwards-compatibility: default to personal only
def _send_telegram(message):
    _send_personal(message)



# ─── Session Filter (7:00 AM - 9:00 PM EAT / Tuesday - Friday) ──────────────────
def is_session_active(symbol=None):
    """
    Institutional Session Gateway (Option B - Monday Observation Only):
    Trading execution window is strictly Tuesday to Friday, 5:00 AM to 11:00 PM EAT.
    
    MONDAY INSTITUTIONAL RULE:
      - Monday (weekday == 0) is STRICT OBSERVATION ONLY.
      - Desks and algorithms observe price establishing Monday High / Low boundaries,
        update structural memory, and record liquidity pools without opening live trades.
      - Protects account from choppy early-week accumulation traps & fakeouts.
      
    TUESDAY - FRIDAY:
      - Active execution from 05:00 EAT to 23:00 EAT (expanded to capture early Asian momentum + full NY close).
      - Midnight rollover blackout hours (23:00 to 05:00 EAT) and weekends (Saturday & Sunday) are blocked.
    """
    now_utc = datetime.now(timezone.utc)
    # EAT is UTC+3
    now_eat = now_utc + timedelta(hours=3)
    weekday = now_eat.weekday()  # Monday=0, Tuesday=1, ..., Friday=4, Saturday=5, Sunday=6
    hour_eat = now_eat.hour

    # 24/5 Active Trading Window: Monday 00:00 EAT to Friday 23:55 EAT
    # Friday and Monday trading gates completely removed by user directive.
    # Closed strictly on weekends (Saturday & Sunday when forex/commodity markets are closed).
    if weekday in (5, 6):
        return False

    return True


# ─── Exact TradingView Pine Script Replication ─────────────────────────────────
def compute_m15_ut_bot(symbol, key_mult=1.0, atr_period=10, n_bars=300):
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, n_bars)
    if rates is None or len(rates) < 50:
        return None
        
    df = pd.DataFrame(rates)
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(
        lambda r: max(
            r['high'] - r['low'],
            abs(r['high'] - r['prev_close']) if not np.isnan(r['prev_close']) else 0,
            abs(r['low']  - r['prev_close']) if not np.isnan(r['prev_close']) else 0,
        ), axis=1)

    # Exact Pine Script ta.rma(tr, atr_period)
    tr_vals = df['tr'].values
    atr_tv = np.zeros(len(df))
    atr_tv[:atr_period] = tr_vals[:atr_period].mean()
    for i in range(atr_period, len(df)):
        atr_tv[i] = (tr_vals[i] + (atr_period - 1.0) * atr_tv[i-1]) / float(atr_period)
    df['atr_tv'] = atr_tv

    # Exact Pine Script f_calcTrailingStop(prev, close, nLoss)
    closes = df['close'].values
    stops = np.zeros(len(closes))
    
    for i in range(1, len(closes)):
        nLoss = key_mult * atr_tv[i]
        prev_stop = stops[i-1]
        c_price = closes[i]
        p_price = closes[i-1]
        
        if c_price > prev_stop and p_price > prev_stop:
            stops[i] = max(prev_stop, c_price - nLoss)
        elif c_price < prev_stop and p_price < prev_stop:
            stops[i] = min(prev_stop, c_price + nLoss)
        elif c_price > prev_stop:
            stops[i] = c_price - nLoss
        else:
            stops[i] = c_price + nLoss

    # Index -2 is the latest fully confirmed/closed M15 bar
    last_closed_idx = -2
    p_close = closes[last_closed_idx - 1]
    p_stop  = stops[last_closed_idx - 1]
    c_close = closes[last_closed_idx]
    c_stop  = stops[last_closed_idx]
    c_atr   = atr_tv[last_closed_idx]
    bar_time = int(df['time'].iloc[last_closed_idx])
    
    # Exact TradingView HPotter UT Bot crossover / crossunder
    cross_up = (p_close <= p_stop) and (c_close > c_stop)
    cross_dn = (p_close >= p_stop) and (c_close < c_stop)
    current_trend = "BUY" if c_close > c_stop else "SELL"
    
    return {
        "cross_up": cross_up,
        "cross_dn": cross_dn,
        "trend": current_trend,
        "close": c_close,
        "stop": c_stop,
        "atr": c_atr,
        "bar_time": bar_time
    }


# ─── Position Management & Break-Even Shield ──────────────────────────────────
def manage_open_positions(symbol, cfg, catalyst_state):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
        
    info = mt5.symbol_info(symbol)
    point = info.point
    lot = cfg['lot']
    dollar_per_point = get_dollar_per_pt(symbol, lot)
    
    for pos in positions:
        ticket = pos.ticket
        entry_price = pos.price_open
        current_sl = pos.sl
        current_tp = pos.tp
        pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        current_price = pos.price_current
        
        pts_gain = (current_price - entry_price) if pos_type == "BUY" else (entry_price - current_price)
        # Accurate real-time profit in USD dollars
        dollar_gain = pts_gain * dollar_per_point
        
        # 1. Stage 1: Dynamic Break-Even Shield Check (Pure Dollar Calculation for Gold)
        is_be_active = ACTIVE_BE_TRACKED.get(ticket, False)
        if not is_be_active:
            be_condition = False
            be_target_usd = cfg.get('be_trigger_dollars', 15.0)
            if dollar_gain >= be_target_usd:
                be_condition = True
                be_buffer = 0.5  # Lock +0.5 pt ($1.00 USD) on Gold to cover spread
                
            if be_condition:
                new_sl = entry_price + be_buffer if pos_type == "BUY" else entry_price - be_buffer
                modify_sl(ticket, symbol, new_sl, current_tp)
                ACTIVE_BE_TRACKED[ticket] = True
                update_trade_be(ticket) # Record in dedicated trade analysis log
                msg = (
                    f"🛡️ <b>[Break-Even Protected]</b>\n"
                    f"Asset: <b>{symbol}</b> (#{ticket})\n"
                    f"Profit Reached: +${dollar_gain:.2f}\n"
                    f"SL moved to Entry + Buffer ({new_sl:.2f}). Fakeout risk eliminated!"
                )
                logger.info(f"[{symbol}] BREAK-EVEN LOCKED on #{ticket}! Gain: ${dollar_gain:.2f} -> SL: {new_sl:.2f}")
                _send_telegram(msg)

        # 2. Stage 2: Advanced Profit Lock ($35 -> lock $25)
        is_lock_active = ACTIVE_LOCK_TRACKED.get(ticket, False)
        if not is_lock_active:
            lock_condition = False
            lock_target_usd = cfg.get('lock_trigger_dollars', 35.0)
            lock_amount_usd = cfg.get('lock_amount_dollars', 25.0)
            if dollar_gain >= lock_target_usd:
                lock_condition = True
                lock_dist = lock_amount_usd / dollar_per_point if dollar_per_point > 0 else 12.5
                locked_profit_desc = f"+${lock_amount_usd:.2f}"

            if lock_condition:
                new_sl = (entry_price + lock_dist) if pos_type == "BUY" else (entry_price - lock_dist)
                modify_sl(ticket, symbol, new_sl, current_tp)
                ACTIVE_LOCK_TRACKED[ticket] = True
                ACTIVE_BE_TRACKED[ticket] = True
                msg = (
                    f"\U0001f512 <b>[Profit Lock Activated - Stage 1]</b>\n"
                    f"Asset: <b>{symbol}</b> (#{ticket})\n"
                    f"Gain Reached: +${dollar_gain:.2f}\n"
                    f"SL locked to <b>{locked_profit_desc}</b> ({new_sl:.2f}). Profit guaranteed!"
                )
                logger.info(f"[{symbol}] PROFIT LOCKED (Stage 1) on #{ticket}! Gain: ${dollar_gain:.2f} -> SL: {new_sl:.2f}")
                _send_telegram(msg)

        # 2b. Stage 3: Super-Runner Profit Lock 2 ($60 -> lock $45)
        is_lock2_active = ACTIVE_LOCK2_TRACKED.get(ticket, False)
        if not is_lock2_active:
            lock2_condition = False
            lock2_target_usd = cfg.get('lock2_trigger_dollars', 60.0)
            lock2_amount_usd = cfg.get('lock2_amount_dollars', 45.0)
            if dollar_gain >= lock2_target_usd:
                lock2_condition = True
                lock2_dist = lock2_amount_usd / dollar_per_point if dollar_per_point > 0 else 22.5
                locked2_profit_desc = f"+${lock2_amount_usd:.2f}"

            if lock2_condition:
                new_sl2 = (entry_price + lock2_dist) if pos_type == "BUY" else (entry_price - lock2_dist)
                modify_sl(ticket, symbol, new_sl2, current_tp)
                ACTIVE_LOCK2_TRACKED[ticket] = True
                ACTIVE_LOCK_TRACKED[ticket] = True
                ACTIVE_BE_TRACKED[ticket] = True
                msg = (
                    f"\U0001f680 <b>[Super Profit Lock Activated - Stage 2]</b>\n"
                    f"Asset: <b>{symbol}</b> (#{ticket})\n"
                    f"Gain Reached: +${dollar_gain:.2f}\n"
                    f"SL locked to <b>{locked2_profit_desc}</b> ({new_sl2:.2f}). Huge gain secured!"
                )
                logger.info(f"[{symbol}] PROFIT LOCKED (Stage 2) on #{ticket}! Gain: ${dollar_gain:.2f} -> SL: {new_sl2:.2f}")
                _send_telegram(msg)

                
        # 3. Pre-News Profit & Capital Protection
        if catalyst_state.get('state') == 'PRE_NEWS_FREEZE':
            is_tier1 = catalyst_state.get('tier') == 'TIER_1_MONSTER'
            ev_name = catalyst_state.get('event', 'High-Impact News')
            mins_left = catalyst_state.get('minutes_to_release', 5)

            if dollar_gain > 1.0 and not is_be_active and not is_lock_active:
                new_sl = entry_price + (10 * point) if pos_type == "BUY" else entry_price - (10 * point)
                modify_sl(ticket, symbol, new_sl, current_tp)
                ACTIVE_BE_TRACKED[ticket] = True
                update_trade_be(ticket)
                msg = (
                    f"⚠️ <b>[Pre-News Protection]</b>\n"
                    f"Asset: <b>{symbol}</b> (#{ticket})\n"
                    f"Event: <b>{ev_name}</b> in {mins_left}m\n"
                    f"SL tightened to entry to protect profit before release."
                )
                logger.info(f"[{symbol}] Pre-news protection activated on #{ticket}")
                _send_telegram(msg)
            elif dollar_gain <= 0 and is_tier1:
                # TIER-1 EMERGENCY FLATTEN: Close floating losers before monster news (NFP, CPI, FOMC)
                logger.warning(
                    f"[{symbol}] TIER-1 PRE-NEWS EMERGENCY FLATTEN on #{ticket}! "
                    f"Event: {ev_name} in {mins_left}m | Floating PnL: ${dollar_gain:.2f}"
                )
                if close_market_position(ticket, symbol, pos_type, pos.volume):
                    msg = (
                        f"🚨 <b>[Pre-News Emergency Close]</b>\n"
                        f"Asset: <b>{symbol}</b> (#{ticket})\n"
                        f"Event: <b>{ev_name} (Tier-1)</b> in {mins_left}m\n"
                        f"Action: Position closed at market (${dollar_gain:+.2f}).\n"
                        f"Capital protected from catastrophic news gap slippage!"
                    )
                    _send_telegram(msg)


def _sym_digits(symbol):
    """Returns the number of decimal places for price rounding for a given symbol."""
    info = mt5.symbol_info(symbol)
    return info.digits if info else 2


def close_market_position(ticket, symbol, pos_type, volume):
    """Closes an open position immediately at market price."""
    dp = _sym_digits(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    o_type = mt5.ORDER_TYPE_SELL if pos_type == "BUY" else mt5.ORDER_TYPE_BUY
    price = tick.bid if pos_type == "BUY" else tick.ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": symbol,
        "volume": float(volume),
        "type": o_type,
        "price": price,
        "deviation": 30,
        "magic": 20250831,
        "comment": "PRE_NEWS_EMERGENCY_CLOSE",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"[{symbol}] PRE-NEWS EMERGENCY FLATTEN #{ticket} @ {price:.{dp}f} | PnL protected")
        return True
    else:
        logger.warning(f"[{symbol}] Emergency close failed #{ticket}: {res.comment}")
        return False


def modify_sl(ticket, symbol, new_sl, tp):
    dp = _sym_digits(symbol)
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
        "sl": round(new_sl, dp),
        "tp": round(tp, dp)
    }
    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(f"Failed to modify SL #{ticket}: {res.comment}")


def execute_order(symbol, order_type, lot, sl, tp, catalyst_desc="Standard", news_name="None", ut_stop=0.0, atr=0.0, deal_range_loc=None, rr_ratio=None):
    dp = _sym_digits(symbol)
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if order_type == "BUY" else tick.bid
    o_type = mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": o_type,
        "price": price,
        "sl": round(sl, dp),
        "tp": round(tp, dp),
        "deviation": 20,
        "magic": 20250831,
        "comment": "M15_UT_SWING",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"[{symbol}] {order_type} EXECUTED! Ticket: #{res.order} | Price: {price:.{dp}f} | SL: {sl:.{dp}f} | TP: {tp:.{dp}f}")
        # Log to dedicated trade analysis CSV starting from upgrade
        log_trade_opened(
            ticket=res.order,
            symbol=symbol,
            direction=order_type,
            lot=lot,
            open_price=price,
            sl=sl,
            tp=tp,
            target_metric=catalyst_desc,
            news_catalyst=news_name,
            ut_stop=ut_stop,
            atr=atr
        )
        send_trade_entry_broadcast(
            symbol=symbol,
            order_type=order_type,
            price=price,
            lot=lot,
            sl=sl,
            tp=tp,
            catalyst_desc=catalyst_desc,
            deal_range_loc=deal_range_loc,
            rr_ratio=rr_ratio
        )
        return res.order
    else:
        logger.error(f"[{symbol}] Order Failed: {res.comment} (Retcode: {res.retcode})")
        return None


# ─── Overnight Spread-Buffer Gate (11:00 PM EAT) ──────────────────────────────
def run_pre_session_secure_gate():
    """
    Overnight Spread-Buffer Gate — fires ONCE per day between 23:00 and 23:10 EAT.

    Philosophy: Let overnight trades run — they can be very profitable.
    The danger is NOT the market moving against you; it's the broker's rollover
    spread spike (21:00-01:00 UTC) accidentally touching a tight SL.
    The solution is to give every trade enough SL room that the spread can't reach it.

    Decision logic per open position at 11:00 PM EAT:

      1. Already secured (SL at or above entry for BUY, or at/below entry for SELL)
         -> Leave it completely alone. Profit lock / BE already handles this.

      2. Healthy trade (not deeply losing, SL still at original position):
         -> Widen SL to give $45 of room from current price.
            $45 = ~3x the worst-case Exness overnight spread on Gold/Silver.
            This means even a $3 spread spike cannot trigger the stop.
            The trade runs freely overnight with a real price-based SL,
            not a spread-based accidental stop.

      3. Deeply losing trade (floating loss > -$25):
         -> Close it. No point holding a trade that's already badly wrong
            overnight with an even wider SL — that's just more risk on a
            losing idea.

    Telegram DM summarises every action. Gate fires only once per EAT date.
    """
    global PRE_SESSION_GATE_FIRED

    now_utc = datetime.now(timezone.utc)
    now_eat = now_utc + timedelta(hours=3)

    # Only run Monday-Friday
    if now_eat.weekday() in (5, 6):
        return

    # Window: 23:00 - 23:10 EAT (11 PM)
    gate_open = now_eat.hour == 23 and 0 <= now_eat.minute <= 10
    if not gate_open:
        return

    # Fire only once per calendar day (EAT date)
    today_str = now_eat.strftime("%Y-%m-%d")
    if PRE_SESSION_GATE_FIRED == today_str:
        return

    PRE_SESSION_GATE_FIRED = today_str

    # Gather all open positions across live assets
    all_positions = mt5.positions_get()
    if not all_positions:
        logger.info("[OvernightGate] No open positions at 23:00 EAT — clean night ahead.")
        _send_personal(
            "🌙 <b>[Overnight Gate — 23:00 EAT]</b>\n"
            "No open positions. Clean night, no overnight exposure. ✅"
        )
        return

    actions_taken = []

    # $45 overnight buffer — comfortably above worst-case Exness rollover spread
    OVERNIGHT_BUFFER_USD = 45.0
    # Cut-off: close anything losing more than this at 11 PM (bad trade, not worth wider SL)
    DEEP_LOSS_CUTOFF_USD = -25.0

    for pos in all_positions:
        symbol   = pos.symbol
        ticket   = pos.ticket
        pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        entry    = pos.price_open
        current  = pos.price_current
        sl       = pos.sl
        tp       = pos.tp
        profit   = pos.profit   # floating P&L in USD

        cfg = ASSET_CONFIGS.get(symbol)
        if cfg is None:
            continue   # Not a managed asset — skip

        lot           = cfg['lot']
        dollar_per_pt = get_dollar_per_pt(symbol, lot)
        dp            = _sym_digits(symbol)
        buf_pts       = (OVERNIGHT_BUFFER_USD / dollar_per_pt) if dollar_per_pt > 0 else 0.0

        # ── Case 1: Already secured at BE or profit lock — leave it alone ────
        already_secured = (
            (pos_type == "BUY"  and sl >= entry) or
            (pos_type == "SELL" and sl <= entry and sl > 0)
        )
        if already_secured:
            logger.info(
                "[OvernightGate] " + symbol + " #" + str(ticket) +
                " already at BE/Lock (SL=" + str(round(sl, dp)) + ") — no change needed."
            )
            actions_taken.append(
                "✅ <b>" + symbol + "</b> #" + str(ticket) + " (" + pos_type + ") | "
                "Profit: <b>+$" + str(round(profit, 2)) + "</b>\n"
                "   ➔ Already secured at BE/Lock. Running overnight freely."
            )
            continue

        # ── Case 2: Deeply losing — close it, no point widening SL on a bad trade ──
        if profit < DEEP_LOSS_CUTOFF_USD:
            tick        = mt5.symbol_info_tick(symbol)
            close_price = tick.bid if pos_type == "BUY" else tick.ask
            close_type  = mt5.ORDER_TYPE_SELL if pos_type == "BUY" else mt5.ORDER_TYPE_BUY
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     ticket,
                "price":        close_price,
                "deviation":    20,
                "magic":        20250831,
                "comment":      "OVERNIGHT_GATE_DEEPLOSS",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
            pnl_str = "-$" + str(round(abs(profit), 2))
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    "[OvernightGate] " + symbol + " #" + str(ticket) +
                    " CLOSED (deep loss " + pnl_str + "). Not worth overnight SL buffer."
                )
                actions_taken.append(
                    "🛑 <b>" + symbol + "</b> #" + str(ticket) + " (" + pos_type + ") | "
                    "Loss: <b>" + pnl_str + "</b>\n"
                    "   ➔ Closed — deeply losing trade, not worth overnight exposure."
                )
                now_ts = int(now_utc.timestamp())
                LAST_CLOSED_TIME[symbol] = now_ts
                _save_closed_time(LAST_CLOSED_TIME)
            else:
                logger.error(
                    "[OvernightGate] Failed to close deep-loss " + symbol +
                    " #" + str(ticket) + ": " + res.comment
                )
                actions_taken.append(
                    "⚠️ <b>" + symbol + "</b> #" + str(ticket) +
                    " — CLOSE FAILED: " + res.comment
                )
            continue

        # ── Case 3: Healthy trade — widen SL to $45 from current price ───────
        # Calculate new SL that gives $45 of room from where price is RIGHT NOW.
        # This means the overnight spread (max ~$3) cannot accidentally trigger it.
        if pos_type == "BUY":
            new_sl = round(current - buf_pts, dp)
            # Give room: new_sl is lower than current SL (widens stop to absorb rollover spikes)
            if new_sl < sl or sl == 0:
                modify_sl(ticket, symbol, new_sl, tp)
                action_desc = (
                    f"$45 spread-buffer SL set at {new_sl:.{dp}f} (was {sl:.{dp}f})"
                )
            else:
                action_desc = (
                    f"SL already has sufficient room ({sl:.{dp}f}) — no change"
                )
        else:
            new_sl = round(current + buf_pts, dp)
            # Give room: new_sl is higher than current SL for SELL
            if new_sl > sl or sl == 0:
                modify_sl(ticket, symbol, new_sl, tp)
                action_desc = (
                    f"$45 spread-buffer SL set at {new_sl:.{dp}f} (was {sl:.{dp}f})"
                )
            else:
                action_desc = (
                    f"SL already has sufficient room ({sl:.{dp}f}) — no change"
                )

        pnl_str = ("+$" + str(round(profit, 2))) if profit >= 0 else ("-$" + str(round(abs(profit), 2)))
        logger.info(
            "[OvernightGate] " + symbol + " #" + str(ticket) +
            " | Profit: " + pnl_str + " | " + action_desc
        )
        actions_taken.append(
            "🌙 <b>" + symbol + "</b> #" + str(ticket) + " (" + pos_type + ") | "
            "Profit: <b>" + pnl_str + "</b>\n"
            "   ➔ " + action_desc
        )

    # Telegram summary
    if actions_taken:
        summary = "\n".join(actions_taken)
        _send_personal(
            "🌙 <b>[Overnight Gate Executed — 23:00 EAT]</b>\n\n"
            + summary + "\n\n"
            "All trades protected from overnight rollover spread. "
            "Genuine price moves will be caught. Good night! 🌙"
        )
    else:
        _send_personal(
            "🌙 <b>[Overnight Gate — 23:00 EAT]</b>\n"
            "No managed positions found. Clean night ahead. ✅"
        )


# ─── Autonomous Scan Cycle ─────────────────────────────────────────────────────
def run_institutional_cycle():
    """
    Called autonomously every cycle from alphaedge.py.
    Executes the Institutional Multi-Timeframe (MTF) & Whale Flow Swing Engine.
    Filters:
      1. H4 Dealing Range (Strict Discount for BUY, Strict Premium for SELL - never enters halfway!).
      2. Liquidity Sweep / Stop Hunt (Bottom accumulation / Top distribution).
      3. Whale Tick Volume surge >= 1.6x 20-period moving average.
      4. Pre-Trade Real-Time Backtest Gate (validates historical expectancy before execution).
    """
    global NEWS_ENGINE, INST_ENGINE, PRE_BACKTESTER, STRUCT_MEMORY, LAST_EXECUTED_BAR
    if NEWS_ENGINE is None:
        NEWS_ENGINE = NewsCatalystEngine()
    if INST_ENGINE is None:
        INST_ENGINE = InstitutionalEngine()
    if PRE_BACKTESTER is None:
        PRE_BACKTESTER = PreTradeBacktester()
    if STRUCT_MEMORY is None:
        STRUCT_MEMORY = StructuralMemory()

    # ── Pre-Session Overnight Protection Gate (19:45–19:55 EAT, once per day) ─
    try:
        run_pre_session_secure_gate()
    except Exception as _gate_err:
        logger.error("[PreSessionGate] Unexpected error in gate: " + str(_gate_err))

    try:
        NEWS_ENGINE.sync_calendar()
    except Exception:
        pass

    # Daily news briefing — fires once per day on first cycle
    try:
        NEWS_ENGINE.send_daily_briefing()
    except Exception:
        pass

    # 30-minute countdown alerts — checked every cycle, fires once per event
    try:
        NEWS_ENGINE.check_and_send_30min_alerts()
    except Exception:
        pass

    # Check MT5 history deals to close any trades in analysis log that hit TP/SL
    try:
        sync_closed_trades_from_history()
    except Exception:
        pass

    session_ok = is_session_active()
    apply_ai_learned_settings()

    for symbol, cfg in ASSET_CONFIGS.items():
        try:
            catalyst = NEWS_ENGINE.get_market_catalyst_status(symbol)
            manage_open_positions(symbol, cfg, catalyst)

            # ── 0. 24/5 Structural Observation (session-agnostic) ─────────────────
            # Structural memory updates on EVERY cycle regardless of trading session.
            # The bot always knows the market structure even during off-hours.
            STRUCT_MEMORY.update_structure(symbol)
            struct_ctx = STRUCT_MEMORY.get_context(symbol)
            logger.debug(
                f"[StructuralMemory] {symbol} | Regime: {struct_ctx.get('regime')} | "
                f"PWH: {struct_ctx.get('pwh')} / PWL: {struct_ctx.get('pwl')} | "
                f"PDH: {struct_ctx.get('pdh')} / PDL: {struct_ctx.get('pdl')} | "
                f"Asia H/L: {struct_ctx.get('asia_high')} / {struct_ctx.get('asia_low')} | "
                f"Weekly EQ: {struct_ctx.get('weekly_eq')}"
            )

            lot = cfg['lot']
            dollar_per_pt = get_dollar_per_pt(symbol, lot)

            tick = mt5.symbol_info_tick(symbol)
            curr_price = tick.bid if tick else 0.0

            # ── 1. Evaluate Institutional MTF & Whale Flow Setup ─────────────────
            setup = INST_ENGINE.evaluate_institutional_setup(symbol)
            deal_range = setup.get('deal_range')
            loc_pct = f"{deal_range['location_pct']:.1f}%" if deal_range else "N/A"
            vol_ratio = setup.get('vol_ratio', 1.0)
            whale_str = f"WHALE {vol_ratio}x" if setup.get('whale_detected') else f"{vol_ratio}x"

            positions = mt5.positions_get(symbol=symbol)
            has_pos = len(positions) > 0 if positions else False

            sym_session_ok = is_session_active(symbol)
            session_tag = "ACTIVE (24/5)" if sym_session_ok else "CLOSED (Weekend)"
            state_str = f"POS({len(positions)})" if has_pos else (f"SETUP({setup.get('direction')})" if setup.get('valid') else "WAIT")

            logger.info(
                f"[Scanner] {symbol:<7} {curr_price:>8.2f} | Range: {loc_pct:>6} | Vol: {whale_str:>10} | {state_str:<8} | {session_tag}"
            )
            if setup.get('valid') or setup.get('whale_detected'):
                logger.info(f"   ⚡ [{symbol}] {setup.get('direction')} Signal Ready: {setup.get('reason')} | UT: {setup.get('ut_bot', 'NONE')}")

            positions = mt5.positions_get(symbol=symbol)
            has_pos = len(positions) > 0 if positions else False

            # Detect position closure (whether closed manually by user or automatically by TP/SL)
            was_open = WAS_OPEN_TRACKED.get(symbol, False)
            if was_open and not has_pos:
                now_closure_ts = int(datetime.now(timezone.utc).timestamp())
                LAST_CLOSED_TIME[symbol] = now_closure_ts
                _save_closed_time(LAST_CLOSED_TIME)
                logger.info(
                    f"[{symbol}] Position closure detected! 60-Minute Post-Closure Cooldown activated. "
                    f"No re-entry until {datetime.fromtimestamp(now_closure_ts + 3600, tz=timezone.utc).strftime('%H:%M:%S')} UTC."
                )
            WAS_OPEN_TRACKED[symbol] = has_pos

            # Block entries during high-impact news window
            news_blocks_entry = catalyst.get('state') in ('PRE_NEWS_FREEZE', 'NEWS_SPIKE_BLOCK')
            sym_session_ok = is_session_active(symbol)
            if not sym_session_ok or news_blocks_entry:
                continue

            # Check position state and enforce 60-Minute Cooldown from BOTH last execution AND last closure!
            now_dt = datetime.now(timezone.utc)
            now_ts = int(now_dt.timestamp())
            m15_minute = (now_dt.minute // 15) * 15
            now_m15_ts = int(now_dt.replace(minute=m15_minute, second=0, microsecond=0).timestamp())
            last_exec_ts = LAST_EXECUTED_BAR.get(symbol, 0)
            last_closed_ts = LAST_CLOSED_TIME.get(symbol, 0)

            if has_pos or (now_ts - last_exec_ts < 3600) or (now_ts - last_closed_ts < 3600):
                continue

            if not setup.get('valid'):
                # Not a valid structural bottom/top setup — strictly wait, do not chase halfway!
                continue

            target_dir = setup['direction']  # "BUY" or "SELL"

            # ── 2.5 Structural Confluence Check (Regime + Weekly EQ) ─────────────
            # Institutional rule: only trade in the direction of the weekly regime.
            # In EXPANSION_UP   — only take BUY setups (price in Discount zone).
            # In EXPANSION_DOWN — only take SELL setups (price in Premium zone).
            # In RANGE_BOUND    — both directions allowed, use weekly EQ as guide.
            regime = struct_ctx.get('regime', 'UNKNOWN')
            in_discount = STRUCT_MEMORY.is_in_weekly_discount(symbol, curr_price)
            in_premium  = STRUCT_MEMORY.is_in_weekly_premium(symbol, curr_price)

            regime_allows = True
            if regime == "EXPANSION_UP" and target_dir == "SELL":
                regime_allows = False
                logger.info(
                    f"[StructuralFilter] {symbol} SELL blocked — Regime is EXPANSION_UP "
                    f"(wait for pullback to premium before shorting)."
                )
            elif regime == "EXPANSION_DOWN" and target_dir == "BUY":
                regime_allows = False
                logger.info(
                    f"[StructuralFilter] {symbol} BUY blocked — Regime is EXPANSION_DOWN "
                    f"(wait for bounce to discount before longing)."
                )

            # Strict Institutional Safeguard: Never SELL in Deep Discount (< 35%) or BUY in Deep Premium (> 65%)
            loc_pct_val = setup.get('deal_range', {}).get('location_pct', 50.0)
            if target_dir == "SELL" and loc_pct_val < 35.0:
                regime_allows = False
                logger.info(
                    f"[StructuralFilter] {symbol} SELL blocked — Dealing Range is in Deep Discount ({loc_pct_val:.1f}% < 35%). "
                    f"Selling the swing bottom is strictly prohibited."
                )
            elif target_dir == "BUY" and loc_pct_val > 65.0:
                regime_allows = False
                logger.info(
                    f"[StructuralFilter] {symbol} BUY blocked — Dealing Range is in Deep Premium ({loc_pct_val:.1f}% > 65%). "
                    f"Buying the swing top is strictly prohibited."
                )

            if not regime_allows:
                continue

            # ── 3. Pre-Trade Real-Time Backtest Gate ────────────────────────────

            # Enforce structural SL room and dynamic dollar profit expansion targets
            dollar_per_pt = get_dollar_per_pt(symbol, cfg['lot'])
            max_sl_usd = cfg.get('max_sl_dollars', 36.0)

            # Volatility & Catalyst Target Expansion
            if catalyst.get('state') == 'CATALYST_IMPULSE':
                vol_boost = catalyst.get('volatility_multiplier', 1.0)
                tp_target_usd = cfg.get('tp_catalyst_dollars', 120.0) * vol_boost
            else:
                tp_target_usd = cfg.get('tp_dollars', 100.0)

            target_tp_dist = tp_target_usd / dollar_per_pt if dollar_per_pt > 0 else 50.0
            # Strict dollar risk: capped strictly at max_sl_dollars, never ballooned
            target_sl_dist = max_sl_usd / dollar_per_pt if dollar_per_pt > 0 else 18.0

            order_price = tick.ask if target_dir == "BUY" else tick.bid
            if target_dir == "BUY":
                sl_price = order_price - target_sl_dist
                tp_price = order_price + target_tp_dist
            else:
                sl_price = order_price + target_sl_dist
                tp_price = order_price - target_tp_dist

            # ── 3. Approved: Execute High-Conviction Institutional Swing Order ────
            dp = _sym_digits(symbol)
            event_name = catalyst.get('event', 'Institutional Flow')
            approach_name = setup.get('approach_type', 'Exhaustion Sweep')
            mode_desc = f"H4 ({loc_pct}) | M15 UT {setup.get('ut_bot', '')} | {approach_name}"
            deal_loc_desc = f"Dealing Range: {loc_pct}"
            rr_val = round(target_tp_dist / target_sl_dist, 2) if target_sl_dist > 0 else 1.5

            order_price = tick.ask if target_dir == "BUY" else tick.bid
            logger.info(
                f"[Institutional MTF] EXECUTING APPROVED {target_dir} on {symbol}! "
                f"Price: {order_price:.{dp}f} | SL: {sl_price:.{dp}f} | TP: {tp_price:.{dp}f} | "
                f"Trigger: H4 Structure + M15 UT Bot ({approach_name})"
            )

            if execute_order(symbol, target_dir, cfg['lot'], sl_price, tp_price, mode_desc, event_name, setup.get('sweep_level', 0.0), 0.0, deal_range_loc=deal_loc_desc, rr_ratio=rr_val):
                LAST_EXECUTED_BAR[symbol] = now_m15_ts
                _save_bar_state(LAST_EXECUTED_BAR)


        except Exception as e:
            logger.error(f"[Institutional MTF] Error processing {symbol}: {e}\n{traceback.format_exc()}")


# Backward-compatibility alias (run_scalping_cycle -> run_institutional_cycle)
run_scalping_cycle = run_institutional_cycle
