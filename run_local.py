"""Local MT5 runner — main loop for the live demo-account signal bot.

Loops every 5 minutes, runs one cycle per pair, checks the auto-tuner schedule,
then pushes the dashboard to GitHub (master -> main) so the static site stays current.

Usage:
    python run_local.py

Stop with Ctrl+C. MT5 terminal must be open and logged in before starting.
"""
import json
import os
import time
import subprocess
import traceback
from datetime import datetime, timezone

import config
import mt5_executor
import auto_tuner
import discord_poster
import main as bot

CYCLE_SECONDS = 5 * 60    # 5-minute cadence for realistic live signal tracking

# Battery alert state (persists across cycles; resets only when process restarts)
_prev_charging = None
_alerted_low   = False
_alerted_full  = False


def _check_battery_alerts():
    """Send Discord alerts for charging state changes, low battery, and full battery."""
    global _prev_charging, _alerted_low, _alerted_full
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is None:
            return
        pct      = round(bat.percent, 1)
        charging = bat.power_plugged

        if _prev_charging is not None and charging != _prev_charging:
            if charging:
                discord_poster._send({
                    "title":       f"\U0001f50c Charger Plugged In — {pct:.0f}%",
                    "description": "Laptop is now charging.",
                    "color":       0x5865f2,
                    "fields":      [],
                })
            else:
                discord_poster._send({
                    "title":       f"\U0001f50b Charger Removed — {pct:.0f}%",
                    "description": "Laptop is now on battery.",
                    "color":       0xd4a017,
                    "fields":      [],
                })
        _prev_charging = charging

        if not charging and pct <= 20 and not _alerted_low:
            discord_poster._send({
                "title":       f"⚠️ Low Battery — {pct:.0f}%",
                "description": "Laptop battery is low. Connect the charger now!",
                "color":       0xf85149,
                "fields":      [],
            })
            _alerted_low = True
        if pct > 20:
            _alerted_low = False

        if charging and pct >= 100 and not _alerted_full:
            discord_poster._send({
                "title":       "\U0001f50b Battery Full (100%)",
                "description": "Still plugged in — you can safely remove the charger.",
                "color":       0x3fb950,
                "fields":      [],
            })
            _alerted_full = True
        if not charging or pct < 100:
            _alerted_full = False

    except Exception as exc:
        print(f"[battery] Alert check failed: {exc}")


def _push_dashboard():
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            capture_output=True, text=True, timeout=30
        )
        if not result.stdout.strip():
            print("[git] Dashboard unchanged — nothing to push")
            return
        subprocess.run(["git", "add", "docs/"],           check=True, timeout=30)
        subprocess.run(
            ["git", "commit", "-m", "chore: dashboard update [skip ci]"],
            check=True, timeout=30
        )
        subprocess.run(
            ["git", "push", "origin", "master:main", "--force"],
            check=True, timeout=60
        )
        print("[git] Dashboard pushed to main")
    except subprocess.CalledProcessError as exc:
        print(f"[git] Push failed (non-fatal): {exc}")


def main():
    print(f"[run_local] Starting MT5 local runner  "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[run_local] Pairs: {config.PAIRS}  Cycle: {CYCLE_SECONDS}s")

    # Write webhook URL to local-only file so the dashboard can send browser alerts.
    # This file is gitignored — the URL never reaches GitHub.
    _dc_path = os.path.join("docs", "discord_config.json")
    with open(_dc_path, "w") as _f:
        json.dump({"webhook_url": config.DISCORD_WEBHOOK_URL}, _f)
    print(f"[run_local] Discord config written to {_dc_path}")

    mt5_executor.connect()
    auto_tuner.init_baseline()

    _ipc_failures = 0   # consecutive IPC-failure cycles; triggers auto-reconnect

    try:
        while True:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"\n{'='*55}\n[cycle] {now}")

            # Auto-tuner schedule check (runs once per cycle, not per pair)
            try:
                auto_tuner.check_and_run(config.db_path(config.PAIRS[0]))
            except Exception as exc:
                print(f"[tuner] Error: {exc}")
                traceback.print_exc()

            cycle_had_ipc_error = False
            for symbol in config.PAIRS:
                try:
                    bot.run_pair(symbol)
                except Exception as exc:
                    print(f"[{symbol}] Unhandled error: {exc}")
                    traceback.print_exc()
                    if "IPC send failed" in str(exc) or "IPC" in str(exc):
                        cycle_had_ipc_error = True

            if cycle_had_ipc_error:
                _ipc_failures += 1
                print(f"[MT5] IPC failure #{_ipc_failures} — "
                      f"{'attempting reconnect...' if _ipc_failures >= 2 else 'will retry next cycle'}")
                if _ipc_failures >= 2:
                    try:
                        mt5_executor.disconnect()
                        time.sleep(5)
                        mt5_executor.connect()
                        _ipc_failures = 0
                        print("[MT5] Reconnected successfully")
                    except Exception as exc:
                        print(f"[MT5] Reconnect failed: {exc}")
            else:
                _ipc_failures = 0

            _check_battery_alerts()
            _push_dashboard()
            print(f"[run_local] Next cycle in {CYCLE_SECONDS // 60} min...")
            time.sleep(CYCLE_SECONDS)

    except KeyboardInterrupt:
        print("\n[run_local] Stopped by user.")
    finally:
        mt5_executor.disconnect()


if __name__ == "__main__":
    main()
