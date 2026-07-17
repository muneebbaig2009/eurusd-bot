"""
Shared-account simulation: $100 split across both pairs simultaneously.
Loads the trade list from backtest_360_results.json, sorts by entry time,
and re-simulates with a single shared balance (2% risk per trade).
"""
import json, config

PIP_SIZE  = config.PIP_SIZE
PIP_VAL   = config.PIP_VALUE_PER_LOT
MIN_LOT   = config.MIN_LOT
LOT_STEP  = config.LOT_STEP
RISK_PCT  = 0.02
START_BAL = 100.0

with open("backtest_360_results.json") as f:
    d = json.load(f)

# Merge both pairs' trades into one timeline
all_trades = []
for pair in ["EUR/USD", "GBP/USD"]:
    for t in d["trades"][pair]:
        all_trades.append({
            "pair":      pair,
            "time":      t["time"],
            "direction": t["direction"],
            "entry":     t["entry"],
            "sl":        t["sl"],
            "pnl_pips":  t["pnl_pips"],
            "result":    t["result"],
        })

all_trades.sort(key=lambda x: x["time"])

balance = START_BAL
equity  = [balance]
wins = losses = timeouts = 0
total_lots = 0.0
lot_hist = []

for t in all_trades:
    sl_pips = abs(t["entry"] - t["sl"]) / PIP_SIZE
    if sl_pips <= 0:
        lot = MIN_LOT
    else:
        lot = (balance * RISK_PCT) / (sl_pips * PIP_VAL)
        lot = max(MIN_LOT, round(lot / LOT_STEP) * LOT_STEP)
        lot = min(config.MAX_LOT, lot)

    pnl = t["pnl_pips"] * lot * PIP_VAL
    balance += pnl
    equity.append(round(balance, 4))
    lot_hist.append(lot)

    if   t["result"] == "WIN":     wins     += 1
    elif t["result"] == "LOSS":    losses   += 1
    else:                          timeouts += 1

n = len(all_trades)
net = balance - START_BAL
roi = net / START_BAL * 100

print("=" * 54)
print(f"Shared $100 Account — Both Pairs Combined")
print("=" * 54)
print(f"  Starting balance : ${START_BAL:.2f}")
print(f"  Final balance    : ${balance:.2f}")
print(f"  Net P&L          : ${net:+.2f}  ({roi:+.2f}%)")
print(f"  Total trades     : {n}  (W {wins}  L {losses}  T {timeouts})")
print(f"  Lot range        : {min(lot_hist):.2f} – {max(lot_hist):.2f}")
print(f"  Trades at 0.01   : {lot_hist.count(0.01)} / {n}")
print(f"  Trades at 0.02+  : {sum(1 for x in lot_hist if x >= 0.02)} / {n}")
max_bal = max(equity)
max_dd  = max((max_bal - e) / max_bal * 100 for e in equity)
print(f"  Max balance seen : ${max_bal:.2f}")
print(f"  Max drawdown     : {max_dd:.2f}%")
print("=" * 54)
print()
print("Compare with separate $100 per pair:")
print("  EUR/USD alone : $164.20  (+$64.20)")
print("  GBP/USD alone : $203.82  (+$103.82)")
print(f"  Shared $100   : ${balance:.2f}  ({net:+.2f})")
print()
print("Why they differ:")
print("  Shared lot sizing tracks one balance instead of two.")
print("  Losses on one pair reduce the pot for the other.")
print("  Both pairs still hit 0.01 min-lot for most trades.")
