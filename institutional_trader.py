"""
institutional_trader.py — AlphaEdge Pure Price Action 3-Timeframe Execution Engine
===================================================================================
High-conviction Pure Price Action multi-timeframe swing engine (zero lagging indicators):
  - TF1 (D1 + H4 Macro): H4 fractal swing structure (3-state: BULL/BEAR/RANGE) +
                         active H4 swing leg equilibrium (BUY <= 75%, SELL >= 25%).
  - TF2 (Key Pools & M15): Liquidity sweeps of PDH/PDL, Asian H/L (00-06 UTC), and
                           Closed H4 H/L with M15 anti-crash blowout validation.
  - TF3 (M5 Execution): 2-bar sweep-and-reclaim + directional engulf/turn candle
                        breaking prior bar extreme + anti-climax range guard.
  - Calibrated Exits (XAUUSDm 0.02 | USOILm 0.05 | US30m 0.25):
      * TP = $55.00 USD (Catalyst: $70.00) | Hard SL = $34.00 USD
      * Stage 1 BE: +$28.00 -> move SL to Entry + $2.00 buffer
      * Stage 2 Profit Lock: +$44.00 -> lock +$32.00
      * Stage 3 Super-Runner Lock: +$56.00 -> lock +$45.00
  - Risk & Session Shields:
      * Mon-Fri London (06-09 UTC / 09-13 EAT) & NY (14-19 UTC / 17-23 EAT) Killzones.
      * Strict -$50.00 daily drawdown hard stop, max 2 concurrent positions, 60-min cooldown.
      * Friday 20:45+ UTC weekend-close protection; zero SL widening overnight.
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
from pure_price_action import CALIBRATED_ASSET_SPECS, is_killzone_active, is_weekend_quiet
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

MAX_DAILY_DRAWDOWN_USD = 50.0
MAX_CONCURRENT_POSITIONS = 2
_DAILY_DD_ALERT_DATE = None

# Asset Configurations (Pure Price Action 3-TF Engine - Calibrated for $500+ Account)
ASSET_CONFIGS = {
    sym: {
        "symbol": sym,
        **spec,
        "sl_atr_mult": 1.5,
        "currency": "USD",
    }
    for sym, spec in CALIBRATED_ASSET_SPECS.items()
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
    """Loads dynamically tuned parameters from the AI Auto-Learning Brain within calibrated Pure PA bounds."""
    cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_learned_m15.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                c = json.load(f)

            symbols_cfg = c.get("symbols_config", {})
            for sym, asset_cfg in ASSET_CONFIGS.items():
                spec = CALIBRATED_ASSET_SPECS.get(sym, {})
                learned = symbols_cfg.get(sym, {})
                if not learned:
                    learned = {
                        "tp_dollars": c.get("gold_tp_dollars", spec.get("tp_dollars", 55.0)),
                        "tp_catalyst_dollars": c.get("gold_tp_catalyst_dollars", spec.get("tp_catalyst_dollars", 70.0)),
                        "be_trigger_dollars": c.get("gold_be_trigger_dollars", spec.get("be_trigger_dollars", 28.0)),
                        "lock_trigger_dollars": c.get("gold_lock_trigger_dollars", spec.get("lock_trigger_dollars", 44.0)),
                        "lock_amount_dollars": c.get("gold_lock_amount_dollars", spec.get("lock_amount_dollars", 32.0)),
                        "lock2_trigger_dollars": c.get("gold_lock2_trigger_dollars", spec.get("lock2_trigger_dollars", 56.0)),
                        "lock2_amount_dollars": c.get("gold_lock2_amount_dollars", spec.get("lock2_amount_dollars", 45.0)),
                    }

                tp_val = float(learned.get("tp_dollars", spec.get("tp_dollars", 55.0)))
                asset_cfg["tp_dollars"] = max(45.0, min(75.0, tp_val))

                tp_cat = float(learned.get("tp_catalyst_dollars", spec.get("tp_catalyst_dollars", 70.0)))
                asset_cfg["tp_catalyst_dollars"] = max(60.0, min(90.0, tp_cat))

                be_trig = float(learned.get("be_trigger_dollars", spec.get("be_trigger_dollars", 28.0)))
                asset_cfg["be_trigger_dollars"] = max(22.0, min(34.0, be_trig))

                l1_trig = float(learned.get("lock_trigger_dollars", spec.get("lock_trigger_dollars", 44.0)))
                asset_cfg["lock_trigger_dollars"] = max(36.0, min(52.0, l1_trig))

                l1_amt = float(learned.get("lock_amount_dollars", spec.get("lock_amount_dollars", 32.0)))
                asset_cfg["lock_amount_dollars"] = max(24.0, min(38.0, l1_amt))

                l2_trig = float(learned.get("lock2_trigger_dollars", spec.get("lock2_trigger_dollars", 56.0)))
                asset_cfg["lock2_trigger_dollars"] = max(48.0, min(68.0, l2_trig))

                l2_amt = float(learned.get("lock2_amount_dollars", spec.get("lock2_amount_dollars", 45.0)))
                asset_cfg["lock2_amount_dollars"] = max(36.0, min(52.0, l2_amt))

                # SAFETY INVIOLABILITY: initial max_sl_dollars ($34) is NEVER widened
                asset_cfg["max_sl_dollars"] = float(spec.get("max_sl_dollars", 34.0))
        except Exception as e:
            logger.debug(f"[Institutional] Dynamic config load skipped: {e}")


def check_daily_drawdown_guard():
    """
    Enforces strict -$50.00 daily drawdown hard stop (realized today + live floating PnL).
    Returns (blocked: bool, daily_total: float, realized_today: float, floating_pnl: float).
    """
    global _DAILY_DD_ALERT_DATE
    now_utc = datetime.now(timezone.utc)
    now_eat = now_utc + timedelta(hours=3)
    eat_midnight_utc = (now_eat.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3))
    reset_floor_utc = datetime(2026, 11, 13, 14, 50, tzinfo=timezone.utc)
    from_utc = max(eat_midnight_utc, reset_floor_utc)

    realized_today = 0.0
    try:
        deals = mt5.history_deals_get(from_utc, now_utc + timedelta(minutes=5))
        if deals:
            for d in deals:
                if getattr(d, "entry", -1) in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                    realized_today += float(getattr(d, "profit", 0.0)) + float(getattr(d, "swap", 0.0)) + float(getattr(d, "commission", 0.0))
    except Exception:
        pass

    floating_pnl = 0.0
    try:
        positions = mt5.positions_get()
        if positions:
            for p in positions:
                if p.symbol in ASSET_CONFIGS:
                    floating_pnl += float(getattr(p, "profit", 0.0)) + float(getattr(p, "swap", 0.0))
    except Exception:
        pass

    daily_total = round(realized_today + floating_pnl, 2)
    blocked = daily_total <= -MAX_DAILY_DRAWDOWN_USD
    if blocked:
        today_str = now_eat.strftime("%Y-%m-%d")
        logger.warning(
            f"[DailyDrawdownGuard] HARD STOP ACTIVE | Daily PnL ${daily_total:+.2f} "
            f"(Realized ${realized_today:+.2f}, Float ${floating_pnl:+.2f}) <= -${MAX_DAILY_DRAWDOWN_USD:.2f}"
        )
        if _DAILY_DD_ALERT_DATE != today_str:
            _DAILY_DD_ALERT_DATE = today_str
            _send_personal(
                f"🛑 <b>[Daily Drawdown Guard Triggered]</b>\n"
                f"Today's PnL hit <b>${daily_total:+.2f}</b> (Cap: -${MAX_DAILY_DRAWDOWN_USD:.2f}).\n"
                f"New entries paused until next session day to protect your $500 capital."
            )
    return blocked, daily_total, round(realized_today, 2), round(floating_pnl, 2)


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
    """Low-level: sends a single message to one recipient with firewall & weekend cutoff."""
    if not _TELEGRAM_TOKEN or not chat_id or is_weekend_quiet():
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
        detail = "Pure PA Institutional Exit"

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


# ─── Session Filter (Mon-Fri London & NY Killzones) ───────────────────────────
def is_session_active(symbol=None):
    """
    Pure Price Action Execution Gateway (Monday - Friday):
      - London Killzone : 06:00 - 09:59 UTC (09:00 - 12:59 EAT)
      - New York Session: 14:00 - 19:59 UTC (17:00 - 22:59 EAT)
    Blocks low-liquidity Asian midnight chop (00-05 UTC), London lunch (10-13 UTC),
    rollover spread widening (20-23 UTC), and weekends (Saturday & Sunday).
    """
    return is_killzone_active()


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
            be_target_usd = cfg.get('be_trigger_dollars', 28.0)
            if dollar_gain >= be_target_usd:
                be_condition = True
                # Lock +$2.00 USD consistently across Gold/Oil/US30 to cover spread cleanly
                be_buffer = (2.0 / dollar_per_point) if dollar_per_point > 0 else 0.5
                
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
        "comment": "PurePA_3TF",
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


# ─── Capital Protection & Friday Weekend Close Gate ───────────────────────────
def run_pre_session_secure_gate():
    """
    Capital & Weekend Protection Gate:
      1. Friday Weekend Close Guard (Friday >= 20:45 UTC / 23:45 EAT):
         -> Flattens open managed positions before weekend market close so
            weekend gap risk cannot damage the $500 account.
      2. Nightly Audit (23:00 - 23:10 EAT Mon-Thu):
         -> Never widens SL past the strict -$34 initial risk cap.
         -> Closes deep losing trades (< -$25.00 floating loss).
         -> Moves profitable trades (>= +$20.00) without BE yet to Entry + $2.00 buffer.
    """
    global PRE_SESSION_GATE_FIRED

    now_utc = datetime.now(timezone.utc)
    now_eat = now_utc + timedelta(hours=3)

    # 1. Friday Weekend Close Protection (Friday >= 20:45 UTC / 23:45 EAT)
    if now_utc.weekday() == 4 and (now_utc.hour > 20 or (now_utc.hour == 20 and now_utc.minute >= 45)):
        fri_str = now_utc.strftime("%Y-%m-%d")
        open_managed = [p for p in (mt5.positions_get() or []) if p.symbol in ASSET_CONFIGS]
        if open_managed:
            closed_lines = []
            for pos in open_managed:
                symbol = pos.symbol
                ticket = pos.ticket
                pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                tick = mt5.symbol_info_tick(symbol)
                if not tick:
                    continue
                close_price = tick.bid if pos_type == "BUY" else tick.ask
                close_type = mt5.ORDER_TYPE_SELL if pos_type == "BUY" else mt5.ORDER_TYPE_BUY
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": pos.volume,
                    "type": close_type,
                    "position": ticket,
                    "price": close_price,
                    "deviation": 20,
                    "magic": 20250831,
                    "comment": "WEEKEND_CLOSE_GUARD",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    LAST_CLOSED_TIME[symbol] = int(now_utc.timestamp())
                    _save_closed_time(LAST_CLOSED_TIME)
                    closed_lines.append(f"🔒 <b>{symbol}</b> #{ticket} ({pos_type}): ${pos.profit:+.2f}")
                    logger.info(f"[WeekendGuard] Closed {symbol} #{ticket} prior to weekend close (${pos.profit:+.2f})")
            if closed_lines and getattr(run_pre_session_secure_gate, "_weekend_date", None) != fri_str:
                run_pre_session_secure_gate._weekend_date = fri_str
                _send_personal(
                    "🏁 <b>[Friday Weekend Close Protection — 23:45 EAT]</b>\n\n"
                    + "\n".join(closed_lines)
                    + "\n\nAll positions flattened cleanly before weekend gap risk."
                )
        return

    # Only run nightly audit Monday-Thursday
    if now_eat.weekday() in (4, 5, 6):
        return

    # Window: 23:00 - 23:10 EAT (11 PM)
    gate_open = now_eat.hour == 23 and 0 <= now_eat.minute <= 10
    if not gate_open:
        return

    today_str = now_eat.strftime("%Y-%m-%d")
    if PRE_SESSION_GATE_FIRED == today_str:
        return

    PRE_SESSION_GATE_FIRED = today_str

    all_positions = mt5.positions_get()
    if not all_positions:
        logger.info("[NightlyGuard] No open positions at 23:00 EAT — clean night ahead.")
        _send_personal(
            "🌙 <b>[Nightly Capital Guard — 23:00 EAT]</b>\n"
            "No open positions. Zero overnight exposure. ✅"
        )
        return

    actions_taken = []
    DEEP_LOSS_CUTOFF_USD = -25.0

    for pos in all_positions:
        symbol   = pos.symbol
        ticket   = pos.ticket
        pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        entry    = pos.price_open
        sl       = pos.sl
        tp       = pos.tp
        profit   = pos.profit

        cfg = ASSET_CONFIGS.get(symbol)
        if cfg is None:
            continue

        lot           = cfg['lot']
        dollar_per_pt = get_dollar_per_pt(symbol, lot)
        dp            = _sym_digits(symbol)

        # Case 1: Already secured at BE or profit lock
        already_secured = (
            (pos_type == "BUY"  and sl >= entry) or
            (pos_type == "SELL" and sl <= entry and sl > 0)
        )
        if already_secured:
            logger.info(f"[NightlyGuard] {symbol} #{ticket} already secured at BE/Lock (SL={round(sl, dp)}).")
            actions_taken.append(
                f"✅ <b>{symbol}</b> #{ticket} ({pos_type}) | Profit: <b>+${profit:.2f}</b>\n"
                f"   ➔ Already secured at BE/Lock."
            )
            continue

        # Case 2: Deeply losing — cut early rather than widening risk
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
                "comment":      "NIGHTLY_GUARD_DEEPLOSS",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
            pnl_str = f"-${abs(profit):.2f}"
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[NightlyGuard] {symbol} #{ticket} CLOSED (deep loss {pnl_str}).")
                actions_taken.append(
                    f"🛑 <b>{symbol}</b> #{ticket} ({pos_type}) | Loss: <b>{pnl_str}</b>\n"
                    f"   ➔ Closed — deep loss cut strictly."
                )
                LAST_CLOSED_TIME[symbol] = int(now_utc.timestamp())
                _save_closed_time(LAST_CLOSED_TIME)
            else:
                logger.error(f"[NightlyGuard] Failed to close deep-loss {symbol} #{ticket}: {getattr(res, 'comment', 'ERR')}")
            continue

        # Case 3: In good profit (>= +$20) — lock BE + $2 buffer; NEVER widen initial $34 SL
        if profit >= 20.0:
            be_buf = (2.0 / dollar_per_pt) if dollar_per_pt > 0 else 0.5
            new_sl = round((entry + be_buf) if pos_type == "BUY" else (entry - be_buf), dp)
            modify_sl(ticket, symbol, new_sl, tp)
            ACTIVE_BE_TRACKED[ticket] = True
            update_trade_be(ticket)
            action_desc = f"BE + $2.00 buffer secured at {new_sl:.{dp}f} (profit +${profit:.2f})"
        else:
            action_desc = f"Hard $34 SL preserved at {sl:.{dp}f} (never widened)"

        pnl_str = (f"+${profit:.2f}") if profit >= 0 else (f"-${abs(profit):.2f}")
        logger.info(f"[NightlyGuard] {symbol} #{ticket} | Profit: {pnl_str} | {action_desc}")
        actions_taken.append(
            f"🌙 <b>{symbol}</b> #{ticket} ({pos_type}) | Profit: <b>{pnl_str}</b>\n"
            f"   ➔ {action_desc}"
        )

    if actions_taken:
        _send_personal(
            "🌙 <b>[Nightly Capital Guard — 23:00 EAT]</b>\n\n"
            + "\n".join(actions_taken)
            + "\n\nStrict $34 initial SL preserved on all positions."
        )
    else:
        _send_personal(
            "🌙 <b>[Nightly Capital Guard — 23:00 EAT]</b>\n"
            "No managed positions open. Clean night ahead. ✅"
        )


# ─── Autonomous Scan Cycle ─────────────────────────────────────────────────────
def run_institutional_cycle():
    """
    Called autonomously every cycle from alphaedge.py.
    Executes the Pure Price Action 3-Timeframe Institutional Swing Engine.
    Filters:
      1. TF1 (D1 + H4): 3-state fractal structure (NOT_COUNTER) & 75%/25% active leg equilibrium.
      2. TF2 (Pools + M15): Sweeps of PDH/PDL, Asian H/L, or Closed H4 H/L with M15 anti-crash validation.
      3. TF3 (M5): 2-bar sweep reclaim + directional engulf/turn + anti-climax range guard.
      4. Guards: Mon-Fri London/NY Killzones, -$50 daily DD hard stop, max 2 concurrent trades, 1h cooldown.
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

    # ── Capital Guard & Friday Weekend-Close Protection ─────────────────────
    try:
        run_pre_session_secure_gate()
    except Exception as _gate_err:
        logger.error("[CapitalGuard] Unexpected error in gate: " + str(_gate_err))

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

    apply_ai_learned_settings()
    dd_blocked, daily_pnl, _, _ = check_daily_drawdown_guard()

    for symbol, cfg in ASSET_CONFIGS.items():
        try:
            catalyst = NEWS_ENGINE.get_market_catalyst_status(symbol)
            manage_open_positions(symbol, cfg, catalyst)

            # ── 0. 24/5 Structural Observation (session-agnostic) ─────────────────
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

            # ── 1. Evaluate Pure Price Action 3-TF Setup ───────────────────────
            setup = INST_ENGINE.evaluate_institutional_setup(symbol)
            deal_range = setup.get('deal_range')
            loc_pct = f"{deal_range['location_pct']:.1f}%" if deal_range else "N/A"

            all_live_pos = mt5.positions_get() or []
            total_open_managed = sum(1 for p in all_live_pos if p.symbol in ASSET_CONFIGS)
            positions = [p for p in all_live_pos if p.symbol == symbol]
            has_pos = len(positions) > 0

            sym_session_ok = is_session_active(symbol)
            session_tag = "KILLZONE" if sym_session_ok else ("DAILY_DD_STOP" if dd_blocked else "OFF_HOURS")
            state_str = f"POS({len(positions)})" if has_pos else (f"SETUP({setup.get('direction')})" if setup.get('valid') else "WAIT")

            logger.info(
                f"[Scanner] {symbol:<7} {curr_price:>8.2f} | H4 Leg: {loc_pct:>6} | {setup.get('ut_bot', 'PA_WAIT'):>10} | {state_str:<10} | {session_tag} (DayPnL ${daily_pnl:+.2f})"
            )
            if setup.get('valid'):
                logger.info(f"   ⚡ [{symbol}] {setup.get('direction')} Pure PA Ready: {setup.get('reason')}")

            # Detect position closure to enforce 60-min cooldown
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

            # Hard session, daily drawdown, news freeze, and Friday close cutoff guards
            now_dt = datetime.now(timezone.utc)
            fri_late = (now_dt.weekday() == 4 and (now_dt.hour > 20 or (now_dt.hour == 20 and now_dt.minute >= 30)))
            news_blocks_entry = catalyst.get('state') in ('PRE_NEWS_FREEZE', 'NEWS_SPIKE_BLOCK')
            if dd_blocked or not sym_session_ok or news_blocks_entry or fri_late:
                continue

            # Closed M5 bar timestamp + 60-minute post-entry/closure cooldown + max 2 concurrent
            m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 3)
            now_ts = int(now_dt.timestamp())
            m15_minute = (now_dt.minute // 15) * 15
            now_m15_ts = int(now_dt.replace(minute=m15_minute, second=0, microsecond=0).timestamp())
            cur_bar_ts = int(m5_rates[-2]['time']) if (m5_rates is not None and len(m5_rates) >= 2) else now_m15_ts

            last_exec_ts = LAST_EXECUTED_BAR.get(symbol, 0)
            last_closed_ts = LAST_CLOSED_TIME.get(symbol, 0)

            if (
                has_pos
                or total_open_managed >= MAX_CONCURRENT_POSITIONS
                or cur_bar_ts == last_exec_ts
                or (now_ts - last_exec_ts < 3600)
                or (now_ts - last_closed_ts < 3600)
            ):
                continue

            if not setup.get('valid'):
                continue

            target_dir = setup['direction']  # "BUY" or "SELL"

            # ── 2. Calibrated Dollar Targets & Strict -$34 SL ────────────────
            dollar_per_pt = get_dollar_per_pt(symbol, cfg['lot'])
            max_sl_usd = cfg.get('max_sl_dollars', 34.0)

            if catalyst.get('state') == 'CATALYST_IMPULSE':
                vol_boost = catalyst.get('volatility_multiplier', 1.0)
                tp_target_usd = cfg.get('tp_catalyst_dollars', 70.0) * vol_boost
            else:
                tp_target_usd = cfg.get('tp_dollars', 55.0)

            target_tp_dist = tp_target_usd / dollar_per_pt if dollar_per_pt > 0 else 27.5
            target_sl_dist = max_sl_usd / dollar_per_pt if dollar_per_pt > 0 else 17.0

            order_price = tick.ask if target_dir == "BUY" else tick.bid
            if target_dir == "BUY":
                sl_price = order_price - target_sl_dist
                tp_price = order_price + target_tp_dist
            else:
                sl_price = order_price + target_sl_dist
                tp_price = order_price - target_tp_dist

            # ── 3. Execute High-Conviction Pure Price Action 3-TF Order ──────
            dp = _sym_digits(symbol)
            event_name = catalyst.get('event', 'Pure PA Flow')
            pa_tag = setup.get('ut_bot', 'PA_3TF')
            mode_desc = f"Pure PA 3-TF | {pa_tag} | H4 Leg {loc_pct}"
            deal_loc_desc = f"H4 Swing Leg: {loc_pct}"
            rr_val = round(target_tp_dist / target_sl_dist, 2) if target_sl_dist > 0 else 1.62

            logger.info(
                f"[PurePA 3-TF] EXECUTING {target_dir} on {symbol}! "
                f"Price: {order_price:.{dp}f} | SL: {sl_price:.{dp}f} (-${max_sl_usd:.0f}) | "
                f"TP: {tp_price:.{dp}f} (+${tp_target_usd:.0f}) | {setup.get('reason')}"
            )

            if execute_order(symbol, target_dir, cfg['lot'], sl_price, tp_price, mode_desc, event_name, setup.get('sweep_level', 0.0), 0.0, deal_range_loc=deal_loc_desc, rr_ratio=rr_val):
                LAST_EXECUTED_BAR[symbol] = cur_bar_ts
                _save_bar_state(LAST_EXECUTED_BAR)


        except Exception as e:
            logger.error(f"[Institutional MTF] Error processing {symbol}: {e}\n{traceback.format_exc()}")


# Backward-compatibility alias (run_scalping_cycle -> run_institutional_cycle)
run_scalping_cycle = run_institutional_cycle
