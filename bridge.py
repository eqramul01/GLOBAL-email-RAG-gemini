#!/usr/bin/env python3
"""
bridge.py — FastAPI server on 127.0.0.1:8765 that the Thunderbird
MailExtension calls. Holds the Gemini API key and the LanceDB handle so
the extension never touches secrets.

Endpoints:
    GET  /health           -> {"ok": true, "model_gen": "...", "model_embed": "...", "rows": N}
    POST /search           -> {"matches": [...]}
    POST /ask              -> {"answer": "...", "sources": [...]}

Run:
    python3 bridge.py
    python3 bridge.py --port 8765 --maildir ~/GLOBAL_E-mail_Outlook/Maildir_Archive

Env vars expected:
    GEMINI_API_KEY
"""
from __future__ import annotations

import argparse
import email
import email.policy
import os
import sys
from pathlib import Path

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn


# Lazy-imported, populated on startup.
_genai = None
_types = None
_genai_client = None
_lance_table = None

# Defaults (overridden by env / CLI)
MODEL_EMBED = "gemini-embedding-2"
MODEL_EMBED_FALLBACK = "gemini-embedding-001"
MODEL_GEN = os.environ.get("GEMINI_MODEL_GEN", "gemini-3.1-pro-preview")
EMBED_DIM = 3072
LDB_TABLE = "emails"
MAX_BODY_CHARS_PER_DOC = 4000

MAILDIR_ROOT = Path(os.environ.get(
    "MAILDIR_ROOT",
    os.path.expanduser("~/GLOBAL_E-mail_Outlook/Maildir_Archive")
)).expanduser().resolve()

# Tier registry — supported by /ask
SUPPORTED_GEN_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
)


# -------------------------------------------------------------------------
#  Models
# -------------------------------------------------------------------------

class SearchReq(BaseModel):
    query: str
    k: int = Field(default=30, ge=1, le=100)


class AskReq(BaseModel):
    question: str
    k: int = Field(default=30, ge=1, le=100)
    model: Optional[str] = None


# -------------------------------------------------------------------------
#  Startup
# -------------------------------------------------------------------------

def _init_clients() -> None:
    global _genai, _types, _genai_client, _lance_table

    from google import genai
    from google.genai import types
    import lancedb

    _genai = genai
    _types = types
    _genai_client = genai.Client()

    db = lancedb.connect(str(MAILDIR_ROOT / ".lancedb"))
    if LDB_TABLE not in db.table_names():
        sys.stderr.write(
            f"WARN: no '{LDB_TABLE}' table at {MAILDIR_ROOT}/.lancedb/. "
            "Run 03_index_emails_gemini.py first.\n")
        _lance_table = None
    else:
        _lance_table = db.open_table(LDB_TABLE)
        print(f"  LanceDB rows: {_lance_table.count_rows():,}")


# -------------------------------------------------------------------------
#  Helpers
# -------------------------------------------------------------------------

def _embed_query(text: str) -> list[float]:
    cfg = _types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY",
        output_dimensionality=EMBED_DIM,
    )
    try:
        r = _genai_client.models.embed_content(model=MODEL_EMBED, contents=text, config=cfg)
    except Exception as e:
        if any(s in str(e).lower() for s in ("not found", "invalid", "unsupported")):
            r = _genai_client.models.embed_content(
                model=MODEL_EMBED_FALLBACK, contents=text, config=cfg)
        else:
            raise
    return list(r.embeddings[0].values)


def _search(vec: list[float], k: int) -> list[dict]:
    if _lance_table is None:
        return []
    return _lance_table.search(vec).limit(k).to_list()


def _hydrate_body(eml_path: str) -> str:
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


def _row_to_dict(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "score": r.get("_distance"),
        "message_id": r.get("message_id"),
        "subject": r.get("subject", ""),
        "from": r.get("from_addr", ""),
        "to": r.get("to_addr", ""),
        "date": r.get("date", ""),
        "folder": r.get("folder", ""),
        "path": r.get("eml_path", ""),
    }


# -------------------------------------------------------------------------
#  FastAPI app
# -------------------------------------------------------------------------

app = FastAPI(title="Email Archive Bridge")

# CORS: allow the Thunderbird MailExtension (moz-extension://...) and any
# localhost client to call the bridge. The bridge only listens on 127.0.0.1
# so this isn't a network exposure issue — just satisfies browser CORS rules.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(moz-extension|chrome-extension)://.*$|^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "ok": _lance_table is not None,
        "model_embed": MODEL_EMBED,
        "model_gen": MODEL_GEN,
        "supported_gen_models": list(SUPPORTED_GEN_MODELS),
        "maildir": str(MAILDIR_ROOT),
        "indexed_messages": _lance_table.count_rows() if _lance_table else 0,
    }


@app.post("/search")
def search(req: SearchReq):
    if _genai_client is None:
        raise HTTPException(503, "Bridge not initialized")
    if _lance_table is None:
        raise HTTPException(503, "LanceDB table missing — run 03_index first")
    vec = _embed_query(req.query)
    rows = _search(vec, req.k)
    return {"matches": [_row_to_dict(r) for r in rows]}


@app.post("/ask")
def ask(req: AskReq):
    if _genai_client is None:
        raise HTTPException(503, "Bridge not initialized")
    if _lance_table is None:
        raise HTTPException(503, "LanceDB table missing — run 03_index first")

    model = req.model or MODEL_GEN
    if model not in SUPPORTED_GEN_MODELS:
        raise HTTPException(400, f"unsupported model {model!r}; pick one of {SUPPORTED_GEN_MODELS}")

    vec = _embed_query(req.question)
    rows = _search(vec, req.k)

    docs = []
    for r in rows:
        docs.append({
            "id": r.get("id"),
            "subject": r.get("subject", ""),
            "from_addr": r.get("from_addr", ""),
            "date": r.get("date", ""),
            "folder": r.get("folder", ""),
            "eml_path": r.get("eml_path", ""),
            "body": _hydrate_body(r.get("eml_path", "")),
        })
    if not docs:
        return {"answer": "No matching emails found.", "sources": []}

    blocks = []
    for i, d in enumerate(docs, 1):
        blocks.append(
            f"[{i}] Subject: {d['subject']}\n"
            f"    From: {d['from_addr']}\n"
            f"    Date: {d['date']}\n"
            f"    Folder: {d['folder']}\n"
            f"    Path: {d['eml_path']}\n"
            f"    ---\n"
            f"    {d['body']}\n"
        )
    prompt = (
        "You are answering questions about a law firm email archive belonging "
        "to the user. The emails below are the only context available; do not "
        "invent facts not present in them. Cite sources inline using bracketed "
        "numbers like [3] that match the document numbers below.\n\n"
        f"Question: {req.question}\n\nEmails:\n" + "\n".join(blocks) +
        "\n\nAnswer:"
    )

    safety = [
        _types.SafetySetting(category=c, threshold="OFF") for c in (
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_CIVIC_INTEGRITY",
        )
    ]
    resp = _genai_client.models.generate_content(
        model=model,
        contents=prompt,
        config=_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
            safety_settings=safety,
        ),
    )
    return {
        "answer": resp.text or "",
        "sources": [
            {"id": d["id"], "subject": d["subject"], "from": d["from_addr"],
             "date": d["date"], "path": d["eml_path"]}
            for d in docs
        ],
    }


# -------------------------------------------------------------------------
#  Entry point
# -------------------------------------------------------------------------

@app.on_event("startup")
def _on_startup():
    print("bridge: initializing google + lancedb clients...")
    _init_clients()
    print("bridge: ready.")


def main() -> int:
    global MAILDIR_ROOT, MODEL_GEN
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--maildir", default=str(MAILDIR_ROOT),
                   help="Maildir root containing .lancedb/")
    p.add_argument("--model-gen", default=MODEL_GEN,
                   help="Default generative model")
    args = p.parse_args()

    MAILDIR_ROOT = Path(args.maildir).expanduser().resolve()
    MODEL_GEN = args.model_gen

    if not os.environ.get("GEMINI_API_KEY"):
        sys.stderr.write("missing env var: GEMINI_API_KEY\n")
        return 2

    print(f"bridge listening on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Sample launchd plist (~/Library/LaunchAgents/com.user.email-bridge.plist):
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
# <dict>
#   <key>Label</key><string>com.user.email-bridge</string>
#   <key>ProgramArguments</key>
#   <array>
#     <string>/usr/bin/env</string>
#     <string>python3</string>
#     <string>/Users/YOU/GLOBAL_E-mail_Outlook/Email_Archive_Toolkit_Gemini/bridge.py</string>
#   </array>
#   <key>EnvironmentVariables</key>
#   <dict>
#     <key>GEMINI_API_KEY</key><string>...</string>
#     <key>MAILDIR_ROOT</key><string>/Users/YOU/GLOBAL_E-mail_Outlook/Maildir_Archive</string>
#   </dict>
#   <key>RunAtLoad</key><true/>
#   <key>KeepAlive</key><true/>
#   <key>StandardOutPath</key><string>/tmp/email-bridge.out.log</string>
#   <key>StandardErrorPath</key><string>/tmp/email-bridge.err.log</string>
# </dict>
# </plist>
#
# launchctl load ~/Library/LaunchAgents/com.user.email-bridge.plist
# ---------------------------------------------------------------------------
