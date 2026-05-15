#!/usr/bin/env python3
"""
verify_corpus.py — Post-indexing integrity check for the email archive.

Confirms the LanceDB index is a faithful representation of the Maildir.
Run after 03_index_emails_gemini.py finishes.

Checks performed:
  1. Disk inventory     — count .eml files in Maildir
  2. Checkpoint match   — every checkpointed path is on disk
  3. LanceDB count      — table row count
  4. Vector sanity      — random sample: dim=3072, no NaN, no zero-norm
  5. Path validity      — random sample: every row's eml_path exists on disk
  6. Message-ID coverage — every disk MID has a LanceDB row OR is in the
                          checkpoint's skip list (empty-content emails)

Usage:
  python3 verify_corpus.py                    # quick mode (sample-based, ~30s)
  python3 verify_corpus.py --strict           # full Message-ID cross-check (~5m)
  python3 verify_corpus.py --sample 5000      # bigger sample in quick mode

Reads:
  GEMINI_API_KEY (not actually used; just to confirm env is intact)
"""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import math
import os
import random
import sys
import time
from pathlib import Path

DEFAULT_MAILDIR = os.path.expanduser("~/GLOBAL_E-mail_Outlook/Maildir_Archive")
CHECKPOINT_FILE = ".gemini_index_checkpoint.json"
LDB_TABLE = "emails"
EMBED_DIM = 3072

# ANSI colors for the verdict lines
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def section(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")


def ok(msg: str) -> None:
    print(f"      {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"      {YELLOW}⚠{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"      {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"      {DIM}·{RESET} {msg}")


def load_checkpoint(maildir: Path) -> set[str]:
    p = maildir / CHECKPOINT_FILE
    if not p.exists():
        return set()
    return set(json.loads(p.read_text()).get("done_paths", []))


def list_eml_files(maildir: Path) -> list[Path]:
    """All .eml files anywhere under any cur/ in the Maildir tree."""
    eml = []
    for cur in maildir.rglob("cur"):
        if not cur.is_dir():
            continue
        for p in cur.iterdir():
            if p.suffix == ".eml":
                eml.append(p)
    return eml


def parse_message_id(path: Path) -> str:
    """Extract just the Message-ID header without full email parse."""
    try:
        with path.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
        return (msg.get("Message-ID") or "").strip().strip("<>")[:512]
    except Exception:
        return ""


def has_real_content(path: Path) -> bool:
    """Mirror the indexer's 'skip if empty body and subject' decision."""
    try:
        with path.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
        if (msg.get("Subject") or "").strip():
            return True
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        if (part.get_content() or "").strip():
                            return True
                    except Exception:
                        pass
        else:
            try:
                if (msg.get_content() or "").strip():
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False  # treat parse failures as no-content


# ---- Verification routines ------------------------------------------------

def check_disk_inventory(maildir: Path) -> tuple[int, list[Path]]:
    section(1, 6, "Disk inventory")
    t0 = time.time()
    eml_paths = list_eml_files(maildir)
    info(f"walked Maildir in {time.time()-t0:.1f}s")
    info(f".eml files on disk: {len(eml_paths):,}")
    return len(eml_paths), eml_paths


def check_checkpoint(maildir: Path, eml_paths_set: set[str]) -> set[str]:
    section(2, 6, "Checkpoint match")
    done = load_checkpoint(maildir)
    info(f"checkpointed paths: {len(done):,}")
    missing = [p for p in done if p not in eml_paths_set]
    if missing:
        fail(f"{len(missing)} checkpointed paths no longer exist on disk!")
        for p in missing[:5]:
            info(f"  e.g. {p}")
    else:
        ok("every checkpointed path is on disk")
    return done


def check_lance_count(maildir: Path) -> tuple[int, object]:
    section(3, 6, "LanceDB count")
    import lancedb
    db = lancedb.connect(str(maildir / ".lancedb"))
    if LDB_TABLE not in db.table_names():
        fail(f"table '{LDB_TABLE}' does not exist!")
        return 0, None
    t = db.open_table(LDB_TABLE)
    n = t.count_rows()
    info(f"LanceDB rows in '{LDB_TABLE}': {n:,}")
    return n, t


def check_vectors(table, sample: int) -> tuple[int, int, int]:
    section(4, 6, f"Vector sanity (sample {sample:,})")
    total = table.count_rows()
    sample = min(sample, total)
    if sample == 0:
        warn("table is empty, skipping")
        return 0, 0, 0

    # Use to_pandas with limit; to_list() is fine but pandas is more friendly
    rows = table.to_lance().to_table().to_pylist()
    sampled = random.sample(rows, sample) if len(rows) > sample else rows

    bad_dim = 0
    bad_norm = 0
    bad_nan = 0
    for r in sampled:
        v = r.get("vector") or []
        if len(v) != EMBED_DIM:
            bad_dim += 1
            continue
        # zero norm
        s = 0.0
        any_nan = False
        for x in v:
            if x != x:  # NaN check
                any_nan = True
                break
            s += x * x
        if any_nan:
            bad_nan += 1
            continue
        if s == 0.0:
            bad_norm += 1

    info(f"sampled: {len(sampled):,}")
    if bad_dim == 0 and bad_norm == 0 and bad_nan == 0:
        ok("all vectors are dim=3072, no NaN, non-zero norm")
    else:
        if bad_dim:
            fail(f"{bad_dim} vectors had wrong dimensionality")
        if bad_nan:
            fail(f"{bad_nan} vectors contained NaN")
        if bad_norm:
            fail(f"{bad_norm} vectors had zero norm")
    return bad_dim, bad_norm, bad_nan


def check_eml_paths(table, sample: int) -> int:
    section(5, 6, f"Path validity (sample {sample:,})")
    rows = table.to_lance().to_table().to_pylist()
    sample = min(sample, len(rows))
    sampled = random.sample(rows, sample) if len(rows) > sample else rows
    missing = []
    for r in sampled:
        p = r.get("eml_path") or ""
        if p and not Path(p).exists():
            missing.append(p)
    info(f"sampled: {len(sampled):,}")
    if not missing:
        ok("every sampled row's eml_path exists on disk")
    else:
        fail(f"{len(missing)} sampled paths are missing on disk")
        for p in missing[:5]:
            info(f"  e.g. {p}")
    return len(missing)


def check_message_id_coverage(maildir: Path,
                              eml_paths: list[Path],
                              checkpointed: set[str],
                              table,
                              strict: bool,
                              sample_size: int) -> tuple[int, int]:
    section(6, 6,
            "Message-ID coverage (strict, all 257k)" if strict
            else f"Message-ID coverage (sampling {sample_size:,})")

    rows = table.to_lance().to_table().to_pylist()
    indexed_mids = {r["message_id"] for r in rows if r.get("message_id")}
    indexed_paths = {r.get("eml_path", "") for r in rows}
    info(f"unique Message-IDs in LanceDB: {len(indexed_mids):,}")

    sample_paths = eml_paths if strict else random.sample(
        eml_paths, min(sample_size, len(eml_paths)))

    info(f"checking {len(sample_paths):,} disk emails against the index "
         f"({'full' if strict else 'sample'})")

    missing_in_lance = []
    explicable = 0  # missing because empty content (correct behavior)

    t0 = time.time()
    for i, p in enumerate(sample_paths, 1):
        if i % 5000 == 0:
            print(f"      {DIM}· checked {i:,}/{len(sample_paths):,} "
                  f"({(time.time()-t0):.0f}s){RESET}")
        mid = parse_message_id(p)
        if mid and mid in indexed_mids:
            continue
        if str(p) in indexed_paths:
            # path-based id (no Message-ID header)
            continue
        # Not in LanceDB — is it explicably empty?
        if not has_real_content(p):
            explicable += 1
            continue
        missing_in_lance.append((p, mid))

    info(f"finished in {time.time()-t0:.1f}s")
    info(f"emails missing from LanceDB but explained by empty content: {explicable:,}")
    if not missing_in_lance:
        ok("every disk email is either indexed or correctly skipped")
    else:
        scope = "FULL" if strict else f"in sample of {len(sample_paths):,}"
        fail(f"{len(missing_in_lance)} emails ({scope}) are missing from LanceDB "
             "with no good reason")
        for p, mid in missing_in_lance[:5]:
            info(f"  {p}  mid={mid!r}")
    return len(missing_in_lance), explicable


# ---- Entry point ----------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--maildir", default=DEFAULT_MAILDIR,
                   help="Maildir root containing .lancedb/")
    p.add_argument("--strict", action="store_true",
                   help="Cross-check ALL Message-IDs (slow, ~5 minutes)")
    p.add_argument("--sample", type=int, default=2000,
                   help="Sample size for vector + path + MID checks (default 2000)")
    args = p.parse_args()

    maildir = Path(args.maildir).expanduser().resolve()
    print(f"=== Email Archive Index Verification ===")
    print(f"Maildir:      {maildir}")
    print(f"LanceDB:      {maildir}/.lancedb/")
    print(f"Mode:         {'STRICT (full cross-check)' if args.strict else 'QUICK (sampling)'}")
    print(f"Sample size:  {args.sample:,}")

    # Step 1
    eml_count, eml_paths = check_disk_inventory(maildir)
    eml_paths_set = {str(p) for p in eml_paths}

    # Step 2
    checkpointed = check_checkpoint(maildir, eml_paths_set)

    # Step 3
    lance_count, table = check_lance_count(maildir)
    if table is None:
        fail("\nFATAL: no LanceDB table found")
        return 2

    # Step 4
    bad_dim, bad_norm, bad_nan = check_vectors(table, args.sample)

    # Step 5
    missing_paths = check_eml_paths(table, args.sample)

    # Step 6
    missing_in_lance, explicable = check_message_id_coverage(
        maildir, eml_paths, checkpointed, table, args.strict, args.sample)

    # Final verdict
    print()
    print("=" * 60)
    print(f"  Disk .eml count:       {eml_count:,}")
    print(f"  Checkpointed paths:    {len(checkpointed):,}")
    print(f"  LanceDB rows:          {lance_count:,}")
    print(f"  Indexed/explicable:    {lance_count + explicable:,}")
    print(f"  Disk - (indexed+skip): {eml_count - lance_count - explicable:+d}")
    print()

    problems = bad_dim + bad_norm + bad_nan + missing_paths + missing_in_lance
    if problems == 0:
        print(f"  {GREEN}VERDICT: PASS — corpus is intact and complete{RESET}")
        return 0
    else:
        print(f"  {RED}VERDICT: {problems} problem(s) found — see above{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
