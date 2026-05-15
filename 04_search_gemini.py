#!/usr/bin/env python3
"""
04_search_gemini.py — Semantic search over the local LanceDB built by
03_index_emails_gemini.py. No LLM call; embed-the-query and find nearest
neighbors.

Usage:
    python3 04_search_gemini.py "wire instructions Muskat closing"
    python3 04_search_gemini.py "severance" -k 25
    python3 04_search_gemini.py "wire fraud" --json
    python3 04_search_gemini.py --maildir /custom/path "..."

Reads:
    GEMINI_API_KEY              for embedding the query
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MODEL_EMBED = "gemini-embedding-2"
MODEL_EMBED_FALLBACK = "gemini-embedding-001"
EMBED_DIM = 3072
LDB_TABLE = "emails"
DEFAULT_MAILDIR = os.path.expanduser("~/GLOBAL_E-mail_Outlook/Maildir_Archive")


def embed_query(text: str) -> list[float]:
    from google import genai
    from google.genai import types
    client = genai.Client()
    cfg = types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY",
        output_dimensionality=EMBED_DIM,
    )
    try:
        resp = client.models.embed_content(model=MODEL_EMBED, contents=text, config=cfg)
    except Exception as e:
        if any(s in str(e).lower() for s in ("not found", "invalid", "unsupported")):
            resp = client.models.embed_content(
                model=MODEL_EMBED_FALLBACK, contents=text, config=cfg)
        else:
            raise
    return list(resp.embeddings[0].values)


def search(maildir: Path, query_vec: list[float], k: int) -> list[dict]:
    import lancedb
    db = lancedb.connect(str(maildir / ".lancedb"))
    if LDB_TABLE not in db.table_names():
        sys.stderr.write(f"No '{LDB_TABLE}' table at {maildir}/.lancedb/ — run 03_index first.\n")
        return []
    table = db.open_table(LDB_TABLE)
    # LanceDB returns rows with all columns + a `_distance` field
    rows = table.search(query_vec).limit(k).to_list()
    return rows


def render_human(rows: list[dict]) -> None:
    if not rows:
        print("No matches.")
        return
    for rank, r in enumerate(rows, 1):
        score = r.get("_distance", "?")
        if isinstance(score, float):
            score = f"{score:.4f}"
        subject = (r.get("subject") or "").replace("\n", " ")[:80]
        from_ = r.get("from_addr", "")[:50]
        date = (r.get("date") or "")[:25]
        folder = r.get("folder", "")[:40]
        print(f"[{rank:2d}] dist={score}  {subject!r}")
        print(f"     from={from_}  date={date}")
        print(f"     folder={folder}")
        print(f"     path={r.get('eml_path', '')}")
        print()


def render_json(rows: list[dict]) -> None:
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "distance": r.get("_distance"),
            "subject": r.get("subject"),
            "from": r.get("from_addr"),
            "to": r.get("to_addr"),
            "date": r.get("date"),
            "folder": r.get("folder"),
            "eml_path": r.get("eml_path"),
        })
    print(json.dumps(out, indent=2, default=str))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("query", nargs="+", help="Natural language query")
    p.add_argument("-k", "--top-k", type=int, default=30)
    p.add_argument("--maildir", default=DEFAULT_MAILDIR,
                   help="Maildir root containing .lancedb/ (default: %(default)s)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args()

    q = " ".join(args.query)
    maildir = Path(args.maildir).expanduser().resolve()
    vec = embed_query(q)
    rows = search(maildir, vec, args.top_k)

    if args.json:
        render_json(rows)
    else:
        print(f"Top {len(rows)} matches for: {q!r}\n")
        render_human(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
