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


def _pip_pnl(entry: float, target: float, sl: float) -> float:
    """Dollar P&L if `target` is hit, scaled so SL distance = DEMO_RISK_PER_TRADE."""
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0
    return round(config.DEMO_RISK_PER_TRADE * abs(target - entry) / sl_dist, 2)


def post_signal(sig: dict, signal_id: int, symbol: str = "EUR/USD",
                demo_balance: float = None):
    """Posted when a new signal fires. Shows exact TP1 / TP2 / SL dollar amounts."""
    color = 0x3fb950 if sig["direction"] == "BUY" else 0xf85149
    arrow = "📈" if sig["direction"] == "BUY" else "📉"
    contributors = "  ·  ".join(
        f"{t}({'+' if v > 0 else ''}{v})"
        for t, v in sig["contributors"].items() if v != 0
    )

    entry   = sig["entry"]
    sl      = sig["sl"]
    tp1     = sig.get("tp1") or sig["tp"]
    tp2     = sig.get("tp2")
    risk    = config.DEMO_RISK_PER_TRADE
    balance = demo_balance if demo_balance is not None else config.DEMO_INITIAL_BALANCE

    reward_tp1 = _pip_pnl(entry, tp1, sl)
    reward_tp2 = _pip_pnl(entry, tp2, sl) if tp2 else None

    tp2_line = f"\nTP2 Reward  `+${reward_tp2:.2f}`" if reward_tp2 is not None else ""
    demo_value = (
        f"Balance  **${balance:.2f}**\n"
        f"SL Risk  `-${risk:.2f}`\n"
        f"TP1 Reward  `+${reward_tp1:.2f}`"
        f"{tp2_line}"
    )

    embed = {
        "title": f"{arrow} {sig['direction']} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry",          "value": f"`{entry}`",                          "inline": True},
            {"name": "Stop Loss",      "value": f"`{sl}`",                             "inline": True},
            {"name": "Timeframe",      "value": f"`{sig.get('timeframe','1h')}`",       "inline": True},
            {"name": "Take Profit 1",  "value": f"`{tp1}`",                            "inline": True},
            {"name": "Take Profit 2",  "value": f"`{tp2 or '—'}`",                     "inline": True},
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


def post_tp1_hit(signal_id: int, direction: str, entry: float, tp2: float,
                 symbol: str = "EUR/USD"):
    """Mid-trade alert: TP1 reached, SL moved to entry, now targeting TP2."""
    embed = {
        "title": f"🎯 TP1 HIT — {direction} {symbol}  (#{signal_id})",
        "color": 0xffa500,
        "description": (
            "**TP1 was reached!** Stop loss has been moved to **entry (breakeven)**.\n"
            "The trade is now **risk-free** and is targeting **TP2**.\n\n"
            f"> Update your broker SL to `{entry}` now."
        ),
        "fields": [
            {"name": "New Stop Loss", "value": f"`{entry}`  *(breakeven)*", "inline": True},
            {"name": "New Target",    "value": f"`{tp2 or '—'}`",           "inline": True},
            {"name": "Worst Case",    "value": "`Breakeven  ($0.00)`",      "inline": True},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)


def post_result(signal_id: int, direction: str, status: str,
                entry: float, close_price: float, stats: dict,
                symbol: str = "EUR/USD",
                pnl: float = None, new_balance: float = None):
    """Posted when a signal closes (WIN / LOSS / BREAKEVEN). Shows P&L and updated balance."""
    if status == "WIN":
        color, emoji = 0x3fb950, "✅"
    elif status == "BREAKEVEN":
        color, emoji = 0xffa500, "↩️"
    else:
        color, emoji = 0xf85149, "❌"

    # P&L line
    if pnl is not None:
        if pnl == 0:
            pnl_str, pnl_emoji = "$0.00  *(breakeven)*", "🔄"
        elif pnl > 0:
            pnl_str, pnl_emoji = f"+${pnl:.2f}", "💰"
        else:
            pnl_str, pnl_emoji = f"-${abs(pnl):.2f}", "💸"
    else:
        pnl_str, pnl_emoji = "—", "💰"

    # Balance line
    if new_balance is not None:
        initial    = config.DEMO_INITIAL_BALANCE
        total_pnl  = round(new_balance - initial, 2)
        return_pct = round((new_balance / initial - 1) * 100, 1)
        ret_sign   = "+" if return_pct >= 0 else ""
        bal_str    = (
            f"**${new_balance:.2f}**  "
            f"({ret_sign}{return_pct:.1f}% overall  ·  "
            f"total P&L {'+' if total_pnl >= 0 else ''}${total_pnl:.2f})"
        )
    else:
        bal_str = "—"

    # Record line
    wins, losses = stats["wins"], stats["losses"]
    bes = stats.get("breakevens", 0)
    be_part = f"  ·  BE **{bes}**" if bes else ""
    record_str = (
        f"W **{wins}**  ·  L **{losses}**{be_part}"
        f"  ·  Win rate **{stats['win_rate']}%**"
    )

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
             "value": record_str,
             "inline": False},
        ],
        "footer": {"text": "Auto-generated · Educational use only · Not financial advice"},
    }
    _send(embed)
