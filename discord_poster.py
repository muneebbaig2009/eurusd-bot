"""Post signals and results to Discord via webhook (no bot needed)."""
import requests
import config


def _send(embed: dict):
    if not config.DISCORD_WEBHOOK_URL:
        print("[discord] No webhook configured; skipping. Embed:", embed["title"])
        return
    resp = requests.post(
        config.DISCORD_WEBHOOK_URL,
        json={"embeds": [embed]},
        timeout=30,
    )
    resp.raise_for_status()


def _pips(price_a: float, price_b: float) -> float:
    return round(abs(price_a - price_b) / config.PIP_SIZE, 1)


def _usd(pips: float, lot_size: float) -> float:
    return round(pips * lot_size * config.PIP_VALUE_PER_LOT, 2)


def post_signal(sig: dict, signal_id: int, symbol: str = "EUR/USD",
                demo_balance: float = None, id2: int = None):
    """New dual-trade signal alert.

    Shows Trade 1 (TP1) and Trade 2 (TP2) with individual lot sizes,
    pip distances, and dollar risk / reward for each trade.
    """
    color = 0x3fb950 if sig["direction"] == "BUY" else 0xf85149
    arrow = "📈" if sig["direction"] == "BUY" else "📉"

    entry = sig["entry"]
    sl    = sig["sl"]
    tp1   = sig.get("tp1") or sig["tp"]
    tp2   = sig.get("tp2")
    lot   = sig.get("lot_size", config.MIN_LOT)
    bal   = demo_balance if demo_balance is not None else config.DEMO_INITIAL_BALANCE

    sl_pips  = _pips(entry, sl)
    tp1_pips = _pips(entry, tp1)
    sl_usd   = _usd(sl_pips, lot)
    tp1_usd  = _usd(tp1_pips, lot)

    id_str = f"#{signal_id}" if id2 is None else f"#{signal_id} + #{id2}"
    trade_label = "2 Trades Open" if id2 else "Trade Open"

    if id2 and tp2:
        tp2_pips = _pips(entry, tp2)
        tp2_usd  = _usd(tp2_pips, lot)
        tp_field = (
            f"**T1** `{tp1}`  +{tp1_pips:.1f} pips  `+${tp1_usd:.2f}`\n"
            f"**T2** `{tp2}`  +{tp2_pips:.1f} pips  `+${tp2_usd:.2f}`"
        )
        lot_field = f"**{lot} lots × 2** (1% risk each)"
        total_risk = round(sl_usd * 2, 2)
        sl_field = f"`{sl}`  −{sl_pips:.1f} pips  `−${total_risk:.2f}` total"
    else:
        tp_field  = f"`{tp1}`  +{tp1_pips:.1f} pips  `+${tp1_usd:.2f}`"
        lot_field = f"**{lot} lots**"
        sl_field  = f"`{sl}`  −{sl_pips:.1f} pips  `−${sl_usd:.2f}`"

    contributors = "  ·  ".join(
        f"{t}({'+' if v > 0 else ''}{v})"
        for t, v in sig["contributors"].items() if v != 0
    )

    embed = {
        "title":  f"{arrow} {trade_label} — {sig['direction']} {symbol}  ({id_str})",
        "color":  color,
        "fields": [
            {"name": "Entry",       "value": f"`{entry}`", "inline": True},
            {"name": "Stop Loss",   "value": sl_field,     "inline": True},
            {"name": "Take Profit", "value": tp_field,     "inline": True},
            {"name": "Lot Size",    "value": lot_field,    "inline": True},
            {"name": "R : R",       "value": f"`1 : {sig.get('rr', '—')}`", "inline": True},
            {"name": "Demo Balance","value": f"`${bal:.2f}`", "inline": True},
            {"name": "Confidence",  "value": f"`{sig.get('confidence', '—')}%`",  "inline": True},
            {"name": "Trend",       "value": f"`{sig.get('trend_strength', '—')}/100`", "inline": True},
            {"name": "Timeframe",   "value": f"`{sig.get('timeframe', '1h')}`",   "inline": True},
            {"name": "Reasons",     "value": contributors or "—", "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_tp1_win(signal_id: int, direction: str, entry: float, tp1_close: float,
                 tp2: float, pnl: float, new_balance: float,
                 moved_t2: bool = True, symbol: str = "EUR/USD"):
    """Mid-trade alert: Trade 1 closed at TP1, Trade 2 SL moved to entry."""
    color = 0x2d7d46  # dark green (partial win)
    sl_status = "SL → Entry (breakeven) 🔒" if moved_t2 else "SL update pending"
    pip_win  = _pips(entry, tp1_close)
    pnl_str  = f"+${pnl:.2f}  (+{pip_win:.1f} pips)"

    initial    = config.DEMO_INITIAL_BALANCE
    total_pnl  = round(new_balance - initial, 2)
    ret_sign   = "+" if total_pnl >= 0 else ""
    return_pct = round((new_balance / initial - 1) * 100, 1)
    bal_str    = (f"**${new_balance:.2f}**  "
                  f"({ret_sign}{return_pct:.1f}%  ·  total {ret_sign}${total_pnl:.2f})")

    tp2_str = f"`{tp2}`  +{_pips(entry, tp2):.1f} pips" if tp2 else "—"

    embed = {
        "title":  f"✅ TP1 HIT — {direction} {symbol}  (T1 #{signal_id})",
        "color":  color,
        "description": (
            "Trade 1 closed at TP1 profit.\n"
            f"Trade 2 now runs risk-free toward TP2. **{sl_status}**"
        ),
        "fields": [
            {"name": "Entry",      "value": f"`{entry}`",      "inline": True},
            {"name": "TP1 Closed", "value": f"`{tp1_close}`",  "inline": True},
            {"name": "T1 P&L",     "value": pnl_str,           "inline": True},
            {"name": "T2 Target",  "value": tp2_str,           "inline": True},
            {"name": "T2 Stop",    "value": f"`{entry}` (entry = breakeven)", "inline": True},
            {"name": "Balance",    "value": bal_str,           "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_pair_sl(signal_id: int, direction: str, sl_price: float,
                 total_pnl: float, new_balance: float, symbol: str = "EUR/USD"):
    """Both trades closed at SL (before TP1 was reached)."""
    color   = 0xf85149
    pnl_str = f"${total_pnl:.2f}"  # already negative

    initial    = config.DEMO_INITIAL_BALANCE
    total_net  = round(new_balance - initial, 2)
    ret_sign   = "+" if total_net >= 0 else ""
    return_pct = round((new_balance / initial - 1) * 100, 1)
    bal_str    = (f"**${new_balance:.2f}**  "
                  f"({ret_sign}{return_pct:.1f}%  ·  total {ret_sign}${total_net:.2f})")

    embed = {
        "title":  f"❌ BOTH TRADES SL HIT — {direction} {symbol}  (#{signal_id})",
        "color":  color,
        "description": "SL was hit before TP1. Both Trade 1 and Trade 2 closed.",
        "fields": [
            {"name": "SL Triggered", "value": f"`{sl_price}`", "inline": True},
            {"name": "Combined Loss", "value": pnl_str,        "inline": True},
            {"name": "Balance",       "value": bal_str,        "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_result(signal_id: int, direction: str, status: str,
                entry: float, close_price: float, stats: dict,
                symbol: str = "EUR/USD",
                pnl: float = None, new_balance: float = None,
                lot_size: float = None, trade_num: int = None):
    """Trade 2 close alert: WIN (TP2), BREAKEVEN, or legacy single-trade result."""
    if status == "WIN":
        color, emoji = 0x3fb950, "✅"
    elif status == "BREAKEVEN":
        color, emoji = 0xd29922, "↩️"  # amber
    else:
        color, emoji = 0xf85149, "❌"

    pip_move = _pips(entry, close_price)
    lot_str  = f"{lot_size} lots" if lot_size else "—"

    if pnl is not None:
        if status == "BREAKEVEN":
            pnl_str  = f"**$0.00**  (0.0 pips) — risk-free exit"
            pnl_emoji = "🔄"
        elif pnl >= 0:
            pnl_str  = f"**+${pnl:.2f}**  (+{pip_move:.1f} pips)"
            pnl_emoji = "💰"
        else:
            pnl_str  = f"**${pnl:.2f}**  (−{pip_move:.1f} pips)"
            pnl_emoji = "💸"
    else:
        pnl_str, pnl_emoji = "—", "💰"

    if new_balance is not None:
        initial    = config.DEMO_INITIAL_BALANCE
        total_pnl  = round(new_balance - initial, 2)
        ret_sign   = "+" if total_pnl >= 0 else ""
        return_pct = round((new_balance / initial - 1) * 100, 1)
        bal_str = (f"**${new_balance:.2f}**  "
                   f"({ret_sign}{return_pct:.1f}%  ·  total P&L {ret_sign}${total_pnl:.2f})")
    else:
        bal_str = "—"

    record_str = (f"W **{stats['wins']}**  ·  L **{stats['losses']}**"
                  f"  ·  Win rate **{stats['win_rate']}%**")

    t_label = f"T{trade_num} " if trade_num and trade_num > 1 else ""
    title = f"{emoji} {t_label}{status} — {direction} {symbol}  (#{signal_id})"

    embed = {
        "title":  title,
        "color":  color,
        "fields": [
            {"name": "Entry",    "value": f"`{entry}`",              "inline": True},
            {"name": "Closed",   "value": f"`{round(close_price,5)}`","inline": True},
            {"name": "Lot Size", "value": f"`{lot_str}`",            "inline": True},
            {"name": f"{pnl_emoji} P&L", "value": pnl_str,          "inline": False},
            {"name": "Balance",  "value": bal_str,                   "inline": False},
            {"name": "Record",   "value": record_str,                "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)
