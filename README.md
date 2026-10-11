# 🤖 MT5 TradingView AI Agent

> **MetaTrader 5 ↔ TradingView bridge powered by AI** — live signal ingestion, intelligent order execution, and real-time risk management for automated forex/crypto trading.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![MQL5](https://img.shields.io/badge/MQL5-MetaTrader5-green)](https://www.mql5.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: Active](https://img.shields.io/badge/Status-Active-brightgreen)]()

---

## 📌 What it does

This project bridges **TradingView Pine Script alerts** to **MetaTrader 5 (MT5)** execution using a Python AI agent that:

- 📡 **Receives webhooks** from TradingView alerts via a Flask server
- 🧠 **Applies AI signal filtering** — classifies signals as high/low confidence before executing
- ⚡ **Places market/pending orders** on MT5 programmatically using the `MetaTrader5` Python library
- 🛡️ **Manages risk** — auto-calculates lot sizes, enforces stop-loss/take-profit rules
- 📊 **Logs all trades** to JSON + CSV for post-trade analysis

---

## 🏗️ Architecture

```
TradingView Alert
      │  (HTTP webhook / POST)
      ▼
┌─────────────────────┐
│  Flask Webhook Server│  ← agent/webhook_server.py
└────────┬────────────┘
         │  parsed signal JSON
         ▼
┌─────────────────────┐
│  AI Signal Filter   │  ← agent/signal_classifier.py
│  (confidence score) │
└────────┬────────────┘
         │  approved signal
         ▼
┌─────────────────────┐
│  MT5 Order Executor │  ← agent/mt5_executor.py
│  + Risk Manager     │
└────────┬────────────┘
         │
┌─────────────────────┐
│  Trade Logger       │  ← logs/trades.json
└─────────────────────┘
```

---

## 🛠️ Stack

| Layer | Technology |
|---|---|
| Signal source | TradingView Pine Script + Alerts |
| Signal receiver | Python / Flask |
| AI filtering | scikit-learn / rule-based classifier |
| MT5 execution | `MetaTrader5` Python API |
| Logging | JSON + CSV |
| Config | `.env` + `config.yaml` |

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- MetaTrader 5 terminal installed
- TradingView Pro+ account (for webhook alerts)

### 1. Clone & install

```bash
git clone https://github.com/Riffmax2030-hub/mt5-tradingview-ai-agent.git
cd mt5-tradingview-ai-agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your MT5 credentials in .env — NEVER commit this file
```

`.env.example` template (safe to commit):
```env
MT5_LOGIN=your_account_number
MT5_SERVER=your_broker_server
MT5_PASSWORD=your_password
WEBHOOK_SECRET=random_secret_key
NGROK_AUTHTOKEN=your_ngrok_token
```

### 3. Run the agent

```bash
python agent/webhook_server.py
```

### 4. TradingView webhook payload

```json
{
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "price": {{close}},
  "timeframe": "{{interval}}"
}
```

---

## 📁 Project Structure

```
mt5-tradingview-ai-agent/
├── agent/
│   ├── webhook_server.py      # Flask server — receives TradingView alerts
│   ├── signal_classifier.py   # AI confidence filter
│   ├── mt5_executor.py        # MT5 order placement
│   └── risk_manager.py        # Lot size & SL/TP calculator
├── logs/                      # Trade logs (gitignored)
├── config/
│   └── config.yaml            # Symbol settings, risk params
├── tests/
│   ├── test_signal_classifier.py
│   └── test_risk_manager.py
├── .env.example               # Safe template — commit this
├── .gitignore                 # Excludes .env, logs, __pycache__
├── requirements.txt
└── README.md
```

---

## 🔐 Security

> [!CAUTION]
> **Never commit MT5 credentials, API keys, or tokens.**
> All secrets load from `.env` via `python-dotenv`. The `.env` file is listed in `.gitignore`.

Audit for leaked secrets:
```bash
git log --all --full-history -- "*.env"
git grep -i "password\|secret\|login" $(git rev-list --all)
```

---

## 🧪 Tests

```bash
pytest tests/ -v
```

---

## 📄 License

MIT © [Sherif Akanmu](https://github.com/Riffmax2030-hub)
