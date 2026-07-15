# MT5 Local Runner — Setup Guide

## What this does

`run_local.py` replaces GitHub Actions for live execution. It:
- Connects to your MetaTrader 5 terminal
- Fetches candles from MT5 (instead of Twelve Data)
- Places market orders on your **demo account** with SL and TP set
- Tracks order results by querying MT5 position history
- Pushes the dashboard JSON to GitHub after every cycle

**Demo-only**: the bot refuses to connect to real-money accounts at startup.

---

## Prerequisites

1. **MT5 terminal installed and running** — must be open and logged in before you start the bot
2. **AutoTrading enabled** — click the "Algo Trading" button in the toolbar (turns green)
3. **Python 3.11+** with packages installed

---

## One-time setup

### 1. Install the MetaTrader5 Python package

```powershell
pip install MetaTrader5
```

### 2. Set environment variables (already done if you followed the setup chat)

```powershell
[System.Environment]::SetEnvironmentVariable("MT5_LOGIN",    "109664870",      "User")
[System.Environment]::SetEnvironmentVariable("MT5_SERVER",   "MetaQuotes-Demo","User")
[System.Environment]::SetEnvironmentVariable("MT5_PASSWORD", "YOUR_PASSWORD",  "User")
```

Verify:
```powershell
[System.Environment]::GetEnvironmentVariable("MT5_LOGIN",    "User")
[System.Environment]::GetEnvironmentVariable("MT5_SERVER",   "User")
[System.Environment]::GetEnvironmentVariable("MT5_PASSWORD", "User")
```

### 3. Make sure git remote is set

```powershell
git remote -v
```

Should show your GitHub repo. If not:
```powershell
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
```

---

## Running the bot

**Open a new PowerShell window** (so the env vars are loaded), then:

```powershell
cd e:\eurusd-bot
python run_local.py
```

Stop with `Ctrl+C`. MT5 terminal must remain open while the bot is running.

---

## How it works cycle-by-cycle

Every 15 minutes:

1. Fetches 200 candles per timeframe from the MT5 terminal (EURUSD, GBPUSD)
2. Checks all OPEN signals — queries MT5 for whether the tracked position closed
3. If a position closed → records WIN/LOSS, updates balance, posts to Discord
4. If no open signal → runs signal engine; if a signal fires, places a market order with SL+TP
5. Exports dashboard JSON → pushes to GitHub Pages (`master:main`)

---

## Safety checks

| Check | Where | What happens |
|-------|-------|--------------|
| Real account detected | `mt5_executor.connect()` | Bot shuts down immediately, raises error |
| MT5 order rejected | `mt5_executor.open_trade()` | Signal cancelled in DB (logged as LOSS at entry) |
| Data fetch failure | `main.run_pair()` | Cycle skipped, error printed, next cycle continues |
| Git push failure | `run_local.py` | Non-fatal, logged, bot keeps running |

---

## Running sim mode (GitHub Actions / no MT5)

The default `EXECUTION_MODE=sim` is used by GitHub Actions automatically.
Do **not** set `EXECUTION_MODE=mt5` as a GitHub Secret — it would fail in CI.

If you want to test sim mode locally:
```powershell
$env:EXECUTION_MODE = "sim"
python main.py
```

---

## Troubleshooting

**"MT5 initialize() failed"**
→ MT5 terminal is not running. Open it and log in first.

**"MT5 login failed"**
→ Wrong login/password/server. Check env vars match exactly what's in the terminal title bar.

**"SAFETY BLOCK: real-money account"**
→ You logged into a live account. Switch to the demo account in MT5.

**"order_send failed: retcode=10004"** (requote)
→ Price moved during order. The bot will retry next cycle.

**"order_send failed: retcode=10014"** (invalid volume)
→ Lot size below broker minimum. Balance too low to meet 2% risk at minimum lot.
