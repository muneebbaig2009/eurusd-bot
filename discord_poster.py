"""Post signals and results to Discord via webhook (no bot needed)."""
import requests
import config


def _send(embed: dict):
    if not config.DISCORD_WEBHOOK_URL:
        title = embed.get("title", "").encode("ascii", errors="replace").decode("ascii")
        print(f"[discord] No webhook configured; skipping: {title}")
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
                demo_balance: float = None, mt5_ticket: int = None):
    """New signal alert. Shows lot size, pip distances, and dollar risk/reward."""
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
    sl_usd   = _usd(sl_pips,  lot)
    tp1_usd  = _usd(tp1_pips, lot)

    tp2_line = ""
    if tp2:
        tp2_pips = _pips(entry, tp2)
        tp2_usd  = _usd(tp2_pips, lot)
        tp2_line = f"\nTP2 (info)  `{tp2}`  +{tp2_pips:.1f} pips  `+${tp2_usd:.2f}`"

    contributors = "  ·  ".join(
        f"{t}({'+' if v > 0 else ''}{v})"
        for t, v in sig["contributors"].items() if v != 0
    )

    embed = {
        "title": f"{arrow} {sig['direction']} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry",
             "value": f"`{entry}`",
             "inline": True},
            {"name": "Stop Loss",
             "value": f"`{sl}`\n−{sl_pips:.1f} pips  `−${sl_usd:.2f}`",
             "inline": True},
            {"name": "Take Profit",
             "value": f"`{tp1}`\n+{tp1_pips:.1f} pips  `+${tp1_usd:.2f}`{tp2_line}",
             "inline": True},
            {"name": "Lot Size",
             "value": f"**{lot} lots**  (2% risk)",
             "inline": True},
            {"name": "R : R",
             "value": f"`1 : {sig.get('rr', '—')}`",
             "inline": True},
            {"name": "Demo Balance",
             "value": f"`${bal:.2f}`",
             "inline": True},
            {"name": "Confidence",
             "value": f"`{sig.get('confidence', '—')}%`",
             "inline": True},
            {"name": "Trend Strength",
             "value": f"`{sig.get('trend_strength', '—')}/100`",
             "inline": True},
            {"name": "Timeframe",
             "value": f"`{sig.get('timeframe', '1h')}`",
             "inline": True},
            {"name": "Reasons",
             "value": contributors or "—",
             "inline": False},
            {"name": "MT5 Ticket",
             "value": f"`#{mt5_ticket}`" if mt5_ticket else "`pending`",
             "inline": True},
            {"name": "MT5 Balance",
             "value": f"`${bal:,.2f}`",
             "inline": True},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_result(signal_id: int, direction: str, status: str,
                entry: float, close_price: float, stats: dict,
                symbol: str = "EUR/USD",
                pnl: float = None, new_balance: float = None,
                lot_size: float = None):
    """Trade close alert (WIN or LOSS). Shows pip move and dollar P&L."""
    won   = status == "WIN"
    color = 0x3fb950 if won else 0xf85149
    emoji = "✅" if won else "❌"

    pip_move = _pips(entry, close_price)
    lot_str  = f"{lot_size} lots" if lot_size else "—"

    if pnl is not None:
        sign      = "+" if pnl >= 0 else ""
        pip_sign  = "+" if won else "−"
        pnl_str   = f"**{sign}${abs(pnl):.2f}**  ({pip_sign}{pip_move:.1f} pips)"
        pnl_emoji = "💰" if pnl >= 0 else "💸"
    else:
        pnl_str, pnl_emoji = "—", "💰"

    if new_balance is not None:
        initial    = config.DEMO_INITIAL_BALANCE
        total_pnl  = round(new_balance - initial, 2)
        return_pct = round((new_balance / initial - 1) * 100, 1)
        ret_sign   = "+" if return_pct >= 0 else ""
        bal_str = (f"**${new_balance:.2f}**  "
                   f"({ret_sign}{return_pct:.1f}%  ·  "
                   f"total P&L {'+' if total_pnl >= 0 else ''}${total_pnl:.2f})")
    else:
        bal_str = "—"

    record_str = (f"W **{stats['wins']}**  ·  L **{stats['losses']}**"
                  f"  ·  Win rate **{stats['win_rate']}%**")

    embed = {
        "title": f"{emoji} {status} — {direction} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry",
             "value": f"`{entry}`",
             "inline": True},
            {"name": "Closed at",
             "value": f"`{round(close_price, 5)}`",
             "inline": True},
            {"name": "Lot Size",
             "value": f"`{lot_str}`",
             "inline": True},
            {"name": f"{pnl_emoji} Trade P&L",
             "value": pnl_str,
             "inline": False},
            {"name": "Demo Balance",
             "value": bal_str,
             "inline": False},
            {"name": "Overall Record",
             "value": record_str,
             "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)
