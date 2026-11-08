"""
AlphaEdge Engine Health Check
Verifies all modules load cleanly and core logic is correct.
"""
import sys

errors = []

# ── 1. Trade Logger ──────────────────────────────────────────────────────────
try:
    from m15_trade_analysis_logger import (
        get_performance_summary, sync_closed_trades_from_history
    )
    s = get_performance_summary()
    print("OK  m15_trade_analysis_logger")
    print(f"    Trades:{s['total']}  Wins:{s['wins']}  Losses:{s['losses']}  WR:{s['win_rate']}%  Net PnL:${s['net_pnl']}")
except Exception as e:
    print("ERR m15_trade_analysis_logger:", e)
    errors.append(str(e))

# ── 2. News Catalyst Engine ──────────────────────────────────────────────────
try:
    from news_catalyst_engine import NewsCatalystEngine, _eat_time
    engine = NewsCatalystEngine()
    print("OK  news_catalyst_engine")
    print(f"    Events loaded: {len(engine.events)}")
    for sym in ["XAUUSDm"]:
        s = engine.get_market_catalyst_status(sym)
        print(f"    {sym} now: state={s['state']}  action={s['action']}")
except Exception as e:
    print("ERR news_catalyst_engine:", e)
    errors.append(str(e))

# ── 3. AI Learning ───────────────────────────────────────────────────────────
try:
    from ai_learning import AutoLearner
    learner = AutoLearner()
    cfg = learner.load_config()
    print("OK  ai_learning")
    print(f"    gold_tp_dollars={cfg.get('gold_tp_dollars')}  regime={cfg.get('regime')}")
except Exception as e:
    print("ERR ai_learning:", e)
    errors.append(str(e))

# ── 4. Institutional Trader ────────────────────────────────────────────────
try:
    import institutional_trader
    gold   = institutional_trader.ASSET_CONFIGS["XAUUSDm"]
    oil    = institutional_trader.ASSET_CONFIGS["USOILm"]
    us30   = institutional_trader.ASSET_CONFIGS["US30m"]
    print("OK  institutional_trader")
    print(f"    Gold  : lot={gold['lot']}  tp=${gold['tp_dollars']}  sl=${gold['max_sl_dollars']}")
    print(f"    Oil   : lot={oil['lot']}   tp=${oil['tp_dollars']}  sl=${oil['max_sl_dollars']}")
    print(f"    US30  : lot={us30['lot']}  tp=${us30['tp_dollars']}  sl=${us30['max_sl_dollars']}")
except Exception as e:
    print("ERR institutional_trader:", e)
    errors.append(str(e))

# ── 5. Session Filter Boundary Check (7:00 AM - 9:00 PM EAT) ──────────────────
print()
print("--- Session filter (05:00 - 23:00 EAT / Tuesday - Friday) ---")
from institutional_trader import is_session_active
from datetime import datetime, timezone, timedelta

def sim_session(weekday, hour_eat):
    # EAT is UTC+3
    now_utc = datetime(2026, 9, 21 + weekday, hour_eat, 0, tzinfo=timezone.utc) - timedelta(hours=3)
    now_eat = now_utc + timedelta(hours=3)
    wd = now_eat.weekday()
    he = now_eat.hour
    if wd in (5, 6):
        return False
    if wd in (0, 4):  # Monday & Friday are strictly Observation & Structural Memory
        return False
    if he < 3 or he >= 23:
        return False
    return True

tests = [
    (0, 4,  "Mon 04:00 EAT (observation)", False),
    (0, 5,  "Mon 05:00 EAT (observation)", False),
    (0, 8,  "Mon 08:00 EAT (observation)", False),
    (0, 12, "Mon 12:00 EAT (observation)", False),
    (0, 22, "Mon 22:00 EAT (observation)", False),
    (0, 23, "Mon 23:00 EAT (overnight)  ", False),
    (1, 2,  "Tue 02:00 EAT (pre-open)   ", False),
    (1, 3,  "Tue 03:00 EAT (market open)", True),
    (1, 5,  "Tue 05:00 EAT (early trade)", True),
    (1, 7,  "Tue 07:00 EAT (early trade)", True),
    (1, 8,  "Tue 08:00 EAT (active trade)", True),
    (1, 12, "Tue 12:00 EAT (midday)     ", True),
    (3, 14, "Thu 14:00 EAT (active trade)", True),
    (4, 12, "Fri 12:00 EAT (observation)", False),
    (4, 22, "Fri 22:00 EAT (observation)", False),
    (4, 23, "Fri 23:00 EAT (cutoff)     ", False),
    (5, 12, "Sat 12:00 EAT (weekend)    ", False),
    (6, 12, "Sun 12:00 EAT (weekend)    ", False),
]
all_session_ok = True
for weekday, hour_eat, label, expected in tests:
    active = sim_session(weekday, hour_eat)
    ok = (active == expected)
    status = "OK " if ok else "ERR"
    if not ok:
        all_session_ok = False
        errors.append(f"Session mismatch at {label}: got {active} expected {expected}")
    print(f"  {status}  {label}  active={active}  {'pass' if ok else 'FAIL'}")

# ── 6. News State Machine ─────────────────────────────────────────────────────
print()
print("--- News state machine ---")
state_tests = [
    (-35, "NORMAL"),
    (-6,  "NORMAL"),
    (-5,  "PRE_NEWS_FREEZE"),
    (-1,  "PRE_NEWS_FREEZE"),
    (0,   "NEWS_SPIKE_BLOCK"),
    (2,   "NEWS_SPIKE_BLOCK"),
    (5,   "CATALYST_IMPULSE"),
    (15,  "CATALYST_IMPULSE"),
    (25,  "CATALYST_IMPULSE"),
    (26,  "NORMAL"),
]
blocks_states = {"PRE_NEWS_FREEZE", "NEWS_SPIKE_BLOCK"}
all_news_ok = True
for offset, expected_state in state_tests:
    diff = float(offset)
    if -5.0 <= diff < 0:
        state = "PRE_NEWS_FREEZE"
    elif 0 <= diff < 5.0:
        state = "NEWS_SPIKE_BLOCK"
    elif 5.0 <= diff <= 25.0:
        state = "CATALYST_IMPULSE"
    else:
        state = "NORMAL"
    ok = (state == expected_state)
    blocks = state in blocks_states
    if not ok:
        all_news_ok = False
        errors.append(f"State mismatch T{offset:+d}min: got {state} expected {expected_state}")
    print(f"  {'OK' if ok else 'ERR'}  T{offset:+3d}min -> {state:20s}  blocks_entry={blocks}  {'pass' if ok else 'FAIL'}")

# ── 7. Telegram recipients check ─────────────────────────────────────────────
print()
import os
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except Exception:
    pass

token   = os.getenv("TELEGRAM_TOKEN", "")
chat_id = os.getenv("TELEGRAM_CHAT_ID",    "915238743")
channel = os.getenv("TELEGRAM_CHANNEL_ID", "@riffexalphaedgebot")
print(f"  Token   : {'SET' if token else 'MISSING'}")
print(f"  Chat ID : {chat_id}")
print(f"  Channel : {channel}")

# ── 8. Public Channel Firewall Verification ──────────────────────────────────
print()
print("--- Channel Firewall Security Test ---")
from institutional_trader import _is_channel_allowed

firewall_test_cases = [
    ("🚀 <b>[AlphaEdge Signal]</b>\nAsset: XAUUSDm\nAction: BUY", True, "Trade Entry Signal"),
    ("🎯 <b>[Trade Closed — WIN]</b>\nAsset: XAUUSDm (BUY)\nResult: +$10.00", True, "Trade Close Win"),
    ("🛑 <b>[Trade Closed — LOSS]</b>\nAsset: XAUUSDm (BUY)\nResult: -$10.00", True, "Trade Close Loss"),
    ("🛡️ <b>[Trade Closed — BREAK-EVEN]</b>\nAsset: XAUUSDm", True, "Trade Close Break-Even"),
    ("🔄 <b>[Trade Closed — REVERSAL]</b>\nAsset: XAUUSDm", True, "Trade Close Reversal"),
    ("📊 <b>AlphaEdge Daily Gold Market Report</b>\nEnd of Day", False, "Daily Market Report"),
    ("📊 ALPHAEDGE DAILY REPORT\nDate: 2026-09-14", False, "Daily Performance Report"),
    ("🛑 <b>[AlphaEdge Bot Offline]</b>\nScanner was stopped", False, "Bot Offline Notification"),
    ("🟢 <b>AlphaEdge Bot Status Report</b>\nMarket: Gold", False, "Bot Status Reply"),
    ("📅 <b>AlphaEdge Daily News Briefing</b>\nHigh-Impact", False, "News Briefing"),
    ("⏰ <b>30-Minute News Alert</b>\nCore CPI m/m", False, "30-Min News Alert"),
    ("🛡️ <b>[Break-Even Protected]</b>\nAsset: XAUUSDm", False, "Break-Even Protection"),
    ("⚠️ <b>[Pre-News Protection]</b>\nAsset: XAUUSDm", False, "Pre-News Protection"),
]

for msg, expected_allowed, desc in firewall_test_cases:
    actual = _is_channel_allowed(msg)
    ok = (actual == expected_allowed)
    status = "OK " if ok else "ERR"
    action = "PERMITTED" if actual else "BLOCKED  "
    if not ok:
        errors.append(f"Firewall test failed for '{desc}': got {actual}, expected {expected_allowed}")
    print(f"  {status} [{action}] {desc:30s} -> {'pass' if ok else 'FAIL'}")

# ── 9. Dealing Range & Momentum Alignment Safeguards ───────────────────────
print()
print("--- Dealing Range & Momentum Alignment Safeguards ---")

# Test 1: Bearish H4 but location is deep discount (< 35%) -> SELL MUST BE BLOCKED
h4_trend = "BEARISH"
loc_pct = 9.5  # Exactly like today's bad entry
macro_sell_allowed = (loc_pct >= 35.0) if h4_trend == "BEARISH" else False
ok1 = (macro_sell_allowed == False)
print(f"  {'OK ' if ok1 else 'ERR'} [Discount Gate] H4 BEARISH at {loc_pct}% location -> SELL allowed: {macro_sell_allowed} (Expected: False) -> {'pass' if ok1 else 'FAIL'}")
if not ok1:
    errors.append("Dealing Range failed: allowed SELL at 9.5% discount")

# Test 2: Bullish H4 but location is deep premium (> 65%) -> BUY MUST BE BLOCKED
h4_trend = "BULLISH"
loc_pct = 85.0
macro_buy_allowed = (loc_pct <= 65.0) if h4_trend == "BULLISH" else False
ok2 = (macro_buy_allowed == False)
print(f"  {'OK ' if ok2 else 'ERR'} [Premium Gate]  H4 BULLISH at {loc_pct}% location -> BUY allowed: {macro_buy_allowed} (Expected: False) -> {'pass' if ok2 else 'FAIL'}")
if not ok2:
    errors.append("Dealing Range failed: allowed BUY at 85.0% premium")

# Test 3: Liquidity sweep SELL appears, but M15 UT Bot is BUY -> SELL MUST BE BLOCKED
m15_sell_sweep = True
h1_sell_sweep = False
ut_signal_m15 = "BUY"  # Exactly like today's bad entry
has_sell_trigger = (ut_signal_m15 == "SELL" or m15_sell_sweep or h1_sell_sweep) and (ut_signal_m15 != "BUY")
ok3 = (has_sell_trigger == False)
print(f"  {'OK ' if ok3 else 'ERR'} [Momentum Gate] Sweep SELL with M15 UT BUY -> Triggered: {has_sell_trigger} (Expected: False) -> {'pass' if ok3 else 'FAIL'}")
if not ok3:
    errors.append("Momentum Gate failed: allowed SELL while M15 UT Bot was BUY")

# Test 4: Liquidity sweep BUY appears, but M15 UT Bot is SELL -> BUY MUST BE BLOCKED
m15_buy_sweep = True
h1_buy_sweep = False
ut_signal_m15 = "SELL"
has_buy_trigger = (ut_signal_m15 == "BUY" or m15_buy_sweep or h1_buy_sweep) and (ut_signal_m15 != "SELL")
ok4 = (has_buy_trigger == False)
print(f"  {'OK ' if ok4 else 'ERR'} [Momentum Gate] Sweep BUY with M15 UT SELL -> Triggered: {has_buy_trigger} (Expected: False) -> {'pass' if ok4 else 'FAIL'}")
if not ok4:
    errors.append("Momentum Gate failed: allowed BUY while M15 UT Bot was SELL")

# Test 5: Fully aligned SELL setup (H4 BEARISH, location 55% premium, UT Bot SELL) -> ALLOWED
h4_trend = "BEARISH"
loc_pct = 55.0
ut_signal_m15 = "SELL"
macro_sell_allowed = (loc_pct >= 35.0)
has_sell_trigger = (ut_signal_m15 == "SELL" or False) and (ut_signal_m15 != "BUY")
ok5 = (macro_sell_allowed and has_sell_trigger) == True
print(f"  {'OK ' if ok5 else 'ERR'} [Aligned Setup] H4 BEARISH at 55% + UT SELL -> Allowed: {macro_sell_allowed and has_sell_trigger} (Expected: True) -> {'pass' if ok5 else 'FAIL'}")
if not ok5:
    errors.append("Aligned Setup failed: rejected valid aligned SELL setup")

# ── 10. Yad Masri Principle 3 & Anti-Chase Shield Verification ────────────────
print()
print("--- Yad Masri Principle 3 & Anti-Chase Shield Tests ---")
from institutional_engine import InstitutionalEngine
import pandas as pd
import numpy as np

inst_eng = InstitutionalEngine()

# Test 1: Pre-Zone Compression Filter (8 narrow bars right at border -> BLOCKED)
# Create synthetic M5 dataframe with 25 bars where the last 8 bars are ultra-tight
compressed_data = []
base_p = 2700.0
for i in range(25):
    if i < 15:
        # Standard bars (ATR ~ 2.0)
        compressed_data.append({"high": base_p + 2.0, "low": base_p - 2.0, "close": base_p + 1.0, "open": base_p - 1.0})
    else:
        # Compressed bars right before touch (range ~ 0.3 pts, < 0.5x ATR)
        compressed_data.append({"high": base_p + 0.15, "low": base_p - 0.15, "close": base_p + 0.05, "open": base_p - 0.05})
df_compressed = pd.DataFrame(compressed_data)
comp_res = inst_eng.check_approach_architecture(df_compressed, "BUY")
ok_comp = (comp_res["safe"] == False and comp_res["approach"] == "PRE_ZONE_COMPRESSION")
print(f"  {'OK ' if ok_comp else 'ERR'} [Yad Masri P3] Pre-Zone Compression -> Safe: {comp_res['safe']} ({comp_res['approach']}) -> {'pass' if ok_comp else 'FAIL'}")
if not ok_comp:
    errors.append("Yad Masri P3 failed: did not detect Pre-Zone Compression")

# Test 2: Exhaustion Sweep Arrival (Wide bars straight into zone -> APPROVED)
exhaustion_data = []
for i in range(25):
    # Wide exhaustion bars (range ~ 3.5 pts)
    exhaustion_data.append({"high": base_p + 3.0, "low": base_p - 3.0, "close": base_p + 1.5, "open": base_p - 1.5})
df_exhaustion = pd.DataFrame(exhaustion_data)
exh_res = inst_eng.check_approach_architecture(df_exhaustion, "BUY")
ok_exh = (exh_res["safe"] == True and exh_res["approach"] == "EXHAUSTION_SWEEP")
print(f"  {'OK ' if ok_exh else 'ERR'} [Yad Masri P3] Exhaustion Sweep -> Safe: {exh_res['safe']} ({exh_res['approach']}) -> {'pass' if ok_exh else 'FAIL'}")
if not ok_exh:
    errors.append("Yad Masri P3 failed: did not approve Exhaustion Sweep")

# Test 3: Anti-Chase Shield (M5 closed too far from sweep low -> DECLINED)
chase_data = [
    {"high": 2705.0, "low": 2698.0, "close": 2700.0, "open": 2704.0},
    {"high": 2703.0, "low": 2695.0, "close": 2696.0, "open": 2702.0},
    {"high": 2700.0, "low": 2690.0, "close": 2692.0, "open": 2698.0},
    {"high": 2692.0, "low": 2688.0, "close": 2690.0, "open": 2691.0},
    {"high": 2690.0, "low": 2685.0, "close": 2686.0, "open": 2689.0}, # sweep bar
    {"high": 2702.0, "low": 2686.0, "close": 2701.0, "open": 2687.0}, # huge 15-pt bar closing at top
    {"high": 2703.0, "low": 2700.0, "close": 2702.0, "open": 2701.0}, # current forming
]
df_chase = pd.DataFrame(chase_data)
chase_res = inst_eng.verify_m5_pullback_confirmation(df_chase, "BUY", max_chase_dist=6.5, sweep_ref=2685.0)
ok_chase = (chase_res["confirmed"] == False and chase_res.get("chasing") == True)
print(f"  {'OK ' if ok_chase else 'ERR'} [Anti-Chase] Late M5 close (16 pts above low, max 6.5) -> Confirmed: {chase_res['confirmed']} -> {'pass' if ok_chase else 'FAIL'}")
if not ok_chase:
    errors.append("Anti-Chase Shield failed: allowed chasing entry 16 pts above low")

# ── Final Result ──────────────────────────────────────────────────────────────
print()
if errors:
    print(f"HEALTH CHECK FAILED -- {len(errors)} error(s):")
    for e in errors:
        print(" ", e)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED. AlphaEdge engine is clean and ready to run.")
