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


def post_signal(sig: dict, signal_id: int, symbol: str = "EUR/USD",
                demo_balance: float = None):
    """Posted when a new signal fires. Includes demo account risk/reward preview."""
    color = 0x3fb950 if sig["direction"] == "BUY" else 0xf85149
    arrow = "📈" if sig["direction"] == "BUY" else "📉"
    contributors = "  ·  ".join(
        f"{t}({'+' if v > 0 else ''}{v})"
        for t, v in sig["contributors"].items() if v != 0
    )

    rr  = sig.get("rr") or 1.5
    risk    = config.DEMO_RISK_PER_TRADE
    reward  = round(risk * rr, 2)
    balance = demo_balance if demo_balance is not None else config.DEMO_INITIAL_BALANCE

    demo_value = (
        f"Balance  **${balance:.2f}**\n"
        f"Risk (LOSS)  `-${risk:.2f}`\n"
        f"Reward (WIN)  `+${reward:.2f}`\n"
        f"R : R  `1 : {rr}`"
    )

    embed = {
        "title": f"{arrow} {sig['direction']} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry",          "value": f"`{sig['entry']}`",                   "inline": True},
            {"name": "Stop Loss",      "value": f"`{sig['sl']}`",                      "inline": True},
            {"name": "Timeframe",      "value": f"`{sig.get('timeframe','1h')}`",       "inline": True},
            {"name": "Take Profit 1",  "value": f"`{sig.get('tp1', sig['tp'])}`",       "inline": True},
            {"name": "Take Profit 2",  "value": f"`{sig.get('tp2','—')}`",              "inline": True},
            {"name": "Risk : Reward",  "value": f"`1 : {sig.get('rr','—')}`",           "inline": True},
            {"name": "Confidence",     "value": f"`{sig.get('confidence','—')}%`",      "inline": True},
            {"name": "Trend Strength", "value": f"`{sig.get('trend_strength','—')}/100`","inline": True},
            {"name": "Score",          "value": f"`{sig['score']}`",                   "inline": True},
            {"name": "Reasons",        "value": contributors or "—",                   "inline": False},
            {"name": "💰 Demo Account","value": demo_value,                             "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_result(signal_id: int, direction: str, status: str,
                entry: float, close_price: float, stats: dict,
                symbol: str = "EUR/USD",
                pnl: float = None, new_balance: float = None):
    """Posted when a signal closes WIN or LOSS. Shows P&L and updated balance."""
    won   = status == "WIN"
    color = 0x3fb950 if won else 0xf85149
    emoji = "✅" if won else "❌"

    # P&L line
    if pnl is not None:
        pnl_sign  = "+" if pnl >= 0 else ""
        pnl_str   = f"{pnl_sign}${abs(pnl):.2f}"
        pnl_emoji = "💰" if pnl >= 0 else "💸"
    else:
        pnl_str   = "—"
        pnl_emoji = "💰"

    # Balance line
    if new_balance is not None:
        initial      = config.DEMO_INITIAL_BALANCE
        total_pnl    = round(new_balance - initial, 2)
        return_pct   = round((new_balance / initial - 1) * 100, 1)
        ret_sign     = "+" if return_pct >= 0 else ""
        bal_str      = (
            f"**${new_balance:.2f}**  "
            f"({ret_sign}{return_pct:.1f}% overall  ·  "
            f"total P&L {'+' if total_pnl >= 0 else ''}${total_pnl:.2f})"
        )
    else:
        bal_str = "—"

    embed = {
        "title": f"{emoji} {status} — {direction} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry",    "value": f"`{entry}`",               "inline": True},
            {"name": "Closed at","value": f"`{round(close_price,5)}`","inline": True},
            {"name": "​",   "value": "​",                   "inline": True},
            {"name": f"{pnl_emoji} Trade P&L",
             "value": f"**{pnl_str}**",
             "inline": True},
            {"name": "Demo Balance",
             "value": bal_str,
             "inline": False},
            {"name": "Overall Record",
             "value": (f"W **{stats['wins']}**  ·  L **{stats['losses']}**  "
                       f"·  Win rate **{stats['win_rate']}%**"),
             "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)
