// background.js — Thunderbird MailExtension background script.
//
// Receives requests from the popup/sidebar UI to open a specific message by
// Message-ID. The popup itself does the HTTP calls to 127.0.0.1:8765; this
// script only handles the bits that need WebExtension privileges (querying
// Thunderbird's message store and opening a message tab).

const BRIDGE_BASE = "http://127.0.0.1:8765";

browser.runtime.onMessage.addListener(async (msg, sender) => {
  if (msg && msg.type === "openByMessageId") {
    return openByMessageId(msg.messageId);
  }
  if (msg && msg.type === "bridgeFetch") {
    // Optional helper: route fetches through the background script so the
    // popup doesn't need to know the bridge URL. Lets us swap ports later.
    return bridgeFetch(msg.path, msg.body);
  }
});

async function openMessageById(id) {
  if (browser.messageDisplay && browser.messageDisplay.open) {
    await browser.messageDisplay.open({ messageId: id });
  } else if (browser.messages.openMessage) {
    await browser.messages.openMessage(id);
  } else {
    return { ok: false, error: "No messageDisplay API in this Thunderbird." };
  }
  return { ok: true };
}

async function openByMessageId(headerMessageId) {
  console.log("[gemini-email-bg] openByMessageId called with:", headerMessageId);

  // Skip path-based fallback IDs (emails without Message-ID headers)
  if (!headerMessageId || headerMessageId.startsWith("nomid::")) {
    return {
      ok: false,
      error: "This email has no Message-ID header (older email). " +
             "Open it manually from the path shown.",
    };
  }

  const mid = headerMessageId.replace(/^<|>$/g, "");

  // Approach 1 — modern Thunderbird supports cross-folder query (no folder param)
  try {
    const page = await browser.messages.query({ headerMessageId: mid });
    const matches = (page && page.messages) || [];
    console.log("[gemini-email-bg] cross-folder query returned", matches.length, "matches");
    if (matches.length > 0) {
      return await openMessageById(matches[0].id);
    }
  } catch (e) {
    console.warn("[gemini-email-bg] cross-folder query unavailable:", e.message);
  }

  // Approach 2 — explicit folder walk. accounts.list(true) includes folders.
  let accounts;
  try {
    accounts = await browser.accounts.list(true);
  } catch (e) {
    accounts = await browser.accounts.list();   // fallback for older API
  }
  console.log("[gemini-email-bg] accounts (with folders):",
              accounts.map(a => `${a.name} (${(a.folders || []).length} top folders)`));

  let folderCount = 0;
  for (const account of accounts) {
    // Modern Thunderbird may put folders under .rootFolder.subFolders;
    // older API put them flat in .folders. Handle both.
    const startNodes = account.folders && account.folders.length
      ? account.folders
      : (account.rootFolder ? [account.rootFolder] : []);

    for (const folder of walkFolders(startNodes)) {
      folderCount++;
      try {
        const page = await browser.messages.query({ folder, headerMessageId: mid });
        if (page && page.messages && page.messages.length > 0) {
          console.log("[gemini-email-bg] found mid in folder:", folder.path || folder.name,
                      "messageId:", page.messages[0].id);
          return await openMessageById(page.messages[0].id);
        }
      } catch (e) {
        // skip unqueryable folders
      }
    }
  }
  console.log("[gemini-email-bg] not found after scanning", folderCount, "folders");
  return {
    ok: false,
    error: `Message-ID not found after scanning ${folderCount} folders. ` +
           `(mid: ${mid.slice(0, 80)}${mid.length > 80 ? '…' : ''})`,
  };
}

function* walkFolders(node) {
  // Tolerates being passed either an array of folders or a single folder
  if (Array.isArray(node)) {
    for (const f of node) yield* walkFolders(f);
    return;
  }
  if (!node) return;
  yield node;
  if (node.subFolders && node.subFolders.length) {
    yield* walkFolders(node.subFolders);
  }
}

async function bridgeFetch(path, body) {
  const r = await fetch(`${BRIDGE_BASE}${path}`, {
    method: body ? "POST" : "GET",
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    return { ok: false, status: r.status, error: await r.text() };
  }
  return { ok: true, data: await r.json() };
}
