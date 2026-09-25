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
    },
    "api-discovery": {
        title: "API Discovery",
        subtitle: "REST endpoints sniffed from previous crawls."
    }
};

let activeJobId = null;
let pollTimer = null;
let activeMode = null;

// The Agent Mode selector picks which backend runs the request. Crawl modes
// post to /crawl; Visual Action drives the browser agent through /act, which is
// a genuinely different pipeline rather than a shallower crawl.
const MODES = {
    "Deep Research": { kind: "crawl", depth: 2 },
    "Fast Scrape": { kind: "crawl", depth: 1 },
    "Visual Action": { kind: "agent", steps: 3 }
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
    wireApiDiscovery();
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

    const target = parseTarget(query);
    if (!target.url) {
        logLine(
            "Could not find a target. Enter a URL such as example.com, " +
            "optionally followed by what you want done with it.",
            "warn"
        );
        return;
    }

    // Honour the Agent Mode selector instead of ignoring it.
    const modeEl = document.querySelector(".ws-select");
    const mode = modeEl ? modeEl.value : "Fast Scrape";
    const config = MODES[mode] || MODES["Fast Scrape"];
    activeMode = mode;

    const terminal = document.getElementById("agent-terminal-card");
    const logStream = document.getElementById("agent-log-stream");
    const resultArea = document.getElementById("search-result-area");
    const quickPrompts = document.getElementById("quick-prompts");

    if (terminal) terminal.style.display = "block";
    if (quickPrompts) quickPrompts.style.display = "none";
    if (resultArea) resultArea.style.display = "none";
    if (logStream) logStream.innerHTML = "";

    if (config.kind === "agent") {
        logLine(`[SYSTEM] Agent mode: ${mode} (max_steps=${config.steps})`, "system");
    } else {
        logLine(`[SYSTEM] Crawl mode: ${mode} (max_depth=${config.depth})`, "system");
    }
    logLine(`[SYSTEM] Target: ${target.url}`, "system");
    if (target.task) {
        logLine(`[SYSTEM] Task: ${target.task}`, "system");
    }
    stopPolling();

    const isAgent = config.kind === "agent";
    // The agent endpoint requires a task, so fall back to a neutral default
    // when the operator only supplied a URL.
    const body = isAgent
        ? {
            url: target.url,
            task: target.task || "Inspect the page and report what it offers",
            max_steps: config.steps
        }
        : { url: target.url, max_depth: config.depth };

    try {
        const response = await fetch(`${API_BASE}/${isAgent ? "act" : "crawl"}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        if (!response.ok) {
            const detail = await response.text();
            logLine(`[ERROR] Request rejected (${response.status}): ${detail}`, "error");
            return;
        }
        const job = await response.json();
        activeJobId = job.job_id;
        logLine(`[SYSTEM] Job ${job.job_id} accepted`, "system");
        startPolling(job.job_id, isAgent);
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

/**
 * Splits free text into a target URL and an optional task.
 *
 * Accepts a bare domain ("example.com"), a full URL, or an instruction that
 * embeds one ("extract pricing from https://stripe.com/pricing"). The scheme
 * is defaulted to https when omitted, so a plain domain is not silently
 * rejected. The surrounding prose becomes the task, so the placeholder text in
 * the search box is not thrown away.
 */
function parseTarget(text) {
    const raw = (text || "").trim();
    if (!raw) return { url: null, task: "" };

    // Prefer an explicit http(s) URL anywhere in the text.
    const explicit = raw.match(/https?:\/\/[^\s"'<>]+/i);
    if (explicit) {
        const url = explicit[0].replace(/[.,;:)\]]+$/, "");
        const task = (raw.replace(explicit[0], " ")).replace(/\s+/g, " ").trim();
        return { url, task };
    }

    // Otherwise accept a bare host: a dotted domain, localhost, or an IPv4
    // literal, each with an optional port and path. A dotted domain must end in
    // a real TLD so prose containing a version number ("version 1.2") or a file
    // name is not mistaken for a host.
    const bare = raw.match(
        /(?:^|\s)(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:\/[^\s"'<>]*)?/i
    );
    if (bare) {
        const host = bare[0].trim();
        const task = (raw.replace(bare[0], " ")).replace(/\s+/g, " ").trim();
        // A bare host is ambiguous. Public sites are reached over https;
        // localhost and raw IP literals are conventionally plain http. Compare
        // the hostname only, so a port or path does not defeat the test.
        const hostname = host.split(":")[0].split("/")[0].toLowerCase();
        const local = hostname === "localhost" || /^\d{1,3}(\.\d{1,3}){3}$/.test(hostname);
        return { url: `${local ? "http" : "https"}://${host}`, task };
    }

    return { url: null, task: "" };
}

function startPolling(jobId, isAgent) {
    stopPolling();
    const path = isAgent ? "act" : "crawl";
    pollTimer = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/${path}/${jobId}`);
            if (!response.ok) {
                logLine(`[WARN] Status check failed (${response.status})`, "warn");
                return;
            }
            const job = await response.json();
            if (isAgent) {
                logLine(
                    `[AGENT] ${job.status} - step ${job.steps_taken}` +
                    `${job.max_steps !== undefined ? "/" + job.max_steps : ""}` +
                    `, endpoints discovered ${job.endpoints_discovered || 0}`,
                    job.status === "failed" ? "error" : "system"
                );
            } else {
                logLine(
                    `[CRAWLER] ${job.status} - pages ${job.pages_crawled}, ` +
                    `saved ${job.pages_saved}, vectors ${job.chunks_indexed}, ` +
                    `entities ${job.entities_extracted}, relations ${job.relations_written}`,
                    job.status === "failed" ? "error" : "system"
                );
            }
            (job.errors || []).forEach((message) => logLine(`[WARN] ${message}`, "warn"));

            if (["succeeded", "failed", "cancelled"].includes(job.status)) {
                stopPolling();
                const outcome = isAgent && job.succeeded === false && job.status === "succeeded"
                    ? "finished without completing the task"
                    : job.status;
                logLine(`[SYSTEM] Job ${job.job_id} ${outcome}`, "system");
                renderResult(job, isAgent);
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

function renderResult(job, isAgent) {
    const resultArea = document.getElementById("search-result-area");
    if (!resultArea) return;

    if (isAgent) {
        resultArea.style.display = "block";
        const title = resultArea.querySelector(".res-title");
        if (title) title.textContent = `Agent run: ${job.url}`;
        const snippet = resultArea.querySelector(".res-snippet");
        if (snippet) {
            snippet.innerHTML =
                `Task: <b>${escapeHtml(job.task || "-")}</b><br><br>` +
                `Steps taken: <b>${job.steps_taken ?? 0}</b><br><br>` +
                `Task completed: <b>${job.succeeded ? "yes" : "no"}</b><br><br>` +
                `API endpoints discovered: <b>${job.endpoints_discovered || 0}</b><br><br>` +
                `- Job: ${job.job_id}`;
        }
        const meta = resultArea.querySelector(".res-meta");
        if (meta) {
            meta.textContent =
                `Duration: ${job.duration_seconds ?? "n/a"}s | ` +
                `Mode: ${activeMode || "Visual Action"} | Status: ${job.status}`;
        }
        return;
    }

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

/**
 * Writes the status text into the indicator, preserving the <b> emphasis the
 * stylesheet expects. Assigning to label.textContent would delete the element
 * and drop the bold styling.
 */
function setEngineStatus(text) {
    const indicator = document.querySelector(".status-indicator");
    if (!indicator) return;
    const label = indicator.querySelector("span:last-child");
    if (!label) return;
    const strong = label.querySelector("b");
    if (strong) {
        strong.textContent = text;
    } else {
        label.textContent = text;
    }
}

function setEngineDot(color, pulsing) {
    const indicator = document.querySelector(".status-indicator");
    if (!indicator) return;
    const dot = indicator.querySelector(".dot");
    if (!dot) return;
    if (pulsing) {
        dot.classList.add("pulse");
    } else {
        dot.classList.remove("pulse");
    }
    if (color) dot.style.background = color;
}

async function setupHealthIndicator() {
    // Say so before asking. The probe can take a couple of seconds when a
    // dependency is slow to answer, and claiming "ONLINE" in the meantime is a
    // lie the operator would record.
    setEngineStatus("CHECKING...");
    setEngineDot(null, true);
    try {
        const response = await fetch(API_BASE + "/health");
        const health = await response.json();

        setEngineDot(
            health.status === "ok" ? "var(--success)" : "var(--warning)",
            false
        );
        const states = (health.components || [])
            .map((c) => c.name + ":" + c.status)
            .join("  ");
        setEngineStatus(
            String(health.status).toUpperCase() + (states ? "  |  " + states : "")
        );
        renderHealthTable(health);
    } catch (error) {
        setEngineDot("var(--danger)", false);
        setEngineStatus("UNREACHABLE");
        const body = document.getElementById("health-table-body");
        if (body) {
            body.innerHTML = '<tr><td colspan="3" class="text-muted">' +
                "Could not reach " + API_BASE + "/health</td></tr>";
        }
    }
}

/* ------------------------------------------------------- api discovery */
function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function renderApiTable(payload) {
    const body = document.getElementById("api-table-body");
    if (!body) return;
    const rows = (payload && payload.endpoints) || [];
    body.innerHTML = "";
    if (!rows.length) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 5;
        td.className = "text-muted";
        // An empty list is the normal case without PostgreSQL, and also before
        // the first crawl, so it must not look like an error.
        td.textContent =
            "No endpoints discovered yet. Enable API Intelligence and run a crawl " +
            "against a site that calls its own backend.";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }
    rows.forEach((row) => {
        const tr = document.createElement("tr");
        const cells = [
            row.method || "-",
            row.path || row.url || "-",
            row.host || "-",
            row.content_type || "-",
            row.seen_count === undefined ? "-" : String(row.seen_count)
        ];
        cells.forEach((value, index) => {
            const td = document.createElement("td");
            if (index === 0) td.className = "font-jet";
            // textContent, never innerHTML: these are sniffed remote values.
            td.textContent = value;
            tr.appendChild(td);
        });
        body.appendChild(tr);
    });
}

async function loadDiscoveredApis() {
    const body = document.getElementById("api-table-body");
    if (body && !body.querySelector("tr")) {
        body.innerHTML =
            '<tr><td colspan="5" class="text-muted">Loading discovered endpoints...</td></tr>';
    }
    try {
        const response = await fetch(`${API_BASE}/apis`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        renderApiTable(await response.json());
    } catch (error) {
        if (body) {
            body.innerHTML = "";
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 5;
            td.className = "text-muted";
            td.textContent =
                "Could not load discovered endpoints (" + error.message +
                "). The API registry lives in PostgreSQL.";
            tr.appendChild(td);
            body.appendChild(tr);
        }
    }
}

function wireApiDiscovery() {
    const refresh = document.getElementById("refresh-apis-btn");
    if (refresh) refresh.addEventListener("click", loadDiscoveredApis);

    // Load the first time the tab is opened rather than on every page load, so
    // the initial console render stays cheap.
    const navItem = document.querySelector('.nav-item[data-tab="api-discovery"]');
    if (navItem) {
        navItem.addEventListener("click", () => {
            loadDiscoveredApis();
        }, { once: false });
    }
}

function wireHealthRefresh() {
    const button = document.getElementById("refresh-health-btn");
    if (button) button.addEventListener("click", setupHealthIndicator);
}

// Exposed for the inline button handler and for manual console use.
window.startSearch = startSearch;
window.activateTab = activateTab;
