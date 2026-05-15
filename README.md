# Gemini Email Archive

> A Thunderbird-native semantic search and Q&A engine for a converted Outlook archive.
> Powered by **Gemini Embedding 2** + **Gemini 3.1 Pro Preview**, stored locally in **LanceDB**, exposed through a **FastAPI** bridge to a custom **Thunderbird MailExtension**.

![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Thunderbird](https://img.shields.io/badge/thunderbird-115%2B-blue.svg)
![Gemini](https://img.shields.io/badge/gemini-embedding--2%20%2B%203.1--pro--preview-orange.svg)
![LanceDB](https://img.shields.io/badge/vector--store-LanceDB-brightgreen.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

Built to take a 145 GB / 257,096-message Outlook export and turn it into a
searchable, ask-able archive that lives inside Thunderbird and answers natural-
language questions like *"summarize the David Valero settlement
correspondence — what was the matter, what were the key terms, was it finalized?"*

---

## ⚡ Daily Use

Once the one-time setup is done, your daily workflow is just this:

```
1. Open Thunderbird (the bridge auto-starts at login via launchd)
2. Click the Gemini Email Search button (top-right of Thunderbird's toolbar)
3. Search semantically  OR  Ask a natural-language question
4. Click any result → opens the source email in Thunderbird's reader pane
```

That's it. No terminals, no Python invocations, no daily ritual.

If your Mac restarts, the bridge comes back up automatically. If you ever need to see what it's doing:

```bash
# Health check
curl -s http://127.0.0.1:8765/health | python3 -m json.tool

# View live logs
tail -f /tmp/email-bridge.{out,err}.log

# Stop the bridge
launchctl unload ~/Library/LaunchAgents/com.user.email-bridge.plist

# Start it again
launchctl load ~/Library/LaunchAgents/com.user.email-bridge.plist
```

---

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         Your Mac                                   │
│                                                                    │
│  Outlook .pst  ──►  *.mbox files (145 GB, ~257k messages)          │
│                          │                                         │
│                          ▼                                         │
│   02_mbox_to_maildir.py  (preserves full Outlook folder tree)      │
│                          │                                         │
│                          ▼                                         │
│         ┌────────────────────────────────┐                         │
│         │  Maildir_Archive/  (one .eml   │                         │
│         │   per message, .sbd nesting)   │                         │
│         └────────────────────────────────┘                         │
│                  │                  │                              │
│        ┌─────────┘                  └────────┐                     │
│        ▼                                     ▼                     │
│  Thunderbird Local Folders            03_index_emails_gemini.py    │
│  (faithful Outlook tree                   │                        │
│   for visual reading)                     │  batched embed         │
│        ▲                                  ▼                        │
│        │                       ┌──────────────────────┐            │
│        │                       │ Gemini Embedding 2   │ (cloud)    │
│        │                       │ (gemini-embedding-2) │            │
│        │                       └──────────┬───────────┘            │
│        │                                  │ 3072-d vectors         │
│        │                                  ▼                        │
│        │                       ┌──────────────────────┐            │
│        │                       │ LanceDB              │            │
│        │                       │ (.lancedb/ on disk)  │            │
│        │                       └──────────┬───────────┘            │
│        │                                  ▲                        │
│        │  open .eml on click              │ table.search()         │
│        │                                  │                        │
│  ┌─────┴─────────┐    HTTP    ┌───────────┴──────────┐             │
│  │ Thunderbird   │◄──────────►│  bridge.py           │             │
│  │ MailExtension │ localhost  │  (FastAPI on 8765,   │             │
│  │  (sidebar UI) │   :8765    │   launchd-managed)   │             │
│  └───────────────┘            └───────────┬──────────┘             │
│                                           │ generate_content        │
│                                           ▼                        │
│                              ┌──────────────────────┐              │
│                              │ Gemini 3.1 Pro Prev. │ (cloud)      │
│                              └──────────────────────┘  ← RAG answer│
└────────────────────────────────────────────────────────────────────┘
```

**Key design choices:**

- **Local data, cloud reasoning** — your 257k email bodies and their 3072-d vectors live on your Mac. Only the query text (and for Ask, the top-K matched emails) goes to Gemini.
- **No Vertex, no GCP project** — just a single Gemini API key. The SDK's `gemini-embedding-2` and `gemini-3.1-pro-preview` are reached through the unified `google-genai` Python SDK against `https://generativelanguage.googleapis.com`.
- **Thunderbird sees the original Outlook folder tree** — `Inbox`, `Archive/Inbox`, `Sent Items`, `Sync Issues/Conflicts`, all the nested subfolders, exactly as they were in Outlook. The mbox→Maildir converter preserves hierarchy via `.sbd` companion directories.
- **Bridge is the single source of truth** — both the Thunderbird extension (HTTP) and any future MCP connector can call the same `/search` and `/ask` endpoints. One engine, multiple front doors.

---

## 📂 Repository Contents

| File | Purpose |
|---|---|
| `01_inventory.sh` | Read-only mbox audit (sizes, message counts) |
| `02_mbox_to_maildir.py` | Streams mbox → one `.eml` per message, preserves Outlook hierarchy |
| `fix_maildir_layout.py` | Post-conversion repair: moves children into proper `.sbd` companions for Thunderbird |
| `03_index_emails_gemini.py` | Walks Maildir, batched embeds via Gemini Embedding 2, writes LanceDB |
| `04_search_gemini.py` | CLI semantic search (no LLM) |
| `05_ask_gemini.py` | CLI RAG Q&A with citations |
| `bridge.py` | FastAPI server on localhost:8765 — the engine the extension calls |
| `monitor.py` | Live indexing progress monitor (in-place updating) |
| `verify_corpus.py` | Post-indexing integrity check (6 passes, quick or strict mode) |
| `run_full_index.sh` | Background-launches the indexer with `nohup` + unbuffered output |
| `setup_launchd.sh` | One-shot installer for the bridge as a macOS LaunchAgent |
| `requirements.txt` | Python dependencies (`google-genai`, `lancedb`, `fastapi`, etc.) |
| `thunderbird_extension/` | The MV3 MailExtension (manifest, background, sidebar UI, icon) |

---

## 🤖 Models

Defaults match the Models page (May 2026). All configurable via constants at the top of each script.

| Job | Model | Why |
|---|---|---|
| Document embeddings | `gemini-embedding-2` | Gemini Embedding 2 — multimodal (text + image + video + audio + PDF), 3072-d. GA April 2026. |
| Query embeddings | `gemini-embedding-2` (`task_type=RETRIEVAL_QUERY`) | Same model, query-side task type. |
| RAG answer (default) | `gemini-3.1-pro-preview` | Highest reasoning in the 3.1 family — ideal for "summarize this thread with citations". |
| Quick / lower-latency | `gemini-3-flash-preview` | Pro-level intelligence at a fraction of latency. |
| Bulk classification | `gemini-3.1-flash-lite` | Cost-efficient, designed for high-volume work. |
| Stable fallback | `gemini-2.5-pro`, `gemini-2.5-flash` | Use if a 3.1 preview model is unavailable. |

The bridge accepts any model name in this list via the `model` field on `/ask` requests; the extension's UI exposes them in a dropdown.

---

## 🔧 One-Time Setup

### Phase 1 — Inventory the source (3–10 min)

```bash
cd ~/GLOBAL_E-mail_Outlook/Email_Archive_Toolkit_Gemini
bash 01_inventory.sh /path/to/Mails | tee inventory_pre_conversion.txt
```

Counts every `.mbox` file's size and message count. Save the total — you'll compare it after conversion.

### Phase 2 — Convert mbox → Maildir (~4–6 min for 145 GB)

```bash
python3 02_mbox_to_maildir.py /path/to/Mails ~/GLOBAL_E-mail_Outlook/Maildir_Archive 2>&1 | tee conversion.log
```

Pure-bytes streaming parser — no `mailbox.mbox` (which crashes on Outlook-export quirks). Writes one `.eml` per message, preserves Outlook folder hierarchy. Then run `fix_maildir_layout.py` to convert the layout into Thunderbird's `.sbd` convention:

```bash
python3 fix_maildir_layout.py ~/GLOBAL_E-mail_Outlook/Maildir_Archive
```

### Phase 3 — Install Thunderbird and load the archive (~20 min)

1. `brew install --cask thunderbird` (or DMG from thunderbird.net)
2. Create a dedicated profile via `--ProfileManager` (or directly via prefs.js — see the original setup notes)
3. Edit `prefs.js` to set Maildir storage **before** first launch:
   ```javascript
   user_pref("mail.serverDefaultStoreContractID", "@mozilla.org/msgstore/maildirstore;1");
   ```
4. Launch the profile, cancel the email-setup wizard
5. `cp -R ~/GLOBAL_E-mail_Outlook/Maildir_Archive/* ~/path/to/Thunderbird_Profile/Mail/Local\ Folders/` (~5 min on APFS thanks to clonefile)
6. Relaunch — you'll see your full Outlook tree in the sidebar

### Phase 4 — Get a Gemini API key + Python deps (10 min)

Get a key at <https://aistudio.google.com/apikey>, then:

```bash
echo 'export GEMINI_API_KEY="AIza…"' >> ~/.zshrc
source ~/.zshrc
pip3 install --user -r requirements.txt
```

Sanity-check:
```bash
python3 -c "
from google import genai
r = genai.Client().models.embed_content(model='gemini-embedding-2', contents='hello')
print('OK, dim=', len(r.embeddings[0].values))
"
```

### Phase 5 — Index everything (~2–4 hours unattended)

```bash
# Smoke test first (500 emails, ~$0.01, 30 sec)
python3 03_index_emails_gemini.py ~/GLOBAL_E-mail_Outlook/Maildir_Archive --limit 500

# Full run, backgrounded
bash run_full_index.sh

# Watch progress live
python3 monitor.py
```

Monitor prints one updating line: `rows 51,800/257,096 | 20.15% | 39.7/sec | ETA 1.3h | alive ✓`. Indexer is fully resumable — Ctrl-C is safe, restarts pick up at the last checkpoint.

When it's done:

```bash
python3 verify_corpus.py            # quick sample-based check (~30 sec)
python3 verify_corpus.py --strict   # full Message-ID cross-check (~5 min)
```

Look for `VERDICT: PASS — corpus is intact and complete`.

### Phase 6 — Try the CLI to validate the engine

```bash
python3 04_search_gemini.py "wire instructions for the closing"
python3 05_ask_gemini.py "Summarize the David Valero settlement correspondence"
```

If both produce real results, the engine works end-to-end.

### Phase 7 — Wire up the Thunderbird extension

```bash
# 1. Install the bridge as a launchd-managed service (auto-starts at login)
bash setup_launchd.sh

# 2. Confirm the bridge is alive
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
```

Then in Thunderbird:

1. **Tools → Developer Tools → Debug Add-Ons**
2. **Load Temporary Add-on…** → select `thunderbird_extension/manifest.json`
3. The Gemini Email Search button appears in the toolbar — click it
4. Search or Ask — results render in the sidebar; click any result to open the source email in Thunderbird's reader

To make the extension **permanent** (so you don't re-load it after every Thunderbird restart):

1. In Thunderbird's `about:config`, set `xpinstall.signatures.required` = false
2. Zip the extension: `cd thunderbird_extension && zip -r ../gemini-email-search.xpi . -x "*.DS_Store"`
3. **Tools → Add-ons and Themes → ⚙ → Install Add-on From File…** → pick the .xpi

After that, the extension persists across Thunderbird restarts. Updates: re-zip + re-install.

---

## 🔒 Privacy

What goes to Google:

- **Per query (Search):** the query text only. ~100 bytes out, 12 KB back.
- **Per query (Ask):** the question + the top 30 matched emails (subject + sender + ~4 KB body each). ~50–150 KB out, ~2 KB back.
- **Per indexing run (one-time):** every non-empty email body, truncated to 30 KB chars. After indexing, bodies don't go to Gemini again.

What stays on your Mac:

- The 256k+ other emails Gemini doesn't see for any given query
- All vectors, all metadata, all `.eml` files
- The bridge process itself — listens on `127.0.0.1:8765` only, never exposes a port externally

If the corpus is sensitive (legal correspondence, medical records, etc.), the indexing step is the privacy-relevant one — every body goes through the API once. Gemini's data-handling terms apply (currently: API content not used to train models on the paid tier; check current policy at the time of use).

---

## 💵 Cost

Rough numbers based on the actual 257k-message build:

- **Initial indexing:** ~$10–30 total (depends on average email length; most are short)
- **Per Search query:** ~$0.0001 (one embedding call)
- **Per Ask query:** ~$0.001–0.005 (embedding + RAG with 30-email context window)
- **Bridge running idle:** $0 — no API calls when no queries are happening
- **LanceDB storage:** ~3–6 GB on disk for 256k vectors at 3072-d

---

## 🛠️ Troubleshooting

**Bridge won't start under launchd, but works in foreground.**
Check `tail /tmp/email-bridge.err.log`. Most likely a Python module path issue (system `python3` vs the one with the SDK installed). Re-run `pip3 install --user -r requirements.txt` against the same Python that launchd is using.

**Extension says "Bridge unreachable".**
The bridge died, or CORS isn't set up for the extension's origin. `curl http://127.0.0.1:8765/health` to verify the bridge is alive. If yes, the bridge is up but not letting the extension talk to it — check that bridge.py has the `CORSMiddleware` block.

**Search returns duplicates.**
Outlook archive structures often duplicate the same email across `Inbox/`, `Archive/Inbox/`, etc. The same .eml gets indexed under different folder paths. Add a content-hash dedup pass to bridge.py if it bothers you.

**Click-to-open does nothing for older emails.**
About half of pre-2015 emails don't have a `Message-ID` header. The extension's open-by-Message-ID falls back to a path-based ID for those, but the click-to-open uses Thunderbird's `messages.query({headerMessageId})` API which only works for emails with real Message-IDs. The result row still shows the path so you can navigate manually.

**Indexer hangs at a specific row count.**
A single Gemini API call hung without a timeout. Patch already added — `http_options=types.HttpOptions(timeout=60_000)` on the client init. Hung calls now fail after 60 seconds and trigger retry.

**429 RESOURCE_EXHAUSTED errors during indexing.**
The patched indexer has a longer backoff specifically for rate-limit errors: `[10, 30, 60, 120, 240, 480, 960]` seconds across 7 attempts. Most 429 windows clear within that span.

---

## 🚀 What's next (potential extensions)

- **MCP connector** — wrap the bridge's `/search` and `/ask` as MCP tools so Claude (Desktop, Code, Cowork) can query the corpus during conversations. The bridge is the same; the MCP server is a thin protocol wrapper.
- **Multi-corpus** — same toolkit, different `.lancedb/`s for text messages, case files, contracts, etc. One MCP server can expose all of them with a `corpus` parameter.
- **Result deduplication** — content-hash dedup in `/search` and `/ask` to collapse the Outlook-archive folder duplication.
- **Async bridge** — switch `bridge.py` to `client.aio.models.*` for higher concurrency on parallel queries.

---

## 📝 License

MIT — do whatever you want with it.
