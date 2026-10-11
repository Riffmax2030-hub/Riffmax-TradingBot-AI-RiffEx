# 📈 Riffmax-TradingBot-AI-RiffEx

> **Autonomous AI Trading Agent powered by Python and MetaTrader 5 (MT5) MCP** — real-time market structure analysis, automated signal generation, intelligent order execution, and strict risk guardrails.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![MetaTrader 5](https://img.shields.io/badge/MetaTrader-5-green)](https://www.mql5.com/)
[![MCP Protocol](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-orange)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: Active](https://img.shields.io/badge/Status-Active-brightgreen)]()

---

## 📌 What it does

**Riffmax-TradingBot-AI-RiffEx** is an autonomous trading agent that operates directly on **MetaTrader 5 (MT5)** using Python and the **Model Context Protocol (MCP)**:

- 📊 **Real-Time Market Analysis** — Ingests live tick data, OHLCV candles, order books, and open positions directly through MT5 MCP tools.
- 🧠 **AI Decision Engine** — Analyzes price action, market liquidity, and volatility metrics using Python-based quantitative models and LLM agent reasoning.
- ⚡ **Direct Order Execution** — Places, tracks, modifies, and closes market and pending orders programmatically via the MT5 MCP interface.
- 🛡️ **Autonomous Risk Guardrails** — Enforces dynamic lot sizing based on account equity, automatic stop-loss (SL), take-profit (TP), maximum drawdown thresholds, and spread filtering.
- 📝 **Structured Logging & Auditing** — Records all signal evaluations, execution payloads, and deal receipts in JSON/CSV audit trails.

---

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────┐
│             AI Agent Decision Layer (Python)           │
│   • Technical Analysis  • Price Action  • Volatility   │
└───────────────────────────┬────────────────────────────┘
                            │
               Model Context Protocol (MCP)
                            │
┌───────────────────────────▼────────────────────────────┐
│                    MT5 MCP Server                      │
│   • get_candles_latest   • get_account_info            │
│   • place_market_order   • modify_position             │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│               MetaTrader 5 (MT5) Terminal              │
│       Broker Execution · Live Market Liquidity         │
└────────────────────────────────────────────────────────┘
```

---

## 🛠️ Stack

| Component | Technology | Description |
|---|---|---|
| **Language** | Python 3.10+ | Core trading engine and quantitative analysis |
| **Execution Protocol** | MT5 Model Context Protocol (MCP) | Tool-based interface for market data & order dispatch |
| **Broker Platform** | MetaTrader 5 (MT5) | Multi-asset execution terminal (Forex, Indices, Crypto) |
| **Data Processing** | NumPy, Pandas | Candle parsing, moving averages, volatility metrics |
| **Risk Management** | Custom Python Engine | Lot sizing, trailing stops, daily loss limits |
| **Configuration** | `.env` + `config.yaml` | Secure credential isolation |

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- MetaTrader 5 terminal installed and logged into your broker account
- MT5 MCP server configured

### 1. Clone & Install

```bash
git clone https://github.com/Riffmax2030-hub/Riffmax-TradingBot-AI-RiffEx.git
cd Riffmax-TradingBot-AI-RiffEx
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials — NEVER commit real .env files
```

`.env.example` template:
```env
MT5_ACCOUNT=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=your_broker_server
MAX_RISK_PER_TRADE_PERCENT=1.0
MAX_DAILY_DRAWDOWN_PERCENT=3.0
```

### 3. Run the Trading Agent

```bash
python main.py
```

---

## 📁 Project Structure

```
Riffmax-TradingBot-AI-RiffEx/
├── agent/
│   ├── market_analyzer.py    # Price action & indicator analysis
│   ├── decision_engine.py    # Signal generation logic
│   ├── mcp_bridge.py         # MT5 Model Context Protocol interface
│   └── risk_guard.py         # Dynamic lot sizing & risk limits
├── config/
│   └── config.yaml           # Symbol lists, timeframe, max spread
├── logs/                     # Trade logs & audit trails (gitignored)
├── tests/
│   ├── test_market_analyzer.py
│   └── test_risk_guard.py
├── .env.example              # Template with placeholders
├── .gitignore                # Excludes credentials, logs, caches
├── requirements.txt
└── README.md
```

---

## 🔐 Security & Risk Disclaimer

> [!CAUTION]
> **Financial & Credential Security:**
> - Never hardcode broker account numbers, server details, or passwords in code or commits.
> - All sensitive configurations are loaded strictly via environment variables.
> - Trading financial markets carries risk. Use a demo account for testing before any live deployment.

---

## 📄 License

MIT © [Sherif Akanmu](https://github.com/Riffmax2030-hub)
