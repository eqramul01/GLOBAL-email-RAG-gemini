#!/usr/bin/env python3
"""
02_mbox_to_maildir.py — Stream mbox files into a Maildir tree that mirrors
the original Outlook folder hierarchy.

* Folder structure is PRESERVED, not flattened.
* Wherever a Maildir folder has children, a sibling `.sbd` directory is
  also created so Thunderbird treats the tree as nested Local Folders.
* Manifest at Maildir_Archive/manifest.json records counts per folder.

PARSER DESIGN — important for huge Outlook exports:
We do NOT use Python's stdlib `mailbox.mbox` or `email` module here.
Outlook-exported mboxes routinely contain malformed headers, surrogate
escape characters, and 'From ' lines that confuse stdlib's table-of-
contents builder, which crashes on files in the 1+ GB range. Instead
we stream the file as bytes and split on lines starting with 'From '.
Each yielded chunk is the raw RFC 822 message bytes; the actual email
parsing happens later, per-message, in the indexer where bad messages
can be skipped individually.

Usage:
    python3 02_mbox_to_maildir.py <SRC_MAILS_DIR> <DST_MAILDIR_ROOT>

Idempotent: if the destination Maildir's `cur/` already has files, that
folder is skipped (delete it to force a re-run).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator


# Characters Thunderbird tolerates poorly in folder names. We replace, not strip,
# so message counts still round-trip.
_BAD_CHARS = '/\\:*?"<>|'


def safe_name(name: str) -> str:
    """Make a single path component safe for Maildir/Thunderbird."""
    out = "".join("_" if c in _BAD_CHARS else c for c in name)
    out = out.strip().rstrip(".")
    return out or "_"


def mbox_to_maildir_path(src_root: Path, mbox_file: Path, dst_root: Path) -> Path:
    """
    Translate /SRC/foo/bar/Inbox.mbox  ->  /DST/foo/bar/Inbox/
    Each parent directory becomes a directory at the destination, and the
    `.mbox` suffix is stripped to leave the bare folder name.
    """
    rel = mbox_file.relative_to(src_root)
    parts = [safe_name(p) for p in rel.parts]
    # last part: "Inbox.mbox" -> "Inbox"
    parts[-1] = safe_name(parts[-1][:-5] if parts[-1].endswith(".mbox") else parts[-1])
    return dst_root.joinpath(*parts)


def ensure_maildir(folder: Path) -> None:
    """Create the cur/new/tmp triplet that defines a Maildir."""
    for sub in ("cur", "new", "tmp"):
        (folder / sub).mkdir(parents=True, exist_ok=True)


def ensure_sbd_chain(dst_root: Path, folder: Path) -> None:
    """
    For every ancestor of `folder` between `dst_root` and `folder`, create a
    sibling `<name>.sbd` directory if one doesn't exist. That's the marker
    Thunderbird uses to say "this folder has subfolders".
    """
    rel_parts = folder.relative_to(dst_root).parts
    cur = dst_root
    for part in rel_parts[:-1]:  # every ancestor of the final folder
        sbd = cur / f"{part}.sbd"
        sbd.mkdir(parents=True, exist_ok=True)
        cur = sbd / part if (sbd / part).exists() else cur / part
    # Also make sure the final folder's own .sbd exists in case future passes
    # add children. Cheap, harmless if unused.
    (folder.parent / f"{folder.name}.sbd").mkdir(parents=True, exist_ok=True)


def already_populated(folder: Path) -> bool:
    cur = folder / "cur"
    if not cur.exists():
        return False
    try:
        next(cur.iterdir())
        return True
    except StopIteration:
        return False


def stream_messages_raw(mbox_file: Path) -> Iterator[bytes]:
    """
    Yield raw bytes for each message in the mbox.

    Splits on lines starting with b'From ' (the mbox-style separator).
    Pure I/O, no parsing — tolerates 64 GB files, surrogate chars in
    headers, mixed encodings, and other Outlook-export weirdness that
    blows up Python's stdlib `mailbox` module.

    The first 'From ' line of each chunk is the mbox separator, not part
    of the RFC 822 message itself; it gets stripped in write_one().
    """
    buf = bytearray()
    with mbox_file.open("rb") as fh:
        for line in fh:
            if line.startswith(b"From ") and len(buf) > 0:
                yield bytes(buf)
                buf.clear()
            buf.extend(line)
        if buf:
            yield bytes(buf)


def write_one(raw: bytes, cur_dir: Path, seq: int) -> None:
    """
    Write raw message bytes as a unique .eml file under cur/.

    Strips the leading mbox 'From ' separator line (not part of RFC 822).
    Filename: <seq>.<sha1-prefix>.eml — sequence guarantees uniqueness
    within a folder; the hash prefix keeps re-runs deterministic.
    """
    # Strip the mbox separator line if present
    if raw.startswith(b"From "):
        nl = raw.find(b"\n")
        if nl >= 0:
            raw = raw[nl + 1:]
    digest = hashlib.sha1(raw[:8192]).hexdigest()[:16]
    name = f"{seq:08d}.{digest}.eml"
    (cur_dir / name).write_bytes(raw)


def convert_one(src_root: Path, mbox_file: Path, dst_root: Path) -> dict:
    folder = mbox_to_maildir_path(src_root, mbox_file, dst_root)

    if already_populated(folder):
        count = sum(1 for _ in (folder / "cur").iterdir())
        print(f"  SKIP (already populated): {folder.relative_to(dst_root)} [{count} msgs]")
        return {
            "src": str(mbox_file.relative_to(src_root)),
            "dst": str(folder.relative_to(dst_root)),
            "messages": count,
            "skipped": True,
        }

    ensure_maildir(folder)
    ensure_sbd_chain(dst_root, folder)

    cur_dir = folder / "cur"
    n = 0
    written = 0
    failed = 0
    t0 = time.time()
    for raw in stream_messages_raw(mbox_file):
        n += 1
        try:
            write_one(raw, cur_dir, n)
            written += 1
        except Exception as e:
            failed += 1
            sys.stderr.write(f"  skipped msg {n} in {mbox_file.name}: {e}\n")
        if n % 5000 == 0:
            print(f"    {folder.name}: {n:,} messages…")
    dt = time.time() - t0
    suffix = f", {failed} skipped" if failed else ""
    print(f"  DONE: {folder.relative_to(dst_root)} [{written:,} msgs in {dt:.1f}s{suffix}]")
    return {
        "src": str(mbox_file.relative_to(src_root)),
        "dst": str(folder.relative_to(dst_root)),
        "messages": written,
        "failed_messages": failed,
        "skipped": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("src", help="Source directory containing *.mbox files (recursively)")
    p.add_argument("dst", help="Destination root for the Maildir tree")
    args = p.parse_args()

    src_root = Path(args.src).expanduser().resolve()
    dst_root = Path(args.dst).expanduser().resolve()

    if not src_root.is_dir():
        sys.stderr.write(f"src is not a directory: {src_root}\n")
        return 2
    dst_root.mkdir(parents=True, exist_ok=True)

    mboxes = sorted(src_root.rglob("*.mbox"))
    if not mboxes:
        sys.stderr.write(f"No .mbox files under {src_root}\n")
        return 1

    print(f"Found {len(mboxes)} mbox files. Converting → {dst_root}")
    manifest = {
        "_meta": {
            "src_root": str(src_root),
            "dst_root": str(dst_root),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": "02_mbox_to_maildir.py",
            "version": 2,
        },
        "folders": [],
    }

    for mbox_file in mboxes:
        try:
            entry = convert_one(src_root, mbox_file, dst_root)
        except Exception as e:
            sys.stderr.write(f"FAIL {mbox_file}: {e}\n")
            entry = {
                "src": str(mbox_file.relative_to(src_root)),
                "error": repr(e),
            }
        manifest["folders"].append(entry)
        # write incrementally so a crash doesn't lose progress info
        (dst_root / "manifest.json").write_text(json.dumps(manifest, indent=2))

    total = sum(f.get("messages", 0) for f in manifest["folders"])
    manifest["_meta"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest["_meta"]["total_messages"] = total
    (dst_root / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nTotal: {total:,} messages across {len(manifest['folders'])} folders")
    print(f"Manifest: {dst_root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
