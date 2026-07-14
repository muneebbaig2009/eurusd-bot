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


def post_signal(sig: dict, signal_id: int, symbol: str = 'EUR/USD'):
    color = 0x2ecc71 if sig["direction"] == "BUY" else 0xe74c3c
    contributors = ", ".join(
        f"{t}({'+' if v > 0 else ''}{v})" for t, v in sig["contributors"].items() if v != 0
    )
    embed = {
        "title": f"📈 {sig['direction']} {symbol}  (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry", "value": f"`{sig['entry']}`", "inline": True},
            {"name": "Stop Loss", "value": f"`{sig['sl']}`", "inline": True},
            {"name": "Timeframe", "value": f"`{sig.get('timeframe','1h')}`", "inline": True},
            {"name": "Take Profit 1", "value": f"`{sig.get('tp1', sig['tp'])}`", "inline": True},
            {"name": "Take Profit 2", "value": f"`{sig.get('tp2','—')}`", "inline": True},
            {"name": "Risk : Reward", "value": f"`1 : {sig.get('rr','—')}`", "inline": True},
            {"name": "Confidence", "value": f"`{sig.get('confidence','—')}%`", "inline": True},
            {"name": "Trend Strength", "value": f"`{sig.get('trend_strength','—')}/100`", "inline": True},
            {"name": "Score", "value": f"`{sig['score']}`", "inline": True},
            {"name": "Reasons", "value": contributors or "—", "inline": False},
        ],
        "footer": {"text": "Auto-generated. Educational use only. Not financial advice."},
    }
    _send(embed)


def post_result(signal_id: int, direction: str, status: str,
                entry: float, close_price: float, stats: dict, symbol: str = 'EUR/USD'):
    won = status == "WIN"
    color = 0x2ecc71 if won else 0xe74c3c
    emoji = "✅" if won else "❌"
    embed = {
        "title": f"{emoji} {status} — {direction} {symbol} (#{signal_id})",
        "color": color,
        "fields": [
            {"name": "Entry", "value": f"`{entry}`", "inline": True},
            {"name": "Closed at", "value": f"`{round(close_price, 5)}`", "inline": True},
            {"name": "Record",
             "value": f"W:{stats['wins']} L:{stats['losses']} | Win rate: {stats['win_rate']}%",
             "inline": False},
        ],
        "footer": {"text": "Auto-generated. Educational use only."},
    }
    _send(embed)
