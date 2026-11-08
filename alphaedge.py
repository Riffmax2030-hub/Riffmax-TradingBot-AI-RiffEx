import os
import sys
import time
import concurrent.futures
import logging
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import sys, os
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, ".agents"))
from metatrader_client import MT5Client
from metatrader_client.order.send_order import send_order
from metatrader_client.types import TradeRequestActions, OrderType
import MetaTrader5 as mt5
from telegram_commands import poll_telegram_commands, BOT_PAUSED

# Set up logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("alphaedge_trading.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("AlphaEdge")

def load_environment_file() -> None:
    """Load local KEY=VALUE settings without adding another dependency."""
    environment_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(environment_path):
        return

    with open(environment_path, encoding="utf-8") as environment_file:
        for line in environment_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()


load_environment_file()

MT5_CONFIG = {
    "login": int(os.environ["MT5_LOGIN"]),
    "password": os.environ["MT5_PASSWORD"],
    "server": os.environ["MT5_SERVER"],
}

ASSET_CONFIG = {
    "XAUUSDm": {"strategies": ["h1_swing"], "timeframes": [mt5.TIMEFRAME_H1], "sessions": ["London", "NY"]},
}
SYMBOLS = list(ASSET_CONFIG.keys())



MAX_DAILY_LOSS_USD = 15.0       # Soft daily warning limit, no hard block
DAILY_PROFIT_TARGET_USD = 50.0  # Daily profit target ($50 for $50 account - 100% growth milestone)
PULLBACK_ATR_FRACTION = 0.5

PENDING_ORDER_EXPIRY_SECONDS = 3600

# Runtime configuration (override with .env)
MODE = os.getenv("AE_MODE", "M1_SCALPING")  # default to M1 scalping
AE_LOT_MULTIPLIER = float(os.getenv("AE_LOT_MULTIPLIER", "1.0"))
AE_MAX_WORKERS = int(os.getenv("AE_MAX_WORKERS", "10"))
AE_RR_MIN = float(os.getenv("AE_RR_MIN", "1.1"))
AE_MAX_CONCURRENT_TRADES = int(os.getenv("AE_MAX_CONCURRENT_TRADES", "2"))
AE_PULLBACK_ATR_FRACTION = 0.5
AE_ATR_SL_MULTIPLIER = 1.0
AE_TRAILING_ENABLE = False
AE_TRAIL_PROFIT_ATR = 1.0
AE_TRAIL_SL_MULT = 0.5

# Gold Scalping Layer — set to False to disable without editing code
ENABLE_SCALPING = os.getenv("AE_SCALPING_ENABLED", "true").lower() == "true"




# Optional fixed-point trailing stop (pips/points). If >0, overrides ATR-based trail.
AE_TRAIL_SL_PIPS = float(os.getenv("AE_TRAIL_SL_PIPS", "0"))

# Map mode to timeframe defaults
if MODE == "M5_FAST":
    MAIN_TIMEFRAME = mt5.TIMEFRAME_M5
    CONFIRM_TIMEFRAME = mt5.TIMEFRAME_M1
    # faster, looser defaults for higher frequency
    AE_PULLBACK_ATR_FRACTION = float(os.getenv("AE_PULLBACK_ATR_FRACTION", "0.3"))
    AE_ATR_SL_MULTIPLIER = float(os.getenv("AE_ATR_SL_MULTIPLIER", "0.8"))
    AE_RR_MIN = float(os.getenv("AE_RR_MIN", "1.5"))
else:
    MAIN_TIMEFRAME = mt5.TIMEFRAME_M30
    CONFIRM_TIMEFRAME = mt5.TIMEFRAME_M5
    AE_PULLBACK_ATR_FRACTION = float(os.getenv("AE_PULLBACK_ATR_FRACTION", "0.5"))
    AE_ATR_SL_MULTIPLIER = float(os.getenv("AE_ATR_SL_MULTIPLIER", "1.0"))
    AE_RR_MIN = float(os.getenv("AE_RR_MIN", "2.0"))

# Backwards-compatible names used later
PULLBACK_ATR_FRACTION = AE_PULLBACK_ATR_FRACTION
ATR_SL_MULTIPLIER = AE_ATR_SL_MULTIPLIER
RISK_REWARD_MIN = AE_RR_MIN
MAX_WORKERS = AE_MAX_WORKERS

from trading_bot_skills.indicators import (
    calculate_bollinger_bands,
    calculate_rsi,
    calculate_ema,
    calculate_atr,
    find_support_resistance,
)
from trading_bot_skills.risk import assess_risk
from trading_bot_skills.token_stub import get_tradingagents_token
from trading_bot_skills.trade_config import TELEGRAM_ENABLED, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# ── Silence noisy third-party loggers so the terminal stays clean ──────────
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

_last_telegram_update_id = 0

def process_telegram_commands():
    """Polls Telegram getUpdates API for incoming commands (/status, /pnl, /help, /stop_scanner, /start_scanner) and responds instantly in plain English."""
    global _last_telegram_update_id
    token = os.getenv("TELEGRAM_TOKEN", "")
    if not token:
        return
    import urllib.request, json, MetaTrader5 as mt5
    from pathlib import Path
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={_last_telegram_update_id + 1}&timeout=1"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                return
            results = data.get("result", [])
            for item in results:
                _last_telegram_update_id = max(_last_telegram_update_id, item.get("update_id", 0))
                msg = item.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")
                if not text or not chat_id:
                    continue
                
                cmd = text.split()[0].lower()
                if cmd in ["/status", "/state"]:
                    acc = mt5.account_info() if mt5.terminal_info() else None
                    bal_str = f"${acc.balance:.2f}" if acc else "N/A"
                    eq_str = f"${acc.equity:.2f}" if acc else "N/A"
                    pos = mt5.positions_get() if acc else None
                    scalp_pos = [p for p in pos if p.magic == 20250831] if pos else []
                    hedge_pos = [p for p in pos if p.magic != 20250831] if pos else []
                    
                    status_reply = (
                        "<b>🟢 AlphaEdge Bot Status Report</b>\n\n"
                        f"• <b>Market:</b> Gold (XAUUSD)\n"
                        f"• <b>Balance:</b> {bal_str}\n"
                        f"• <b>Equity:</b> {eq_str}\n"
                        f"• <b>Active Scalps:</b> {len(scalp_pos)}/3 trades (0.01 lot size)\n"
                        f"• <b>Manual Hedge:</b> {len(hedge_pos)} positions active\n"
                        f"• <b>Scan Speed:</b> 5-Second Real-Time\n"
                        f"• <b>Protections:</b> 24/7 Trading | RSI Retest | Price Action Wicks | Auto-TP1 Lock"
                    )
                    send_telegram_alert(status_reply)

                elif cmd in ["/pnl", "/balance"]:
                    acc = mt5.account_info() if mt5.terminal_info() else None
                    bal = acc.balance if acc else 0
                    eq = acc.equity if acc else 0
                    pnl = eq - bal
                    pnl_reply = (
                        "<b>📊 Account Balance & PnL Summary</b>\n\n"
                        f"• <b>Account Balance:</b> ${bal:.2f}\n"
                        f"• <b>Account Equity:</b> ${eq:.2f}\n"
                        f"• <b>Floating PnL:</b> ${pnl:+.2f}\n"
                    )
                    send_telegram_alert(pnl_reply)

                elif cmd in ["/help", "/start"]:
                    help_reply = (
                        "<b>🤖 AlphaEdge Institutional Engine Telegram Commands</b>\n\n"
                        "• /status — View live bot health, balance, and open position count\n"
                        "• /pnl — View account balance, equity, and floating PnL\n"
                        "• /shadow — View Shadow Tracking performance on EUR/USD, Oil & US30\n"
                        "• /stop_scanner — Pause the scanner\n"
                        "• /start_scanner — Resume the scanner\n"
                        "• /help — Display command menu"
                    )
                    send_telegram_alert(help_reply)

                elif cmd in ["/shadow", "/shadow_report"]:
                    try:
                        from shadow_tracker import generate_shadow_report
                        shadow_rep = generate_shadow_report()
                        send_telegram_alert(shadow_rep)
                    except Exception as shadow_err:
                        send_telegram_alert(f"⚠️ Shadow Report Error: {shadow_err}")

                elif cmd == "/stop_scanner":
                    Path("bot_state.txt").write_text("STOPPED")
                    send_telegram_alert("🛑 <b>Scanner Paused</b>\nScanner is now paused. Send /start_scanner to resume.")

                elif cmd == "/start_scanner":
                    Path("bot_state.txt").write_text("RUNNING")
                    send_telegram_alert("🟢 <b>Scanner Resumed</b>\nScanner is actively monitoring Gold for setups.")

    except Exception as err:
        logger.debug(f"Telegram command check error: {err}")


def _start_telegram_command_listener():
    """Starts a non-blocking background daemon thread to handle Telegram commands with zero lag on trading."""
    import threading, time
    def _worker():
        while True:
            try:
                process_telegram_commands()
            except Exception as e:
                pass
            time.sleep(2)
    t = threading.Thread(target=_worker, daemon=True, name="TelegramListener")
    t.start()
    logger.info("Telegram background command listener initialized.")


def is_trading_session_active() -> bool:
    """
    Day Trading Session Gate:
    Trading window is strictly 8:00 AM to 8:00 PM EAT (Monday to Friday).
    Once it hits 8:00 PM (20:00) EAT, no new trades are opened.
    Overnight trading (8:00 PM to 8:00 AM EAT) and weekends are completely blocked.
    """
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    now_eat = now_utc + timedelta(hours=3)
    weekday = now_eat.weekday()
    hour_eat = now_eat.hour

    if weekday in (5, 6):
        return False
    if weekday == 0:  # Option B: Monday is strictly Observation & Structure memory recording
        return False
    if hour_eat < 5 or hour_eat >= 23:
        return False
    return True









def send_telegram_alert(message: str):
    """Sends system-level alerts to owner DM only (startup, shutdown, daily reports, commands).
    NOT sent to the public channel — channel receives trade signals only."""
    token   = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "915238743")
    if not token or not chat_id:
        logger.warning("Telegram token or chat_id not configured. Alert skipped.")
        return
    import urllib.request
    import json
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            logger.info("Telegram alert sent to owner DM.")
    except Exception as e:
        try:
            payload.pop("parse_mode", None)
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                logger.info("Telegram alert sent to owner DM (plain text fallback).")
        except Exception as e2:
            logger.error(f"Failed to send Telegram alert: {e2}")


def get_lot_size(symbol: str, sl_price: float = 0.0, entry_price: float = 0.0) -> float:
    """
    Calculate lot size based on 0.2% dynamic risk (~$30 per trade on $15K account).
    
    Returns 0.0 if the trade is not viable — i.e., even the broker minimum lot
    would risk more than 1.5x the intended risk ($45). In that case the bot
    skips the trade rather than overexpose the account.
    """
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return 0.0

    vol_min  = symbol_info.volume_min
    vol_max  = symbol_info.volume_max
    vol_step = symbol_info.volume_step

    if sl_price == 0.0 or entry_price == 0.0:
        return vol_min

    account = mt5.account_info()
    if not account:
        return 0.0

    balance = account.balance if account.balance > 0 else 100.0
    risk_usd = max(3.0, balance * 0.03)  # Risk exactly 3% of balance, minimum $3.00

    price_distance = abs(entry_price - sl_price)
    if price_distance == 0:
        return 0.0

    tick_size  = symbol_info.trade_tick_size
    tick_value = symbol_info.trade_tick_value
    if tick_size == 0 or tick_value == 0:
        return 0.0

    ticks_at_risk    = price_distance / tick_size
    loss_per_one_lot = ticks_at_risk * tick_value

    if loss_per_one_lot == 0:
        return 0.0

    optimal_volume = risk_usd / loss_per_one_lot

    # Round to the broker's volume step
    if vol_step > 0:
        optimal_volume = round(optimal_volume / vol_step) * vol_step

    # Clamp between broker min and max
    clamped_volume = max(vol_min, min(optimal_volume, vol_max))

    # -------------------------------------------------------
    # VIABILITY CHECK: If the broker minimum lot forces us to
    # risk more than 1.5x our intended risk, skip the trade.
    # -------------------------------------------------------
    actual_risk_at_min = vol_min * loss_per_one_lot
    max_acceptable_risk = risk_usd * 1.5   # Allow up to 1.5x of the 3% risk

    if optimal_volume < vol_min and actual_risk_at_min > max_acceptable_risk:
        logger.info(
            f"[Lot Sizing] {symbol} SKIPPED — min lot risk ${actual_risk_at_min:.2f} "
            f"exceeds max acceptable ${max_acceptable_risk:.2f}. "
            f"SL distance too wide for safe sizing."
        )
        return 0.0   # Signal to caller: do not place this trade

    decimals = len(str(vol_step).split('.')[1]) if '.' in str(vol_step) else 0
    return round(clamped_volume, decimals)

# Simple test lot size that respects the instrument's minimum volume
def get_test_lot(symbol: str) -> float:
    """Return the minimum allowable volume for the symbol, suitable for test trades.
    """
    info = mt5.symbol_info(symbol)
    if not info:
        return 0.01
    return max(info.volume_min, 0.01)

# Simple trade logger to record trade details to a CSV file
def log_trade(symbol: str, action: str, price: float, sl: float, tp: float, quantity: float, comment: str = ""):
    """Append a trade record to 'trade_log.csv'."""
    import csv, os
    log_path = os.path.join(os.path.dirname(__file__), "trade_log.csv")
    file_exists = os.path.isfile(log_path)
    fields = ["timestamp", "symbol", "action", "price", "sl", "tp", "quantity", "comment"]
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "action": action,
            "price": price,
            "sl": sl,
            "tp": tp,
            "quantity": quantity,
            "comment": comment,
        })






def get_h4_bias(symbol: str) -> str:
    """H4 EMA50/200 bias — the single most important trend filter."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 210)
    if rates is None or len(rates) < 200:
        return "NEUTRAL"
    df_h4 = pd.DataFrame(rates)
    df_h4['ema50']  = calculate_ema(df_h4, 50)
    df_h4['ema200'] = calculate_ema(df_h4, 200)
    last = df_h4.iloc[-1]
    if last['ema50'] > last['ema200']:
        return "BULLISH"
    elif last['ema50'] < last['ema200']:
        return "BEARISH"
    return "NEUTRAL"

def find_supply_demand_zones(df: pd.DataFrame, lookback: int = 100):
    """
    Detect institutional Supply & Demand zones.
    A zone is marked when price makes an impulsive move > 1.8x ATR
    from a base/consolidation candle — the origin of the move.
    """
    zones = []
    for i in range(len(df) - 5, max(len(df) - lookback, 2), -1):
        c_prev2 = df.iloc[i - 2]
        c_curr = df.iloc[i]
        atr_val = c_curr.get('atr', 0)
        if atr_val <= 0:
            continue
        move = c_curr['close'] - c_prev2['open']
        if abs(move) / atr_val > 1.8:
            if move > 0:
                zones.append({
                    "type": "DEMAND",
                    "low": c_prev2['low'],
                    "high": max(c_prev2['close'], c_prev2['open']),
                    "index": i
                })
            else:
                zones.append({
                    "type": "SUPPLY",
                    "high": c_prev2['high'],
                    "low": min(c_prev2['close'], c_prev2['open']),
                    "index": i
                })
    return zones


def find_order_blocks(df: pd.DataFrame, lookback: int = 80):
    """
    Detect institutional Order Blocks (OB).
    - Bullish OB: Last down-candle before strong bullish displacement (>1.5 ATR).
    - Bearish OB: Last up-candle before strong bearish displacement (>1.5 ATR).
    """
    obs = []
    if len(df) < 10:
        return obs
    atr = df['atr'].values if 'atr' in df.columns else np.zeros(len(df))
    close = df['close'].values
    open_ = df['open'].values
    high = df['high'].values
    low = df['low'].values
    n = len(df)
    for i in range(max(2, n - lookback), n - 2):
        a = atr[i]
        if a <= 0 or np.isnan(a):
            continue
        # Bullish OB
        if close[i] < open_[i]:
            impulse = close[i+2] - open_[i+1]
            if impulse > 1.5 * a and close[i+2] > high[i]:
                obs.append({'type': 'BULLISH_OB', 'high': high[i], 'low': low[i], 'index': i})
        # Bearish OB
        elif close[i] > open_[i]:
            impulse = open_[i+1] - close[i+2]
            if impulse > 1.5 * a and close[i+2] < low[i]:
                obs.append({'type': 'BEARISH_OB', 'high': high[i], 'low': low[i], 'index': i})
    return obs


def calculate_poc(df_cons: pd.DataFrame, bins: int = 15) -> float:
    """
    Compute the Volume Profile Point of Control (POC) for a consolidation range.
    Divides price range into discrete volume bins and returns the price of the highest volume node.
    """
    if len(df_cons) == 0:
        return 0.0
    low = df_cons['low'].min()
    high = df_cons['high'].max()
    if high <= low:
        return float((high + low) / 2.0)
    bin_edges = np.linspace(low, high, bins + 1)
    bin_volumes = np.zeros(bins)
    for _, row in df_cons.iterrows():
        c_mid = (row['high'] + row['low']) / 2.0
        vol = row.get('tick_volume', 1)
        b_idx = int(np.clip((c_mid - low) / (high - low) * (bins - 1), 0, bins - 1))
        bin_volumes[b_idx] += vol
    poc_idx = int(np.argmax(bin_volumes))
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2.0
    return float(poc_price)


def analyze_institutional_daytrade_df(df: pd.DataFrame, symbol: str):
    """
    HIGH WIN RATE Institutional Day Trading Strategy -- M15 Timeframe.
    Tested Model 1: 1:1.0 TP Target with 1.2x ATR Wick-Protected Stop Loss.

    5 Mandatory Confluences:
      1. H4 EMA Trend  -- strict BULLISH for buys, strict BEARISH for sells (NEUTRAL = no trade).
      2. M15 EMA Trend -- EMA8 > EMA21 for buys, EMA8 < EMA21 for sells.
      3. Fibonacci 50%  -- price in DISCOUNT (<50% daily range) for buys, PREMIUM (>50%) for sells.
      4. RSI Momentum  -- RSI < 52 for buys, RSI > 48 for sells.
      5. Zone + Candle -- body >= 40% touching/inside institutional S&D zone (0.25 ATR prox).

    SL  = 1.2 ATR beyond zone boundary (maximum wick protection).
    TP  = 1.0 x Risk (1:1.0 R:R) -- fast session fills targeting 65%+ win rate.
    """
    if len(df) < 150:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Insufficient data"

    df = df.copy()
    df = calculate_atr(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]   # Last CLOSED candle -- all pattern logic uses this

    last_close = last["close"]
    last_atr   = last["atr"]
    if last_atr <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "ATR is zero -- no volatility data"

    # 1. H4 TREND BIAS (strict: NEUTRAL = no trade)
    h4_bias = get_h4_bias(symbol) if symbol else "NEUTRAL"

    # 2. S&D ZONES
    zones = find_supply_demand_zones(df, lookback=100)

    # 3. FIBONACCI 50% DAILY RANGE
    daily_window = df.iloc[-80:-1]
    daily_high   = daily_window["high"].max()
    daily_low    = daily_window["low"].min()
    daily_range  = daily_high - daily_low
    if daily_range <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Flat daily range -- no Fib context"
    fib_50 = daily_low + 0.50 * daily_range

    # 4. RSI MOMENTUM FILTER
    df = calculate_rsi(df)
    last_rsi    = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50.0
    rsi_buy_ok  = last_rsi < 55   # Wider RSI → more BUY setups caught
    rsi_sell_ok = last_rsi > 45   # Wider RSI → more SELL setups caught

    # 5. M15 LOCAL TREND (EMA8 vs EMA21)
    df["ema8"]  = calculate_ema(df, 8)
    df["ema21"] = calculate_ema(df, 21)
    ema8        = float(df["ema8"].iloc[-1])
    ema21       = float(df["ema21"].iloc[-1])
    m15_bullish = ema8 > ema21
    m15_bearish = ema8 < ema21

    # CANDLE ANALYSIS on the last CLOSED candle
    prev_open  = prev["open"]
    prev_close = prev["close"]
    prev_high  = prev["high"]
    prev_low   = prev["low"]
    candle_range = prev_high - prev_low
    if candle_range <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Invalid candle range"

    is_green = prev_close > prev_open
    is_red   = prev_close < prev_open
    body_pct = abs(prev_close - prev_open) / candle_range

    # >=40% body confirmation
    buy_candle_ok  = is_green and body_pct >= 0.40
    sell_candle_ok = is_red   and body_pct >= 0.40

    # Zone proximity: 0.30 ATR — wider catch radius for more trade opportunities
    zone_prox = 0.30 * last_atr

    # BUY SETUP -- all 5 confluences must be TRUE
    if (h4_bias == "BULLISH"
            and m15_bullish
            and last_close < fib_50
            and rsi_buy_ok
            and buy_candle_ok):
        for z in zones:
            if z["type"] == "DEMAND":
                if (prev_low <= z["high"] + zone_prox
                        and prev_close > z["low"] - zone_prox):
                    sl   = min(prev_low, z["low"]) - 1.2 * last_atr
                    risk = last_close - sl
                    if risk > 0:
                        tp = last_close + 1.0 * risk
                        sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
                        rr = (tp - last_close) / (last_close - sl) if (last_close - sl) > 0 else 0
                        detail = (
                            f"HIGH-WIN-RATE BUY | Demand [{z['low']:.5f}-{z['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} | RSI={last_rsi:.0f} | H4=BULL | M15=BULL | "
                            f"Body={body_pct*100:.0f}% | SL=1.2xATR | R:R 1:{rr:.2f}"
                        )
                        return "BUY", sl, tp, last_close, detail

    # SELL SETUP -- all 5 confluences must be TRUE
    if (h4_bias == "BEARISH"
            and m15_bearish
            and last_close > fib_50
            and rsi_sell_ok
            and sell_candle_ok):
        for z in zones:
            if z["type"] == "SUPPLY":
                if (prev_high >= z["low"] - zone_prox
                        and prev_close < z["high"] + zone_prox):
                    sl   = max(prev_high, z["high"]) + 1.2 * last_atr
                    risk = sl - last_close
                    if risk > 0:
                        tp = last_close - 1.0 * risk
                        sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
                        rr = (last_close - tp) / (sl - last_close) if (sl - last_close) > 0 else 0
                        detail = (
                            f"HIGH-WIN-RATE SELL | Supply [{z['low']:.5f}-{z['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} | RSI={last_rsi:.0f} | H4=BEAR | M15=BEAR | "
                            f"Body={body_pct*100:.0f}% | SL=1.2xATR | R:R 1:{rr:.2f}"
                        )
                        return "SELL", sl, tp, last_close, detail

    m15_label = "BULL" if m15_bullish else "BEAR"
    return (
        "NEUTRAL", 0.0, 0.0, 0.0,
        f"No setup | Fib50={fib_50:.5f} | Close={last_close:.5f} | RSI={last_rsi:.0f} | "
        f"H4={h4_bias} | M15={m15_label} | Green={is_green} Red={is_red} Body={body_pct*100:.0f}%"
    )


def analyze_liquidity_reversion_df(df: pd.DataFrame, symbol: str | None = None):
    """Liquidity sweep mean-reversion entry logic."""
    if df is None or len(df) < 70:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Insufficient data"

    df = df.copy()
    df = calculate_bollinger_bands(df)
    df = calculate_rsi(df)
    df = calculate_atr(df)
    df['ema8'] = calculate_ema(df, 8)
    df['ema21'] = calculate_ema(df, 21)

    last = df.iloc[-1]    # Live (forming) candle — used only for current price/EMA/ATR
    prev = df.iloc[-2]    # Last CLOSED candle — used for all structural pattern geometry
    prior_swing = df.iloc[-50:-10] if len(df) >= 60 else df.iloc[:-10]
    if len(prior_swing) < 20:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Not enough swing history"

    swing_low = prior_swing['low'].min()
    swing_high = prior_swing['high'].max()
    bb_mid = last['bb_mid']
    bb_lower = last['bb_lower']
    bb_upper = last['bb_upper']
    last_atr = last['atr']
    last_ema8 = last['ema8']
    last_ema21 = last['ema21']

    # Entry price: use current live price for fills
    last_close = last['close']
    last_rsi = last['rsi']

    # --- All candlestick geometry uses the PREVIOUS CLOSED candle ---
    prev_close = prev['close']
    prev_open  = prev['open']
    prev_high  = prev['high']
    prev_low   = prev['low']
    prev_rsi   = prev['rsi']

    body_size  = abs(prev_close - prev_open)
    lower_wick = min(prev_close, prev_open) - prev_low
    upper_wick = prev_high - max(prev_close, prev_open)

    # Correct direction: prev candle swept low AND closed bullish = bullish rejection confirmed
    bullish_rejection = prev_close > prev_open   # prev candle closed bullish
    bearish_rejection = prev_close < prev_open   # prev candle closed bearish

    sweep_buy = (
        prev_low < swing_low                        # prev candle swept below swing low
        and prev_close > swing_low                  # and CLOSED back above it
    )
    sweep_sell = (
        prev_high > swing_high                      # prev candle swept above swing high
        and prev_close < swing_high                  # and CLOSED back below it
    )

    ema_buy_ok = last_ema8 <= last_ema21
    ema_sell_ok = last_ema8 >= last_ema21

    # H4 bias — only trade with the dominant trend
    h4_bias = "NEUTRAL"
    if symbol:
        h4_bias = get_h4_bias(symbol)
    h4_buy_ok  = h4_bias in ["BULLISH", "NEUTRAL"]
    h4_sell_ok = h4_bias in ["BEARISH", "NEUTRAL"]

    # Volume — rejection candle must show institutional participation
    prev_vol = prev['tick_volume'] if 'tick_volume' in prev.index else 0
    avg_vol  = df['tick_volume'].iloc[-21:-1].mean() if len(df) >= 21 else df['tick_volume'].mean()
    vol_ok   = prev_vol >= 1.1 * avg_vol

    h1_buy_ok = True
    h1_sell_ok = True
    h1_status = "H1 unchecked"
    if symbol is not None:
        h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 30)
        if h1_rates is not None and len(h1_rates) >= 20:
            df_h1 = pd.DataFrame(h1_rates)
            df_h1['sma20'] = df_h1['close'].rolling(window=20).mean()
            df_h1 = calculate_rsi(df_h1)
            last_h1_close = df_h1['close'].iloc[-1]
            last_h1_sma20 = df_h1['sma20'].iloc[-1]
            last_h1_rsi = df_h1['rsi'].iloc[-1]
            if last_h1_close < last_h1_sma20 and last_h1_rsi < 45:
                h1_buy_ok = False
                h1_status = f"H1 bear continuation ({last_h1_rsi:.1f})"
            elif last_h1_close > last_h1_sma20 and last_h1_rsi > 55:
                h1_sell_ok = False
                h1_status = f"H1 bull continuation ({last_h1_rsi:.1f})"
            else:
                h1_status = f"H1 friendly ({last_h1_rsi:.1f})"

    action = "NEUTRAL"
    sl = 0.0
    tp = 0.0
    entry_price = last_close
    details = f"RSI {last_rsi:.1f} | EMA8/21 {last_ema8:.5f}/{last_ema21:.5f} | H1 {h1_status}"

    # Fallback: extreme BB/RSI conditions on the CLOSED candle only
    buy_pullback = prev_close <= bb_lower and prev_rsi <= 30
    sell_pullback = prev_close >= bb_upper and prev_rsi >= 70
    basic_buy = buy_pullback and last_ema8 <= last_ema21
    basic_sell = sell_pullback and last_ema8 >= last_ema21
    rr_threshold = 2.0

    if sweep_buy and ema_buy_ok and h1_buy_ok:
        sl = prev_low - (2.0 * last_atr)   # Below wick low + 2 ATR — smaller lots, wider breath
        tp = max(bb_mid, last_close + max(2.5 * (last_close - sl), 0.6 * (swing_high - last_close)))
        sl, tp = assess_risk("BUY", sl, tp, entry_price, last_atr, risk_level="neutral")
        risk = entry_price - sl
        reward = tp - entry_price
        if risk > 0 and reward > 0 and (reward / risk) >= rr_threshold:
            action = "BUY"
            details = f"Liquidity sweep BUY. Sweep low {swing_low:.5f}. RSI {last_rsi:.1f}. H1 {h1_status}. R:R {reward/risk:.2f}"
        else:
            details = f"BUY sweep rejected by R:R ({reward/risk:.2f})."

    elif basic_buy and h1_buy_ok:
        sl = prev_low - (2.0 * last_atr)   # Below wick low + 2 ATR — smaller lots, wider breath
        tp = last_close + max(2.5 * (last_close - sl), bb_mid - last_close, 0.6 * (swing_high - last_close))
        sl, tp = assess_risk("BUY", sl, tp, entry_price, last_atr, risk_level="neutral")
        risk = entry_price - sl
        reward = tp - entry_price
        if risk > 0 and reward > 0 and (reward / risk) >= rr_threshold:
            action = "BUY"
            details = f"Aggressive BUY. Close {last_close:.5f} below BB lower {bb_lower:.5f} or RSI {last_rsi:.1f}. H1 {h1_status}. R:R {reward/risk:.2f}"
        else:
            details = f"Aggressive BUY rejected by R:R ({reward/risk:.2f})."

    elif sweep_sell and ema_sell_ok and h1_sell_ok:
        sl = prev_high + (2.0 * last_atr)  # Above wick high + 2 ATR — smaller lots, wider breath
        tp = min(bb_mid, last_close - max(2.5 * (sl - last_close), 0.6 * (last_close - swing_low)))
        sl, tp = assess_risk("SELL", sl, tp, entry_price, last_atr, risk_level="neutral")
        risk = sl - entry_price
        reward = entry_price - tp
        if risk > 0 and reward > 0 and (reward / risk) >= rr_threshold:
            action = "SELL"
            details = f"Liquidity sweep SELL. Sweep high {swing_high:.5f}. RSI {last_rsi:.1f}. H1 {h1_status}. R:R {reward/risk:.2f}"
        else:
            details = f"SELL sweep rejected by R:R ({reward/risk:.2f})."

    elif basic_sell and h1_sell_ok:
        sl = prev_high + (3.5 * last_atr)
        tp = last_close - max(2.5 * last_atr, last_close - bb_mid, 0.6 * (last_close - swing_low))
        sl, tp = assess_risk("SELL", sl, tp, entry_price, last_atr, risk_level="neutral")
        risk = sl - entry_price
        reward = entry_price - tp
        if risk > 0 and reward > 0 and (reward / risk) >= rr_threshold:
            action = "SELL"
            details = f"Aggressive SELL. Close {last_close:.5f} above BB upper {bb_upper:.5f} or RSI {last_rsi:.1f}. H1 {h1_status}. R:R {reward/risk:.2f}"
        else:
            details = f"Aggressive SELL rejected by R:R ({reward/risk:.2f})."

    else:
        details = f"No sweep setup. RSI {last_rsi:.1f}. EMA8/21 {last_ema8:.5f}/{last_ema21:.5f}."

    return action, sl, tp, entry_price, details


def analyze_amd_poc_pullback_df(df: pd.DataFrame, symbol: str):
    """
    AMD (Accumulation, Manipulation, Distribution) + Point of Control (POC) Pullback Sequence:
    1. Identify Consolidation (Accumulation range).
    2. Mark the Volume Profile POC (Point of Control).
    3. Locate Manipulation (Judas swing / Liquidity sweep).
    4. Locate Distribution (Impulsive displacement away from manipulation).
    5. Wait for price to pull back to retest the POC (NO chasing entries).
    """
    if len(df) < 60:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Insufficient data for AMD POC"
        
    df = df.copy()
    df = calculate_atr(df)
    df = calculate_rsi(df)
    df['ema8'] = calculate_ema(df, 8)
    df['ema21'] = calculate_ema(df, 21)
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_close = last['close']
    last_atr = last['atr']
    if last_atr <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "ATR is zero"

    # 1. Identify Prior Consolidation (Accumulation Phase: bars -40 to -12)
    cons = df.iloc[-40:-12]
    c_high = cons['high'].max()
    c_low = cons['low'].min()
    c_range = c_high - c_low
    if c_range <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Flat consolidation"

    # 2. Mark Point of Control (POC) using Volume Profile
    poc = calculate_poc(cons)

    # 3. Locate Manipulation & 4. Distribution in the recent leg (bars -12 to -2)
    mid_leg = df.iloc[-12:-2]
    leg_min = mid_leg['low'].min()
    leg_max = mid_leg['high'].max()

    # 5. Check Pullback to POC (No Chasing)
    # Gold has wider ATR — use a looser proximity threshold so more retests are caught
    poc_prox = 0.55 * last_atr if symbol == "XAUUSDz" else 0.35 * last_atr
    near_poc = (abs(last_close - poc) <= poc_prox) or (prev['low'] <= poc + poc_prox and prev['high'] >= poc - poc_prox)

    # Dominant H4 Trend Context
    h4_bias = get_h4_bias(symbol) if symbol else "NEUTRAL"
    h4_buy_ok = h4_bias in ["BULLISH", "NEUTRAL"]
    h4_sell_ok = h4_bias in ["BEARISH", "NEUTRAL"]

    # --- BULLISH AMD SEQUENCE ---
    # Manipulation swept below c_low, Distribution pushed up above c_high, now pulling back to retest POC
    if leg_min < c_low and leg_max > c_high and near_poc and h4_buy_ok and last_close >= poc - poc_prox:
        sl = leg_min - 1.2 * last_atr
        risk = last_close - sl
        if risk > 0:
            tp = max(leg_max, last_close + 1.8 * risk)
            sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
            rr = (tp - last_close) / (last_close - sl) if (last_close - sl) > 0 else 1.8
            return "BUY", sl, tp, last_close, (
                f"AMD POC Pullback BUY | Consolidation [{c_low:.5f}-{c_high:.5f}] | "
                f"POC={poc:.5f} (Retested) | ManipLow={leg_min:.5f} | H4={h4_bias} | R:R 1:{rr:.2f}"
            )

    # --- BEARISH AMD SEQUENCE ---
    # Manipulation swept above c_high, Distribution pushed down below c_low, now pulling back to retest POC
    if leg_max > c_high and leg_min < c_low and near_poc and h4_sell_ok and last_close <= poc + poc_prox:
        sl = leg_max + 1.2 * last_atr
        risk = sl - last_close
        if risk > 0:
            tp = min(leg_min, last_close - 1.8 * risk)
            sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
            rr = (last_close - tp) / (sl - last_close) if (sl - last_close) > 0 else 1.8
            return "SELL", sl, tp, last_close, (
                f"AMD POC Pullback SELL | Consolidation [{c_low:.5f}-{c_high:.5f}] | "
                f"POC={poc:.5f} (Retested) | ManipHigh={leg_max:.5f} | H4={h4_bias} | R:R 1:{rr:.2f}"
            )

    return "NEUTRAL", 0.0, 0.0, 0.0, f"No AMD setup | POC={poc:.5f} | Consolidation=[{c_low:.5f}-{c_high:.5f}]"


def analyze_core_system_df(df: pd.DataFrame, symbol: str):
    """
    Core System Strategy:
    1. Order Block (OB) & Supply/Demand (S&D) mitigation in Fibonacci Golden Pocket (Discount/Premium).
    2. Previous Day High/Low (PDH/PDL) Liquidity Sweep Mean Reversion.
    3. H4 dominant trend filter.
    """
    if len(df) < 100:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Insufficient data for Core System"
        
    df = df.copy()
    df = calculate_atr(df)
    df = calculate_rsi(df)
    df['ema8'] = calculate_ema(df, 8)
    df['ema21'] = calculate_ema(df, 21)
    df['ema50'] = calculate_ema(df, 50)
    df['ema200'] = calculate_ema(df, 200)

    last = df.iloc[-1]
    prev = df.iloc[-2]   # Last closed candle for structural validation
    
    last_close = last['close']
    last_atr = last['atr']
    if last_atr <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "ATR is zero"

    prev_open = prev['open']
    prev_close = prev['close']
    prev_high = prev['high']
    prev_low = prev['low']
    candle_range = prev_high - prev_low
    if candle_range <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Flat candle"

    body_pct = abs(prev_close - prev_open) / candle_range
    is_green = prev_close > prev_open
    is_red = prev_close < prev_open

    # 1. H4 Trend Bias
    h4_bias = get_h4_bias(symbol) if symbol else "NEUTRAL"
    h4_buy_ok = h4_bias in ["BULLISH", "NEUTRAL"]
    h4_sell_ok = h4_bias in ["BEARISH", "NEUTRAL"]

    # 2. Fibonacci 50% - 61.8% Golden Pocket Context
    daily_window = df.iloc[-80:-1]
    swing_high = daily_window['high'].max()
    swing_low = daily_window['low'].min()
    daily_range = swing_high - swing_low
    fib_50 = swing_low + 0.50 * daily_range if daily_range > 0 else last_close

    # 3. Order Blocks & Supply/Demand Detection
    # Gold-specific: wider zone proximity (0.45 ATR) and lower body_pct threshold (0.25)
    # so more valid OB/SD touches are caught on Gold's naturally wide-ranging candles
    is_gold = (symbol == "XAUUSDz")
    order_blocks = find_order_blocks(df, lookback=80)
    sd_zones = find_supply_demand_zones(df, lookback=80)
    zone_prox = 0.45 * last_atr if is_gold else 0.30 * last_atr
    min_body_pct = 0.25 if is_gold else 0.35

    # --- SETUP A: Bullish Order Block / Demand in Discount (< 50% Fib) ---
    if h4_buy_ok and last_close <= fib_50 and is_green and body_pct >= min_body_pct:
        # Check Bullish Order Blocks
        for ob in reversed(order_blocks):
            if ob['type'] == 'BULLISH_OB':
                if prev_low <= ob['high'] + zone_prox and prev_close >= ob['low'] - zone_prox:
                    sl = min(prev_low, ob['low']) - 1.2 * last_atr
                    risk = last_close - sl
                    if risk > 0:
                        tp = last_close + 1.5 * risk
                        sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
                        rr = (tp - last_close) / (last_close - sl) if (last_close - sl) > 0 else 1.5
                        return "BUY", sl, tp, last_close, (
                            f"Core OB BUY | Bullish OB [{ob['low']:.5f}-{ob['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} (Discount) | H4={h4_bias} | R:R 1:{rr:.2f}"
                        )
        # Check Demand Zones
        for z in reversed(sd_zones):
            if z['type'] == 'DEMAND':
                if prev_low <= z['high'] + zone_prox and prev_close >= z['low'] - zone_prox:
                    sl = min(prev_low, z['low']) - 1.2 * last_atr
                    risk = last_close - sl
                    if risk > 0:
                        tp = last_close + 1.5 * risk
                        sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
                        rr = (tp - last_close) / (last_close - sl) if (last_close - sl) > 0 else 1.5
                        return "BUY", sl, tp, last_close, (
                            f"Core Demand BUY | Demand [{z['low']:.5f}-{z['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} (Discount) | H4={h4_bias} | R:R 1:{rr:.2f}"
                        )

    # --- SETUP B: Bearish Order Block / Supply in Premium (> 50% Fib) ---
    if h4_sell_ok and last_close >= fib_50 and is_red and body_pct >= min_body_pct:
        # Check Bearish Order Blocks
        for ob in reversed(order_blocks):
            if ob['type'] == 'BEARISH_OB':
                if prev_high >= ob['low'] - zone_prox and prev_close <= ob['high'] + zone_prox:
                    sl = max(prev_high, ob['high']) + 1.2 * last_atr
                    risk = sl - last_close
                    if risk > 0:
                        tp = last_close - 1.5 * risk
                        sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
                        rr = (last_close - tp) / (sl - last_close) if (sl - last_close) > 0 else 1.5
                        return "SELL", sl, tp, last_close, (
                            f"Core OB SELL | Bearish OB [{ob['low']:.5f}-{ob['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} (Premium) | H4={h4_bias} | R:R 1:{rr:.2f}"
                        )
        # Check Supply Zones
        for z in reversed(sd_zones):
            if z['type'] == 'SUPPLY':
                if prev_high >= z['low'] - zone_prox and prev_close <= z['high'] + zone_prox:
                    sl = max(prev_high, z['high']) + 1.2 * last_atr
                    risk = sl - last_close
                    if risk > 0:
                        tp = last_close - 1.5 * risk
                        sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
                        rr = (last_close - tp) / (sl - last_close) if (sl - last_close) > 0 else 1.5
                        return "SELL", sl, tp, last_close, (
                            f"Core Supply SELL | Supply [{z['low']:.5f}-{z['high']:.5f}] | "
                            f"Fib50={fib_50:.5f} (Premium) | H4={h4_bias} | R:R 1:{rr:.2f}"
                        )

    # --- SETUP C: Previous Day High/Low (PDH/PDL) Sweep Mean Reversion ---
    pdh, pdl = 0.0, float('inf')
    rates_d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 2)
    if rates_d1 is not None and len(rates_d1) > 0:
        yesterday = rates_d1[0]
        pdh = yesterday['high']
        pdl = yesterday['low']

    recent_15 = df.iloc[-15:]
    sweep_high = recent_15['high'].max()
    sweep_low = recent_15['low'].min()

    if sweep_high >= pdh and pdh > 0 and h4_sell_ok:
        if prev_close < pdh and is_red:
            sl = max(prev_high, sweep_high) + 1.2 * last_atr
            risk = sl - last_close
            if risk > 0:
                tp = last_close - 1.5 * risk
                sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
                return "SELL", sl, tp, last_close, f"Core: PDH Liquidity Sweep Mean Reversion SELL (PDH {pdh:.5f})"

    if sweep_low <= pdl and pdl < float('inf') and h4_buy_ok:
        if prev_close > pdl and is_green:
            sl = min(prev_low, sweep_low) - 1.2 * last_atr
            risk = last_close - sl
            if risk > 0:
                tp = last_close + 1.5 * risk
                sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
                return "BUY", sl, tp, last_close, f"Core: PDL Liquidity Sweep Mean Reversion BUY (PDL {pdl:.5f})"

    return "NEUTRAL", 0.0, 0.0, 0.0, f"No core setup | Fib50={fib_50:.5f} | H4={h4_bias}"


def analyze_ut_liquidity_df(symbol, df, tick):
    """
    TradingAgents Multi-Agent Consensus & Adversarial Risk Gate Engine v2.0
    (Inspired by TauricResearch/TradingAgents Architecture)

    1. Session Gate: Only trade London (07:00-12:00 UTC) + NY (12:00-20:00 UTC)
    2. Macro & Sentiment Agent (Max 35 pts): H1 200/50 EMA + H1 UT Stop
    3. Technical Analyst Agent (Max 40 pts): M1 UT Bot Crossover (ATR 7, Key 2.0)
    4. Mid-Frame Confirmation (Max 5 pts bonus): M5 EMA8/21 alignment
    5. Adversarial Debater Agent (Max 15 pts): Wick Rejection Stress-Test
    6. Risk Manager Gate (Max 10 pts + Full Veto): Spread Sanity + Score >= 75/100
    """
    try:
        import pandas as pd
        import numpy as np
        from datetime import datetime, timezone

        conviction_score = 0
        reasons = []

        # =========================================================================
        # 0. SESSION GATE — 24/5 All-Day & Overnight Mode
        #    Only block during weekend market close (Fri 21:00 UTC - Sun 22:00 UTC)
        # =========================================================================
        if not is_trading_session_active():
            return None  # Weekend Gate VETO: Market is closed for the weekend

        # =========================================================================
        # 1. MACRO & SENTIMENT AGENT (H1 Trend Structure)
        # =========================================================================
        h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 250)
        if h1_rates is None or len(h1_rates) < 100:
            return None
        df_h1 = pd.DataFrame(h1_rates)
        df_h1['ema200'] = df_h1['close'].ewm(span=200, adjust=False).mean()
        df_h1['ema50'] = df_h1['close'].ewm(span=50, adjust=False).mean()

        df_h1['prev_close'] = df_h1['close'].shift(1)
        df_h1['tr'] = df_h1[['high', 'low', 'prev_close']].apply(
            lambda r: max(r['high'] - r['low'], abs(r['high'] - r['prev_close']), abs(r['low'] - r['prev_close'])), axis=1)
        df_h1['atr_7'] = df_h1['tr'].rolling(window=7).mean()

        h1_closes = df_h1['close'].values
        h1_atrs = df_h1['atr_7'].values
        h1_stops = np.zeros(len(h1_closes))
        for i in range(1, len(h1_closes)):
            if np.isnan(h1_atrs[i]): continue
            nLoss = 2.0 * h1_atrs[i]
            ps = h1_stops[i-1]
            pc = h1_closes[i-1]
            cc = h1_closes[i]
            if cc > ps and pc > ps: h1_stops[i] = max(ps, cc - nLoss)
            elif cc < ps and pc < ps: h1_stops[i] = min(ps, cc + nLoss)
            elif cc > ps: h1_stops[i] = cc - nLoss
            else: h1_stops[i] = cc + nLoss

        last_h1_close = h1_closes[-1]
        last_h1_ema50 = df_h1['ema50'].iloc[-1]
        last_h1_ema200 = df_h1['ema200'].iloc[-1]
        last_h1_stop = h1_stops[-1]

        h1_bias = "NEUTRAL"
        if last_h1_close > last_h1_ema50 and last_h1_close > last_h1_stop:
            h1_bias = "BULLISH"
            conviction_score += 25
            reasons.append("H1 Bullish (+25)")
            if last_h1_close > last_h1_ema200:
                conviction_score += 10
                reasons.append("Above 200 EMA (+10)")
        elif last_h1_close < last_h1_ema50 and last_h1_close < last_h1_stop:
            h1_bias = "BEARISH"
            conviction_score += 25
            reasons.append("H1 Bearish (+25)")
            if last_h1_close < last_h1_ema200:
                conviction_score += 10
                reasons.append("Below 200 EMA (+10)")

        if h1_bias == "NEUTRAL":
            return None  # Macro Analyst VETO: Market is indecisive

        # =========================================================================
        # 2. TECHNICAL ANALYST AGENT (M1 Precision Execution)
        # =========================================================================
        m1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 150)
        if m1_rates is None or len(m1_rates) < 20:
            return None
        df_m1 = pd.DataFrame(m1_rates)

        df_m1['prev_close'] = df_m1['close'].shift(1)
        df_m1['tr'] = df_m1[['high', 'low', 'prev_close']].apply(
            lambda r: max(r['high'] - r['low'], abs(r['high'] - r['prev_close']), abs(r['low'] - r['prev_close'])), axis=1)
        df_m1['atr_10'] = df_m1['tr'].ewm(alpha=1.0/10, adjust=False).mean()

        m1_closes = df_m1['close'].values
        m1_atrs = df_m1['atr_10'].values
        m1_stops = np.zeros(len(m1_closes))
        for i in range(1, len(m1_closes)):
            if np.isnan(m1_atrs[i]): continue
            nLoss = 3.0 * m1_atrs[i]
            ps = m1_stops[i-1]
            pc = m1_closes[i-1]
            cc = m1_closes[i]
            if cc > ps and pc > ps: m1_stops[i] = max(ps, cc - nLoss)
            elif cc < ps and pc < ps: m1_stops[i] = min(ps, cc + nLoss)
            elif cc > ps: m1_stops[i] = cc - nLoss
            else: m1_stops[i] = cc + nLoss

        i = len(m1_closes) - 1
        curr_close = m1_closes[i]
        prev_close = m1_closes[i - 1]
        prev_stop = m1_stops[i - 1]
        curr_stop = m1_stops[i]
        curr_atr = m1_atrs[i]

        if np.isnan(curr_atr) or curr_atr == 0:
            return None

        cross_up = prev_close <= prev_stop and curr_close > curr_stop
        cross_dn = prev_close >= prev_stop and curr_close < curr_stop

        strong_buy = cross_up and curr_close > curr_stop
        strong_sell = cross_dn and curr_close < curr_stop

        buy_buffer = strong_buy and curr_close > curr_stop
        sell_buffer = strong_sell and curr_close < curr_stop

        proposed_action = None
        if buy_buffer and h1_bias == "BULLISH":
            proposed_action = "BUY"
            conviction_score += 40
            reasons.append("M1 UT Cross UP (+40)")
        elif sell_buffer and h1_bias == "BEARISH":
            proposed_action = "SELL"
            conviction_score += 40
            reasons.append("M1 UT Cross DOWN (+40)")
        else:
            return None

        # =========================================================================
        # 2b. MID-FRAME CONFIRMATION AGENT (+5 bonus — M5 EMA8/21 alignment)
        #     Tightens signal quality by ensuring M5 trend agrees with M1 cross
        # =========================================================================
        try:
            m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 50)
            if m5_rates is not None and len(m5_rates) >= 22:
                df_m5 = pd.DataFrame(m5_rates)
                df_m5['ema8'] = df_m5['close'].ewm(span=8, adjust=False).mean()
                df_m5['ema21'] = df_m5['close'].ewm(span=21, adjust=False).mean()
                m5_ema8 = df_m5['ema8'].iloc[-1]
                m5_ema21 = df_m5['ema21'].iloc[-1]
                if proposed_action == "BUY" and m5_ema8 > m5_ema21:
                    conviction_score += 5
                    reasons.append("M5 EMA Aligned (+5)")
                elif proposed_action == "SELL" and m5_ema8 < m5_ema21:
                    conviction_score += 5
                    reasons.append("M5 EMA Aligned (+5)")
        except Exception:
            pass  # Non-critical: skip if M5 fetch fails

        # =========================================================================
        # 3. ADVERSARIAL DEBATER AGENT (Stress-Test & Wick Rejection)
        # =========================================================================
        curr_open = df_m1['open'].iloc[-1]
        curr_high = df_m1['high'].iloc[-1]
        curr_low = df_m1['low'].iloc[-1]
        candle_range = max(curr_high - curr_low, 0.00001)

        if proposed_action == "BUY":
            upper_wick = curr_high - max(curr_open, curr_close)
            if upper_wick / candle_range > 0.45:
                # Bear researcher wins debate: severe top wick rejection
                return None
            conviction_score += 15
            reasons.append("Debate: Clean Bullish Expansion (+15)")

        elif proposed_action == "SELL":
            lower_wick = min(curr_open, curr_close) - curr_low
            if lower_wick / candle_range > 0.45:
                # Bull researcher wins debate: severe bottom wick absorption
                return None
            conviction_score += 15
            reasons.append("Debate: Clean Bearish Expansion (+15)")

        # =========================================================================
        # 4. RISK MANAGER GATE (Spread Sanity & Final Score Execution)
        # =========================================================================
        symbol_info = mt5.symbol_info(symbol)
        spread_val = (symbol_info.spread * symbol_info.point) if symbol_info else 0.0

        if spread_val > (0.45 * curr_atr):
            # Risk Manager VETO: Spread is too wide compared to M1 ATR
            logger.warning(f"[Risk Gate] {symbol} {proposed_action} VETOED: Spread {spread_val:.5f} exceeds 45% of M1 ATR {curr_atr:.5f}")
            return None

        conviction_score += 10
        reasons.append("Risk Gate Approved (+10)")

        # Final Conviction Threshold Check (Requires >= 75/100)
        if conviction_score < 75:
            return None

        # Calculate Execution Parameters
        sl_dist = 2.5 * curr_atr
        tp_dist = 4.0 * curr_atr

        if proposed_action == "BUY":
            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
            return {
                'action': 'BUY',
                'sl': sl,
                'tp': tp,
                'reason': f'TradingAgents BUY (Score: {conviction_score}/100) | {", ".join(reasons)} | ATR {curr_atr:.2f}'
            }
        elif proposed_action == "SELL":
            sl = curr_close + sl_dist
            tp = curr_close - tp_dist
            return {
                'action': 'SELL',
                'sl': sl,
                'tp': tp,
                'reason': f'TradingAgents SELL (Score: {conviction_score}/100) | {", ".join(reasons)} | ATR {curr_atr:.2f}'
            }

        return None

    except Exception as e:
        logger.error(f'Error in TradingAgents engine for {symbol}: {e}')
        return None


def analyze_breakout_df(df: pd.DataFrame, symbol: str):
    """
    Volatility Expansion & Structural Breakout Strategy:
    1. Clean close outside Bollinger Bands or 20-bar consolidation range.
    2. Aligned with dominant H4 trend.
    3. Strong directional candle body (>40%).
    """
    if len(df) < 50:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Insufficient data for Breakout"
    df = df.copy()
    df = calculate_bollinger_bands(df)
    df = calculate_atr(df)
    df['ema8'] = calculate_ema(df, 8)
    df['ema21'] = calculate_ema(df, 21)
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    last_close = last['close']
    last_atr = last['atr']
    if last_atr <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "ATR is zero"

    prev_open = prev['open']
    prev_close = prev['close']
    prev_high = prev['high']
    prev_low = prev['low']
    candle_range = prev_high - prev_low
    if candle_range <= 0:
        return "NEUTRAL", 0.0, 0.0, 0.0, "Flat candle"

    body_pct = abs(prev_close - prev_open) / candle_range
    is_green = prev_close > prev_open
    is_red = prev_close < prev_open

    h4_bias = get_h4_bias(symbol) if symbol else "NEUTRAL"
    h4_buy_ok = h4_bias in ["BULLISH", "NEUTRAL"]
    h4_sell_ok = h4_bias in ["BEARISH", "NEUTRAL"]

    # Bullish Breakout
    if last_close > last['bb_upper'] and prev_close <= prev['bb_upper'] and h4_buy_ok and is_green and body_pct >= 0.40:
        sl = last['bb_mid'] - (0.5 * last_atr)
        risk = last_close - sl
        if risk > 0:
            tp = last_close + 2.0 * risk
            sl, tp = assess_risk("BUY", sl, tp, last_close, last_atr, "neutral")
            return "BUY", sl, tp, last_close, f"Breakout BUY | Upper BB Expansion | H4={h4_bias} | Body={body_pct*100:.0f}% | R:R 1:2.0"
        
    # Bearish Breakout
    if last_close < last['bb_lower'] and prev_close >= prev['bb_lower'] and h4_sell_ok and is_red and body_pct >= 0.40:
        sl = last['bb_mid'] + (0.5 * last_atr)
        risk = sl - last_close
        if risk > 0:
            tp = last_close - 2.0 * risk
            sl, tp = assess_risk("SELL", sl, tp, last_close, last_atr, "neutral")
            return "SELL", sl, tp, last_close, f"Breakout SELL | Lower BB Expansion | H4={h4_bias} | Body={body_pct*100:.0f}% | R:R 1:2.0"
        
    return "NEUTRAL", 0.0, 0.0, 0.0, f"No breakout | H4={h4_bias}"

def analyze_strategies(symbol: str):
    config = ASSET_CONFIG.get(symbol, {"strategies": ["liquidity_sweep"], "timeframes": [MAIN_TIMEFRAME]})
    strategies = config["strategies"]
    timeframes = config["timeframes"]
    
    best_action = "NEUTRAL"
    best_result = ("NEUTRAL", 0.0, 0.0, 0.0, "No setup found across any strategy or timeframe", "NONE")
    
    try:
        for tf in timeframes:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, 250)
            if rates is None or len(rates) < 70:
                continue
            df = pd.DataFrame(rates)
            
            for strategy in strategies:
                if strategy in ["amd_poc", "amd_poc_pullback"]:
                    res = analyze_amd_poc_pullback_df(df, symbol)
                elif strategy == "institutional_daytrade":
                    res = analyze_institutional_daytrade_df(df, symbol)
                elif strategy == "core_system":
                    # ARCHIVED — not active (kept for reference only)
                    res = ("NEUTRAL", 0.0, 0.0, 0.0, "Core OB archived")
                elif strategy in ["liquidity_sweep", "amd_poc_pullback", "amd_poc"]:
                    # ARCHIVED — not active (kept for reference only)
                    res = ("NEUTRAL", 0.0, 0.0, 0.0, "AMD/Sweep archived")
                elif strategy == "ut_liquidity":
                    tick = mt5.symbol_info_tick(symbol)
                    ut_res = analyze_ut_liquidity_df(symbol, df, tick)
                    if ut_res and ut_res.get("action") in ["BUY", "SELL"]:
                        res = (
                            ut_res["action"],
                            ut_res["sl"],
                            ut_res["tp"],
                            mt5.symbol_info_tick(symbol).ask if ut_res["action"] == "BUY" else mt5.symbol_info_tick(symbol).bid,
                            ut_res.get("reason", "UT Liquidity Signal")
                        )
                    else:
                        res = ("NEUTRAL", 0.0, 0.0, 0.0, "UT: No setup yet")
                elif strategy == "scalp_1.1":
                    res = analyze_scalp_11_df(df, symbol) if hasattr(__builtins__, '__dict__') else ("NEUTRAL", 0.0, 0.0, 0.0, "Scalp disabled")
                elif strategy in ["breakout", "breakout_retest"]:
                    # ARCHIVED — not active
                    res = ("NEUTRAL", 0.0, 0.0, 0.0, "Breakout archived")
                elif strategy in ["trend_continuation", "trend_pullback"]:
                    res = analyze_trend_pullback_df(df, symbol)
                elif strategy == "opening_range":
                    res = analyze_opening_range_df(df, symbol)
                else:
                    res = ("NEUTRAL", 0.0, 0.0, 0.0, f"Unknown strategy: {strategy}")
                
                action, sl, tp, ep, details = res
                if action in ["BUY", "SELL"]:
                    details = f"[{strategy.upper()} on {tf}] " + details
                    return action, sl, tp, ep, details, strategy.upper()
                    
        return best_result
    except Exception as e:
        logger.error(f"Failed to analyze structural edge for {symbol}: {e}")
        return "NEUTRAL", 0.0, 0.0, 0.0, f"Error: {e}", "NONE"
def run_alphaedge(execute_orders: bool = False, approved_symbols: set[str] | None = None):
    client = MT5Client(MT5_CONFIG)
    try:
        client.connect()
        mt5.login(MT5_CONFIG["login"], password=MT5_CONFIG["password"], server=MT5_CONFIG["server"])
        logger.info("AlphaEdge Strategy initialized.")
    except Exception as e:
        logger.error(f"MT5 connection failed: {e}")
        raise RuntimeError(f"MT5 connection failed: {e}")
        
    # 1. Calculate Daily Profit (For Logging Only - Real Account Mode)
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day, 0, 0, 0)
    deals = mt5.history_deals_get(today_start, now)
    daily_profit = 0.0
    bot_tags = ["ALPHAEDGE_TRADE", "CORE_SYSTEM", "LIQUIDITY_SWEEP", "BREAKOUT"]
    if deals:
        df_deals = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())
        our_pos_ids = df_deals[df_deals['comment'].isin(bot_tags) & (df_deals['entry'] == 0)]['position_id'].unique()
        exits_today = df_deals[df_deals['entry'].isin([1, 3]) & df_deals['position_id'].isin(our_pos_ids)]
        if not exits_today.empty:
            daily_profit = exits_today['profit'].sum() + exits_today['commission'].sum() + exits_today['swap'].sum()
            
    logger.info(f"Current Daily Net P&L: ${daily_profit:+.2f} (Real Account Live Mode)")

    # 2. Manage Breakeven for Active Positions
    open_positions = mt5.positions_get()
    if execute_orders and open_positions:
        for pos in open_positions:
            if getattr(pos, 'comment', '') == "ALPHAEDGE_TRADE":
                symbol = pos.symbol
                # use main timeframe to estimate ATR for trailing/lock adjustments
                rates = mt5.copy_rates_from_pos(symbol, MAIN_TIMEFRAME, 0, 20)
                if rates is not None and len(rates) >= 14:
                    df_rates = pd.DataFrame(rates)
                    df_rates = calculate_atr(df_rates)
                    atr = df_rates['atr'].iloc[-1]

                    # 2-Stage Trailing / lock-in logic
                    try:
                        is_stage1 = False
                        is_stage2 = False
                        if pos.type == mt5.ORDER_TYPE_BUY:
                            if pos.price_current >= (pos.price_open + 2.0 * atr):
                                is_stage2 = True
                            elif pos.price_current >= (pos.price_open + 1.0 * atr):
                                is_stage1 = True
                        elif pos.type == mt5.ORDER_TYPE_SELL:
                            if pos.price_current <= (pos.price_open - 2.0 * atr):
                                is_stage2 = True
                            elif pos.price_current <= (pos.price_open - 1.0 * atr):
                                is_stage1 = True

                        if (is_stage1 or is_stage2) and AE_TRAILING_ENABLE:
                            if pos.type == mt5.ORDER_TYPE_BUY:
                                if is_stage2:
                                    new_sl = round(pos.price_current - (1.0 * atr), 5)
                                    stage_name = "Stage 2 Trail (1 ATR)"
                                else:
                                    # Breakeven + small buffer
                                    new_sl = round(pos.price_open + (0.1 * atr), 5)
                                    stage_name = "Stage 1 Breakeven Lock"
                                
                                # ensure we only move SL forward
                                if pos.sl == 0 or new_sl > pos.sl:
                                    request = {
                                        "action": mt5.TRADE_ACTION_SLTP,
                                        "position": pos.ticket,
                                        "symbol": pos.symbol,
                                        "sl": new_sl,
                                        "tp": pos.tp
                                    }
                                    res = mt5.order_send(request)
                                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                        msg = f"🔒 <b>[AlphaEdge SL Adjusted]</b>\nSymbol: {symbol}\nAction: Moved SL to {new_sl} ({stage_name})"
                                        logger.info(f"Moved SL to {new_sl} for {symbol} ({stage_name})")
                                        send_telegram_alert(msg)
                                    else:
                                        logger.error(f"Failed to adjust SL for {symbol}: {getattr(res,'retcode', res) if res else mt5.last_error()}")
                            else:
                                if is_stage2:
                                    new_sl = round(pos.price_current + (1.0 * atr), 5)
                                    stage_name = "Stage 2 Trail (1 ATR)"
                                else:
                                    new_sl = round(pos.price_open - (0.1 * atr), 5)
                                    stage_name = "Stage 1 Breakeven Lock"
                                
                                if pos.sl == 0 or new_sl < pos.sl:
                                    request = {
                                        "action": mt5.TRADE_ACTION_SLTP,
                                        "position": pos.ticket,
                                        "symbol": pos.symbol,
                                        "sl": new_sl,
                                        "tp": pos.tp
                                    }
                                    res = mt5.order_send(request)
                                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                        msg = f"🔒 <b>[AlphaEdge SL Adjusted]</b>\nSymbol: {symbol}\nAction: Moved SL to {new_sl} ({stage_name})"
                                        logger.info(f"Moved SL to {new_sl} for {symbol} ({stage_name})")
                                        send_telegram_alert(msg)
                                    else:
                                        logger.error(f"Failed to adjust SL for {symbol}: {getattr(res,'retcode', res) if res else mt5.last_error()}")
                    except Exception as e:
                        logger.error(f"Trailing SL adjustment failure for {symbol}: {e}")

    # Force-select valid Gold symbols present on current MT5 server
    session_symbols = [sym for sym in ASSET_CONFIG.keys() if mt5.symbol_info(sym) is not None]
    for sym in session_symbols:
        mt5.symbol_select(sym, True)

    
    # Disable weekend logic: Force active weekday scanning for all assets at all times
    is_weekend = False
    
    # -------------------------------------------------------------
    # PHASE 0: 24/5 ALL-DAY & OVERNIGHT MODE (WEEKEND MARKET CLOSE GATE)
    # -------------------------------------------------------------
    if not is_trading_session_active():
        logger.info("-> [Weekend Gate] Market is closed for the weekend. Scanning will resume Sunday 22:00 UTC.")
        client.disconnect()
        return []




    # -------------------------------------------------------------
    # PHASE 1: NEWS FILTER (MACRO-AWARENESS)
    # Automatically filter out assets with imminent high-impact news

    # -------------------------------------------------------------
    from news_filter import NewsFilter
    nf = NewsFilter()
    active_symbols = []
    for sym in session_symbols:
        safe, reason = nf.is_safe_to_trade(sym)
        if not safe:
            logger.info(f"🚫 [NEWS FILTER] Skipping {sym}: {reason}")
            continue
        active_symbols.append(sym)
        
    logger.info(f"[24/7 Mode] Scanning {len(active_symbols)} macro-safe symbols: {active_symbols}")

    # -------------------------------------------------------
    # DXY SENTIMENT CHECK (Dollar Index Directional Bias)
    # -------------------------------------------------------
    dxy_bias = "NEUTRAL"
    dxy_info = ""
    try:
        dxy_symbol = None
        for dxy_name in ["DXYm", "DXYz", "USDX", "DXY", "DOLLAR_INDX", "USDIndex"]:
            info = mt5.symbol_info(dxy_name)

            if info is not None:
                mt5.symbol_select(dxy_name, True)
                dxy_symbol = dxy_name
                break
        if dxy_symbol:
            dxy_rates = mt5.copy_rates_from_pos(dxy_symbol, mt5.TIMEFRAME_H1, 0, 20)
            if dxy_rates is not None and len(dxy_rates) >= 10:
                dxy_df = pd.DataFrame(dxy_rates)
                dxy_df['ema_fast'] = dxy_df['close'].ewm(span=5).mean()
                dxy_df['ema_slow'] = dxy_df['close'].ewm(span=14).mean()
                fast = dxy_df['ema_fast'].iloc[-1]
                slow = dxy_df['ema_slow'].iloc[-1]
                dxy_price = dxy_df['close'].iloc[-1]
                prev_close = dxy_df['close'].iloc[-2]
                if fast > slow and dxy_price > prev_close:
                    dxy_bias = "STRONG_USD"
                    dxy_info = f"DXY {dxy_price:.3f} RISING (Bearish for Gold/GBP, Bullish for Shorts)"
                elif fast < slow and dxy_price < prev_close:
                    dxy_bias = "WEAK_USD"
                    dxy_info = f"DXY {dxy_price:.3f} FALLING (Bullish for Gold/GBP, Bullish for Longs)"
                else:
                    dxy_bias = "NEUTRAL"
                    dxy_info = f"DXY {dxy_price:.3f} Ranging (No strong bias)"
                logger.info(f"[DXY Sentiment] {dxy_info}")
        else:
            logger.info("[DXY Sentiment] DXY not found in MT5 market watch - add USDX or DXY to see dollar bias.")
    except Exception as e:
        logger.warning(f"[DXY Sentiment] Could not read DXY: {e}")

    logger.info("=== -> Multi-Timeframe Scan (H1 Macro Trend + M1 Precision UT Scalp) ===")
    logger.info("| Symbol | Setup | Price | Stop Loss | Take Profit | R:R | Strategy | Analysis Details |")
    logger.info("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    
    scan_results = {}
    
    def scan_symbol(symbol):
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info or not symbol_info.visible:
            return symbol, ("NEUTRAL", 0.0, 0.0, 0.0, "Symbol not available/visible", "NONE")
        try:
            res = analyze_strategies(symbol)
            return symbol, res
        except Exception as e:
            return symbol, ("NEUTRAL", 0.0, 0.0, 0.0, f"Error: {e}", "NONE")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scan_symbol, s): s for s in active_symbols}
        for future in concurrent.futures.as_completed(futures):
            symbol, (action, sl, tp, entry_price, details, strategy) = future.result()
            scan_results[symbol] = {
                "action": action,
                "sl": sl,
                "tp": tp,
                "entry_price": entry_price,
                "details": details,
                "strategy": strategy
            }
        

                
    # --- Autonomous trading: place orders only when the structural edge strategy signals BUY or SELL ---
    triggers = []
    for symbol, result in scan_results.items():
        action = result["action"]
        if action not in ["BUY", "SELL"]:
            continue
        sl = result["sl"]
        tp = result["tp"]
        entry_price = result["entry_price"]
        strategy = result["strategy"]
        logger.info(f"Strategy {strategy} signals {action} for {symbol} at {entry_price:.5f} (SL={sl:.5f}, TP={tp:.5f})")
        triggers.append((symbol, action, sl, tp, entry_price, result["details"], strategy))

    if not triggers:
        logger.info("-> No UT setups confirmed. Waiting for ATR trailing stop crossover.")
        client.disconnect()
        return []

    if not execute_orders:
        logger.info("=== -> Macro Scan Complete (Scan-Only Mode) ===")
        for symbol, action, sl, tp, entry_price, details, strategy in triggers:
            logger.info(f"SCAN ONLY | {symbol} | {action} | Entry {entry_price:.5f} | SL {sl:.5f} | TP {tp:.5f} | Strategy: {strategy}")
        client.disconnect()
        return triggers

    logger.info("=== -> Executing Structural Edge Orders ===")
    open_positions = mt5.positions_get()
    active_trades = []
    if open_positions:
        active_trades = [(pos.symbol, getattr(pos, 'comment', '')) for pos in open_positions]
    pending_orders = mt5.orders_get() or []
    active_trades.extend([(order.symbol, getattr(order, 'comment', '')) for order in pending_orders])

    for symbol, action, sl, tp, entry_price, details, strategy_name in triggers:
        if approved_symbols is not None and symbol not in approved_symbols:
            logger.info(f"Skipping {symbol}: not in the approved symbol list.")
            continue
            
        # Max Concurrent Macro Positions Limit
        fresh_positions = mt5.positions_get()
        macro_positions = [p for p in fresh_positions if getattr(p, 'magic', 0) == 1001] if fresh_positions else []
        fresh_count = len(macro_positions)
        if fresh_count >= AE_MAX_CONCURRENT_TRADES:
            logger.info(f"Skipping {symbol}: Max Concurrent Macro Trades Limit ({AE_MAX_CONCURRENT_TRADES}) reached (Active Macro: {fresh_count}).")
            continue

        # Prevent chasing stale setups (stale entries that pull back and hit SL)
        # Check current live price. If price has moved > 0.5 * ATR from the trigger close, skip.
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            current_price = tick.bid if action == "SELL" else tick.ask
            price_drift = abs(current_price - entry_price)
            
            # Fetch ATR for drift threshold calculation
            symbol_info = mt5.symbol_info(symbol)
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 20)
            if rates is not None and len(rates) > 0:
                # Quick ATR calculation using global pd/np imports
                temp_df = pd.DataFrame(rates)
                high_low = temp_df['high'] - temp_df['low']
                high_close = np.abs(temp_df['high'] - temp_df['close'].shift())
                low_close = np.abs(temp_df['low'] - temp_df['close'].shift())
                ranges = pd.concat([high_low, high_close, low_close], axis=1)
                true_range = np.max(ranges, axis=1)
                atr = true_range.rolling(14).mean().iloc[-1]
            else:
                atr = 0.005 * entry_price
                
            max_drift = 0.5 * atr if atr and not np.isnan(atr) else 0.005 * entry_price
            if price_drift > max_drift:
                logger.info(f"Skipping {symbol}: Stale Entry. Price drifted {price_drift:.5f} > max allowed {max_drift:.5f} from trigger price ({entry_price:.5f}).")
                continue
            
        # Enforce max 2 open positions per symbol for macro engine
        symbol_positions = [p for p in (mt5.positions_get(symbol=symbol) or []) if getattr(p, 'magic', 0) == 1001]
        if len(symbol_positions) >= 2:
            logger.info(f"Skipping {symbol}: already has {len(symbol_positions)} open macro positions (Max 2 allowed).")
            continue

        volume = 0.01  # Fixed 0.01 micro lot per order
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        
        # Pre-fetch tick price once to avoid connection latency delay between orders
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Cannot get tick for {symbol}, skipping execution.")
            continue
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        
        try:
            opened_count = 0
            tp2 = price + 1.5 * (tp - price) if action == "BUY" else price - 1.5 * (price - tp)
            for i in range(1, 3):
                order_tp = tp if i == 1 else tp2
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": order_type,
                    "price": price,
                    "sl": round(sl, 5),
                    "tp": round(order_tp, 5),
                    "deviation": 20,
                    "magic": 1001,
                    "comment": f"{strategy_name}_TP{i}",
                    "type_filling": mt5.ORDER_FILLING_FOK,
                    "type_time": mt5.ORDER_TIME_GTC,
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    opened_count += 1
                else:
                    # Fallback to IOC filling if FOK rejected
                    request["type_filling"] = mt5.ORDER_FILLING_IOC
                    result_ioc = mt5.order_send(request)
                    if result_ioc and result_ioc.retcode == mt5.TRADE_RETCODE_DONE:
                        opened_count += 1
                    else:
                        logger.error(f"Failed order {i} for {symbol}: {getattr(result, 'comment', mt5.last_error())}")

            if opened_count > 0:
                action_emoji = "🟢 BUY" if action == "BUY" else "🔴 SELL"
                risk_dist = abs(price - sl)
                reward_dist = abs(tp - price)
                rr_calc = (reward_dist / risk_dist) if risk_dist > 0 else 1.5
                tot_vol = round(volume * opened_count, 2)

                logger.info(f"Successfully opened {opened_count} positions: {action} on {symbol} (Entry: {price:.5f}, Lot: {volume}, SL: {sl:.5f}, TP1: {tp:.5f}, TP2: {tp2:.5f})")
                
                msg = (
                    f"🚀 <b>[AlphaEdge Position Opened]</b>\n\n"
                    f"• <b>Asset:</b> {symbol}\n"
                    f"• <b>Direction:</b> {action_emoji}\n"
                    f"• <b>Entry Price:</b> <code>{price:.5f}</code>\n"
                    f"• <b>Stop Loss:</b> <code>{sl:.5f}</code>\n"
                    f"• <b>Take Profit:</b> <code>{tp:.5f}</code> (TP1) | <code>{tp2:.5f}</code> (TP2)\n"
                    f"• <b>Risk:Reward:</b> 1 : {rr_calc:.2f}\n"
                    f"• <b>Position Size:</b> {tot_vol} Lots ({opened_count}x {volume} Orders)\n"
                    f"• <b>Session:</b> 🌐 24/5 All-Day & Overnight Mode\n"
                    f"• <b>Strategy:</b> {strategy_name}\n\n"
                    f"📌 <b>Setup Context:</b>\n{details}"
                )
                send_telegram_alert(msg)

                try:
                    log_trade(symbol, action, price, sl, tp, tot_vol, f"{strategy_name}")
                except Exception as log_err:
                    logger.error(f"Failed to log trade for {symbol}: {log_err}")

        except Exception as e:
            logger.error(f"Order send error on {symbol}: {e}")

            
    client.disconnect()


def process_tv_signals():
    import os
    signal_file = "tv_signals.txt"
    if not os.path.exists(signal_file):
        return
        
    try:
        with open(signal_file, "r") as f:
            lines = f.readlines()
            
        with open(signal_file, "w") as f:
            pass
            
        if not lines:
            return
            
        for line in lines:
            line = line.strip()
            if not line: continue
            parts = line.split(",")
            if len(parts) >= 3:
                symbol = parts[0].strip()
                action = parts[1].strip().upper()
                price_str = parts[2].strip()
                
                logger.info("Processing TV Webhook: " + action + " for " + symbol)
                
                try:
                    from news_catalyst_engine import NewsCatalystEngine
                    nce = NewsCatalystEngine()
                    status = nce.get_market_catalyst_status(symbol)
                    if status.get('state') in ('PRE_NEWS_FREEZE', 'NEWS_SPIKE_BLOCK'):
                        logger.warning(f"Ignored TV Webhook for {symbol} due to News: {status.get('event')}")
                        continue
                except Exception:
                    pass
                    
                rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 15)
                if rates is None or len(rates) == 0:
                    continue
                import pandas as pd, numpy as np
                temp_df = pd.DataFrame(rates)
                tr = np.maximum(temp_df["high"] - temp_df["low"], 
                     np.maximum(abs(temp_df["high"] - temp_df["close"].shift()), 
                                abs(temp_df["low"] - temp_df["close"].shift())))
                atr = tr.rolling(14).mean().iloc[-1]
                if np.isnan(atr) or atr <= 0:
                    atr = 0.005 * float(price_str)
                    
                tick = mt5.symbol_info_tick(symbol)
                if not tick: continue
                
                current_price = tick.ask if action == "BUY" else tick.bid
                
                if action == "BUY":
                    sl = current_price - (1.5 * atr)
                    tp = current_price + (3.0 * atr)
                else:
                    sl = current_price + (1.5 * atr)
                    tp = current_price - (3.0 * atr)
                    
                vol = get_lot_size(symbol, sl, current_price)
                if vol <= 0: continue
                
                order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": vol,
                    "type": order_type,
                    "price": current_price,
                    "sl": round(sl, 5),
                    "tp": round(tp, 5),
                    "deviation": 20,
                    "comment": "TV_Webhook",
                    "type_filling": mt5.ORDER_FILLING_FOK,
                    "type_time": mt5.ORDER_TIME_GTC,
                }
                res = mt5.order_send(request)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    logger.info("TV SUCCESS: " + action + " " + symbol)
                    msg = "🚀 [TradingView Webhook Executed]\nAsset: " + symbol + "\nDirection: " + action
                    send_telegram_alert(msg)
                else:
                    logger.error("TV Failed: " + str(getattr(res, "comment", mt5.last_error())))
                    
    except Exception as e:
        logger.error("Error processing TV signals: " + str(e))

if __name__ == "__main__":

    logger.info("=" * 65)
    logger.info("  ALPHAEDGE — INSTITUTIONAL MTF WHALE FLOW SWING ENGINE")
    logger.info("  Live Assets: Gold (XAUUSDm) | Crude Oil (USOILm) | Dow Jones (US30m)")
    logger.info("  Strategy: H4 Dealing Range | Yad Masri Principle 3 Approach Architecture | M5 Anti-Chase")
    logger.info("  Gold:   0.02 lot | TP: $100.00 | Max SL: $36.00 (18 pts)  | BE: +$20 | Lock1: $35->$25 | Lock2: $60->$45")
    logger.info("  Oil:    0.05 lot | TP: $100.00 | Max SL: $35.00 ($0.70)   | BE: +$20 | Lock1: $35->$25 | Lock2: $60->$45")
    logger.info("  US30:   0.25 lot | TP: $100.00 | Max SL: $36.00 (144 pts) | BE: +$20 | Lock1: $35->$25 | Lock2: $60->$45")
    logger.info("  Session: Tuesday - Friday, 5:00 AM - 11:00 PM EAT (Monday = Observation Only) | Overnight Gate")
    logger.info("  Guidance: ForexFactory Real-Time Macro News Engine (USD)")
    logger.info("=" * 65)

    # 1. Send Online Startup Notification to Telegram
    startup_msg = (
        "<b>🏛 AlphaEdge — Institutional MTF Whale Flow Engine Active</b>\n\n"
        "<b>Live Trading Assets (3 Assets Active):</b>\n"
        "• <b>Gold (XAUUSDm):</b> $100.00 TP | $36.00 SL (18 pts) | BE +$20 | Lock1 $35->$25 | Lock2 $60->$45 | <b>0.02 Lot</b>\n"
        "• <b>Crude Oil (USOILm):</b> $100.00 TP | $35.00 SL ($0.70) | BE +$20 | Lock1 $35->$25 | Lock2 $60->$45 | <b>0.05 Lot</b>\n"
        "• <b>Dow Jones (US30m):</b> $100.00 TP | $36.00 SL (144 pts) | BE +$20 | Lock1 $35->$25 | Lock2 $60->$45 | <b>0.25 Lot</b>\n"
        "<b>Shadow Observation (6 Assets):</b> EURUSDm · USTECm (Nasdaq) · GBPUSDm · US500m (S&P 500) · GBPJPYm (Guppy) · DE30m (DAX) (paper mode)\n\n"
        "<b>Yad Masri Institutional Architecture:</b>\n"
        "• <b>Principle 3 (Approach Architecture):</b> Filters pre-zone compression vs. exhaustion arrival. Blocks breakout traps.\n"
        "• <b>Anti-Chase Wholesale Shield:</b> Declines late entries at candle tops; guarantees wholesale fill pricing.\n"
        "• <b>Solo Gold Focus:</b> Silver decoupled completely to eliminate correlated drag.\n"
        "• <b>Overnight Spread Gate (23:00 EAT):</b> $45 buffer shields open swings from rollover spread spikes.\n\n"
        "<b>Execution Window:</b> Tuesday – Friday, 5:00 AM – 11:00 PM EAT (Monday = Observation Only)\n\n"
        "<b>Status:</b> 🟢 Ready for Tuesday open. Hunting high-conviction institutional setups."
    )
    send_telegram_alert(startup_msg)




    # 1.5 Auto-Learning Self-Correction Pass
    try:
        from ai_learning import AutoLearner
        logger.info("Running AI Auto-Learning Self-Correction (Last 48 Hours)...")
        learner = AutoLearner()
        learner.analyze_history_and_adapt()
    except Exception as e:
        logger.error(f"Auto-Learning failed: {e}")

    # 2. Continuous Autonomous Scan Loop
    from desktop_trade_report import refresh_report
    from performance_report import generate_performance_report
    from datetime import timedelta
    from pathlib import Path

    last_daily_report_date = None
    last_weekly_report_date = None
    _session_open_alert_sent = False
    _ai_run_state = {}   # tracks last AI auto-learning run to avoid re-running same minute

    _start_telegram_command_listener()

    try:
        while True:
            cycle_start = datetime.now()
            print("\n" + "-" * 65)


            logger.info(f"AlphaEdge M15 Swing Engine scanning at {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}...")

            # Check pause state from Telegram /stop_scanner
            state_file = Path("bot_state.txt")
            if state_file.exists() and state_file.read_text().strip().upper() == "STOPPED":
                logger.info("Scanner is PAUSED via Telegram (/start_scanner to resume). Skipping scan.")
            else:
                try:
                    from institutional_trader import run_scalping_cycle as run_institutional_cycle
                    run_institutional_cycle()
                except Exception as inst_err:
                    import traceback
                    logger.error(f"[Institutional] Cycle error:\n{traceback.format_exc()}")

                # ── 1.5 Shadow Observation Scan (EURUSDm, US30m) ────────────────────
                # Completely non-trading paper observation mode. Never calls order_send.
                try:
                    import shadow_tracker
                    shadow_tracker.run_shadow_cycle()
                except Exception as shadow_err:
                    logger.debug(f"[ShadowTracker] Observation error: {shadow_err}")


            # ── End-of-Day Pre-Close Gold Market Analysis (20:45 UTC) ──────────────
            # Gold market closes at 21:00 UTC. We fire at 20:45 UTC to give a
            # full daily performance & market analysis before the close.
            now = datetime.now()
            today_date = now.date()
            # Suppress on weekends (Saturday=5, Sunday=6) when markets are closed
            if now.weekday() in (5, 6):
                pass
            elif now.hour == 20 and 44 <= now.minute <= 49:
                eod_key = today_date.strftime("EOD_%Y-%m-%d")
                if not getattr(process_telegram_commands, "_eod_sent", None) or process_telegram_commands._eod_sent != eod_key:
                    process_telegram_commands._eod_sent = eod_key
                    try:
                        acc = mt5.account_info()
                        bal  = acc.balance if acc else 0.0
                        eq   = acc.equity  if acc else 0.0
                        pnl  = eq - bal

                        # Count today's scalp history
                        from_ts = int(datetime(now.year, now.month, now.day).timestamp())
                        deals = mt5.history_deals_get(from_ts, int(now.timestamp()))
                        scalp_deals = [d for d in deals if d.magic == 20250831] if deals else []
                        total_trades = len(scalp_deals)
                        wins  = len([d for d in scalp_deals if d.profit > 0])
                        loss  = len([d for d in scalp_deals if d.profit < 0])
                        win_rate = round((wins / total_trades) * 100, 1) if total_trades > 0 else 0.0
                        day_pnl  = sum(d.profit for d in scalp_deals)

                        # Current gold price
                        tick = mt5.symbol_info_tick("XAUUSDm")
                        gold_price = tick.bid if tick else 0.0

                        # Build the daily analysis message
                        eod_msg = (
                            "<b>📊 AlphaEdge Daily Gold Market Report</b>\n"
                            "<b>End of Day — Pre-Close Analysis</b>\n\n"
                            f"• <b>Gold Price (Close):</b> ${gold_price:.2f}\n"
                            f"• <b>Account Balance:</b> ${bal:.2f}\n"
                            f"• <b>Account Equity:</b> ${eq:.2f}\n"
                            f"• <b>Floating PnL:</b> ${pnl:+.2f}\n\n"
                            f"<b>Today's Scalp Performance:</b>\n"
                            f"• <b>Total Scalp Trades:</b> {total_trades}\n"
                            f"• <b>Wins:</b> {wins}  |  <b>Losses:</b> {loss}\n"
                            f"• <b>Win Rate:</b> {win_rate}%\n"
                            f"• <b>Today's Realized PnL:</b> ${day_pnl:+.2f}\n\n"
                            f"<b>Market Session Summary:</b>\n"
                            f"• Asian Session: Completed\n"
                            f"• London Session: Completed\n"
                            f"• NY Session: Closing in 15 minutes\n\n"
                            f"<b>Bot Status:</b> 🟢 Active | Lot Size: 0.01 (Micro) | Single Order TP1 Lock"
                        )
                        send_telegram_alert(eod_msg)
                        logger.info("[Scalp] End-of-Day daily analysis sent to Telegram.")

                        # Send Shadow Tracking Daily Summary to Telegram
                        try:
                            from shadow_tracker import generate_shadow_report
                            shadow_report_msg = generate_shadow_report()
                            send_telegram_alert(shadow_report_msg)
                            logger.info("[ShadowTracker] EOD Shadow report sent to Telegram.")
                        except Exception as s_err:
                            logger.debug(f"[ShadowTracker] EOD report send error: {s_err}")
                    except Exception as eod_err:
                        logger.error(f"[Scalp] EOD report error: {eod_err}")

            # Daily & Weekly Reporting (Runs at 23:50 UTC)
            # Suppress daily reports on weekends (Saturday=5, Sunday=6)
            if now.weekday() in (5, 6):
                pass
            elif now.hour == 23 and now.minute >= 50:
                if last_daily_report_date != today_date:
                    try:
                        start_time = datetime(now.year, now.month, now.day)
                        end_time = start_time + timedelta(days=1)
                        daily_msg = generate_performance_report(start_time, end_time, "Daily")
                        send_telegram_alert(daily_msg)
                        last_daily_report_date = today_date
                    except Exception as e:
                        logger.error(f"Failed to send daily report: {e}")

                if now.weekday() == 4 and last_weekly_report_date != today_date:
                    try:
                        start_time = datetime(now.year, now.month, now.day) - timedelta(days=4)
                        end_time = datetime(now.year, now.month, now.day) + timedelta(days=1)
                        weekly_msg = generate_performance_report(start_time, end_time, "Weekly")
                        send_telegram_alert(weekly_msg)
                        last_weekly_report_date = today_date
                    except Exception as e:
                        logger.error(f"Failed to send weekly report: {e}")

            # ── Periodic AI Auto-Learning Brain (Every 30 Minutes) ──────────
            if now.minute in [0, 30] and _ai_run_state.get("last_run") != (now.hour, now.minute):
                _ai_run_state["last_run"] = (now.hour, now.minute)
                try:
                    from ai_learning import AutoLearner
                    logger.info("Executing periodic AI Auto-Learning evaluation...")
                    learner = AutoLearner()
                    learner.analyze_history_and_adapt()
                except Exception as ai_err:
                    logger.error(f"[AI Brain] Periodic learning error: {ai_err}")

            # Remainder sleep to complete precision 60-second M1 candle cycle
            cycle_end = datetime.now()
            elapsed = (cycle_end - cycle_start).total_seconds()
            sleep_time = max(0, 60.0 - elapsed)
            time.sleep(sleep_time)


    except KeyboardInterrupt:
        logger.info("AlphaEdge stopped manually by user.")
        try:
            send_telegram_alert("🛑 <b>[AlphaEdge Bot Offline]</b>\nScanner was manually shut down.")
        except Exception:
            pass
    except Exception as fatal_error:
        error_msg = f"🚨 <b>CRITICAL BOT FAILURE</b> 🚨\nError: {fatal_error}"
        logger.critical(error_msg)
        try:
            send_telegram_alert(error_msg)
        except Exception:
            pass
        raise
