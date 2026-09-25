/**
 * BYCONN-X dashboard controller.
 *
 * Owns sidebar tab navigation for the four panels in index.html
 * (new-search, history, saved, api-keys) and wires the search box to the
 * FastAPI backend in server.py.
 *
 * Tab switching is handled with a single delegated listener on .nav-links so
 * the handler is attached exactly once, no matter how often this file loads.
 */

const API_BASE = "/api/v1";

const TAB_META = {
    "new-search": {
        title: "New Search",
        subtitle: "Command the AI Agent to fetch and analyze data."
    },
    history: {
        title: "Search History",
        subtitle: "Review your past queries and crawler executions."
    },
    saved: {
        title: "Saved Insights",
        subtitle: "Manage exported data and saved research."
    },
    "api-keys": {
        title: "API Keys",
        subtitle: "Inspect the engine health and service credentials."
    }
};

let activeJobId = null;
let pollTimer = null;

// The Agent Mode selector drives how far the crawl follows links.
const MODE_DEPTH = {
    "Deep Research": 2,
    "Fast Scrape": 1,
    "Visual Action": 0
};

let initialized = false;

document.addEventListener("DOMContentLoaded", () => {
    // Guarded: a repeated DOMContentLoaded (bfcache restore, a test harness,
    // a soft navigation) must not rebind handlers or re-fetch /health.
    if (initialized) return;
    initialized = true;
    setupTabNavigation();
    setupSearch();
    wireHealthRefresh();
    setupHealthIndicator();
});

/* ------------------------------------------------------------------ tabs */
function setupTabNavigation() {
    const nav = document.querySelector(".nav-links");
    if (!nav) return;

    nav.addEventListener("click", (event) => {
        const item = event.target.closest(".nav-item");
        if (!item) return;
        event.preventDefault();
        activateTab(item.getAttribute("data-tab"));
    });
}

function activateTab(tabId) {
    if (!tabId || !(tabId in TAB_META)) return;

    document.querySelectorAll(".nav-item").forEach((node) => {
        node.classList.toggle("active", node.getAttribute("data-tab") === tabId);
    });

    // Hide every panel, then reveal the requested one. A nav entry without a
    // matching panel is reported rather than throwing on a null reference.
    let panelShown = false;
    document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.remove("active");
    });

    const panel = document.getElementById(`panel-${tabId}`);
    if (panel) {
        panel.classList.add("active");
        panelShown = true;
    } else {
        showTransientNotice(`Panel "panel-${tabId}" is not implemented yet.`);
    }

    const meta = TAB_META[tabId];
    const title = document.getElementById("page-title");
    const subtitle = document.getElementById("page-subtitle");
    if (title) title.textContent = meta.title;
    if (subtitle) subtitle.textContent = meta.subtitle;

    return panelShown;
}

/* ---------------------------------------------------------------- search */
function setupSearch() {
    const button = document.querySelector('button[onclick="startSearch()"]');
    if (button) {
        // Replace the inline handler so this file is the single source of truth.
        button.removeAttribute("onclick");
        button.addEventListener("click", startSearch);
    }
    const clear = document.querySelector(".btn-secondary");
    if (clear && clear.textContent.trim().toLowerCase() === "clear") {
        clear.addEventListener("click", clearSearch);
    }
}

async function startSearch() {
    const input = document.getElementById("main-search-input");
    if (!input) return;
    const query = input.value.trim();
    if (!query) return;

    const url = extractUrl(query);
    if (!url) {
        logLine("Provide a target URL, for example https://example.com", "warn");
        return;
    }

    // Honour the Agent Mode selector instead of ignoring it.
    const modeEl = document.querySelector(".ws-select");
    const mode = modeEl ? modeEl.value : "Fast Scrape";
    const maxDepth = MODE_DEPTH[mode] !== undefined ? MODE_DEPTH[mode] : 1;

    const terminal = document.getElementById("agent-terminal-card");
    const logStream = document.getElementById("agent-log-stream");
    const resultArea = document.getElementById("search-result-area");
    const quickPrompts = document.getElementById("quick-prompts");

    if (terminal) terminal.style.display = "block";
    if (quickPrompts) quickPrompts.style.display = "none";
    if (resultArea) resultArea.style.display = "none";
    if (logStream) logStream.innerHTML = "";

    logLine(`[SYSTEM] Agent mode: ${mode} (max_depth=${maxDepth})`, "system");
    logLine(`[SYSTEM] Submitting crawl request for ${url}`, "system");
    stopPolling();

    try {
        const response = await fetch(`${API_BASE}/crawl`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url, max_depth: maxDepth })
        });
        if (!response.ok) {
            const detail = await response.text();
            logLine(`[ERROR] Crawl rejected (${response.status}): ${detail}`, "error");
            return;
        }
        const job = await response.json();
        activeJobId = job.job_id;
        logLine(`[SYSTEM] Job ${job.job_id} accepted`, "system");
        startPolling(job.job_id);
    } catch (error) {
        logLine(`[ERROR] Could not reach the engine: ${error.message}`, "error");
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
}

function extractUrl(text) {
    const match = text.match(/https?:\/\/[^\s"'<>]+/i);
    return match ? match[0] : null;
}

function startPolling(jobId) {
    stopPolling();
    pollTimer = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/crawl/${jobId}`);
            if (!response.ok) {
                logLine(`[WARN] Status check failed (${response.status})`, "warn");
                return;
            }
            const job = await response.json();
            logLine(
                `[CRAWLER] ${job.status} - pages ${job.pages_crawled}, ` +
                `saved ${job.pages_saved}, vectors ${job.chunks_indexed}, ` +
                `entities ${job.entities_extracted}, relations ${job.relations_written}`,
                job.status === "failed" ? "error" : "system"
            );
            (job.errors || []).forEach((message) => logLine(`[WARN] ${message}`, "warn"));

            if (["succeeded", "failed", "cancelled"].includes(job.status)) {
                stopPolling();
                logLine(`[SYSTEM] Job ${job.job_id} ${job.status}`, "system");
                renderResult(job);
            }
        } catch (error) {
            logLine(`[ERROR] Polling failed: ${error.message}`, "error");
            stopPolling();
        }
    }, 2000);
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function renderResult(job) {
    const resultArea = document.getElementById("search-result-area");
    if (!resultArea) return;
    if (job.status !== "succeeded" || job.pages_crawled === 0) {
        logLine(`[SYSTEM] No pages were retrieved for ${job.url}`, "warn");
        return;
    }
    resultArea.style.display = "block";
    const title = resultArea.querySelector(".res-title");
    if (title) title.textContent = `Result: ${job.url}`;
    const snippet = resultArea.querySelector(".res-snippet");
    if (snippet) {
        snippet.innerHTML =
            `Pages crawled: <b>${job.pages_crawled}</b><br><br>` +
            `Saved to PostgreSQL: <b>${job.pages_saved}</b><br><br>` +
            `Vectors indexed: <b>${job.chunks_indexed}</b><br><br>` +
            `Entities extracted: <b>${job.entities_extracted}</b><br><br>` +
            `Relations written to Neo4j: <b>${job.relations_written}</b><br><br>` +
            `- Job: ${job.job_id}`;
    }
    const meta = resultArea.querySelector(".res-meta");
    if (meta) {
        meta.textContent =
            `Duration: ${job.duration_seconds ?? "n/a"}s | ` +
            `Depth: ${job.max_depth} | Status: ${job.status}`;
    }
}

/* ----------------------------------------------------------------- logs */
function logLine(message, level = "system") {
    const stream = document.getElementById("agent-log-stream");
    if (!stream) return;
    const line = document.createElement("div");
    // Only stylesheet classes that actually exist are used; severity is colour.
    line.className = "log-line font-jet";
    line.textContent = "> " + message;
    if (level === "error") line.style.color = "var(--danger)";
    if (level === "warn") line.style.color = "var(--text-muted)";
    stream.appendChild(line);
    stream.scrollTop = stream.scrollHeight;
    while (stream.children.length > 100) {
        stream.removeChild(stream.firstChild);
    }
}

function showTransientNotice(message) {
    logLine(message, "warn");
}

/* --------------------------------------------------------------- health */
function badgeClass(status) {
    if (status === "up") return "badge badge-success";
    if (status === "disabled") return "badge";
    return "badge badge-warning";
}

function renderHealthTable(health) {
    const body = document.getElementById("health-table-body");
    if (!body) return;
    const rows = []
        .concat(health.components || [])
        .concat([health.llm, health.embeddings].filter(Boolean));
    body.innerHTML = "";
    rows.forEach((component) => {
        const tr = document.createElement("tr");
        const name = document.createElement("td");
        name.textContent = component.name;
        const status = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = badgeClass(component.status);
        badge.textContent = component.status;
        status.appendChild(badge);
        const detail = document.createElement("td");
        detail.className = "text-muted";
        detail.textContent = component.detail || "-";
        tr.appendChild(name);
        tr.appendChild(status);
        tr.appendChild(detail);
        body.appendChild(tr);
    });
}

async function setupHealthIndicator() {
    const indicator = document.querySelector(".status-indicator");
    try {
        const response = await fetch(API_BASE + "/health");
        const health = await response.json();

        if (indicator) {
            const dot = indicator.querySelector(".dot");
            const label = indicator.querySelector("span:last-child");
            if (dot) {
                dot.classList.remove("pulse");
                dot.style.background = health.status === "ok"
                    ? "var(--success)"
                    : "var(--warning)";
            }
            if (label) {
                const states = (health.components || [])
                    .map((c) => c.name + ":" + c.status)
                    .join("  ");
                label.textContent = "ENGINE: " +
                    String(health.status).toUpperCase() + "  |  " + states;
            }
        }
        renderHealthTable(health);
    } catch (error) {
        if (indicator) {
            const label = indicator.querySelector("span:last-child");
            if (label) label.textContent = "ENGINE: UNREACHABLE";
        }
        const body = document.getElementById("health-table-body");
        if (body) {
            body.innerHTML = '<tr><td colspan="3" class="text-muted">' +
                "Could not reach " + API_BASE + "/health</td></tr>";
        }
    }
}

function wireHealthRefresh() {
    const button = document.getElementById("refresh-health-btn");
    if (button) button.addEventListener("click", setupHealthIndicator);
}

// Exposed for the inline button handler and for manual console use.
window.startSearch = startSearch;
window.activateTab = activateTab;
