"""
m15_trade_analysis_logger.py — Dedicated Trade Analysis Logger for AlphaEdge M15 Engine
=======================================================================================
Tracks every trade executed starting from September 10, 2026.
Records detailed performance metrics to 'm15_trade_analysis.csv' for comprehensive post-trade analysis.
"""

import os
import csv
from datetime import datetime, timezone
import MetaTrader5 as mt5

CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m15_trade_analysis.csv")

CSV_HEADERS = [
    "ticket",
    "symbol",
    "direction",
    "lot",
    "open_time",
    "open_price",
    "sl_initial",
    "tp_target",
    "target_metric",
    "be_activated",
    "news_catalyst",
    "m15_ut_stop",
    "m15_atr",
    "close_time",
    "close_price",
    "pnl_usd",
    "exit_reason",
    "hold_duration_mins",
    "status"
]

def _ensure_csv_file():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

def log_trade_opened(ticket, symbol, direction, lot, open_price, sl, tp, target_metric, news_catalyst, ut_stop, atr):
    """Logs a newly opened M15 trade."""
    _ensure_csv_file()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "ticket": ticket,
        "symbol": symbol,
        "direction": direction,
        "lot": lot,
        "open_time": now_str,
        "open_price": round(open_price, 2 if symbol == "XAUUSDm" else 1),
        "sl_initial": round(sl, 2 if symbol == "XAUUSDm" else 1),
        "tp_target": round(tp, 2 if symbol == "XAUUSDm" else 1),
        "target_metric": target_metric,
        "be_activated": "False",
        "news_catalyst": news_catalyst,
        "m15_ut_stop": round(ut_stop, 2 if symbol == "XAUUSDm" else 1),
        "m15_atr": round(atr, 2 if symbol == "XAUUSDm" else 1),
        "close_time": "",
        "close_price": "",
        "pnl_usd": "",
        "exit_reason": "",
        "hold_duration_mins": "",
        "status": "OPEN"
    }
    with open(CSV_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(row)

def update_trade_be(ticket):
    """Updates a trade row to mark Break-Even as activated."""
    if not os.path.exists(CSV_FILE):
        return
    rows = []
    updated = False
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("ticket")) == str(ticket) and row.get("status") == "OPEN":
                row["be_activated"] = "True"
                updated = True
            rows.append(row)
    if updated:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

def log_trade_closed(ticket, close_price, pnl_usd, exit_reason):
    """Updates a trade row when closed with final exit price, realized profit, and exit reason."""
    if not os.path.exists(CSV_FILE):
        return
    rows = []
    updated = False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("ticket")) == str(ticket) and row.get("status") == "OPEN":
                row["close_time"] = now_str
                row["close_price"] = round(close_price, 2)
                row["pnl_usd"] = round(pnl_usd, 2)
                row["exit_reason"] = exit_reason
                row["status"] = "CLOSED"
                try:
                    open_dt = datetime.strptime(row["open_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    close_dt = datetime.now(timezone.utc)
                    duration = (close_dt - open_dt).total_seconds() / 60.0
                    row["hold_duration_mins"] = round(duration, 1)
                except Exception:
                    row["hold_duration_mins"] = ""
                updated = True
            rows.append(row)
    if updated:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

def sync_closed_trades_from_history():
    """Scans MT5 deal history to automatically close out any trades that hit TP or SL on broker side."""
    if not os.path.exists(CSV_FILE):
        return
    rows = []
    open_tickets = []
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") == "OPEN":
                open_tickets.append(str(row.get("ticket")))
            rows.append(row)
            
    if not open_tickets:
        return
        
    deals = mt5.history_deals_get(datetime.now(timezone.utc).timestamp() - (14 * 86400), datetime.now(timezone.utc).timestamp() + 3600)
    if not deals:
        return

    closed_notifications = []
    updated = False
    for deal in deals:
        pos_id = str(deal.position_id)
        if pos_id in open_tickets and deal.entry == mt5.DEAL_ENTRY_OUT:
            for row in rows:
                if str(row.get("ticket")) == pos_id and row.get("status") == "OPEN":
                    row["close_time"] = datetime.fromtimestamp(deal.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    sym = row.get("symbol", "")
                    row["close_price"] = round(deal.price, 3 if "OIL" in sym else 2)
                    row["pnl_usd"] = round(deal.profit, 2)
                    # Determine exit reason using broker deal.reason and relative price tolerance
                    open_val = float(row.get("open_price", 0.0) or 0.0)
                    tp_val = float(row.get("tp_target", 0.0) or 0.0)
                    is_be = (row.get("be_activated") == "True")
                    deal_reason = getattr(deal, "reason", None)
                    tp_tol = max(abs(tp_val - open_val) * 0.05, 0.005) if tp_val > 0 and open_val > 0 else 0.05

                    if deal.profit > 0 and (deal_reason == getattr(mt5, "DEAL_REASON_TP", 4) or (tp_val > 0 and abs(deal.price - tp_val) <= tp_tol)):
                        reason = "TP_HIT"
                    elif is_be and abs(deal.profit) <= 3.0 and deal.profit >= 0:
                        reason = "BE_PROTECTED"
                    elif deal.profit > 0 and is_be:
                        reason = "LOCK_HIT"
                    elif deal.profit < 0:
                        reason = "SL_HIT"
                    else:
                        reason = "EXIT_CLOSED"

                    row["exit_reason"] = reason
                    row["status"] = "CLOSED"
                    try:
                        open_dt = datetime.strptime(row["open_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        close_dt = datetime.fromtimestamp(deal.time, tz=timezone.utc)
                        row["hold_duration_mins"] = round((close_dt - open_dt).total_seconds() / 60.0, 1)
                    except Exception:
                        pass

                    closed_notifications.append({
                        "symbol": row.get("symbol", ""),
                        "direction": row.get("direction", ""),
                        "open_price": row.get("open_price", ""),
                        "close_price": row["close_price"],
                        "pnl_usd": row["pnl_usd"],
                        "reason": reason
                    })

                    updated = True
                    open_tickets.remove(pos_id)

    if updated:
        with open(CSV_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

        # Broadcast each trade result (Win/Loss) to both channel and personal DM
        for item in closed_notifications:
            try:
                from institutional_trader import send_trade_close_broadcast
                send_trade_close_broadcast(
                    symbol=item["symbol"],
                    direction=item["direction"],
                    open_price=item["open_price"],
                    close_price=item["close_price"],
                    pnl_usd=item["pnl_usd"],
                    reason=item["reason"]
                )
            except Exception:
                pass

def get_performance_summary():
    """Returns a dict of metrics from m15_trade_analysis.csv."""
    if not os.path.exists(CSV_FILE):
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0}
    with open(CSV_FILE, mode='r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        trades = [r for r in reader if r.get("status") == "CLOSED"]
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0}
    pnls = [float(t["pnl_usd"]) for t in trades if t["pnl_usd"] != ""]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = len(pnls)
    wr = round((len(wins) / total) * 100, 1) if total > 0 else 0.0
    net = round(sum(pnls), 2)
    return {"total": total, "wins": len(wins), "losses": len(losses), "win_rate": wr, "net_pnl": net}
