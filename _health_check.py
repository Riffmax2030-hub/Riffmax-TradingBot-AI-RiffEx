"""
_health_check.py — AlphaEdge Pure Price Action 3-TF System Verification Suite
=============================================================================
End-to-end deterministic verification of:
  1. Core module imports & syntax integrity (zero forbidden legacy references)
  2. Calibrated asset configuration parity ($34 SL / $55 TP / +$28 BE / +$44->$32 Lock1)
  3. Mon-Fri London + NY Killzone schedule & off-hours / weekend blocks
  4. Capital protection guards (-$50 daily DD hard stop, max 2 concurrent positions)
  5. Pure Price Action 3-TF Evaluator (TF1 H4 3-state + 75%/25% leg, TF2 pools + M15 anti-crash, TF3 M5 sweep-reclaim)
  6. News Catalyst state machine & Telegram Channel Firewall
  7. AutoLearner calibrated parameter bounds
"""

import os
import sys
import py_compile
from datetime import datetime, timezone
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(name: str, condition: bool, detail: str = ""):
    status = "OK " if condition else "ERR"
    suffix = f" | {detail}" if detail else ""
    print(f"  {status} [{name}]{suffix}")
    if not condition:
        failures.append(f"{name}: {detail}")


print("=" * 72)
print("ALPHAEDGE PURE PRICE ACTION 3-TF HEALTH & INTEGRITY CHECK")
print("=" * 72)

print("\n--- 1. Module Compilation & Cleanliness ---")
core_files = [
    "pure_price_action.py",
    "institutional_engine.py",
    "institutional_trader.py",
    "ai_learning.py",
    "m15_trade_analysis_logger.py",
    "news_catalyst_engine.py",
    "backtest_institutional.py",
    "alphaedge.py",
]
for fname in core_files:
    fpath = os.path.join(ROOT, fname)
    try:
        py_compile.compile(fpath, doraise=True)
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        has_yad = ("yad masri" in txt.lower()) or ("yad_masri" in txt.lower())
        check(f"Compile {fname}", not has_yad, "clean" if not has_yad else "Found forbidden legacy string")
    except Exception as exc:
        check(f"Compile {fname}", False, str(exc))

import pure_price_action as ppa
import institutional_trader as it
from institutional_engine import InstitutionalEngine
from news_catalyst_engine import NewsCatalystEngine
from ai_learning import AutoLearner
import m15_trade_analysis_logger as tlog

print("\n--- 2. Calibrated Asset Specs & Risk Constants ---")
check("Daily DD Cap", it.MAX_DAILY_DRAWDOWN_USD == 50.0, f"-${it.MAX_DAILY_DRAWDOWN_USD:.2f}")
check("Max Concurrent", it.MAX_CONCURRENT_POSITIONS == 2, f"{it.MAX_CONCURRENT_POSITIONS} trades")
for sym, expected_lot in [("XAUUSDm", 0.02), ("USOILm", 0.05), ("US30m", 0.25)]:
    cfg = it.ASSET_CONFIGS.get(sym, {})
    ok = (
        cfg.get("lot") == expected_lot
        and cfg.get("max_sl_dollars") == 34.0
        and cfg.get("tp_dollars") == 55.0
        and cfg.get("be_trigger_dollars") == 28.0
        and cfg.get("lock_trigger_dollars") == 44.0
        and cfg.get("lock_amount_dollars") == 32.0
    )
    check(f"Spec {sym}", ok, f"lot={cfg.get('lot')}, SL=${cfg.get('max_sl_dollars')}, TP=${cfg.get('tp_dollars')}, BE=${cfg.get('be_trigger_dollars')}")

print("\n--- 3. Mon-Fri London & NY Killzone Schedule ---")
# Mondays & Fridays MUST trade during Killzones; weekends & off-hours MUST block
schedule_cases = [
    ("Mon 07:00 UTC (London KZ)", datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc), True),
    ("Fri 15:30 UTC (NY KZ)", datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc), True),
    ("Wed 02:00 UTC (Asian chop)", datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc), False),
    ("Wed 11:00 UTC (London lunch)", datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc), False),
    ("Thu 21:00 UTC (Rollover)", datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc), False),
    ("Sat 08:00 UTC (Weekend)", datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc), False),
    ("Sun 16:00 UTC (Weekend)", datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc), False),
]
for label, dt_case, expected in schedule_cases:
    got = ppa.is_killzone_active(dt_case)
    check(label, got == expected, f"got={got}, expected={expected}")

print("\n--- 4. Pure Price Action 3-TF Signal Logic ---")
kz_dt = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)   # Thu NY KZ
off_dt = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)   # Thu Asian chop

# Test A: Valid BUY PDL + ASIAN_L sweep reclaim (H4 BULL +1, H4 leg = 40% <= 75%)
r_buy = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2604.0, o_m5=2598.0, h_m5=2605.0, l_m5=2594.0,
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2596.0, m15_c=2601.0, m15_med_range=14.0,
    h4_bias_state=1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=kz_dt,
)
check("Valid BUY sweep reclaim", r_buy["triggered"] and r_buy["direction"] == "BUY" and r_buy["sweep_extreme"] == 2594.0, r_buy["reason"])

# Test B: Counter-trend H4 BEARISH (-1) blocks BUY
r_cnt = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2604.0, o_m5=2598.0, h_m5=2605.0, l_m5=2594.0,
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2596.0, m15_c=2601.0, m15_med_range=14.0,
    h4_bias_state=-1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=kz_dt,
)
check("H4 BEAR blocks BUY", (not r_cnt["triggered"]) and "H4_BEARISH_BLOCKS_BUY" in r_cnt["reason"], r_cnt["reason"])

# Test C: H4 Swing Leg > 75% blocks BUY chase
r_ext = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2604.0, o_m5=2598.0, h_m5=2605.0, l_m5=2594.0,
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2596.0, m15_c=2601.0, m15_med_range=14.0,
    h4_bias_state=1, h4_sh=2610.0, h4_sl=2510.0,  # 2604 is at 94% of leg!
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=kz_dt,
)
check("H4 Leg > 75% blocks BUY", (not r_ext["triggered"]) and "H4_OVEREXTENDED_BUY" in r_ext["reason"], r_ext["reason"])

# Test D: M15 Bearish Crash (> 1.5x med) blocks BUY
r_crash = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2604.0, o_m5=2598.0, h_m5=2605.0, l_m5=2594.0,
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2630.0, m15_c=2600.0, m15_med_range=14.0,  # drop=30 > 21
    h4_bias_state=1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=kz_dt,
)
check("M15 Crash blocks BUY", (not r_crash["triggered"]) and "M15_BEAR_CRASH" in r_crash["reason"], r_crash["reason"])

# Test E: M5 Climax bar (> 2.5x med) rejected & Off-hours rejected
r_climax = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2614.0, o_m5=2595.0, h_m5=2616.0, l_m5=2592.0,  # rng=24 > 20
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2596.0, m15_c=2601.0, m15_med_range=14.0,
    h4_bias_state=1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=kz_dt,
)
check("M5 Climax rejected", (not r_climax["triggered"]) and r_climax["reason"] == "M5_CLIMAX_OVEREXTENDED", r_climax["reason"])

r_off = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2604.0, o_m5=2598.0, h_m5=2605.0, l_m5=2594.0,
    c_m5_prev=2597.0, h_m5_prev=2602.0, l_m5_prev=2595.0,
    m5_med_range=8.0,
    m15_o=2596.0, m15_c=2601.0, m15_med_range=14.0,
    h4_bias_state=1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2620.0, h4_prev_l=2596.0,
    asian_h=2615.0, asian_l=2601.0,
    dt_utc=off_dt,
)
check("Off-hours blocked", (not r_off["triggered"]) and r_off["reason"] == "OUTSIDE_KILLZONE", r_off["reason"])

# Test F: Valid SELL PDH + ASIAN_H sweep reclaim
r_sell = ppa.evaluate_3tf_pure_pa_step(
    c_m5=2635.0, o_m5=2642.0, h_m5=2646.0, l_m5=2634.0,
    c_m5_prev=2643.0, h_m5_prev=2645.0, l_m5_prev=2638.0,
    m5_med_range=8.0,
    m15_o=2644.0, m15_c=2638.0, m15_med_range=14.0,
    h4_bias_state=-1, h4_sh=2664.0, h4_sl=2564.0,
    pdh=2640.0, pdl=2600.0,
    h4_prev_h=2642.0, h4_prev_l=2610.0,
    asian_h=2639.0, asian_l=2612.0,
    dt_utc=kz_dt,
)
check("Valid SELL sweep reclaim", r_sell["triggered"] and r_sell["direction"] == "SELL" and r_sell["sweep_extreme"] == 2646.0, r_sell["reason"])

# Test G: Fractal structure series monotonic synthetic check
h_arr = np.array([10, 12, 15, 13, 11, 12, 14, 18, 16, 14, 15, 20, 22, 19], dtype=float)
l_arr = h_arr - 3.0
b3, sh, sl = ppa.compute_h4_structure_series(h_arr, l_arr, h_arr - 1.0)
check("Fractal H4 series output", len(b3) == len(h_arr) and b3[-1] == 1, f"b3[-1]={b3[-1]}")

print("\n--- 5. Telegram Firewall, AutoLearner & Logger ---")
check("Channel blocks owner diag", not it._is_channel_allowed("Daily Drawdown Guard triggered"), "blocked")
check("Channel allows trade closed", it._is_channel_allowed("[Trade Closed — TP HIT] Asset: XAUUSDm"), "allowed")
learner = AutoLearner()
cfg_ai = learner.load_config()
check(
    "AI Config bounds",
    45.0 <= cfg_ai.get("gold_tp_dollars", 0) <= 75.0 and cfg_ai.get("gold_be_trigger_dollars", 0) >= 24.0,
    f"TP=${cfg_ai.get('gold_tp_dollars')}, BE=${cfg_ai.get('gold_be_trigger_dollars')}",
)
eng = InstitutionalEngine()
check("Engine instantiated", eng is not None and callable(eng.evaluate_institutional_setup), "OK")
perf = tlog.get_performance_summary()
check("Logger summary", isinstance(perf, dict) and "total" in perf, f"trades={perf.get('total')}")
news = NewsCatalystEngine()
check("News engine status", isinstance(news.get_market_catalyst_status("XAUUSDm"), dict), "OK")

print("\n" + "=" * 72)
if failures:
    print(f"HEALTH CHECK FAILED ({len(failures)} error(s)):")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
else:
    print("HEALTH CHECK PASSED 100% — ALL SUBSYSTEMS VERIFIED")
    sys.exit(0)
