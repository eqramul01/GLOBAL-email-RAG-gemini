#!/usr/bin/env python3
"""
monitor.py — Live progress monitor for the indexing run.

Polls LanceDB row count every N seconds, displays:
  - rows / total
  - rate (rows added since last poll)
  - % complete
  - ETA at current rate
  - process alive / dead
  - last few lines from index.log

Updates in place (single-line carriage-return) — no scrolling spam.

Usage:
  python3 monitor.py
  python3 monitor.py --interval 10 --target 257096
  python3 monitor.py --once          # print once and exit (good for cron/scripting)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MAILDIR = os.path.expanduser("~/GLOBAL_E-mail_Outlook/Maildir_Archive")
DEFAULT_LOG = os.path.expanduser("~/GLOBAL_E-mail_Outlook/Email_Archive_Toolkit_Gemini/index.log")
DEFAULT_TARGET = 257_096
PROC_NAME = "03_index_emails"


def get_row_count(maildir: Path) -> int | None:
    try:
        import lancedb
        db = lancedb.connect(str(maildir / ".lancedb"))
        if "emails" not in db.table_names():
            return 0
        return db.open_table("emails").count_rows()
    except Exception as e:
        sys.stderr.write(f"\n  lance error: {e}\n")
        return None


def is_process_running() -> tuple[bool, str | None]:
    try:
        out = subprocess.check_output(["pgrep", "-f", PROC_NAME], text=True).strip()
        return (True, out.split()[0] if out else None)
    except subprocess.CalledProcessError:
        return (False, None)


def fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def fmt_int(n: int) -> str:
    return f"{n:,}"


def display(maildir: Path, target: int, interval: int, once: bool) -> None:
    started = time.time()
    last_rows = None
    last_t = None

    while True:
        rows = get_row_count(maildir)
        running, pid = is_process_running()
        now = time.time()

        if rows is None:
            line = "lance: error"
        else:
            pct = 100.0 * rows / target if target else 0
            line_parts = [
                f"rows {fmt_int(rows)}/{fmt_int(target)}",
                f"{pct:5.2f}%",
            ]
            if last_rows is not None and last_t is not None:
                delta = rows - last_rows
                dt = now - last_t
                rate = delta / dt if dt > 0 else 0
                if rate > 0 and rows < target:
                    eta = (target - rows) / rate
                    line_parts.append(f"+{delta}/{dt:.0f}s ({rate:.1f}/sec)")
                    line_parts.append(f"ETA {fmt_eta(eta)}")
                else:
                    line_parts.append(f"+{delta}/{dt:.0f}s ({rate:.1f}/sec)")
            line_parts.append("alive ✓" if running else "DEAD ✗")
            line = "  " + " | ".join(line_parts)

        # In-place update with \r, padded to clear leftover characters
        sys.stdout.write("\r" + line.ljust(120))
        sys.stdout.flush()

        last_rows = rows if rows is not None else last_rows
        last_t = now

        if once:
            print()
            return

        if rows is not None and rows >= target:
            print(f"\n\nDone — reached target ({target:,} rows). Total elapsed: {fmt_eta(now - started)}")
            return

        if not running and rows is not None:
            print(f"\n\nIndexer process is GONE but only {rows:,}/{target:,} rows complete.")
            print("Restart with: bash run_full_index.sh")
            return

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped (indexer keeps running).")
            return


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--maildir", default=DEFAULT_MAILDIR,
                   help="Maildir root containing .lancedb/")
    p.add_argument("--target", type=int, default=DEFAULT_TARGET,
                   help="Expected total rows (default 257,096)")
    p.add_argument("--interval", type=int, default=5,
                   help="Poll interval in seconds (default 5)")
    p.add_argument("--once", action="store_true",
                   help="Print one line and exit (no loop)")
    args = p.parse_args()

    maildir = Path(args.maildir).expanduser().resolve()
    print(f"monitoring {maildir}/.lancedb/  (poll every {args.interval}s, target {args.target:,} rows)")
    print("press Ctrl-C to stop monitoring (indexer keeps running)\n")

    try:
        display(maildir, args.target, args.interval, args.once)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
