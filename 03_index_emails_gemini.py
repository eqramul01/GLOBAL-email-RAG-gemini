#!/usr/bin/env python3
"""
03_index_emails_gemini.py — Embed every email in the Maildir tree using
Gemini Embedding 2 (gemini-embedding-2) and store the vectors in a local
LanceDB at <maildir>/.lancedb/.

No GCP. No Vertex. No service to run. The whole index lives as files
under your Maildir root; back it up by copying the directory.

How it works:
  1. Walk the Maildir tree, parse each .eml.
  2. Build batches of up to 100 messages.
  3. For each batch, call client.models.embed_content with
     task_type=RETRIEVAL_DOCUMENT to get 3072-d vectors.
  4. Upsert vectors + metadata into LanceDB.
  5. Checkpoint every batch — Ctrl-C is safe; rerun resumes.

Env vars:
  GEMINI_API_KEY              your Gemini Developer API key

Usage:
  python3 03_index_emails_gemini.py <MAILDIR_ROOT>
  python3 03_index_emails_gemini.py <MAILDIR_ROOT> --limit 500     # smoke test
  python3 03_index_emails_gemini.py <MAILDIR_ROOT> --status        # progress
  python3 03_index_emails_gemini.py <MAILDIR_ROOT> --reset         # wipe index
"""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import re
import sys
import time
from email.utils import parsedate_to_datetime, getaddresses
from pathlib import Path
from typing import Iterator

# ---- Config ---------------------------------------------------------------

MODEL_EMBED = "gemini-embedding-2"            # Gemini Embedding 2 (multimodal, GA Apr 22 2026)
MODEL_EMBED_FALLBACK = "gemini-embedding-001" # alias used in older SDK examples
EMBED_DIM = 3072                              # native dim for gemini-embedding-2
EMBED_BATCH = 100                             # messages per embed_content call
LDB_TABLE = "emails"                          # LanceDB table name
CHECKPOINT_FILE = ".gemini_index_checkpoint.json"

# Truncate per-message text fed to the embedder so large emails don't blow
# the per-input character budget. Empirically: ~30k chars ≈ 7-8k tokens
# which fits well below most embedding model context limits.
MAX_TEXT_CHARS = 30_000


# ---- Email parsing --------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    return _WS.sub(" ", _HTML_TAG.sub(" ", s)).strip()


def _addr_list(header_value) -> str:
    if not header_value:
        return ""
    pairs = getaddresses([header_value])
    return ", ".join(addr for _name, addr in pairs if addr)[:1024]


def _safe_get(msg, name: str) -> str:
    """Get a header without exploding on malformed values."""
    try:
        return msg.get(name) or ""
    except Exception:
        # Fall back to raw bytes if header parsing breaks
        try:
            return str(msg.get(name, failobj="", decode=False) or "")
        except Exception:
            return ""


def parse_eml(path: Path) -> dict:
    with path.open("rb") as fh:
        raw = fh.read()

    # Try the modern strict policy first (richer API, but breaks on Outlook
    # exports with malformed headers). Fall back to compat32, which is the
    # older lenient policy that tolerates almost anything.
    msg = None
    for policy in (email.policy.default, email.policy.compat32):
        try:
            msg = email.message_from_bytes(raw, policy=policy)
            break
        except Exception:
            continue

    if msg is None:
        # Both policies failed — return minimal record. Indexer will see
        # empty content and checkpoint-skip it (counted as explained, not
        # as failure).
        return {
            "message_id": "", "subject": "",
            "from_addr": "", "to_addr": "", "cc_addr": "",
            "date": "", "body": "",
            "eml_path": str(path),
        }

    date_iso = ""
    raw_date = _safe_get(msg, "Date")
    if raw_date:
        try:
            date_iso = parsedate_to_datetime(raw_date).isoformat()
        except Exception:
            date_iso = str(raw_date)[:64]

    body_parts: list[str] = []
    try:
        is_mp = msg.is_multipart()
    except Exception:
        is_mp = False

    if is_mp:
        try:
            walker = msg.walk()
        except Exception:
            walker = []
        for part in walker:
            try:
                ctype = part.get_content_type()
                disp = (_safe_get(part, "Content-Disposition")).lower()
            except Exception:
                continue
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    try:
                        payload = part.get_payload(decode=True) or b""
                        body_parts.append(payload.decode(
                            part.get_content_charset() or "utf-8", "replace"))
                    except Exception:
                        pass
            elif ctype == "text/html" and not body_parts:
                try:
                    body_parts.append(_strip_html(part.get_content()))
                except Exception:
                    pass
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:
            try:
                payload = msg.get_payload(decode=True) or b""
                body_parts.append(payload.decode(
                    msg.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass

    body = _WS.sub(" ", "\n".join(body_parts)).strip()

    return {
        "message_id": _safe_get(msg, "Message-ID").strip().strip("<>")[:512],
        "subject":    _safe_get(msg, "Subject").strip()[:512],
        "from_addr":  _addr_list(_safe_get(msg, "From")),
        "to_addr":    _addr_list(_safe_get(msg, "To")),
        "cc_addr":    _addr_list(_safe_get(msg, "Cc")),
        "date":       date_iso,
        "body":       body,
        "eml_path":   str(path),
    }


# ---- Maildir walker -------------------------------------------------------

def walk_maildir(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (folder_label, eml_path) for every .eml under any cur/ in the tree."""
    for cur_dir in root.rglob("cur"):
        if not cur_dir.is_dir():
            continue
        # Folder label = path from root to the maildir, displayed nicely
        rel = cur_dir.parent.relative_to(root)
        label = str(rel) if str(rel) != "." else "(root)"
        for p in sorted(cur_dir.iterdir()):
            if p.suffix == ".eml":
                yield label, p


# ---- Checkpoint -----------------------------------------------------------

def load_checkpoint(maildir: Path) -> set[str]:
    p = maildir / CHECKPOINT_FILE
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()).get("done_paths", []))
    except Exception:
        return set()


def save_checkpoint(maildir: Path, done: set[str]) -> None:
    (maildir / CHECKPOINT_FILE).write_text(
        json.dumps({"done_paths": sorted(done)}, separators=(",", ":")))


# ---- LanceDB --------------------------------------------------------------

def open_lance_table(maildir: Path):
    """Open or create the LanceDB table at <maildir>/.lancedb/."""
    import lancedb
    import pyarrow as pa

    db_dir = maildir / ".lancedb"
    db = lancedb.connect(str(db_dir))

    if LDB_TABLE in db.table_names():
        return db.open_table(LDB_TABLE)

    # Schema: metadata + 3072-d vector
    schema = pa.schema([
        pa.field("id",         pa.string()),
        pa.field("message_id", pa.string()),
        pa.field("subject",    pa.string()),
        pa.field("from_addr",  pa.string()),
        pa.field("to_addr",    pa.string()),
        pa.field("cc_addr",    pa.string()),
        pa.field("date",       pa.string()),
        pa.field("folder",     pa.string()),
        pa.field("eml_path",   pa.string()),
        pa.field("vector",     pa.list_(pa.float32(), EMBED_DIM)),
    ])
    return db.create_table(LDB_TABLE, schema=schema, mode="create")


# ---- Embedding ------------------------------------------------------------

def embed_batch(client, types_, texts: list[str]) -> list[list[float]]:
    """
    One synchronous embed_content call against gemini-embedding-2.
    Falls back to gemini-embedding-001 if the newer alias is rejected.
    """
    cfg = types_.EmbedContentConfig(
        task_type="RETRIEVAL_DOCUMENT",
        output_dimensionality=EMBED_DIM,
    )
    try:
        resp = client.models.embed_content(
            model=MODEL_EMBED, contents=texts, config=cfg)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid" in msg or "unsupported" in msg:
            sys.stderr.write(
                f"  {MODEL_EMBED} rejected; falling back to {MODEL_EMBED_FALLBACK}\n")
            resp = client.models.embed_content(
                model=MODEL_EMBED_FALLBACK, contents=texts, config=cfg)
        else:
            raise
    return [list(e.values) for e in resp.embeddings]


def build_text_for_embedding(p: dict) -> str:
    """
    Concatenate header context + body so the embedding represents the email
    holistically, not just the body.
    """
    header = (
        f"Subject: {p['subject']}\n"
        f"From: {p['from_addr']}\n"
        f"To: {p['to_addr']}\n"
        f"Date: {p['date']}\n\n"
    )
    return (header + p['body'])[:MAX_TEXT_CHARS]


# ---- Main commands --------------------------------------------------------

def cmd_status(args) -> int:
    maildir = Path(args.maildir).expanduser().resolve()
    done = load_checkpoint(maildir)
    print(f"Checkpoint: {len(done):,} emails marked done")

    try:
        import lancedb
        db = lancedb.connect(str(maildir / ".lancedb"))
        if LDB_TABLE in db.table_names():
            t = db.open_table(LDB_TABLE)
            print(f"LanceDB:    {t.count_rows():,} rows in '{LDB_TABLE}'")
        else:
            print(f"LanceDB:    table '{LDB_TABLE}' does not exist yet")
    except Exception as e:
        print(f"LanceDB:    not readable ({e})")
    return 0


def cmd_reset(args) -> int:
    maildir = Path(args.maildir).expanduser().resolve()
    db_dir = maildir / ".lancedb"
    ck = maildir / CHECKPOINT_FILE

    print(f"This will DELETE {db_dir} and {ck}")
    if not args.yes:
        ans = input("Type 'yes' to confirm: ").strip().lower()
        if ans != "yes":
            print("Aborted."); return 1

    import shutil
    if db_dir.exists():
        shutil.rmtree(db_dir)
        print(f"  removed {db_dir}")
    if ck.exists():
        ck.unlink()
        print(f"  removed {ck}")
    print("Done.")
    return 0


def cmd_index(args) -> int:
    from google import genai
    from google.genai import types

    maildir = Path(args.maildir).expanduser().resolve()
    if not maildir.is_dir():
        sys.stderr.write(f"Not a directory: {maildir}\n"); return 2

    # 60-second per-call timeout. Without this, hung connections wait forever
    # and our retry logic never fires. Timeout in milliseconds per SDK convention.
    client = genai.Client(
        http_options=types.HttpOptions(timeout=60_000),
    )  # picks up GEMINI_API_KEY from env
    table = open_lance_table(maildir)
    done = load_checkpoint(maildir)
    print(f"Resuming with {len(done):,} emails already indexed.")

    # Buffers
    pending_meta: list[dict] = []
    pending_texts: list[str] = []

    processed = 0
    skipped_already_done = 0
    failed_messages = 0
    started = time.time()

    # Backoff schedules (seconds). Rate-limit (429) windows are minutes long,
    # so we use much longer waits for those — up to ~32 min total before giving up.
    # Other transient errors get the normal 31s exponential schedule.
    BACKOFF_RATE_LIMIT = [10, 30, 60, 120, 240, 480, 960]   # 7 attempts, ~32 min
    BACKOFF_OTHER      = [1, 2, 4, 8, 16]                    # 5 attempts, ~31 s

    def flush():
        nonlocal failed_messages
        if not pending_texts:
            return
        max_attempts = max(len(BACKOFF_RATE_LIMIT), len(BACKOFF_OTHER))
        vecs = None
        for attempt in range(max_attempts):
            try:
                vecs = embed_batch(client, types, pending_texts)
                break
            except Exception as e:
                err = str(e).lower()
                is_rate = "429" in err or "resource_exhausted" in err or "rate limit" in err
                schedule = BACKOFF_RATE_LIMIT if is_rate else BACKOFF_OTHER
                if attempt >= len(schedule):
                    failed_messages += len(pending_texts)
                    sys.stderr.write(
                        f"  GIVING UP on batch of {len(pending_texts)} after "
                        f"{attempt} retries: {e}\n")
                    pending_meta.clear(); pending_texts.clear()
                    return
                wait = schedule[attempt]
                tag = "rate-limit" if is_rate else "transient"
                sys.stderr.write(
                    f"  embed {tag} attempt {attempt+1}/{len(schedule)}: "
                    f"{e}; retry in {wait}s\n")
                time.sleep(wait)
        if vecs is None:
            failed_messages += len(pending_texts)
            pending_meta.clear(); pending_texts.clear()
            return

        rows = []
        for meta, vec in zip(pending_meta, vecs):
            rows.append({
                "id":         meta["message_id"] or f"nomid::{meta['eml_path']}",
                "message_id": meta["message_id"],
                "subject":    meta["subject"],
                "from_addr":  meta["from_addr"],
                "to_addr":    meta["to_addr"],
                "cc_addr":    meta["cc_addr"],
                "date":       meta["date"],
                "folder":     meta["folder"],
                "eml_path":   meta["eml_path"],
                "vector":     vec,
            })
        table.add(rows)
        for meta in pending_meta:
            done.add(meta["eml_path"])
        save_checkpoint(maildir, done)
        pending_meta.clear(); pending_texts.clear()

    for folder_label, eml_path in walk_maildir(maildir):
        if str(eml_path) in done:
            skipped_already_done += 1
            continue
        if args.limit and processed >= args.limit:
            break

        try:
            parsed = parse_eml(eml_path)
        except Exception as e:
            sys.stderr.write(f"  parse fail {eml_path}: {e}\n")
            failed_messages += 1
            continue

        if not parsed["body"] and not parsed["subject"]:
            done.add(str(eml_path))
            continue

        parsed["folder"] = folder_label
        text = build_text_for_embedding(parsed)
        pending_meta.append(parsed)
        pending_texts.append(text)

        if len(pending_texts) >= EMBED_BATCH:
            flush()

        processed += 1
        if processed % 1000 == 0:
            rate = processed / (time.time() - started + 1e-9)
            print(f"  processed {processed:,} (skipped {skipped_already_done:,} from checkpoint, "
                  f"{rate:.1f} msgs/sec)")

    flush()
    elapsed = time.time() - started
    rate = processed / elapsed if elapsed > 0 else 0
    print(f"\nDone. processed={processed:,} skipped_existing={skipped_already_done:,} "
          f"failed={failed_messages} elapsed={elapsed:.1f}s ({rate:.1f} msgs/sec)")
    print(f"LanceDB rows: {table.count_rows():,}")
    return 0


# ---- Argparse -------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("maildir", help="Path to Maildir_Archive root")
    p.add_argument("--limit", type=int, default=0,
                   help="Index at most this many emails (smoke test)")
    p.add_argument("--status", action="store_true",
                   help="Show checkpoint + LanceDB row count")
    p.add_argument("--reset", action="store_true",
                   help="Delete the LanceDB and checkpoint, then exit")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip 'yes' prompt for --reset")
    args = p.parse_args()

    if args.status:
        return cmd_status(args)
    if args.reset:
        return cmd_reset(args)
    return cmd_index(args)


if __name__ == "__main__":
    raise SystemExit(main())
