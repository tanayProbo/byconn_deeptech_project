/**
 * BYCONN-X operator console.
 *
 * Everything on screen comes from the API: jobs stream live over server-sent
 * events (with a polling fallback), and results, graph and exports are read
 * from /api/v1/crawl/{id}/results. Untrusted text (page content, model output)
 * is only ever inserted with textContent, never as HTML.
 */

const API_BASE = "/api/v1";

const TAB_META = {
    "new-search": { title: "New Search", subtitle: "Crawl a site and extract exactly the data you describe." },
    history: { title: "Job History", subtitle: "Every crawl and agent task this server has run since it started." },
    health: { title: "Service Health", subtitle: "Live status of the model and the datastores." },
    "api-discovery": { title: "API Discovery", subtitle: "Backend endpoints the crawled pages called." }
};

const MODES = {
    "Fast Scrape":   { kind: "crawl", depth: 0 },
    "Deep Research": { kind: "crawl", depth: 1 },
    "Visual Action": { kind: "agent", steps: 8 }
};

const SCHEMA_PRESETS = {
    products: {
        type: "object",
        properties: {
            products: {
                type: "array",
                items: {
                    type: "object",
                    properties: {
                        name: { type: "string" },
                        price: { type: "string" },
                        availability: { type: "string" }
                    },
                    required: ["name"]
                }
            }
        }
    },
    pricing: {
        type: "object",
        properties: {
            plans: {
                type: "array",
                items: {
                    type: "object",
                    properties: {
                        name: { type: "string" },
                        price: { type: "string" },
                        billing_period: { type: "string" },
                        features: { type: "array", items: { type: "string" } }
                    },
                    required: ["name"]
                }
            }
        }
    },
    articles: {
        type: "object",
        properties: {
            articles: {
                type: "array",
                items: {
                    type: "object",
                    properties: {
                        title: { type: "string" },
                        author: { type: "string" },
                        published: { type: "string" },
                        url: { type: "string" }
                    },
                    required: ["title"]
                }
            }
        }
    }
};

const TERMINAL = ["succeeded", "failed", "cancelled"];
const TYPE_COLORS = {
    ORGANIZATION: "var(--mint)", PERSON: "var(--peach)", PRODUCT: "var(--yellow)",
    TECHNOLOGY: "var(--lavender)", LOCATION: "var(--cyan)", EVENT: "var(--pink)",
    CONCEPT: "#E5E7EB", ENTITY: "#FFFFFF"
};

let tracker = null;          // the job currently being followed live
let citationOrigin = null;   // marker that opened the citation popover

/* ============================================================== DOM utils */
function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

function svgEl(tag, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    return node;
}

function safeHref(url) {
    return /^https?:\/\//i.test(String(url || "")) ? String(url) : null;
}

function linkTo(url, label) {
    const href = safeHref(url);
    const text = label || url || "—";
    return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "link", text }) : el("span", { text });
}

function badge(text, tone) {
    return el("span", { class: `badge ${tone ? "badge-" + tone : ""}`.trim(), text });
}

function statusTone(status) {
    if (status === "succeeded") return "success";
    if (status === "failed" || status === "cancelled") return "danger";
    return "warning";
}

function emptyState(message) {
    return el("p", { class: "empty-state", text: message });
}

/* ================================================================== INIT */
document.addEventListener("DOMContentLoaded", () => {
    setupTabNavigation();
    setupSearch();
    setupSchemaEditor();
    setupResultTabs();
    setupCitationPopover();
    document.getElementById("refresh-history-btn").addEventListener("click", loadHistory);
    document.getElementById("refresh-health-btn").addEventListener("click", setupHealthIndicator);
    document.getElementById("refresh-apis-btn").addEventListener("click", loadDiscoveredApis);
    setupHealthIndicator();
});

/* ================================================================== TABS */
function setupTabNavigation() {
    document.querySelectorAll(".nav-item[data-tab]").forEach(item => {
        item.addEventListener("click", () => activateTab(item.getAttribute("data-tab")));
    });
}

function activateTab(tabId) {
    if (!tabId || !(tabId in TAB_META)) return;
    document.querySelectorAll(".nav-item[data-tab]").forEach(item => {
        const active = item.getAttribute("data-tab") === tabId;
        item.classList.toggle("active", active);
        if (active) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
    });
    document.querySelectorAll(".tab-panel").forEach(panel => {
        panel.classList.toggle("active", panel.id === `panel-${tabId}`);
    });
    document.getElementById("page-title").textContent = TAB_META[tabId].title;
    document.getElementById("page-subtitle").textContent = TAB_META[tabId].subtitle;

    if (tabId === "history") loadHistory();
    if (tabId === "health") setupHealthIndicator();
    if (tabId === "api-discovery") loadDiscoveredApis();
}

/* ================================================================ SEARCH */
function setupSearch() {
    document.getElementById("launch-btn").addEventListener("click", startSearch);
    document.getElementById("clear-btn").addEventListener("click", clearSearch);
    document.getElementById("main-search-input").addEventListener("keydown", event => {
        if (event.key === "Enter") startSearch();
    });
}

function readSchema() {
    const raw = document.getElementById("schema-input").value.trim();
    if (!raw) return { schema: null };
    try {
        const schema = JSON.parse(raw);
        if (!schema || typeof schema !== "object" || Array.isArray(schema) || schema.type !== "object") {
            return { error: 'The schema must be a JSON object with "type": "object".' };
        }
        return { schema };
    } catch (err) {
        return { error: `The schema is not valid JSON: ${err.message}` };
    }
}

function setupSchemaEditor() {
    const input = document.getElementById("schema-input");
    const status = document.getElementById("schema-status");
    const validate = () => {
        const { schema, error } = readSchema();
        status.textContent = error || (schema ? "Schema looks valid." : "");
        status.classList.toggle("is-error", Boolean(error));
        input.setAttribute("aria-invalid", error ? "true" : "false");
    };
    input.addEventListener("input", validate);
    document.querySelectorAll("[data-preset]").forEach(button => {
        button.addEventListener("click", () => {
            input.value = JSON.stringify(SCHEMA_PRESETS[button.getAttribute("data-preset")], null, 2);
            validate();
        });
    });
}

async function startSearch() {
    const input = document.getElementById("main-search-input");
    const target = parseTarget(input.value);
    if (!target.url) {
        showLive("Nothing to fetch");
        logLine("Enter a URL or a domain such as example.com.", "warn");
        return;
    }
    const mode = document.getElementById("mode-select").value;
    const config = MODES[mode] || MODES["Fast Scrape"];
    const isAgent = config.kind === "agent";

    let body;
    if (isAgent) {
        body = { url: target.url, task: target.task || "Inspect the page and report what it offers", max_steps: config.steps };
    } else {
        const { schema, error } = readSchema();
        if (error) {
            document.getElementById("schema-details").open = true;
            document.getElementById("schema-input").focus();
            return;
        }
        body = { url: target.url, max_depth: config.depth, prompt: target.task };
        if (schema) body.schema = schema;
    }

    hideResults();
    showLive(`${mode}: ${target.url}`);
    if (target.task) logLine(`Instruction: ${target.task}`);
    setLaunching(true);
    try {
        const res = await fetch(`${API_BASE}/${isAgent ? "act" : "crawl"}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        if (!res.ok) {
            logLine(`The engine rejected the request (${res.status}): ${await errorDetail(res)}`, "error");
            setLiveStatus("rejected", "danger");
            return;
        }
        const job = await res.json();
        logLine(`Job ${job.job_id} accepted.`);
        trackJob(job.job_id, isAgent);
    } catch (err) {
        logLine(`Could not reach the engine: ${err.message}`, "error");
        setLiveStatus("unreachable", "danger");
    } finally {
        setLaunching(false);
    }
}

async function errorDetail(res) {
    try {
        const body = await res.json();
        if (Array.isArray(body.detail)) return body.detail.map(d => d.msg).join("; ");
        return body.detail || JSON.stringify(body);
    } catch (_) {
        return res.statusText;
    }
}

function setLaunching(busy) {
    const button = document.getElementById("launch-btn");
    button.disabled = busy;
    button.textContent = busy ? "Starting…" : "Launch";
}

function clearSearch() {
    stopTracking();
    document.getElementById("main-search-input").value = "";
    document.getElementById("live-card").hidden = true;
    hideResults();
}

/* ======================================================= LIVE JOB CARD */
function showLive(title) {
    document.getElementById("live-card").hidden = false;
    document.getElementById("live-title").textContent = title;
    document.getElementById("agent-log-stream").replaceChildren();
    document.getElementById("live-counters").replaceChildren();
    setLiveStatus("queued", "warning");
}

function setLiveStatus(text, tone) {
    const node = document.getElementById("live-status");
    node.textContent = text;
    node.className = `badge badge-${tone}`;
}

function logLine(message, level) {
    const stream = document.getElementById("agent-log-stream");
    const time = new Date().toLocaleTimeString();
    stream.append(el("div", { class: `log-line log-${level || "info"}` },
        el("span", { class: "log-time", text: time }), " ", message));
    while (stream.children.length > 300) stream.firstChild.remove();
    stream.scrollTop = stream.scrollHeight;
}

function renderCounters(job) {
    const pairs = job.kind === "agent"
        ? [["Steps", `${job.steps_taken || 0} / ${job.max_steps}`], ["Endpoints", job.endpoints_discovered || 0]]
        : [["Pages", job.pages_crawled], ["Saved", job.pages_saved], ["Vectors", job.chunks_indexed],
           ["Entities", job.entities_extracted], ["Relations", job.relations_written],
           ["Verified fields", job.fields_verified], ["Unverified", job.fields_unverified]];
    document.getElementById("live-counters").replaceChildren(...pairs.map(([label, value]) =>
        el("div", { class: "counter" }, el("dt", { text: label }), el("dd", { text: value ?? 0 }))));
}

/* ========================================================== JOB TRACKING */
function trackJob(jobId, isAgent) {
    stopTracking();
    tracker = { jobId, isAgent, source: null, timer: null, backoff: 1000, lastSignature: "", seenErrors: new Set(), errorLines: new Map(), done: false };
    const path = isAgent ? "act" : "crawl";
    if (!("EventSource" in window)) {
        schedulePoll(0);
        return;
    }
    const source = new EventSource(`${API_BASE}/${path}/${jobId}/events`);
    tracker.source = source;
    const parse = event => JSON.parse(event.data);
    source.addEventListener("snapshot", event => onJobUpdate(parse(event)));
    source.addEventListener("status", event => onJobUpdate(parse(event)));
    source.addEventListener("page", event => {
        const { page, job } = parse(event);
        logLine(`Page done: ${page.url} (${page.status_code ?? "?"}) · ${page.entities} entities · ` +
                `${page.fields_verified} verified / ${page.fields_unverified} unverified fields`);
        onJobUpdate(job);
    });
    source.addEventListener("step", event => {
        const step = parse(event);
        const action = step.action || {};
        logLine(`Step ${step.step}: ${action.type}${action.reason ? " — " + action.reason : ""}${action.error ? " (" + action.error + ")" : ""}`);
    });
    // Named job_error, not error: an SSE event called "error" is also
    // delivered to onerror and would be mistaken for a dropped connection.
    source.addEventListener("job_error", event => logError(parse(event).message));
    source.addEventListener("done", event => finishJob(parse(event)));
    source.onerror = () => {
        if (!tracker || tracker.done || tracker.source !== source) return;
        source.close();
        tracker.source = null;
        logLine("Live stream interrupted; following the job by polling instead.", "warn");
        schedulePoll(tracker.backoff);
    };
}

function schedulePoll(delay) {
    if (!tracker) return;
    const current = tracker;
    const path = current.isAgent ? "act" : "crawl";
    current.timer = setTimeout(async () => {
        if (tracker !== current || current.done) return;
        try {
            const res = await fetch(`${API_BASE}/${path}/${current.jobId}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const job = await res.json();
            current.backoff = 1000;
            onJobUpdate(job);
            (job.errors || []).forEach(logError);
            if (TERMINAL.includes(job.status)) finishJob(job);
            else schedulePoll(1000);
        } catch (err) {
            current.backoff = Math.min(current.backoff * 2, 15000);
            logLine(`Engine not reachable (${err.message}); retrying in ${current.backoff / 1000}s.`, "warn");
            schedulePoll(current.backoff);
        }
    }, delay);
}

function stopTracking() {
    if (!tracker) return;
    tracker.done = true;
    if (tracker.source) tracker.source.close();
    if (tracker.timer) clearTimeout(tracker.timer);
    tracker = null;
}

function onJobUpdate(job) {
    if (!tracker || !job) return;
    setLiveStatus(job.status, statusTone(job.status));
    renderCounters(job);
    const signature = job.kind === "agent"
        ? `${job.status}|${job.steps_taken}`
        : `${job.status}|${job.pages_crawled}|${job.entities_extracted}|${job.fields_verified}`;
    if (signature !== tracker.lastSignature) {
        tracker.lastSignature = signature;
        if (job.kind !== "agent") {
            logLine(`${job.status}: ${job.pages_crawled} pages, ${job.chunks_indexed} vectors, ` +
                    `${job.entities_extracted} entities, ${job.relations_written} relations`);
        }
    }
}

function logError(message) {
    if (!tracker || !message || tracker.seenErrors.has(message)) return;
    tracker.seenErrors.add(message);
    // "<url>: postgres save failed (...)" repeats for every page while a store
    // is down; show it once with a count instead of flooding the log.
    const kind = message.replace(/^\S+: /, "").replace(/\(.*$/s, "").trim();
    const existing = tracker.errorLines.get(kind);
    if (existing && existing.isConnected) {
        existing.count += 1;
        existing.line.lastChild.textContent = ` ×${existing.count}`;
        return;
    }
    logLine(message, "error");
    const line = document.getElementById("agent-log-stream").lastChild;
    line.append(el("span", { class: "log-count" }));
    tracker.errorLines.set(kind, { line, count: 1, get isConnected() { return line.isConnected; } });
}

function finishJob(job) {
    if (!tracker || tracker.done) return;
    onJobUpdate(job);
    (job.errors || []).forEach(logError);
    const isAgent = tracker.isAgent;
    logLine(`Job ${job.status}${job.duration_seconds != null ? ` in ${job.duration_seconds}s` : ""}.`);
    stopTracking();
    if (isAgent) renderAgentResult(job);
    else loadResults(job.job_id);
}

/* ============================================================== RESULTS */
function hideResults() {
    document.getElementById("results-area").hidden = true;
    closeCitation();
}

async function loadResults(jobId) {
    try {
        const res = await fetch(`${API_BASE}/crawl/${jobId}/results`);
        if (!res.ok) throw new Error(await errorDetail(res));
        renderResults(await res.json());
    } catch (err) {
        showLive("Results unavailable");
        logLine(`Could not load results: ${err.message}`, "error");
    }
}

function renderResults(results) {
    const { job, pages, structured, graph } = results;
    document.getElementById("results-title").textContent = `Results · ${job.url || job.job_id}`;
    const verified = Object.keys(structured.citations || {}).length;
    const unverified = (structured.unverified || []).length;
    document.getElementById("results-summary").textContent =
        `${pages.length} page(s) · ${graph.nodes.length} entities · ${graph.edges.length} relations` +
        (verified || unverified ? ` · ${verified} values backed by a quote, ${unverified} unverified` : "");

    renderExports(job.job_id, structured);
    renderData(structured, job);
    renderPages(pages);
    renderEntities(pages);
    renderGraph(graph);
    document.getElementById("tab-agent").hidden = true;
    document.getElementById("results-area").hidden = false;
    selectResultTab(hasLeaves(structured.data) ? "data" : "pages");
}

function renderExports(jobId, structured) {
    const base = `${API_BASE}/crawl/${encodeURIComponent(jobId)}/export?format=`;
    const links = [["JSON", "json"], ["JSONL", "jsonl"]];
    if (tableField(structured.data)) links.push(["CSV", "csv"]);
    document.getElementById("results-exports").replaceChildren(...links.map(([label, format]) =>
        el("a", { class: "btn btn-sm btn-secondary", href: base + format, download: "", text: `Download ${label}` })));
}

function hasLeaves(value) {
    if (value === null || value === undefined || value === "") return false;
    if (Array.isArray(value)) return value.some(hasLeaves);
    if (typeof value === "object") return Object.values(value).some(hasLeaves);
    return true;
}

function tableField(data) {
    if (!data || typeof data !== "object") return null;
    return Object.keys(data).find(key => Array.isArray(data[key]) && data[key].length &&
        data[key].every(item => item && typeof item === "object" && !Array.isArray(item))) || null;
}

function pointerToken(key) {
    return String(key).replace(/~/g, "~0").replace(/\//g, "~1");
}

/* --- data tab: tables and trees with citation markers --- */
function renderData(structured, job) {
    const panel = document.getElementById("result-data");
    const data = structured.data || {};
    if (!hasLeaves(data)) {
        panel.replaceChildren(emptyState(job.prompt || job.has_schema
            ? "The model found nothing on these pages that matches the request."
            : "No structured extraction was requested. Add an instruction after the URL, or an output schema, and run again."));
        return;
    }
    const context = {
        citations: structured.citations || {},
        unverified: new Set(structured.unverified || [])
    };
    const blocks = [];
    if ((structured.schema_errors || []).length) {
        blocks.push(el("div", { class: "notice notice-warn" },
            el("strong", { text: "Output does not fully match the schema: " }),
            structured.schema_errors.slice(0, 5).join("; ")));
    }
    for (const [key, value] of Object.entries(data)) {
        const pointer = `/${pointerToken(key)}`;
        if (Array.isArray(value) && value.length && value.every(v => v && typeof v === "object" && !Array.isArray(v))) {
            blocks.push(renderTable(key, value, pointer, context));
        } else {
            blocks.push(el("section", { class: "data-block" },
                el("h4", { text: key }), renderTree(value, pointer, context)));
        }
    }
    panel.replaceChildren(...blocks);
}

function renderTable(caption, rows, pointer, context) {
    const columns = [];
    rows.forEach(row => Object.keys(row).forEach(col => { if (!columns.includes(col)) columns.push(col); }));
    const head = el("tr", {}, columns.map(col => el("th", { scope: "col", text: col })));
    const body = rows.map((row, index) => el("tr", {}, columns.map(col =>
        el("td", {}, renderValue(row[col], `${pointer}/${index}/${pointerToken(col)}`, context)))));
    return el("div", { class: "table-scroll" },
        el("table", { class: "data-table result-table" },
            el("caption", { text: `${caption} (${rows.length})` }),
            el("thead", {}, head), el("tbody", {}, body)));
}

function renderTree(value, pointer, context) {
    if (Array.isArray(value)) {
        return el("ol", { class: "json-list" },
            value.map((item, i) => el("li", {}, renderTree(item, `${pointer}/${i}`, context))));
    }
    if (value && typeof value === "object") {
        return el("dl", { class: "json-object" }, Object.entries(value).flatMap(([key, item]) => [
            el("dt", { text: key }),
            el("dd", {}, renderTree(item, `${pointer}/${pointerToken(key)}`, context))
        ]));
    }
    return renderValue(value, pointer, context);
}

function renderValue(value, pointer, context) {
    if (value === null || value === undefined || value === "") return el("span", { class: "text-muted", text: "—" });
    const isComposite = typeof value === "object";
    const text = isComposite ? JSON.stringify(value) : String(value);
    // A composite cell is sourced by the quotes of the leaves inside it.
    const quotes = isComposite
        ? Object.entries(context.citations).filter(([p]) => p.startsWith(pointer + "/")).flatMap(([, q]) => q)
        : context.citations[pointer] || [];
    const unverified = isComposite
        ? [...context.unverified].some(p => p.startsWith(pointer + "/"))
        : context.unverified.has(pointer);
    const wrap = el("span", { class: "value" }, el("span", { text }));
    if (quotes.length) {
        wrap.append(el("button", {
            type: "button", class: "cite-marker", "aria-haspopup": "dialog",
            "aria-label": `Show the source quote for ${pointer}`, text: "source",
            onclick: event => openCitation(event.currentTarget, pointer, quotes)
        }));
    }
    if (unverified) {
        wrap.append(el("span", {
            class: "badge badge-unverified", text: "unverified",
            title: "No quote on the page supports this value"
        }));
    }
    return wrap;
}

/* --- citation popover --- */
function setupCitationPopover() {
    document.getElementById("citation-close").addEventListener("click", closeCitation);
    document.addEventListener("keydown", event => { if (event.key === "Escape") closeCitation(); });
    document.addEventListener("click", event => {
        const popover = document.getElementById("citation-popover");
        if (popover.hidden || popover.contains(event.target) || event.target.closest(".cite-marker")) return;
        closeCitation();
    });
}

function openCitation(marker, pointer, quotes) {
    const popover = document.getElementById("citation-popover");
    document.getElementById("citation-title").textContent = `Source for ${pointer}`;
    document.getElementById("citation-body").replaceChildren(...quotes.slice(0, 5).map(quote =>
        el("figure", { class: "citation" },
            el("blockquote", { text: quote.quote }),
            el("figcaption", {}, linkTo(quote.url)))));
    popover.hidden = false;
    const rect = marker.getBoundingClientRect();
    const width = Math.min(380, window.innerWidth - 24);
    popover.style.width = `${width}px`;
    popover.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
    const below = rect.bottom + 8;
    popover.style.top = `${below + popover.offsetHeight > window.innerHeight ? Math.max(12, rect.top - popover.offsetHeight - 8) : below}px`;
    citationOrigin = marker;
    document.getElementById("citation-close").focus();
}

function closeCitation() {
    const popover = document.getElementById("citation-popover");
    if (popover.hidden) return;
    popover.hidden = true;
    if (citationOrigin && document.body.contains(citationOrigin)) citationOrigin.focus();
    citationOrigin = null;
}

/* --- pages tab --- */
function renderPages(pages) {
    const panel = document.getElementById("result-pages");
    if (!pages.length) {
        panel.replaceChildren(emptyState("No pages were processed. Check the job log for navigation or robots.txt errors."));
        return;
    }
    const rows = pages.map(page => {
        const s = page.structured || {};
        const verified = Object.keys(s.citations || {}).length;
        const unverified = (s.unverified || []).length;
        return el("tr", {},
            el("td", {}, linkTo(page.url, page.title || page.url)),
            el("td", { text: page.status_code ?? "—" }),
            el("td", { text: page.summary || "—" }),
            el("td", { text: (page.topics || []).join(", ") || "—" }),
            el("td", { text: s.data ? `${verified} / ${unverified}` : "—" }));
    });
    panel.replaceChildren(el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
        el("thead", {}, el("tr", {}, ["Page", "HTTP", "Summary", "Topics", "Verified / unverified"]
            .map(label => el("th", { scope: "col", text: label })))),
        el("tbody", {}, rows))));
}

/* --- entities tab --- */
function renderEntities(pages) {
    const panel = document.getElementById("result-entities");
    const byName = new Map();
    pages.forEach(page => (page.entities || []).forEach(entity => {
        const name = String(entity.name || "").trim();
        if (!name) return;
        const key = name.toLowerCase();
        const entry = byName.get(key) || { name, type: entity.type || entity.entity_type || "ENTITY", pages: new Set() };
        entry.pages.add(page.url);
        byName.set(key, entry);
    }));
    const entities = [...byName.values()].sort((a, b) => b.pages.size - a.pages.size || a.name.localeCompare(b.name));
    if (!entities.length) {
        panel.replaceChildren(emptyState("No entities were extracted. Is an LLM configured? See Service Health."));
        return;
    }
    const filter = el("input", { type: "search", class: "filter-input", placeholder: "Filter entities", "aria-label": "Filter entities" });
    const types = [...new Set(entities.map(e => e.type))].sort();
    const typeSelect = el("select", { class: "ws-select", "aria-label": "Entity type" },
        el("option", { value: "", text: "All types" }), types.map(t => el("option", { value: t, text: t })));
    const tbody = el("tbody");
    const count = el("p", { class: "text-muted", "aria-live": "polite" });
    const draw = () => {
        const q = filter.value.trim().toLowerCase();
        const shown = entities.filter(e => (!q || e.name.toLowerCase().includes(q)) && (!typeSelect.value || e.type === typeSelect.value));
        count.textContent = `${shown.length} of ${entities.length} entities`;
        tbody.replaceChildren(...shown.map(e => el("tr", {},
            el("td", { text: e.name }), el("td", {}, typeChip(e.type)), el("td", { text: e.pages.size }))));
    };
    filter.addEventListener("input", draw);
    typeSelect.addEventListener("change", draw);
    draw();
    panel.replaceChildren(el("div", { class: "filter-row" }, filter, typeSelect), count,
        el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
            el("thead", {}, el("tr", {}, ["Entity", "Type", "Pages"].map(l => el("th", { scope: "col", text: l })))),
            tbody)));
}

function typeChip(type) {
    return el("span", { class: "type-chip", style: `background:${TYPE_COLORS[type] || TYPE_COLORS.ENTITY}`, text: type });
}

/* --- graph tab: a static force-directed layout, drawn as SVG --- */
function layoutGraph(nodes, edges, width, height) {
    const n = nodes.length;
    const index = new Map(nodes.map((node, i) => [node.id, i]));
    // Deterministic start on a circle, so the same data draws the same graph.
    const pos = nodes.map((_, i) => ({
        x: width / 2 + (width / 3) * Math.cos((2 * Math.PI * i) / Math.max(n, 1)),
        y: height / 2 + (height / 3) * Math.sin((2 * Math.PI * i) / Math.max(n, 1))
    }));
    const links = edges.map(e => [index.get(e.source), index.get(e.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
    const k = Math.sqrt((width * height) / Math.max(n, 1)) * 0.75;
    let temperature = width / 8;
    for (let iter = 0; iter < 220; iter++) {
        const disp = pos.map(() => ({ x: 0, y: 0 }));
        for (let i = 0; i < n; i++) {
            for (let j = i + 1; j < n; j++) {
                const dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
                const dist = Math.max(0.01, Math.hypot(dx, dy));
                const force = (k * k) / dist;
                disp[i].x += (dx / dist) * force; disp[i].y += (dy / dist) * force;
                disp[j].x -= (dx / dist) * force; disp[j].y -= (dy / dist) * force;
            }
        }
        for (const [a, b] of links) {
            const dx = pos[a].x - pos[b].x, dy = pos[a].y - pos[b].y;
            const dist = Math.max(0.01, Math.hypot(dx, dy));
            const force = (dist * dist) / k;
            disp[a].x -= (dx / dist) * force; disp[a].y -= (dy / dist) * force;
            disp[b].x += (dx / dist) * force; disp[b].y += (dy / dist) * force;
        }
        for (let i = 0; i < n; i++) {
            const d = Math.max(0.01, Math.hypot(disp[i].x, disp[i].y));
            pos[i].x = Math.min(width - 40, Math.max(40, pos[i].x + (disp[i].x / d) * Math.min(d, temperature)));
            pos[i].y = Math.min(height - 24, Math.max(24, pos[i].y + (disp[i].y / d) * Math.min(d, temperature)));
        }
        temperature *= 0.97;
    }
    return pos;
}

function renderGraph(graph) {
    const panel = document.getElementById("result-graph");
    if (!graph.nodes.length) {
        panel.replaceChildren(emptyState("No entities or relations were extracted, so there is no graph to draw."));
        return;
    }
    const width = 960, height = 600;
    const pos = layoutGraph(graph.nodes, graph.edges, width, height);
    const at = new Map(graph.nodes.map((node, i) => [node.id, pos[i]]));
    const label = new Map(graph.nodes.map(node => [node.id, node.label]));
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "graph-svg", role: "img",
        "aria-label": `Knowledge graph: ${graph.nodes.length} entities, ${graph.edges.length} relations. A table of relations follows.` });
    graph.edges.forEach(edge => {
        const a = at.get(edge.source), b = at.get(edge.target);
        if (!a || !b) return;
        const line = svgEl("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "graph-edge" });
        const title = svgEl("title");
        title.textContent = `${label.get(edge.source)} ${edge.type} ${label.get(edge.target)}`;
        line.append(title);
        svg.append(line);
    });
    graph.nodes.forEach((node, i) => {
        const group = svgEl("g", { class: "graph-node", transform: `translate(${pos[i].x},${pos[i].y})` });
        group.append(svgEl("circle", { r: 8, fill: TYPE_COLORS[node.type] || TYPE_COLORS.ENTITY }));
        // Labels on the right-hand side read leftwards so they stay in frame.
        const flip = pos[i].x > width * 0.7;
        const text = svgEl("text", flip ? { x: -11, y: 4, "text-anchor": "end" } : { x: 11, y: 4 });
        text.textContent = node.label.length > 26 ? node.label.slice(0, 25) + "…" : node.label;
        const title = svgEl("title");
        title.textContent = `${node.label} (${node.type})`;
        group.append(text, title);
        svg.append(group);
    });

    const types = [...new Set(graph.nodes.map(n => n.type))].sort();
    const legend = el("div", { class: "graph-legend" }, types.map(typeChip));
    const relations = el("details", { class: "relations" },
        el("summary", { text: `Relations (${graph.edges.length})` }),
        el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
            el("thead", {}, el("tr", {}, ["Subject", "Relation", "Object", "Source"].map(l => el("th", { scope: "col", text: l })))),
            el("tbody", {}, graph.edges.map(edge => el("tr", {},
                el("td", { text: label.get(edge.source) }), el("td", { class: "font-jet", text: edge.type }),
                el("td", { text: label.get(edge.target) }), el("td", {}, linkTo(edge.source_url))))))));
    panel.replaceChildren(
        graph.truncated ? el("p", { class: "notice notice-warn", text: `Graph truncated to the first ${graph.nodes.length} entities.` }) : "",
        legend, el("div", { class: "graph-frame" }, svg), relations);
}

/* --- agent result --- */
function renderAgentResult(job) {
    document.getElementById("results-title").textContent = `Agent · ${job.url}`;
    document.getElementById("results-summary").textContent =
        `${job.succeeded ? "Reached the goal" : "Did not reach the goal"} after ${job.steps_taken} step(s)` +
        ` · ${job.endpoints_discovered || 0} API endpoint(s) observed`;
    document.getElementById("results-exports").replaceChildren();
    const steps = job.steps || [];
    const panel = document.getElementById("result-agent");
    panel.replaceChildren(
        el("p", {}, el("strong", { text: "Task: " }), job.task),
        (job.errors || []).length ? el("div", { class: "notice notice-warn", text: job.errors.join("; ") }) : "",
        steps.length ? el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
            el("thead", {}, el("tr", {}, ["#", "Action", "Target", "Reason"].map(l => el("th", { scope: "col", text: l })))),
            el("tbody", {}, steps.map(step => {
                const a = step.action || {};
                const target = a.x !== undefined ? `(${a.x}, ${a.y})${a.element_id !== undefined ? " #" + a.element_id : ""}` : "—";
                return el("tr", {}, el("td", { text: step.step }), el("td", { class: "font-jet", text: a.type }),
                    el("td", { text: a.value ? `${target} "${a.value}"` : target }), el("td", { text: a.reason || a.error || "—" }));
            }))))
            : emptyState("The agent took no steps."));
    ["data", "pages", "entities", "graph"].forEach(tab => {
        document.getElementById(`result-${tab}`).replaceChildren();
    });
    document.getElementById("tab-agent").hidden = false;
    document.getElementById("results-area").hidden = false;
    selectResultTab("agent");
}

/* --- result tabs (WAI-ARIA tabs pattern) --- */
function setupResultTabs() {
    const list = document.getElementById("results-tabs");
    list.addEventListener("click", event => {
        const tab = event.target.closest("[data-result-tab]");
        if (tab) selectResultTab(tab.getAttribute("data-result-tab"));
    });
    list.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tabs = [...list.querySelectorAll("[data-result-tab]")].filter(t => !t.hidden);
        const current = tabs.indexOf(document.activeElement);
        let next = current;
        if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
        if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        event.preventDefault();
        selectResultTab(tabs[next].getAttribute("data-result-tab"));
        tabs[next].focus();
    });
}

function selectResultTab(name) {
    document.querySelectorAll("#results-tabs [data-result-tab]").forEach(tab => {
        const selected = tab.getAttribute("data-result-tab") === name;
        tab.setAttribute("aria-selected", selected ? "true" : "false");
        tab.tabIndex = selected ? 0 : -1;
        document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
    });
    closeCitation();
}

/* ============================================================== HISTORY */
async function loadHistory() {
    const tbody = document.getElementById("history-table-body");
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 6, class: "text-muted", text: "Loading…" })));
    try {
        const res = await fetch(`${API_BASE}/jobs?limit=100`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const { jobs } = await res.json();
        if (!jobs.length) {
            tbody.replaceChildren(el("tr", {}, el("td", { colspan: 6, class: "text-muted",
                text: "No jobs yet. Launch one from New Search." })));
            return;
        }
        tbody.replaceChildren(...jobs.map(historyRow));
    } catch (err) {
        tbody.replaceChildren(el("tr", {}, el("td", { colspan: 6, class: "text-muted",
            text: `Could not load job history (${err.message}).` })));
    }
}

function historyRow(job) {
    const isAgent = job.kind === "agent";
    const running = !TERMINAL.includes(job.status);
    const output = isAgent
        ? `${job.steps_taken} steps · ${job.succeeded ? "goal reached" : "goal not reached"}`
        : `${job.pages_crawled} pages · ${job.entities_extracted} entities · ${job.fields_verified} verified fields`;
    const instruction = isAgent ? job.task : job.prompt;
    return el("tr", {},
        el("td", {}, el("b", { text: job.url }), instruction ? el("div", { class: "text-muted small", text: instruction }) : ""),
        el("td", {}, badge(isAgent ? "agent" : "crawl", isAgent ? "accent" : "")),
        el("td", { class: "text-muted", text: new Date(job.created_at * 1000).toLocaleString() }),
        el("td", {}, badge(job.status, statusTone(job.status))),
        el("td", { class: "text-muted", text: output }),
        el("td", {}, el("button", {
            type: "button", class: "btn btn-sm btn-secondary", text: running ? "Follow" : "Open",
            "aria-label": `${running ? "Follow" : "Open"} job for ${job.url}`,
            onclick: () => openJob(job)
        })));
}

function openJob(job) {
    const isAgent = job.kind === "agent";
    activateTab("new-search");
    showLive(`${isAgent ? "Agent" : "Crawl"}: ${job.url}`);
    if (TERMINAL.includes(job.status)) {
        onJobSnapshotOnly(job);
        if (isAgent) renderAgentResult(job);
        else loadResults(job.job_id);
    } else {
        hideResults();
        trackJob(job.job_id, isAgent);
    }
}

function onJobSnapshotOnly(job) {
    setLiveStatus(job.status, statusTone(job.status));
    renderCounters(job);
    (job.errors || []).forEach(message => logLine(message, "error"));
}

/* =============================================================== HEALTH */
function badgeClass(status) {
    if (status === "up") return "badge badge-success";
    if (status === "disabled") return "badge";
    return "badge badge-warning";
}

function setEngineStatus(text) {
    const label = document.querySelector(".status-indicator b");
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
    const body = document.getElementById("health-table-body");
    try {
        const res = await fetch(API_BASE + "/health");
        const health = await res.json();
        const ok = health.status === "ok";
        setEngineDot(ok ? "var(--success)" : "var(--warning)", false);
        setEngineStatus(ok ? "ONLINE" : "DEGRADED");
        document.getElementById("engine-model").textContent =
            health.llm && health.llm.status === "up" ? `LLM: ${health.llm.detail}` : "LLM: not configured";
        const rows = [].concat(health.components || [], [health.llm, health.embeddings].filter(Boolean));
        body.replaceChildren(...rows.map(c => el("tr", {},
            el("td", {}, el("b", { text: c.name })),
            el("td", {}, el("span", { class: badgeClass(c.status), text: String(c.status).toUpperCase() })),
            el("td", { class: "text-muted", text: c.detail || "—" }))));
    } catch (err) {
        setEngineDot("var(--danger)", false);
        setEngineStatus("UNREACHABLE");
        document.getElementById("engine-model").textContent = "";
        body.replaceChildren(el("tr", {}, el("td", { colspan: 3, class: "text-muted",
            text: `Could not reach ${API_BASE}/health (${err.message}).` })));
    }
}

/* ======================================================== API DISCOVERY */
async function loadDiscoveredApis() {
    const body = document.getElementById("api-table-body");
    body.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "text-muted", text: "Loading…" })));
    try {
        const res = await fetch(`${API_BASE}/apis`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const rows = (await res.json()).endpoints || [];
        if (!rows.length) {
            body.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "text-muted",
                text: "No endpoints discovered yet. Crawl a site that calls its own backend." })));
            return;
        }
        body.replaceChildren(...rows.map(row => el("tr", {},
            el("td", {}, el("span", { class: `api-method method-${String(row.method || "get").toLowerCase()}`, text: row.method || "—" })),
            el("td", { class: "font-jet", text: row.path || row.url || "—" }),
            el("td", { class: "text-muted", text: row.host || "—" }),
            el("td", { class: "text-muted", text: row.content_type || "—" }),
            el("td", { class: "text-muted", text: row.seen_count ?? "—" }))));
    } catch (err) {
        body.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "text-muted",
            text: `Could not load endpoints (${err.message}).` })));
    }
}

/* =========================================================== URL PARSER */
function parseTarget(text) {
    const raw = (text || "").trim();
    if (!raw) return { url: null, task: "" };

    // Prefer an explicit URL anywhere in the text, e.g. "https://example.com/x".
    const explicit = raw.match(/https?:\/\/[^\s"'<>]+/i);
    if (explicit) {
        const url = explicit[0].replace(/[.,;:)\]]+$/, "");
        const task = (raw.replace(explicit[0], " ")).replace(/\s+/g, " ").trim();
        return { url, task };
    }

    // Otherwise accept a bare host: a dotted domain, localhost, or an IPv4
    // literal, each with an optional port and path. A dotted domain must end in
    // a real TLD so prose containing a version number ("version 1.2") or a file
    // name is not mistaken for a host. Public sites such as example.com are
    // reached over https; localhost and raw IPs over plain http.
    const bare = raw.match(
        /(?:^|\s)(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:\/[^\s"'<>]*)?/i
    );
    if (bare) {
        const host = bare[0].trim();
        const task = (raw.replace(bare[0], " ")).replace(/\s+/g, " ").trim();
        const hostname = host.split(":")[0].split("/")[0].toLowerCase();
        const local = hostname === "localhost" || /^\d{1,3}(\.\d{1,3}){3}$/.test(hostname);
        return { url: `${local ? "http" : "https"}://${host}`, task };
    }

    return { url: null, task: "" };
}

/* ======================================================= GLOBAL EXPORTS */
window.startSearch = startSearch;
window.clearSearch = clearSearch;
window.activateTab = activateTab;
