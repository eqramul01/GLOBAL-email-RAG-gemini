#!/usr/bin/env python3
"""
fix_maildir_layout.py — Rearranges nested Maildir folders so Thunderbird's
Maildir storage backend recognizes the hierarchy.

Bug we're fixing in 02_mbox_to_maildir.py: child Maildir folders ended up
nested INSIDE the parent Maildir (alongside cur/new/tmp). Thunderbird
expects them in a sibling `.sbd` directory.

WRONG (what conversion produced):
    Inbox/
        cur/, new/, tmp/
        Inbox/             ← child Maildir, wrong location
        _.54/              ← child Maildir, wrong location
        Inbox.sbd/         ← empty placeholder

RIGHT (what Thunderbird wants):
    Inbox/
        cur/, new/, tmp/
    Inbox.sbd/
        Inbox/             ← child Maildir
        _.54/              ← child Maildir

This script is idempotent: re-running on already-fixed trees is a no-op.

Usage:
    python3 fix_maildir_layout.py /path/to/Local_Folders_or_Maildir_Archive
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

MAILDIR_SUBS = {"cur", "new", "tmp"}


def is_maildir(p: Path) -> bool:
    return p.is_dir() and (p / "cur").is_dir()


def fix_one(maildir: Path, root: Path) -> int:
    """
    Move every non-cur/new/tmp subdirectory of `maildir` into a sibling
    `<name>.sbd` companion. Returns count of items moved.
    """
    children_to_move = []
    for entry in maildir.iterdir():
        if entry.name in MAILDIR_SUBS:
            continue
        if entry.is_dir():
            children_to_move.append(entry)

    if not children_to_move:
        return 0

    sbd = maildir.parent / f"{maildir.name}.sbd"
    sbd.mkdir(exist_ok=True)

    moved = 0
    for child in children_to_move:
        target = sbd / child.name
        if target.exists():
            # Idempotent merge: move contents into existing target, drop empty src
            for sub in list(child.iterdir()):
                sub_target = target / sub.name
                if sub_target.exists():
                    print(f"  WARN double-conflict, skipping: {sub_target.relative_to(root)}",
                          file=sys.stderr)
                    continue
                shutil.move(str(sub), str(sub_target))
            try:
                child.rmdir()
            except OSError:
                pass  # not empty for some reason; leave it
        else:
            shutil.move(str(child), str(target))
        moved += 1
    return moved


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write(f"Usage: {sys.argv[0]} <maildir-root>\n")
        return 2

    root = Path(sys.argv[1]).expanduser().resolve()
    if not root.is_dir():
        sys.stderr.write(f"Not a directory: {root}\n")
        return 2

    print(f"Fixing Maildir layout under: {root}")

    # Bottom-up walk so deeper Maildirs are fixed before their parents
    # (parent moves a child whose internal structure is already correct).
    fixed_count = 0
    moved_total = 0
    for dirpath, dirnames, _ in os.walk(root, topdown=False):
        d = Path(dirpath)
        if not is_maildir(d):
            continue
        n = fix_one(d, root)
        if n:
            fixed_count += 1
            moved_total += n
            print(f"  moved {n} children of {d.relative_to(root)} → {d.name}.sbd/")

    print(f"\nDone. Fixed {fixed_count} maildirs, moved {moved_total} child folders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
