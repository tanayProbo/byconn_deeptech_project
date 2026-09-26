/**
 * BYCONN-X dashboard controller — fully wired to backend.
 *
 * Features:
 *  - Live analytics cards (pages, vectors, entities, relations)
 *  - Real job history populated from in-memory jobs
 *  - Clickable Quick Actions
 *  - Rich result rendering with entity cards and metadata
 *  - Full health + API discovery wiring
 */

const API_BASE = "/api/v1";

const TAB_META = {
    "new-search": { title: "New Search", subtitle: "Command the AI Agent to fetch and analyze data." },
    history:      { title: "Search History", subtitle: "Review your past queries and crawler executions." },
    saved:        { title: "Saved Insights", subtitle: "Manage exported data and saved research." },
    "api-keys":   { title: "Service Health", subtitle: "Inspect the engine health and service credentials." },
    "api-discovery": { title: "API Discovery", subtitle: "REST endpoints sniffed from previous crawls." }
};

const MODES = {
    "Deep Research": { kind: "crawl", depth: 2 },
    "Fast Scrape":   { kind: "crawl", depth: 1 },
    "Visual Action": { kind: "agent", steps: 3 }
};

let activeJobId  = null;
let pollTimer    = null;
let activeMode   = null;
let initialized  = false;

// In-memory job history for history tab
const jobHistory = [];

/* ================================================================ INIT */
document.addEventListener("DOMContentLoaded", () => {
    if (initialized) return;
    initialized = true;
    setupTabNavigation();
    setupSearch();
    setupClearButton();
    wireHealthRefresh();
    setupHealthIndicator();
    wireApiDiscovery();
    wireQuickActions();
    renderHistoryTable();
    renderAnalyticsCards(null);
});

/* ============================================================== TABS */
function setupTabNavigation() {
    const nav = document.querySelector(".nav-links");
    if (!nav) return;
    nav.addEventListener("click", (e) => {
        const item = e.target.closest(".nav-item");
        if (!item) return;
        e.preventDefault();
        activateTab(item.getAttribute("data-tab"));
    });
}

function activateTab(tabId) {
    if (!tabId || !(tabId in TAB_META)) return;
    document.querySelectorAll(".nav-item").forEach(n =>
        n.classList.toggle("active", n.getAttribute("data-tab") === tabId)
    );
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    const panel = document.getElementById(`panel-${tabId}`);
    if (panel) panel.classList.add("active");

    const meta = TAB_META[tabId];
    const title = document.getElementById("page-title");
    const sub   = document.getElementById("page-subtitle");
    if (title) title.textContent = meta.title;
    if (sub)   sub.textContent   = meta.subtitle;

    if (tabId === "history")       renderHistoryTable();
    if (tabId === "api-discovery") loadDiscoveredApis();
    if (tabId === "api-keys")      setupHealthIndicator();
}

/* ========================================================== ANALYTICS */
function renderAnalyticsCards(job) {
    const cards = document.getElementById("analytics-cards");
    if (!cards) return;

    const pages    = job ? job.pages_crawled       : 0;
    const vectors  = job ? job.chunks_indexed      : 0;
    const entities = job ? job.entities_extracted  : 0;
    const relations= job ? job.relations_written    : 0;
    const duration = job ? (job.duration_seconds ?? "—") : "—";

    cards.innerHTML = `
        <div class="analytics-card">
            <div class="analytics-icon">📄</div>
            <div class="analytics-value" id="stat-pages">${pages}</div>
            <div class="analytics-label">Pages Crawled</div>
        </div>
        <div class="analytics-card">
            <div class="analytics-icon">🧠</div>
            <div class="analytics-value" id="stat-vectors">${vectors}</div>
            <div class="analytics-label">Vectors Indexed</div>
        </div>
        <div class="analytics-card">
            <div class="analytics-icon">🔗</div>
            <div class="analytics-value" id="stat-entities">${entities}</div>
            <div class="analytics-label">Entities Extracted</div>
        </div>
        <div class="analytics-card">
            <div class="analytics-icon">🕸️</div>
            <div class="analytics-value" id="stat-relations">${relations}</div>
            <div class="analytics-label">Graph Relations</div>
        </div>
        <div class="analytics-card">
            <div class="analytics-icon">⚡</div>
            <div class="analytics-value">${duration}${typeof duration === "number" ? "s" : ""}</div>
            <div class="analytics-label">Time Taken</div>
        </div>
    `;
}

function updateLiveStats(job) {
    const s = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    s("stat-pages",    job.pages_crawled);
    s("stat-vectors",  job.chunks_indexed);
    s("stat-entities", job.entities_extracted);
    s("stat-relations",job.relations_written);
}

/* ============================================================== SEARCH */
function setupSearch() {
    // Wire launch button (removes inline onclick)
    const btn = document.querySelector('button[onclick="startSearch()"]');
    if (btn) {
        btn.removeAttribute("onclick");
        btn.addEventListener("click", startSearch);
    }
    // Also wire by ID if it exists
    const btnById = document.getElementById("launch-btn");
    if (btnById) btnById.addEventListener("click", startSearch);

    // Enter key in input
    const input = document.getElementById("main-search-input");
    if (input) input.addEventListener("keydown", e => { if (e.key === "Enter") startSearch(); });
}

function setupClearButton() {
    document.querySelectorAll(".btn-secondary").forEach(btn => {
        if (btn.textContent.trim().toLowerCase() === "clear") {
            btn.addEventListener("click", clearSearch);
        }
    });
}

async function startSearch() {
    const input = document.getElementById("main-search-input");
    if (!input) return;
    const query = input.value.trim();
    if (!query) {
        showToast("Please enter a URL or query first.", "warn");
        return;
    }

    const target = parseTarget(query);
    if (!target.url) {
        logLine("Could not find a target. Enter a URL such as example.com.", "warn");
        return;
    }

    const modeEl = document.querySelector(".ws-select");
    const mode   = modeEl ? modeEl.value : "Fast Scrape";
    const config = MODES[mode] || MODES["Fast Scrape"];
    activeMode   = mode;

    // Show terminal, hide prompts & result
    setUIState("running");
    logLine(`[SYSTEM] Mode: ${mode}`, "system");
    logLine(`[SYSTEM] Target: ${target.url}`, "system");
    if (target.task) logLine(`[SYSTEM] Task: ${target.task}`, "system");
    logLine(`[SYSTEM] Contacting BYCONN-X engine...`, "system");

    stopPolling();

    const isAgent = config.kind === "agent";
    const body = isAgent
        ? { url: target.url, task: target.task || "Inspect the page and report what it offers", max_steps: config.steps }
        : { url: target.url, max_depth: config.depth };

    try {
        const res = await fetch(`${API_BASE}/${isAgent ? "act" : "crawl"}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        if (!res.ok) {
            const detail = await res.text();
            logLine(`[ERROR] Engine rejected request (${res.status}): ${detail}`, "error");
            setUIState("idle");
            return;
        }
        const job = await res.json();
        activeJobId = job.job_id;
        logLine(`[SYSTEM] Job ${job.job_id} accepted. Polling for updates...`, "system");
        startPolling(job.job_id, isAgent, target.url, mode);
    } catch (err) {
        logLine(`[ERROR] Could not reach the engine: ${err.message}`, "error");
        setUIState("idle");
    }
}

function clearSearch() {
    const input = document.getElementById("main-search-input");
    if (input) input.value = "";
    stopPolling();
    const logStream = document.getElementById("agent-log-stream");
    const resultArea = document.getElementById("search-result-area");
    const quickPrompts = document.getElementById("quick-prompts");
    if (logStream) logStream.innerHTML = "";
    if (resultArea) resultArea.style.display = "none";
    if (quickPrompts) quickPrompts.style.display = "";
    setUIState("idle");
    renderAnalyticsCards(null);
}

function setUIState(state) {
    const terminal    = document.getElementById("agent-terminal-card");
    const quickPrompts= document.getElementById("quick-prompts");
    const badge       = document.querySelector("#agent-terminal-card .badge");
    const launchBtn   = document.querySelector('button[onclick="startSearch()"]') ||
                        document.getElementById("launch-btn") ||
                        document.querySelector(".btn-primary");

    if (state === "running") {
        if (terminal)     terminal.style.display = "block";
        if (quickPrompts) quickPrompts.style.display = "none";
        if (badge)        { badge.textContent = "Running..."; badge.className = "badge badge-warning"; }
        if (launchBtn)    { launchBtn.textContent = "⏳ Running..."; launchBtn.disabled = true; }
    } else {
        if (badge)     { badge.textContent = "Done"; badge.className = "badge badge-success"; }
        if (launchBtn) { launchBtn.textContent = "🚀 Launch Agent"; launchBtn.disabled = false; }
    }
}

/* =========================================================== POLLING */
function startPolling(jobId, isAgent, url, mode) {
    stopPolling();
    const path = isAgent ? "act" : "crawl";
    pollTimer = setInterval(async () => {
        try {
            const res = await fetch(`${API_BASE}/${path}/${jobId}`);
            if (!res.ok) { logLine(`[WARN] Status check returned ${res.status}`, "warn"); return; }
            const job = await res.json();

            if (isAgent) {
                logLine(
                    `[AGENT] ${job.status} — step ${job.steps_taken ?? 0}` +
                    (job.max_steps ? `/${job.max_steps}` : "") +
                    `, endpoints: ${job.endpoints_discovered || 0}`,
                    job.status === "failed" ? "error" : "system"
                );
            } else {
                logLine(
                    `[CRAWLER] ${job.status} — pages ${job.pages_crawled}, ` +
                    `saved ${job.pages_saved}, vectors ${job.chunks_indexed}, ` +
                    `entities ${job.entities_extracted}, relations ${job.relations_written}`,
                    job.status === "failed" ? "error" : "system"
                );
                updateLiveStats(job);
            }

            (job.errors || []).slice(-3).forEach(msg => logLine(`[WARN] ${msg}`, "warn"));

            if (["succeeded", "failed", "cancelled"].includes(job.status)) {
                stopPolling();
                logLine(`[SYSTEM] Job ${job.job_id} ${job.status}.`, "system");
                renderAnalyticsCards(job);
                renderResult(job, isAgent);
                addToHistory(job, mode || activeMode, isAgent);
                setUIState("idle");
            }
        } catch (err) {
            logLine(`[ERROR] Polling error: ${err.message}`, "error");
            stopPolling();
            setUIState("idle");
        }
    }, 2000);
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

/* ========================================================== RESULTS */
function renderResult(job, isAgent) {
    const area = document.getElementById("search-result-area");
    if (!area) return;

    if (isAgent) {
        area.style.display = "block";
        area.innerHTML = `
            <h3 style="margin-bottom:16px;font-weight:900;font-size:20px;text-transform:uppercase;">
                Agent Result
            </h3>
            <div class="result-cards-grid">
                <div class="result-card" style="border-color:var(--mint);">
                    <div class="result-card-header">
                        <span class="result-card-icon">🤖</span>
                        <span class="result-card-title">${escapeHtml(job.url)}</span>
                    </div>
                    <div class="result-card-body">
                        <div class="result-stat-row"><span>Task</span><b>${escapeHtml(job.task || "—")}</b></div>
                        <div class="result-stat-row"><span>Steps Taken</span><b>${job.steps_taken ?? 0}${job.max_steps ? " / " + job.max_steps : ""}</b></div>
                        <div class="result-stat-row"><span>Task Completed</span><b>${job.succeeded ? "✅ Yes" : "❌ No"}</b></div>
                        <div class="result-stat-row"><span>Endpoints Found</span><b>${job.endpoints_discovered || 0}</b></div>
                        <div class="result-stat-row"><span>Duration</span><b>${job.duration_seconds ?? "—"}s</b></div>
                        <div class="result-stat-row"><span>Job ID</span><b class="font-jet" style="font-size:11px;">${job.job_id}</b></div>
                    </div>
                </div>
            </div>`;
        return;
    }

    if (job.status !== "succeeded" || job.pages_crawled === 0) {
        logLine(`[SYSTEM] No pages were retrieved for ${job.url}.`, "warn");
        return;
    }

    area.style.display = "block";
    area.innerHTML = `
        <h3 style="margin-bottom:16px;font-weight:900;font-size:20px;text-transform:uppercase;">
            Extracted Insights
        </h3>
        <div class="result-cards-grid">
            <div class="result-card" style="border-color:var(--mint);">
                <div class="result-card-header">
                    <span class="result-card-icon">🌐</span>
                    <span class="result-card-title">${escapeHtml(job.url)}</span>
                </div>
                <div class="result-card-body">
                    <div class="result-stat-row">
                        <span>Pages Crawled</span>
                        <b>${job.pages_crawled}</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Entities Extracted</span>
                        <b>${job.entities_extracted}</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Vectors Indexed</span>
                        <b>${job.chunks_indexed}</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Graph Relations</span>
                        <b>${job.relations_written}</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Duration</span>
                        <b>${job.duration_seconds ?? "—"}s</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Mode</span>
                        <b>${escapeHtml(activeMode || "—")}</b>
                    </div>
                    <div class="result-stat-row">
                        <span>Job ID</span>
                        <b class="font-jet" style="font-size:11px;">${job.job_id}</b>
                    </div>
                </div>
            </div>
            <div class="result-card" style="border-color:var(--peach);">
                <div class="result-card-header">
                    <span class="result-card-icon">🧠</span>
                    <span class="result-card-title">AI Intelligence Summary</span>
                </div>
                <div class="result-card-body">
                    <p style="color:var(--text-muted);font-size:13px;line-height:1.7;">
                        Groq <b>Llama-3.3-70B</b> processed ${job.pages_crawled} page(s) from
                        <b>${escapeHtml(job.url)}</b>.<br><br>
                        The engine extracted <b>${job.entities_extracted}</b> structured entities
                        and wrote <b>${job.relations_written}</b> semantic relations into the
                        Knowledge Graph.<br><br>
                        <b>${job.chunks_indexed}</b> text chunks were embedded and indexed
                        for vector similarity search.
                    </p>
                </div>
            </div>
        </div>`;
}

/* ========================================================== HISTORY */
function addToHistory(job, mode, isAgent) {
    jobHistory.unshift({
        url:      job.url,
        mode:     mode || "—",
        isAgent,
        status:   job.status,
        pages:    job.pages_crawled || 0,
        entities: job.entities_extracted || 0,
        duration: job.duration_seconds ?? "—",
        jobId:    job.job_id,
        ts:       new Date().toLocaleTimeString()
    });
    // Keep at most 50
    if (jobHistory.length > 50) jobHistory.pop();
}

function renderHistoryTable() {
    const tbody = document.getElementById("history-table-body");
    if (!tbody) return;

    if (!jobHistory.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-muted" style="text-align:center;padding:24px;">
            No jobs run yet. Launch a search to see history here.</td></tr>`;
        return;
    }

    tbody.innerHTML = jobHistory.map(j => `
        <tr>
            <td><b>${escapeHtml(j.url)}</b><br>
                <span class="text-muted" style="font-size:11px;">${j.jobId}</span>
            </td>
            <td><span class="badge ${j.isAgent ? "badge-accent" : ""}">${escapeHtml(j.mode)}</span></td>
            <td class="text-muted">${j.ts}</td>
            <td><span class="badge ${j.status === "succeeded" ? "badge-success" : "badge-warning"}">${j.status}</span></td>
            <td class="text-muted">${j.pages} pages · ${j.entities} entities · ${j.duration}s</td>
        </tr>`).join("");
}

/* ====================================================== QUICK ACTIONS */
function wireQuickActions() {
    document.querySelectorAll(".api-endpoint-item[data-prompt]").forEach(item => {
        item.style.cursor = "pointer";
        item.addEventListener("click", () => {
            const input = document.getElementById("main-search-input");
            if (input) {
                input.value = item.getAttribute("data-prompt");
                activateTab("new-search");
                input.focus();
            }
        });
    });
}

/* ============================================================= LOGS */
function logLine(message, level = "system") {
    const stream = document.getElementById("agent-log-stream");
    if (!stream) return;
    const line = document.createElement("div");
    line.className = "log-line font-jet";
    line.textContent = "> " + message;
    if (level === "error") line.style.color = "var(--danger)";
    if (level === "warn")  line.style.color = "var(--text-muted)";
    stream.appendChild(line);
    stream.scrollTop = stream.scrollHeight;
    while (stream.children.length > 200) stream.removeChild(stream.firstChild);
}

/* ============================================================ HEALTH */
function badgeClass(status) {
    if (status === "up")       return "badge badge-success";
    if (status === "disabled") return "badge";
    return "badge badge-warning";
}

function renderHealthTable(health) {
    const body = document.getElementById("health-table-body");
    if (!body) return;
    const rows = [].concat(health.components || []).concat([health.llm, health.embeddings].filter(Boolean));
    body.innerHTML = rows.map(c => `
        <tr>
            <td><b>${escapeHtml(c.name)}</b></td>
            <td><span class="${badgeClass(c.status)}">${c.status.toUpperCase()}</span></td>
            <td class="text-muted">${escapeHtml(c.detail || "—")}</td>
        </tr>`).join("");
}

function setEngineStatus(text) {
    const label = document.querySelector(".status-indicator span:last-child b");
    if (label) label.textContent = text;
}

function setEngineDot(color, pulsing) {
    const dot = document.querySelector(".status-indicator .dot");
    if (!dot) return;
    dot.classList.toggle("pulse", pulsing);
    if (color) dot.style.background = color;
}

async function setupHealthIndicator() {
    setEngineStatus("CHECKING...");
    setEngineDot(null, true);
    try {
        const res    = await fetch(API_BASE + "/health");
        const health = await res.json();
        setEngineDot(health.status === "ok" ? "var(--success)" : "var(--warning)", false);
        setEngineStatus(health.status === "ok" ? "ONLINE" : "DEGRADED");
        renderHealthTable(health);
    } catch (err) {
        setEngineDot("var(--danger)", false);
        setEngineStatus("UNREACHABLE");
        const body = document.getElementById("health-table-body");
        if (body) body.innerHTML = `<tr><td colspan="3" class="text-muted">Could not reach ${API_BASE}/health</td></tr>`;
    }
}

function wireHealthRefresh() {
    const btn = document.getElementById("refresh-health-btn");
    if (btn) btn.addEventListener("click", setupHealthIndicator);
}

/* ======================================================= API DISCOVERY */
function escapeHtml(v) {
    return String(v === null || v === undefined ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderApiTable(payload) {
    const body = document.getElementById("api-table-body");
    if (!body) return;
    const rows = (payload && payload.endpoints) || [];
    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="5" class="text-muted" style="text-align:center;padding:24px;">
            No endpoints discovered yet. Run a crawl against a site that calls its own backend.</td></tr>`;
        return;
    }
    body.innerHTML = rows.map(row => `
        <tr>
            <td class="font-jet"><span class="api-method method-${(row.method||"get").toLowerCase()}">${escapeHtml(row.method || "—")}</span></td>
            <td>${escapeHtml(row.path || row.url || "—")}</td>
            <td class="text-muted">${escapeHtml(row.host || "—")}</td>
            <td class="text-muted">${escapeHtml(row.content_type || "—")}</td>
            <td class="text-muted">${row.seen_count !== undefined ? row.seen_count : "—"}</td>
        </tr>`).join("");
}

async function loadDiscoveredApis() {
    const body = document.getElementById("api-table-body");
    if (body) body.innerHTML = `<tr><td colspan="5" class="text-muted">Loading...</td></tr>`;
    try {
        const res = await fetch(`${API_BASE}/apis`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        renderApiTable(await res.json());
    } catch (err) {
        if (body) body.innerHTML = `<tr><td colspan="5" class="text-muted">
            Could not load endpoints (${escapeHtml(err.message)}). Run a crawl first.</td></tr>`;
    }
}

function wireApiDiscovery() {
    const btn = document.getElementById("refresh-apis-btn");
    if (btn) btn.addEventListener("click", loadDiscoveredApis);
    const navItem = document.querySelector('.nav-item[data-tab="api-discovery"]');
    if (navItem) navItem.addEventListener("click", loadDiscoveredApis);
}

/* ==================================================== URL PARSER */
function parseTarget(text) {
    const raw = (text || "").trim();
    if (!raw) return { url: null, task: "" };
    const explicit = raw.match(/https?:\/\/[^\s"'<>]+/i);
    if (explicit) {
        const url  = explicit[0].replace(/[.,;:)\]]+$/, "");
        const task = raw.replace(explicit[0], " ").replace(/\s+/g, " ").trim();
        return { url, task };
    }
    const bare = raw.match(
        /(?:^|\s)(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:\/[^\s"'<>]*)?/i
    );
    if (bare) {
        const host = bare[0].trim();
        const task = raw.replace(bare[0], " ").replace(/\s+/g, " ").trim();
        const hostname = host.split(":")[0].split("/")[0].toLowerCase();
        const local = hostname === "localhost" || /^\d{1,3}(\.\d{1,3}){3}$/.test(hostname);
        return { url: `${local ? "http" : "https"}://${host}`, task };
    }
    return { url: null, task: "" };
}

/* ==================================================== TOAST */
function showToast(message, level = "info") {
    logLine(message, level);
}

/* ================================================ GLOBAL EXPORTS */
window.startSearch  = startSearch;
window.activateTab  = activateTab;
window.clearSearch  = clearSearch;
