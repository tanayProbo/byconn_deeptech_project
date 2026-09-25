document.addEventListener("DOMContentLoaded", () => {
    setupTabNavigation();
    setupLogSimulator();
    loadApiRegistry();
});

// Load static API registry data into the API tab
function loadApiRegistry() {
    const scroller = document.querySelector('.api-endpoints-scroller');
    if (!scroller) return;
    const sampleEndpoints = [
        { method: 'GET', path: '/api/v1/repos/apify/crawlee' },
        { method: 'POST', path: '/api/graphql' },
        { method: 'GET', path: '/users/active/metrics' }
    ];
    sampleEndpoints.forEach(ep => {
        const div = document.createElement('div');
        div.className = 'api-endpoint-item';
        div.innerHTML = `<span class="api-method method-${ep.method.toLowerCase()}">${ep.method}</span> <span class="api-path">${ep.path}</span>`;
        scroller.appendChild(div);
    });
}

// Setup sidebar tab toggling
function setupTabNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    const panels = document.querySelectorAll(".tab-panel");
    const pageTitle = document.getElementById("page-title");
    const pageSubtitle = document.getElementById("page-subtitle");

    const tabMeta = {
        overview: { title: "Overview Dashboard", subtitle: "Real-time status of planetary crawling operations." },
        crawlers: { title: "Crawler Configurations", subtitle: "Configure target websites and crawling limits." },
        apis: { title: "API Intelligence Registry", subtitle: "Discovered APIs, GraphQL routes, and schemas." },
        workflows: { title: "Workflow Orchestrator", subtitle: "Build visual data pipelines without code." },
        search: { title: "Hybrid Search Portal", subtitle: "Perform dense vector and full-text searches." },
        analytics: { title: "System Analytics", subtitle: "System resource efficiency, extraction rates, and cost analysis." },
        graph: { title: "Knowledge Graph Visualization", subtitle: "Discovered entity nodes and predicate relationships." },
        monitoring: { title: "Active Health Metrics", subtitle: "Prometheus status and Kafka queue levels." }
    };

    navItems.forEach(item => {
        item.addEventListener("click", (e) => {
            e.preventDefault();
            const tab = item.getAttribute("data-tab");

            // Deactivate others
            navItems.forEach(ni => ni.classList.remove("active"));
            panels.forEach(p => p.classList.remove("active"));

            // Activate current
            item.classList.add("active");
            const activePanel = document.getElementById(`panel-${tab}`);
            if (activePanel) {
                activePanel.classList.add("active");
            }

            // Update title text
            if (tabMeta[tab]) {
                pageTitle.textContent = tabMeta[tab].title;
                pageSubtitle.textContent = tabMeta[tab].subtitle;
            }
        });
    });
}

// Log Terminal updates simulator
function setupLogSimulator() {
    const logsContainer = document.getElementById("overview-log-stream");
    if (!logsContainer) return;

    const urls = [
        "https://github.com/apify/crawlee",
        "https://firecrawl.dev",
        "https://github.com/Genymobile/scrcpy",
        "https://github.com/public-apis/public-apis",
        "https://playwright.dev",
        "https://news.ycombinator.com"
    ];

    const tags = [
        { name: "CRAWLER", class: "tag-crawler" },
        { name: "API-INTEL", class: "tag-api" },
        { name: "VISION", class: "tag-vision" },
        { name: "MOBILE", class: "tag-mobile" },
        { name: "PIPELINE", class: "tag-pipeline" }
    ];

    const actions = [
        "Crawled page successfully",
        "Extracted 15 table items",
        "Identified XHR Fetch background request",
        "Indexed chunk node to Qdrant vector database",
        "Updated Neo4j Knowledge Graph triples",
        "Mirrored physical touch gesture via Scrcpy ADB interface"
    ];

    setInterval(() => {
        const timeStr = new Date().toLocaleTimeString();
        const randTag = tags[Math.floor(Math.random() * tags.length)];
        const randUrl = urls[Math.floor(Math.random() * urls.length)];
        const randAction = actions[Math.floor(Math.random() * actions.length)];

        const logLine = document.createElement("div");
        logLine.className = "log-line";
        logLine.innerHTML = `<span class="log-time">[${timeStr}]</span> <span class="log-tag ${randTag.class}">${randTag.name}</span> ${randAction}: <span class="log-url">${randUrl}</span>`;

        logsContainer.appendChild(logLine);
        logsContainer.scrollTop = logsContainer.scrollHeight;

        // Truncate logs if too long
        if (logsContainer.children.length > 50) {
            logsContainer.removeChild(logsContainer.firstChild);
        }
    }, 3000);
}

// Interactive button triggers
function downloadOpenAPI() {
    alert("Compiling discovered schemas... Exporting OpenAPI v3.0 JSON specification document to downloads folder.");
}

function addNewCrawler() {
    const name = prompt("Enter Crawler Name:");
    const url = prompt("Enter Start URL:");
    if (!name || !url) return;

    const tableBody = document.getElementById("crawler-table-body");
    const row = document.createElement("tr");
    row.innerHTML = `
        <td><b>${name}</b></td>
        <td><a href="#" class="link">${url}</a></td>
        <td><span class="badge">Playwright</span></td>
        <td>3 levels</td>
        <td>2 req/sec</td>
        <td><span class="badge badge-success">Active</span></td>
        <td><button class="btn-sm btn-danger">Pause</button></td>
    `;
    tableBody.appendChild(row);
}

    const navItems = document.querySelectorAll(".nav-item");
    const panels = document.querySelectorAll(".tab-panel");
    const pageTitle = document.getElementById("page-title");
    const pageSubtitle = document.getElementById("page-subtitle");

    const tabMeta = {
        overview: { title: "Overview Dashboard", subtitle: "Real-time status of planetary crawling operations." },
        crawlers: { title: "Crawler Configurations", subtitle: "Configure target websites and crawling limits." },
        apis: { title: "API Intelligence Registry", subtitle: "Discovered APIs, GraphQL routes, and schemas." },
        workflows: { title: "Workflow Orchestrator", subtitle: "Build visual data pipelines without code." },
        search: { title: "Hybrid Search Portal", subtitle: "Perform dense vector and full-text searches." },
        analytics: { title: "System Analytics", subtitle: "System resource efficiency, extraction rates, and cost analysis." },
        graph: { title: "Knowledge Graph Visualization", subtitle: "Discovered entity nodes and predicate relationships." },
        monitoring: { title: "Active Health Metrics", subtitle: "Prometheus status and Kafka queue levels." }
    };

    navItems.forEach(item => {
        item.addEventListener("click", (e) => {
            e.preventDefault();
            const tab = item.getAttribute("data-tab");

            // Deactivate others
            navItems.forEach(ni => ni.classList.remove("active"));
            panels.forEach(p => p.classList.remove("active"));

            // Activate current
            item.classList.add("active");
            const activePanel = document.getElementById(`panel-${tab}`);
            if (activePanel) {
                activePanel.classList.add("active");
            }

            // Update title text
            if (tabMeta[tab]) {
                pageTitle.textContent = tabMeta[tab].title;
                pageSubtitle.textContent = tabMeta[tab].subtitle;
            }
        });
    });
}

// Log Terminal updates simulator
function setupLogSimulator() {
    const logsContainer = document.getElementById("overview-log-stream");
    if (!logsContainer) return;

    const urls = [
        "https://github.com/apify/crawlee",
        "https://firecrawl.dev",
        "https://github.com/Genymobile/scrcpy",
        "https://github.com/public-apis/public-apis",
        "https://playwright.dev",
        "https://news.ycombinator.com"
    ];

    const tags = [
        { name: "CRAWLER", class: "tag-crawler" },
        { name: "API-INTEL", class: "tag-api" },
        { name: "VISION", class: "tag-vision" },
        { name: "MOBILE", class: "tag-mobile" },
        { name: "PIPELINE", class: "tag-pipeline" }
    ];

    const actions = [
        "Crawled page successfully",
        "Extracted 15 table items",
        "Identified XHR Fetch background request",
        "Indexed chunk node to Qdrant vector database",
        "Updated Neo4j Knowledge Graph triples",
        "Mirrored physical touch gesture via Scrcpy ADB interface"
    ];

    setInterval(() => {
        const timeStr = new Date().toLocaleTimeString();
        const randTag = tags[Math.floor(Math.random() * tags.length)];
        const randUrl = urls[Math.floor(Math.random() * urls.length)];
        const randAction = actions[Math.floor(Math.random() * actions.length)];

        const logLine = document.createElement("div");
        logLine.className = "log-line";
        logLine.innerHTML = `<span class="log-time">[${timeStr}]</span> <span class="log-tag ${randTag.class}">${randTag.name}</span> ${randAction}: <span class="log-url">${randUrl}</span>`;

        logsContainer.appendChild(logLine);
        logsContainer.scrollTop = logsContainer.scrollHeight;

        // Truncate logs if too long
        if (logsContainer.children.length > 50) {
            logsContainer.removeChild(logsContainer.firstChild);
        }
    }, 3000);
}

// Interactive button triggers
function downloadOpenAPI() {
    alert("Compiling discovered schemas... Exporting OpenAPI v3.0 JSON specification document to downloads folder.");
}

function addNewCrawler() {
    const name = prompt("Enter Crawler Name:");
    const url = prompt("Enter Start URL:");
    if (!name || !url) return;

    const tableBody = document.getElementById("crawler-table-body");
    const row = document.createElement("tr");
    row.innerHTML = `
        <td><b>${name}</b></td>
        <td><a href="#" class="link">${url}</a></td>
        <td><span class="badge">Playwright</span></td>
        <td>3 levels</td>
        <td>2 req/sec</td>
        <td><span class="badge badge-success">Active</span></td>
        <td><button class="btn-sm btn-danger">Pause</button></td>
    `;
    tableBody.appendChild(row);
}
