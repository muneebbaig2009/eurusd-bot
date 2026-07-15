"""Local MT5 runner — main loop for the live demo-account signal bot.

Loops every 5 minutes, runs one cycle per pair, checks the auto-tuner schedule,
then pushes the dashboard to GitHub (master -> main) so the static site stays current.

Usage:
    python run_local.py

Stop with Ctrl+C. MT5 terminal must be open and logged in before starting.
"""
import time
import subprocess
import traceback
from datetime import datetime, timezone

import config
import mt5_executor
import auto_tuner
import main as bot

CYCLE_SECONDS = 5 * 60    # 5-minute cadence for realistic live signal tracking


def _push_dashboard():
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            capture_output=True, text=True
        )
        if not result.stdout.strip():
            print("[git] Dashboard unchanged — nothing to push")
            return
        subprocess.run(["git", "add", "docs/"],           check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: dashboard update [skip ci]"],
            check=True
        )
        subprocess.run(
            ["git", "push", "origin", "master:main", "--force"],
            check=True
        )
        print("[git] Dashboard pushed to main")
    except subprocess.CalledProcessError as exc:
        print(f"[git] Push failed (non-fatal): {exc}")


def main():
    print(f"[run_local] Starting MT5 local runner  "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[run_local] Pairs: {config.PAIRS}  Cycle: {CYCLE_SECONDS}s")

    mt5_executor.connect()
    auto_tuner.init_baseline()

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

            for symbol in config.PAIRS:
                try:
                    bot.run_pair(symbol)
                except Exception as exc:
                    print(f"[{symbol}] Unhandled error: {exc}")
                    traceback.print_exc()

            _push_dashboard()
            print(f"[run_local] Next cycle in {CYCLE_SECONDS // 60} min...")
            time.sleep(CYCLE_SECONDS)

    except KeyboardInterrupt:
        print("\n[run_local] Stopped by user.")
    finally:
        mt5_executor.disconnect()


if __name__ == "__main__":
    main()
