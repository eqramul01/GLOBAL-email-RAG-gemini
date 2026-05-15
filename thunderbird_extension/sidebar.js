// sidebar.js — popup logic for the Gemini Email Search MailExtension.

const BRIDGE = "http://127.0.0.1:8765";

const $ = (sel) => document.querySelector(sel);

// ---- Tab switching --------------------------------------------------------

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById(`panel-${t.dataset.panel}`).classList.add("active");
  });
});

// ---- Bridge health on load ------------------------------------------------

(async function pingHealth() {
  try {
    const r = await fetch(`${BRIDGE}/health`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    $("#status").textContent =
      `Bridge: ok • embed=${j.model_embed} • gen=${j.model_gen} • ` +
      `${(j.indexed_messages || 0).toLocaleString()} messages`;
  } catch (e) {
    $("#status").innerHTML =
      `<span class="err">Bridge unreachable. Run <code>python3 bridge.py</code>.</span>`;
  }
})();

// ---- Search --------------------------------------------------------------

$("#searchBtn").addEventListener("click", runSearch);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  $("#searchBtn").disabled = true;
  $("#searchResults").innerHTML = `<div class="hint">Searching…</div>`;
  try {
    const r = await fetch(`${BRIDGE}/search`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: q, k: 30 }),
    });
    const data = await r.json();
    renderResults(data.matches || []);
  } catch (e) {
    $("#searchResults").innerHTML =
      `<div class="err">Search failed: ${escapeHtml(String(e))}</div>`;
  } finally {
    $("#searchBtn").disabled = false;
  }
}

function renderResults(matches) {
  if (!matches.length) {
    $("#searchResults").innerHTML = `<div class="hint">No matches.</div>`;
    return;
  }
  const html = matches.map((m, i) => `
    <div class="hit" data-mid="${escapeAttr(m.id)}">
      <div class="subject">${escapeHtml(m.subject || "(no subject)")}</div>
      <div class="meta">
        ${escapeHtml(m.from || "")} · ${escapeHtml(m.date || "")}
        · score ${m.score != null ? m.score.toFixed(3) : "?"}
      </div>
      <div class="folder">${escapeHtml(m.folder || "")}</div>
    </div>
  `).join("");
  $("#searchResults").innerHTML = html;
  document.querySelectorAll("#searchResults .hit").forEach((el) => {
    el.addEventListener("click", () =>
      openMessage(el.dataset.mid));
  });
}

// ---- Ask -----------------------------------------------------------------

$("#askBtn").addEventListener("click", runAsk);

async function runAsk() {
  const q = $("#askQ").value.trim();
  if (!q) return;
  const model = $("#modelPick").value || undefined;
  $("#askBtn").disabled = true;
  $("#askAnswer").innerHTML = `<div class="hint">Asking ${model || "gemini-3.1-pro-preview"}…</div>`;
  try {
    const r = await fetch(`${BRIDGE}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: q, k: 30, model }),
    });
    const data = await r.json();
    renderAnswer(data);
  } catch (e) {
    $("#askAnswer").innerHTML =
      `<div class="err">Ask failed: ${escapeHtml(String(e))}</div>`;
  } finally {
    $("#askBtn").disabled = false;
  }
}

function renderAnswer({ answer, sources }) {
  const srcHtml = (sources || []).map((s, i) => `
    <div class="hit" data-mid="${escapeAttr(s.id)}">
      <div class="subject">[${i + 1}] ${escapeHtml(s.subject || "(no subject)")}</div>
      <div class="meta">${escapeHtml(s.from || "")} · ${escapeHtml(s.date || "")}</div>
    </div>
  `).join("");
  $("#askAnswer").innerHTML = `
    <div class="answer">${escapeHtml(answer || "")}</div>
    <div class="sources">
      <h4>Sources</h4>
      ${srcHtml || `<div class="hint">No sources.</div>`}
    </div>
  `;
  document.querySelectorAll("#askAnswer .hit").forEach((el) => {
    el.addEventListener("click", () => openMessage(el.dataset.mid));
  });
}

// ---- Open in Thunderbird --------------------------------------------------

async function openMessage(messageId) {
  if (!messageId) return;
  console.log("[gemini-email] openMessage clicked, mid =", messageId);
  $("#status").textContent = "Opening…";
  try {
    const res = await browser.runtime.sendMessage({
      type: "openByMessageId",
      messageId,
    });
    console.log("[gemini-email] openByMessageId response:", res);
    if (res && res.ok) {
      $("#status").textContent = "Opened ✓ (check the main mail tab)";
    } else {
      $("#status").innerHTML =
        `<span class="err">Couldn't open: ${escapeHtml((res && res.error) || "unknown error")}</span>`;
    }
  } catch (e) {
    console.error("[gemini-email] sendMessage failed:", e);
    $("#status").innerHTML =
      `<span class="err">Click handler failed: ${escapeHtml(String(e))}</span>`;
  }
}

// ---- helpers --------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
