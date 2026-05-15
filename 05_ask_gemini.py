#!/usr/bin/env python3
"""
05_ask_gemini.py — Retrieval-augmented Q&A. Embeds your question with
gemini-embedding-2 (RETRIEVAL_QUERY), pulls the top-k matching emails
from the local LanceDB, then asks gemini-3.1-pro-preview to answer with
inline citations.

Usage:
    python3 05_ask_gemini.py "Summarize the Muskat closing correspondence"
    python3 05_ask_gemini.py --model gemini-3-flash-preview "Who handled severance?"
    python3 05_ask_gemini.py                 # interactive REPL

Safety: all HarmCategory thresholds set to OFF. Legal correspondence
routinely contains threats, profanity, and adversarial language; default
filters refuse to summarize whole threads.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import os
import sys
import textwrap
from pathlib import Path

MODEL_EMBED = "gemini-embedding-2"
MODEL_EMBED_FALLBACK = "gemini-embedding-001"
MODEL_GEN_DEFAULT = "gemini-3.1-pro-preview"
EMBED_DIM = 3072
DEFAULT_K = 30
LDB_TABLE = "emails"
DEFAULT_MAILDIR = os.path.expanduser("~/GLOBAL_E-mail_Outlook/Maildir_Archive")
MAX_BODY_CHARS_PER_DOC = 4000   # cap per-source text fed to the LLM


SAFETY_OFF = [
    {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY",   "threshold": "OFF"},
]


def _client():
    from google import genai
    return genai.Client()


def embed_query(client, text: str) -> list[float]:
    from google.genai import types
    cfg = types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY",
        output_dimensionality=EMBED_DIM,
    )
    try:
        r = client.models.embed_content(model=MODEL_EMBED, contents=text, config=cfg)
    except Exception as e:
        if any(s in str(e).lower() for s in ("not found", "invalid", "unsupported")):
            r = client.models.embed_content(
                model=MODEL_EMBED_FALLBACK, contents=text, config=cfg)
        else:
            raise
    return list(r.embeddings[0].values)


def lance_search(maildir: Path, vec: list[float], k: int) -> list[dict]:
    import lancedb
    db = lancedb.connect(str(maildir / ".lancedb"))
    if LDB_TABLE not in db.table_names():
        return []
    table = db.open_table(LDB_TABLE)
    return table.search(vec).limit(k).to_list()


def hydrate_body(eml_path: str) -> str:
    """Read the .eml from disk and extract plain-text body (capped)."""
    p = Path(eml_path)
    if not p.exists():
        return ""
    try:
        with p.open("rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
    except Exception:
        return ""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_content()
                    break
                except Exception:
                    continue
    else:
        try:
            body = msg.get_content()
        except Exception:
            body = ""
    return (body or "")[:MAX_BODY_CHARS_PER_DOC]


def build_prompt(question: str, docs: list[dict]) -> str:
    blocks = []
    for i, d in enumerate(docs, 1):
        blocks.append(textwrap.dedent(f"""\
            [{i}] Subject: {d.get('subject', '')}
                From: {d.get('from_addr', '')}
                Date: {d.get('date', '')}
                Folder: {d.get('folder', '')}
                Path: {d.get('eml_path', '')}
                ---
                {d.get('body', '')}
            """).strip())
    joined = "\n\n".join(blocks)
    return textwrap.dedent(f"""\
        You are answering questions about a law firm email archive belonging
        to the user. The emails below are the only context available; do not
        invent facts not present in them. Cite sources inline using bracketed
        numbers like [3] that match the document numbers below.

        Question: {question}

        Emails:
        {joined}

        Answer:""")


def ask(client, question: str, k: int, model: str, maildir: Path) -> tuple[str, list[dict]]:
    from google.genai import types

    vec = embed_query(client, question)
    rows = lance_search(maildir, vec, k)

    if not rows:
        return "No matching emails found.", []

    docs = []
    for r in rows:
        docs.append({
            "id": r.get("id"),
            "subject": r.get("subject", ""),
            "from_addr": r.get("from_addr", ""),
            "to_addr": r.get("to_addr", ""),
            "date": r.get("date", ""),
            "folder": r.get("folder", ""),
            "eml_path": r.get("eml_path", ""),
            "body": hydrate_body(r.get("eml_path", "")),
        })

    prompt = build_prompt(question, docs)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
            safety_settings=[types.SafetySetting(**s) for s in SAFETY_OFF],
        ),
    )
    return resp.text or "(empty response)", docs


def render(answer: str, docs: list[dict]) -> None:
    print(answer.strip())
    print("\nSources:")
    for i, d in enumerate(docs, 1):
        subj = (d.get("subject") or "(no subject)")[:60]
        print(f"  [{i}] {subj!r}  {d.get('from_addr', '')}  {d.get('date', '')}")
        print(f"        {d.get('eml_path', '')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="*", help="Natural-language question")
    p.add_argument("-k", "--top-k", type=int, default=DEFAULT_K)
    p.add_argument("--model", default=MODEL_GEN_DEFAULT)
    p.add_argument("--maildir", default=DEFAULT_MAILDIR,
                   help="Maildir root containing .lancedb/ (default: %(default)s)")
    args = p.parse_args()

    client = _client()
    maildir = Path(args.maildir).expanduser().resolve()

    if args.question:
        ans, docs = ask(client, " ".join(args.question), args.top_k, args.model, maildir)
        render(ans, docs)
        return 0

    print(f"ask> using {args.model} (^D to quit)")
    while True:
        try:
            q = input("ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        ans, docs = ask(client, q, args.top_k, args.model, maildir)
        render(ans, docs)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
