// Outage Dashboard client.
// Fetches /api/dashboard, renders filters/regions/cards/customer impact,
// persists filter state in localStorage, auto-refreshes, and shows the
// "How it works" modal.

(function () {
  "use strict";

  const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
  const FILTER_STORAGE_KEY = "oa_dash_filter_state";
  const SIDEBAR_STORAGE_KEY = "oa_dash_sidebar_w";
  const SIDEBAR_SNAPS = [160, 200, 240, 280, 320, 400];
  const SIDEBAR_MIN = 140;
  const SIDEBAR_MAX = 500;
  const SIDEBAR_DEFAULT = 220;

  const VIEW_STORAGE_KEY = "oa_dash_active_view";
  const VIEWS = ["outages", "all", "customers", "regions", "graphs"];
  let nextRefreshTime = Date.now() + REFRESH_INTERVAL_MS;

  const SEV_COLORS = {
    critical: "#dc2626",
    warning: "#f59e0b",
    info: "#3b82f6",
    resolved: "#6b7280",
  };

  const SERVICES = [
    "azure", "aws", "cloudflare", "connectwise", "m365", "zscaler",
    "chorus", "googleworkspace", "vector", "atlassian", "wiz", "xero", "halopsa",
    "ninjaone", "duo", "kaseya", "enable", "mimecast", "lightwire", "myob",
    "rewst", "crowdstrike", "kordia", "pia", "nbn", "avepoint",
    "wasabi", "tenable", "eset", "googleai"
  ];
  const SEVERITIES = ["critical", "warning", "info", "resolved"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function relTime(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const diff = Math.max(0, Date.now() - t);
    const m = Math.floor(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.floor(h / 24);
    return d + "d ago";
  }

  function activeCount(rows) {
    return rows.filter(r => {
      if (!r || !r.raw_id) return false;
      return (r.severity || "").toLowerCase() !== "resolved";
    }).length;
  }

  function serviceRegionCounts(rows) {
    const counts = new Map();
    for (const r of rows || []) {
      const svc = r.service;
      if (!svc) continue;
      if (!counts.has(svc)) {
        counts.set(svc, new Map());
      }
      const svcMap = counts.get(svc);
      for (const rg of r.regions || []) {
        if (!rg) continue;
        svcMap.set(rg, (svcMap.get(rg) || 0) + 1);
      }
    }
    return counts;
  }

  function subServiceCounts(rows) {
    const counts = new Map();
    for (const r of rows || []) {
      for (const s of r.sub_services || []) {
        if (!s) continue;
        counts.set(s, (counts.get(s) || 0) + 1);
      }
    }
    return counts;
  }

  function customersForRow(row, impact) {
    return (impact && impact.by_outage && impact.by_outage[row.raw_id]) || [];
  }

  function sortByName(a, b) {
    return a.localeCompare(b);
  }

  function regionPriority(code) {
    const c = String(code || "").toLowerCase();
    if (c === "global") return 1;
    if (c === "multiple") return 2;
    return 3;
  }

  function compareRegionCodes(codeA, codeB) {
    const pA = regionPriority(codeA);
    const pB = regionPriority(codeB);
    if (pA !== pB) return pA - pB;
    return String(codeA || "").localeCompare(String(codeB || ""));
  }

  function compareRegionEntries(entryA, entryB) {
    const pA = regionPriority(entryA[0]);
    const pB = regionPriority(entryB[0]);
    if (pA !== pB) return pA - pB;
    return (entryB[1] - entryA[1]) || String(entryA[0] || "").localeCompare(String(entryB[0] || ""));
  }

  function renderLayout() {
    const root = document.getElementById("app");
    root.innerHTML = `
<header class="app-header">
  <div class="header-left">
    <h1 id="app-brand-title">Outage Dashboard</h1>
    <span class="updated" id="refresh-timer-label"></span>
  </div>
  <nav class="tabs" id="tabs-nav" role="tablist" aria-label="Dashboard views">
    <button type="button" class="tab" data-view="outages" role="tab" aria-selected="true">Outages</button>
    <button type="button" class="tab" data-view="all" role="tab" aria-selected="false">All Outages</button>
    <button type="button" class="tab" data-view="customers" role="tab" aria-selected="false">Customers</button>
    <button type="button" class="tab" data-view="regions" role="tab" aria-selected="false">Regions</button>
    <button type="button" class="tab" data-view="graphs" role="tab" aria-selected="false">Graphs</button>
  </nav>
  <div class="meta">
    <span class="total" id="visible-count">…</span>
    <span class="updated" id="updated-label">Loading…</span>
    <span class="updated clickable" id="last-run-label" title="View import run history" role="button" tabindex="0" aria-haspopup="dialog" aria-controls="runs-modal">Last run: …</span>
    <button type="button" class="help-btn" id="help-btn" aria-haspopup="dialog" aria-controls="help-modal">How it works</button>
    <a href="/logout" class="sign-out-btn" id="logout-btn" title="Sign out of dashboard">Sign out</a>
  </div>
</header>

<section id="view-outages" class="view">
  <div class="app-body">
    <aside class="sidebar">
      <details class="filter-section" open>
        <summary>Service</summary>
        <div id="svc-filters"></div>
      </details>
      <details class="filter-section" open>
        <summary>Severity</summary>
        <div id="sev-filters"></div>
      </details>
      <details class="filter-section">
        <summary>Region</summary>
        <div id="region-filters"></div>
      </details>
      <details class="filter-section" open>
        <summary>Sub-service</summary>
        <div id="subsvc-filters"></div>
      </details>
      <button type="button" class="reset-btn" id="reset-btn">Reset filters</button>
    </aside>

    <div class="v-splitter" id="vSplitter" title="Drag to resize sidebar"></div>

    <main class="content">
      <div class="customer-impact-header">
        <div class="customer-impact-title-group">
          <h2 class="section-h2" style="margin:0;">Customer impact</h2>
          <span class="customer-impact-count-badge" id="cust-impact-summary">0 affected</span>
          <span class="active-filter-pill" id="cust-filter-pill" hidden>1 customer</span>
        </div>
        <div class="customer-impact-controls">
          <input type="search" class="form-input customer-search-input" id="cust-search-input" placeholder="Search customers..." style="font-size:12px; padding:2px 8px; height:26px; width:170px;">
          <button type="button" class="btn-subtle" id="cust-select-all-btn" title="Filter by all impacted customers">Select all</button>
          <button type="button" class="btn-subtle" id="cust-clear-btn" title="Clear customer filter">Clear</button>
          <button type="button" class="btn-subtle" id="cust-collapse-btn" title="Toggle section collapse" aria-expanded="true">▲</button>
        </div>
      </div>
      <div class="customer-grid-container" id="customer-grid-container">
        <div class="customer-grid" id="customer-list"></div>
      </div>

      <h2 class="section-h2">Outages</h2>
      <div class="card-list" id="card-list"></div>
    </main>
  </div>
</section>

<section id="view-all" class="view" hidden>
  <div class="editor-toolbar all-toolbar">
    <input type="search" class="form-input" id="all-search" placeholder="Search outages (title, ID, provider, region, customer)..." style="min-width:200px; flex:1; max-width:320px">

    <div class="multi-dropdown-wrap" id="all-svc-dropdown">
      <button type="button" class="form-select multi-dropdown-btn" id="all-svc-btn" aria-haspopup="true" aria-expanded="false">
        <span id="all-svc-btn-label">Providers: All</span>
        <span class="dropdown-caret">▾</span>
      </button>
      <div class="multi-dropdown-menu hidden" id="all-svc-menu">
        <div class="multi-menu-header">
          <label class="multi-menu-item multi-select-all">
            <input type="checkbox" id="all-svc-check-all" checked>
            <span><strong>All Providers</strong></span>
          </label>
        </div>
        <div class="multi-menu-search">
          <input type="search" class="form-input form-input-sm" id="all-svc-search" placeholder="Find provider…">
        </div>
        <div class="multi-menu-list" id="all-svc-list"></div>
        <div class="multi-menu-footer">
          <button type="button" class="btn-ghost btn-xs" id="all-svc-select-all">Select all</button>
          <button type="button" class="btn-ghost btn-xs" id="all-svc-clear-all">Clear</button>
        </div>
      </div>
    </div>

    <div class="multi-dropdown-wrap" id="all-sev-dropdown">
      <button type="button" class="form-select multi-dropdown-btn" id="all-sev-btn" aria-haspopup="true" aria-expanded="false">
        <span id="all-sev-btn-label">Status: All</span>
        <span class="dropdown-caret">▾</span>
      </button>
      <div class="multi-dropdown-menu hidden" id="all-sev-menu">
        <div class="multi-menu-header">
          <label class="multi-menu-item multi-select-all">
            <input type="checkbox" id="all-sev-check-all" checked>
            <span><strong>All Statuses</strong></span>
          </label>
        </div>
        <div class="multi-menu-list" id="all-sev-list"></div>
        <div class="multi-menu-footer">
          <button type="button" class="btn-ghost btn-xs" id="all-sev-preset-crit-warn">⚡ Critical & Warning</button>
          <button type="button" class="btn-ghost btn-xs" id="all-sev-preset-active">Active</button>
          <button type="button" class="btn-ghost btn-xs" id="all-sev-select-all">All</button>
        </div>
      </div>
    </div>

    <button type="button" class="btn-preset-pill" id="all-quick-crit-warn" title="Quickly filter all providers to Critical & Warning outages">⚠️ Critical & Warning</button>

    <select class="form-select" id="all-cust-filter" style="max-width:165px">
      <option value="">All Customers</option>
      <option value="_impacted">Impacted Only</option>
    </select>
    <div class="spacer"></div>
    <div class="all-pagination-controls">
      <select class="form-select" id="all-page-size" style="max-width:115px">
        <option value="25">25 / page</option>
        <option value="50" selected>50 / page</option>
        <option value="100">100 / page</option>
        <option value="250">250 / page</option>
        <option value="all">All</option>
      </select>
      <button type="button" class="btn-secondary" id="all-page-prev" title="Previous page" disabled>←</button>
      <span id="all-page-indicator" style="font-size:12px; color:var(--text); white-space:nowrap">1 / 1</span>
      <button type="button" class="btn-secondary" id="all-page-next" title="Next page">→</button>
    </div>
  </div>
  <div class="all-status-bar" id="all-status-bar">
    <span id="all-count-summary" style="font-size:11px; color:var(--text-dim)">Loading outages…</span>
  </div>
  <div id="all-table-wrap" style="overflow:auto; flex:1 1 0; min-height:0"></div>
</section>

<section id="view-customers" class="view" hidden>
  <div class="editor-toolbar">
    <input type="search" class="form-input" id="cust-search" placeholder="Filter by name…" style="max-width:280px">
    <div class="spacer"></div>
    <button type="button" class="btn-primary" id="cust-add">+ Add customer</button>
  </div>
  <div class="customer-editor-list" id="cust-list"></div>
</section>

<section id="view-regions" class="view" hidden>
  <div class="editor-toolbar">
    <input type="search" class="form-input" id="reg-search" placeholder="Filter by service or region…" style="max-width:320px">
    <div class="spacer"></div>
    <button type="button" class="btn-primary" id="reg-add">+ Add region</button>
  </div>
  <div id="reg-table-wrap" style="overflow:auto"></div>
</section>

<section id="view-graphs" class="view" hidden>
  <div class="graphs-body">
    <aside class="graphs-sidebar">
      <div class="graphs-control-group">
        <label class="graphs-label" for="graph-chart-type">Chart Type</label>
        <select class="form-select" id="graph-chart-type">
          <option value="stacked-bar" selected>Stacked Bar Chart</option>
          <option value="grouped-bar">Grouped Bar Chart</option>
          <option value="line">Multi-Series Line</option>
          <option value="area">Area Trend</option>
        </select>
      </div>

      <div class="graphs-control-group">
        <label class="graphs-label">Time Range</label>
        <div class="graphs-btn-group" id="graph-range-btns">
          <button type="button" class="graph-pill-btn" data-range="24h">24h</button>
          <button type="button" class="graph-pill-btn" data-range="7d">7d</button>
          <button type="button" class="graph-pill-btn active" data-range="30d">30d</button>
          <button type="button" class="graph-pill-btn" data-range="90d">90d</button>
          <button type="button" class="graph-pill-btn" data-range="all">All</button>
        </div>
      </div>

      <div class="graphs-control-group">
        <label class="graphs-label" for="graph-x-dim">X-Axis Dimension</label>
        <select class="form-select" id="graph-x-dim">
          <option value="time" selected>Time (Chronological)</option>
          <option value="provider">Provider</option>
          <option value="severity">Severity</option>
          <option value="customer">Customer</option>
        </select>
      </div>

      <div class="graphs-control-group">
        <label class="graphs-label" for="graph-y-metric">Y-Axis Metric</label>
        <select class="form-select" id="graph-y-metric">
          <option value="count" selected>Incident / Outage Count</option>
          <option value="impacted_customers">Impacted Customer Count</option>
          <option value="severity_score">Severity Weighted Impact</option>
        </select>
      </div>

      <div class="graphs-control-group">
        <label class="graphs-label" for="graph-z-group">Breakdown (Z-Axis / Color)</label>
        <select class="form-select" id="graph-z-group">
          <option value="provider" selected>Group by Provider</option>
          <option value="severity">Group by Severity</option>
          <option value="customer">Group by Customer</option>
          <option value="none">None (Single Series)</option>
        </select>
      </div>

      <details class="filter-section" open>
        <summary>Filter Providers</summary>
        <div id="graph-provider-filters"></div>
      </details>

      <details class="filter-section" open>
        <summary>Filter Severity</summary>
        <div id="graph-severity-filters"></div>
      </details>

      <details class="filter-section">
        <summary>Filter Customer</summary>
        <select class="form-select" id="graph-customer-filter" style="width:100%;margin-top:4px">
          <option value="">All Customers</option>
        </select>
      </details>
    </aside>

    <div class="v-splitter" id="vSplitterGraphs" title="Drag to resize controls"></div>

    <main class="graphs-content">
      <div class="graphs-summary-bar" id="graphs-summary-bar"></div>

      <div class="graph-card">
        <div class="graph-card-header">
          <div class="graph-card-title-group">
            <h2 class="graph-card-title" id="graph-main-title">Outages over Time</h2>
            <span class="graph-card-subtitle" id="graph-main-subtitle">Daily breakdown by provider</span>
          </div>
          <div class="graph-card-actions">
            <span class="graph-legend-hint">Click legend item to toggle series</span>
          </div>
        </div>

        <div class="graph-viewport-wrap" id="graph-viewport-wrap">
          <svg id="graph-svg" class="graph-svg" preserveAspectRatio="xMidYMid meet"></svg>
          <div class="graph-tooltip hidden" id="graph-tooltip"></div>
        </div>

        <div class="graph-legend" id="graph-legend"></div>
      </div>
    </main>
  </div>
</section>

<div class="modal-backdrop hidden" id="help-backdrop">
  <div class="modal" id="help-modal" role="dialog" aria-modal="true" aria-labelledby="help-title" tabindex="-1">
    <div class="modal-head">
      <h2 class="modal-title" id="help-title">How this dashboard works</h2>
      <button type="button" class="modal-close" id="help-close">Close</button>
    </div>
    <div class="modal-body">
      <div class="help-grid">
        <section class="help-card">
          <h3>Pipeline</h3>
          <p>Provider ingesters fetch &amp; normalize incidents from 26 cloud, SaaS, telco, and MSP infrastructure feeds into a common schema; the Core Processor then maintains <code>alert_history.json</code>.</p>
          <ul>
            <li>Intake &rarr; Normalize (Atlassian, AvePoint, AWS, Azure, Chorus, Cloudflare, ConnectWise, CrowdStrike, Duo, Enable, Google Workspace, HaloPSA, Kaseya, Kordia, Lightwire, M365, Mimecast, MYOB, NBN Co, NinjaOne, Pia, Rewst, Vector, Wiz, Xero, Zscaler)</li>
            <li>Smart Noise &amp; Region Filtering: localized outages are filtered so ANZ/APAC customers are not spammed with irrelevant overseas incidents; global infrastructure events remain visible.</li>
            <li>Dynamic Region Discovery: Chorus exchanges, Vector Auckland suburbs, and Wiz cloud/geographical regions are automatically discovered and populated into approved regions.</li>
            <li>Core &rarr; Merge + dedup + 7-day retention window</li>
            <li>Dashboard &rarr; Match customer subscriptions on the fly</li>
          </ul>
        </section>
        <section class="help-card">
          <h3>What you're seeing</h3>
          <p>Each card is one outage record keyed by <code>raw_id</code>. Customer impact is computed at request time against the current <code>customers.json</code>.</p>
          <ul>
            <li><strong>Customer impact:</strong> customers with &ge;1 matching outage (click a row to narrow the outage list to that customer).</li>
            <li><strong>Outages:</strong> filtered client-side by your active selections in the sidebar.</li>
            <li><strong>Wiz:</strong> cloud security and runtime incidents mapped to cloud data center regions (e.g. <code>us-west-2</code>, <code>me-central-1</code>) and geographic scopes (<code>global</code>, <code>us</code>, <code>eu</code>, <code>apac</code>).</li>
            <li><strong>Chorus &amp; Enable:</strong> classified by customer impact sum (<strong>critical</strong>: &ge;100, <strong>warning</strong>: 10–99, <strong>info</strong>: &lt;10). Sub-services: <code>fibre</code>, <code>copper</code>, <code>broadband</code>.</li>
            <li><strong>Vector:</strong> planned/in-progress power outages mapped by suburb. Sub-services: <code>power</code>, <code>hv</code> (High Voltage), <code>lv</code> (Low Voltage).</li>
          </ul>
        </section>
        <section class="help-card">
          <h3>Tabs &amp; editing</h3>
          <p>Five tabs at the top: <strong>Outages</strong> (active &amp; recent outages), <strong>All Outages</strong> (dense multi-select chronological table view with quick presets and CSV export), <strong>Customers</strong> (manage customer subscriptions and sub-services), <strong>Regions</strong> (manage approved regions per provider), and <strong>Graphs</strong> (interactive native SVG analytics with time series, customer breakdowns, and metric filtering).</p>
          <ul>
            <li>Active tab is persisted in <code>localStorage</code> and restored on next visit.</li>
            <li>Customer &amp; region changes propagate immediately across the UI and to subsequent ingest cycles.</li>
            <li>Write endpoints require HTTP Basic Auth (configured via <code>OA_DASHBOARD_USER</code> / <code>OA_DASHBOARD_PASSWORD_FILE</code>).</li>
          </ul>
        </section>
        <section class="help-card">
          <h3>Filtering &amp; refresh</h3>
          <p>Filters are applied instantly in your browser and persisted locally so they survive auto-refresh. All filter controls live in the left sidebar and table toolbars.</p>
          <ul>
            <li><strong>Service / Severity:</strong> toggle individual providers and severity tiers, or use the multi-select dropdowns with master checkboxes.</li>
            <li><strong>Quick Presets:</strong> one-click <em>Critical &amp; Warning</em> toggle instantly isolates high-priority outages across all services.</li>
            <li><strong>Region:</strong> geographic regions and suburbs; uncheck to hide.</li>
            <li><strong>Sub-services:</strong> filter by specific infrastructure products (e.g. fibre/copper, hv/lv, workflows).</li>
            <li><strong>Customer:</strong> click a customer in the impact table to isolate their matched outages; click again to clear.</li>
            <li><strong>Auto-refresh:</strong> every 5 minutes with a countdown timer; pauses when modals are open.</li>
          </ul>
        </section>
        <section class="help-card help-card-full">
          <h3>Monitored Feeds (29 Providers)</h3>
          <p>The system periodically fetches and processes the following source feeds across global cloud, SaaS, ANZ telco, MSP tooling, and power infrastructure:</p>
          <ul class="help-feeds-list">
            <li><strong>Atlassian:</strong> <a href="https://confluence.status.atlassian.com/api/v2/incidents.json" target="_blank" rel="noopener">confluence.status.atlassian.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>AvePoint:</strong> <a href="https://identity.avepointonlineservices.com" target="_blank" rel="noopener">identity.avepointonlineservices.com</a> (AOS Partner Health API)</li>
            <li><strong>AWS:</strong> <a href="https://status.aws.amazon.com/rss/all.rss" target="_blank" rel="noopener">status.aws.amazon.com/rss/all.rss</a> (RSS Feed)</li>
            <li><strong>Azure:</strong> <a href="https://rssfeed.azure.status.microsoft/en-us/status/feed/" target="_blank" rel="noopener">rssfeed.azure.status.microsoft/en-us/status/feed/</a> (RSS Feed)</li>
            <li><strong>Chorus:</strong> <a href="https://api.chorus.co.nz/events/v3/incidents" target="_blank" rel="noopener">api.chorus.co.nz/events/v3/incidents</a> (NZ Fibre Outage API)</li>
            <li><strong>Cloudflare:</strong> <a href="https://www.cloudflarestatus.com/api/v2/incidents.json" target="_blank" rel="noopener">cloudflarestatus.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>ConnectWise:</strong> <a href="https://api.status.io/1.0/status/619cf82551fec9053d612f09" target="_blank" rel="noopener">api.status.io/1.0/status/619cf82551fec9053d612f09</a> (Status.io API)</li>
            <li><strong>CrowdStrike:</strong> <a href="https://falcon.crowdstrike.com" target="_blank" rel="noopener">api.crowdstrike.com/system-health/v1</a> (Falcon OAuth2 Health API)</li>
            <li><strong>Duo Security:</strong> <a href="https://status.duo.com/api/v2/incidents.json" target="_blank" rel="noopener">status.duo.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Enable Networks:</strong> <a href="https://enable.statuspage.io/api/v2/incidents.json" target="_blank" rel="noopener">enable.statuspage.io/api/v2/incidents.json</a> (Christchurch Fibre API)</li>
            <li><strong>ESET:</strong> <a href="https://status.eset.com/api/v2/incidents.json" target="_blank" rel="noopener">status.eset.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Google Workspace:</strong> <a href="https://www.google.com/appsstatus/dashboard/incidents.json" target="_blank" rel="noopener">google.com/appsstatus/dashboard/incidents.json</a> (Status API)</li>
            <li><strong>Google AI &amp; Antigravity:</strong> <a href="https://aistudio.google.com/status" target="_blank" rel="noopener">aistudio.google.com/status</a> (Gemini API &amp; AI Studio Status RPC)</li>
            <li><strong>HaloPSA:</strong> <a href="https://api.status.io/1.0/status/63ef45da7ee94905308a1a4a" target="_blank" rel="noopener">api.status.io/1.0/status/63ef45da7ee94905308a1a4a</a> (Status.io API)</li>
            <li><strong>Kaseya:</strong> <a href="https://status.kaseya.com/api/v2/incidents.json" target="_blank" rel="noopener">status.kaseya.com/api/v2/incidents.json</a> (IT Complete Statuspage API)</li>
            <li><strong>Kordia:</strong> <a href="https://portal.kordia.co.nz" target="_blank" rel="noopener">portal.kordia.co.nz/api/v1/network/incidents</a> (NZ Telco NOC Portal)</li>
            <li><strong>Lightwire:</strong> <a href="https://status.lightwire.co.nz/api/v1/incidents" target="_blank" rel="noopener">status.lightwire.co.nz/api/v1/incidents</a> (NZ Telco Status API)</li>
            <li><strong>Microsoft 365:</strong> <a href="https://status.cloud.microsoft/api/feed/mac" target="_blank" rel="noopener">status.cloud.microsoft/api/feed/mac</a> (M365 Service Comms API)</li>
            <li><strong>Mimecast:</strong> <a href="https://api.status.io/1.0/status/5d849b1c02e65b3ec45369d4" target="_blank" rel="noopener">api.status.io/1.0/status/5d849b1c02e65b3ec45369d4</a> (Status.io API)</li>
            <li><strong>MYOB:</strong> <a href="https://status.myob.com" target="_blank" rel="noopener">status.myob.com/api/blocks/calendar/v1</a> (Calendar Health API)</li>
            <li><strong>NBN Co:</strong> <a href="https://www.nbnco.com.au" target="_blank" rel="noopener">nbnco.com.au/api/v1/network-status</a> (Australia Wholesale RSP B2B API)</li>
            <li><strong>NinjaOne:</strong> <a href="https://status.ninjaone.com/api/v2/incidents.json" target="_blank" rel="noopener">status.ninjaone.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Pia (aiDesk):</strong> <a href="https://pia.ai" target="_blank" rel="noopener">api.pia.ai/v1/health</a> (AI Helpdesk Partner API)</li>
            <li><strong>Rewst:</strong> <a href="https://status-au.rewst.io" target="_blank" rel="noopener">status-au.rewst.io</a> (RPA Workflow Engine / Cronitor)</li>
            <li><strong>Tenable:</strong> <a href="https://status.tenable.com/api/v2/incidents.json" target="_blank" rel="noopener">status.tenable.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Vector:</strong> <a href="https://help.vector.co.nz/map" target="_blank" rel="noopener">outage-centre.api.vector.co.nz/v1/outages</a> (Auckland Power API)</li>
            <li><strong>Wasabi:</strong> <a href="https://status.wasabi.com/api/v2/incidents.json" target="_blank" rel="noopener">status.wasabi.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Wiz:</strong> <a href="https://status.wiz.io/history.rss" target="_blank" rel="noopener">status.wiz.io/history.rss</a> (Cloud Security RSS Feed)</li>
            <li><strong>Xero:</strong> <a href="https://status.xero.com/api/v2/incidents.json" target="_blank" rel="noopener">status.xero.com/api/v2/incidents.json</a> (Statuspage API)</li>
            <li><strong>Zscaler:</strong> <a href="https://trust.zscaler.com/rss-feed" target="_blank" rel="noopener">trust.zscaler.com/rss-feed</a> (Trust Portal RSS Feed)</li>
          </ul>
        </section>
      </div>
      <div class="help-foot">Tip: click region chips to narrow focus; use Reset filters to return to the full view.</div>
    </div>
  </div>
</div>

<div class="modal-backdrop hidden" id="runs-backdrop">
  <div class="modal" id="runs-modal" role="dialog" aria-modal="true" aria-labelledby="runs-title" tabindex="-1">
    <div class="modal-head">
      <h2 class="modal-title" id="runs-title">Import runs</h2>
      <button type="button" class="modal-close" id="runs-close">Close</button>
    </div>
    <div class="modal-body">
      <div id="runs-list-view">
        <div class="run-list" id="run-list"></div>
        <div class="help-foot" id="runs-list-foot"></div>
      </div>
      <div id="runs-detail-view" hidden>
        <button type="button" class="run-back" id="runs-back">← Back to runs</button>
        <div class="run-detail" id="run-detail"></div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop hidden" id="cust-edit-backdrop">
  <div class="modal" id="cust-edit-modal" role="dialog" aria-modal="true" aria-labelledby="cust-edit-title" tabindex="-1">
    <div class="modal-head">
      <h2 class="modal-title" id="cust-edit-title">Edit customer</h2>
      <button type="button" class="modal-close" id="cust-edit-close">Close</button>
    </div>
    <div class="modal-body" id="cust-edit-body"></div>
  </div>
</div>

<div class="modal-backdrop hidden" id="reg-edit-backdrop">
  <div class="modal" id="reg-edit-modal" role="dialog" aria-modal="true" aria-labelledby="reg-edit-title" tabindex="-1">
    <div class="modal-head">
      <h2 class="modal-title" id="reg-edit-title">Edit region</h2>
      <button type="button" class="modal-close" id="reg-edit-close">Close</button>
    </div>
    <div class="modal-body" id="reg-edit-body"></div>
  </div>
</div>
`;
  }

  function init() {
    renderLayout();
    restoreActiveView();
    bindTabs();
    bindAllOutagesEvents();
    bindCustomerEditor();
    bindRegionEditor();
    bindGraphsEvents();
    bindHelpModal();
    bindRunsModal();
    bindReset();
    bindSidebarResize();
    bindCustomerImpactControls();
    startRefreshTimer();
    if (currentView === "outages" || currentView === "all") {
      void refresh();
    } else if (currentView === "customers") {
      void loadCustomers();
    } else if (currentView === "regions") {
      void loadRegions();
    } else if (currentView === "graphs") {
      void loadGraphsView();
    }
    setInterval(() => {
      if ((currentView === "outages" || currentView === "all") && !helpIsOpen() && !runsAreOpen() && !editorIsOpen()) {
        void refresh();
      }
    }, REFRESH_INTERVAL_MS);
  }

  function startRefreshTimer() {
    setInterval(() => {
      const label = document.getElementById("refresh-timer-label");
      if (!label) return;
      if ((currentView !== "outages" && currentView !== "all") || helpIsOpen() || runsAreOpen() || editorIsOpen()) {
        label.textContent = "";
        return;
      }
      const remainMs = nextRefreshTime - Date.now();
      if (remainMs <= 0) {
        label.textContent = "Refreshing…";
        return;
      }
      const totalSec = Math.ceil(remainMs / 1000);
      const min = Math.floor(totalSec / 60);
      const sec = totalSec % 60;
      const padSec = String(sec).padStart(2, "0");
      label.textContent = `Next refresh: ${min}:${padSec}`;
    }, 1000);
  }

  function bindHelpModal() {
    const btn = document.getElementById("help-btn");
    const backdrop = document.getElementById("help-backdrop");
    const close = document.getElementById("help-close");
    btn.addEventListener("click", openHelp);
    close.addEventListener("click", closeHelp);
    backdrop.addEventListener("click", e => {
      if (e.target === backdrop) closeHelp();
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && helpIsOpen()) closeHelp();
    });
  }

  function openHelp() {
    document.getElementById("help-backdrop").classList.remove("hidden");
    document.getElementById("help-modal").focus();
  }
  function closeHelp() {
    document.getElementById("help-backdrop").classList.add("hidden");
  }
  function helpIsOpen() {
    return !document.getElementById("help-backdrop").classList.contains("hidden");
  }

  // ----- Runs modal -----------------------------------------------------
  // The "Last run: …" label in the header opens this modal. It lists the
  // most recent import runs and lets the user click into one to see the
  // per-provider success/failure and any error messages. Lazy-loaded —
  // the 5-minute auto-refresh on /api/dashboard does NOT poll this.
  function bindRunsModal() {
    document.getElementById("last-run-label").addEventListener("click", openRuns);
    document.getElementById("last-run-label").addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRuns(); }
    });
    const close = document.getElementById("runs-close");
    const backdrop = document.getElementById("runs-backdrop");
    close.addEventListener("click", closeRuns);
    backdrop.addEventListener("click", e => { if (e.target === backdrop) closeRuns(); });
    document.getElementById("runs-back").addEventListener("click", () => {
      showRunsListView();
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && runsAreOpen()) closeRuns();
    });
  }

  function runsAreOpen() {
    return !document.getElementById("runs-backdrop").classList.contains("hidden");
  }

  function openRuns() {
    document.getElementById("runs-backdrop").classList.remove("hidden");
    document.getElementById("runs-modal").focus();
    showRunsListView();
    void fetchAndRenderRuns();
  }

  function closeRuns() {
    document.getElementById("runs-backdrop").classList.add("hidden");
  }

  function showRunsListView() {
    document.getElementById("runs-list-view").hidden = false;
    document.getElementById("runs-detail-view").hidden = true;
  }

  function showRunsDetailView() {
    document.getElementById("runs-list-view").hidden = true;
    document.getElementById("runs-detail-view").hidden = false;
  }

  // In-memory cache of the most recent runs list so the "back" button
  // doesn't trigger another fetch.
  const runsCache = { items: [] };

  async function fetchAndRenderRuns() {
    const list = document.getElementById("run-list");
    const foot = document.getElementById("runs-list-foot");
    list.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px 0">Loading…</div>';
    foot.textContent = "";
    try {
      const r = await fetch("/api/runs?limit=20", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const body = await r.json();
      runsCache.items = body.runs || [];
      renderRunsList(runsCache.items);
      foot.textContent = runsCache.items.length
        ? `Showing ${runsCache.items.length} most recent run${runsCache.items.length === 1 ? "" : "s"}.`
        : "";
    } catch (e) {
      list.innerHTML = '<div class="empty"><div class="big">!</div><div>Failed to load runs: ' + esc(e.message) + '</div></div>';
    }
  }

  function renderRunsList(runs) {
    const list = document.getElementById("run-list");
    if (!runs || runs.length === 0) {
      list.innerHTML = '<div class="empty"><div class="big">∅</div><div>No import runs recorded yet.</div></div>';
      return;
    }
    const html = runs.map(r => {
      const when = (r.started_at || "").replace("T", " ").replace("Z", " UTC");
      const dur = formatDuration(r.duration_ms);
      const badge = r.ok
        ? '<span class="run-badge run-badge-ok">ok</span>'
        : '<span class="run-badge run-badge-err">with errors</span>';
      const ev = (r.event_count || 0) + " events · " + (r.providers_ok || 0) + "/" + (r.provider_count || 0) + " providers";
      return `
        <button type="button" class="run-row" data-run-id="${esc(r.run_id)}" aria-label="View run ${esc(r.run_id)}">
          <div class="run-when">${esc(when)}</div>
          <div class="run-meta">${esc(dur)} · ${esc(ev)}</div>
          ${badge}
        </button>`;
    }).join("");
    list.innerHTML = html;
    list.querySelectorAll(".run-row").forEach(btn => {
      btn.addEventListener("click", () => void openRunDetail(btn.dataset.runId));
    });
  }

  async function openRunDetail(runId) {
    const detail = document.getElementById("run-detail");
    detail.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:12px 0">Loading…</div>';
    showRunsDetailView();
    try {
      const r = await fetch("/api/runs/" + encodeURIComponent(runId), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const report = await r.json();
      renderRunDetail(report);
    } catch (e) {
      detail.innerHTML = '<div class="empty"><div class="big">!</div><div>Failed to load run: ' + esc(e.message) + '</div></div>';
    }
  }

  function renderRunDetail(report) {
    const detail = document.getElementById("run-detail");
    const when = (report.started_at || "").replace("T", " ").replace("Z", " UTC");
    const dur = formatDuration(report.duration_ms);
    const overallOk = (report.providers || []).every(p => p.ok) && (report.providers || []).length > 0;
    const badge = overallOk
      ? '<span class="run-badge run-badge-ok">ok</span>'
      : '<span class="run-badge run-badge-err">with errors</span>';
    const history = report.history || {};
    const historyLine = history.history_size != null
      ? `<span class="run-stat">history size: <strong>${history.history_size}</strong></span>
         <span class="run-stat">active: <strong>${history.active_count || 0}</strong></span>`
      : "";
    const providers = (report.providers || []).map(p => {
      const pBadge = p.ok
        ? '<span class="run-badge run-badge-ok">ok</span>'
        : '<span class="run-badge run-badge-err">fail</span>';
      const pDur = formatDuration(p.duration_ms);
      const pCount = (p.event_count || 0) + " events";
      const errBlock = p.error
        ? `<pre class="run-error">${esc(p.error)}</pre>`
        : "";
      return `
        <tr class="run-provider-row">
          <td class="run-provider-name">${esc(p.provider)}</td>
          <td class="run-provider-badge">${pBadge}</td>
          <td class="run-provider-meta">${esc(pDur)} · ${esc(pCount)}</td>
        </tr>
        ${errBlock ? `<tr class="run-provider-err"><td colspan="3">${errBlock}</td></tr>` : ""}`;
    }).join("");
    const impact = report.impact_summary;
    const impactBlock = impact && impact.top_customers && impact.top_customers.length
      ? `<div class="run-impact">Top customers: ${impact.top_customers.map(c => `${esc(c.name)} (${c.total})`).join(", ")}</div>`
      : "";
    detail.innerHTML = `
      <div class="run-detail-head">
        <div class="run-detail-when">${esc(when)}</div>
        <div class="run-detail-meta">${esc(dur)} · ${esc(report.run_id || "")}</div>
        ${badge}
      </div>
      <div class="run-detail-stats">${historyLine}</div>
      <table class="run-detail-table">
        <thead>
          <tr><th>Provider</th><th>Result</th><th>Detail</th></tr>
        </thead>
        <tbody>${providers}</tbody>
      </table>
      ${impactBlock}
    `;
  }

  function formatDuration(ms) {
    if (!ms || ms < 0) return "—";
    if (ms < 1000) return ms + "ms";
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    return m + "m " + r + "s";
  }

  function bindReset() {
    document.getElementById("reset-btn").addEventListener("click", () => {
      document.querySelectorAll(".flt-svc, .flt-sev, .flt-region, .flt-subsvc").forEach(c => (c.checked = true));
      state.activeCustomers.clear();
      // Reflect the cleared state in the customer filter rows and pill.
      // (Without this, a row that was active would stay visually active
      // even though the underlying state is now empty.)
      syncCustomerFilterRows();
      saveFilterState();
      applyFilters();
    });
  }

  function bindSidebarResize() {
    const splitter = document.getElementById("vSplitter");
    const root = document.documentElement;
    let startPos = 0;
    let startSize = SIDEBAR_DEFAULT;
    let dragging = false;
    const stored = parseInt(localStorage.getItem(SIDEBAR_STORAGE_KEY) || "", 10);
    if (Number.isFinite(stored) && stored >= SIDEBAR_MIN && stored <= SIDEBAR_MAX) {
      root.style.setProperty("--sidebar-w", stored + "px");
    }
    splitter.addEventListener("mousedown", e => {
      dragging = true;
      startPos = e.clientX;
      startSize = parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10) || SIDEBAR_DEFAULT;
      splitter.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", e => {
      if (!dragging) return;
      const delta = e.clientX - startPos;
      const total = window.innerWidth;
      const newSize = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, total - 100, startSize + delta));
      root.style.setProperty("--sidebar-w", newSize + "px");
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      const cur = parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10);
      let best = SIDEBAR_SNAPS[0], bestDist = Math.abs(cur - best);
      for (const s of SIDEBAR_SNAPS) {
        const d = Math.abs(cur - s);
        if (d < bestDist) { best = s; bestDist = d; }
      }
      root.style.setProperty("--sidebar-w", best + "px");
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(best));
    });
    splitter.addEventListener("dblclick", () => {
      root.style.setProperty("--sidebar-w", SIDEBAR_DEFAULT + "px");
      localStorage.setItem(SIDEBAR_STORAGE_KEY, String(SIDEBAR_DEFAULT));
    });
  }

  // ----- State + rendering ---------------------------------------------
  const state = {
    payload: null,
    rows: [],
    impact: null,
    activeRegions: new Set(),
    activeSubServices: new Set(),
    activeCustomers: new Set(),
  };

  async function refresh() {
    let payload;
    try {
      const r = await fetch("/api/dashboard", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      payload = await r.json();
    } catch (e) {
      document.getElementById("updated-label").textContent = "Error: " + e.message;
      return;
    }
    state.payload = payload;
    state.rows = payload.rows || [];
    state.impact = payload.impact || { by_customer: {}, by_outage: {} };
    
    render();
    restoreFilterState();
    applyFilters();
    nextRefreshTime = Date.now() + REFRESH_INTERVAL_MS;
  }

  function render() {
    const rows = state.rows;
    const impact = state.impact;

    if (state.payload && state.payload.brand_title) {
      state.brandTitle = state.payload.brand_title;
      const h1 = document.getElementById("app-brand-title");
      if (h1) h1.textContent = state.brandTitle;
      document.title = state.brandTitle;
    }

    document.getElementById("updated-label").textContent =
      "Last updated: " + (state.payload.generated_at || "").replace("T", " ") + " UTC";

    document.getElementById("visible-count").textContent =
      activeCount(rows) + " active · " + rows.length + " total";

    const lr = state.payload.last_run;
    const lrEl = document.getElementById("last-run-label");
    if (lrEl) {
      if (lr && lr.started_at) {
        const started = (lr.started_at || "").replace("T", " ").replace("Z", " UTC");
        const ok = Array.isArray(lr.providers) && lr.providers.every(p => p.ok);
        lrEl.textContent = "Last run: " + started + (ok ? " · ok" : " · with errors");
        lrEl.title = "Run " + (lr.run_id || "");
        lrEl.style.color = ok ? "var(--text-dim)" : "#fca5a5";
      } else {
        lrEl.textContent = "Last run: never";
        lrEl.style.color = "var(--text-dim)";
      }
    }

    renderServiceFilters(rows);
    renderSeverityFilters(rows);
    renderSubServiceFilters(subServiceCounts(rows));
    renderRegionFilters(serviceRegionCounts(rows));
    renderCustomerImpact(impact);
    renderCards(rows, impact);
    setupDescriptionToggles();
    renderAllOutagesView();
  }

  function renderServiceFilters(rows) {
    const counts = Object.fromEntries(SERVICES.map(s => [s, 0]));
    for (const r of rows) if (r.service && counts[r.service] != null) counts[r.service]++;
    const html = SERVICES.map(s => `
      <label class="filter-row">
        <input type="checkbox" class="flt-svc" value="${esc(s)}" checked>
        <span class="filter-label">${esc(s)}</span>
        <span class="filter-count">${counts[s]}</span>
      </label>`).join("");
    document.getElementById("svc-filters").innerHTML = html || '<div class="filter-row" style="color:var(--text-dim)">No services</div>';
  }

  function renderSeverityFilters(rows) {
    const counts = Object.fromEntries(SEVERITIES.map(s => [s, 0]));
    for (const r of rows) {
      const sev = (r.severity || "").toLowerCase();
      if (counts[sev] != null) counts[sev]++;
    }
    const html = SEVERITIES.map(s => `
      <label class="filter-row">
        <input type="checkbox" class="flt-sev" value="${esc(s)}" checked>
        <span class="sev-dot" style="background:${SEV_COLORS[s] || '#999'}"></span>
        <span class="filter-label">${esc(s)}</span>
        <span class="filter-count">${counts[s]}</span>
      </label>`).join("");
    document.getElementById("sev-filters").innerHTML = html || '<div class="filter-row" style="color:var(--text-dim)">No severities</div>';
  }

  // Region filter — checkbox rows (not chips). The sidebar is narrow
  // (220px default) so chip presentation wraps badly; checkbox rows
  // match the Service / Severity pattern and stay scannable.
  function renderRegionFilters(countsBySvc) {
    let html = "";
    const sortedSvcs = Array.from(countsBySvc.keys()).sort();
    for (const svc of sortedSvcs) {
      const svcCounts = countsBySvc.get(svc);
      if (!svcCounts || svcCounts.size === 0) continue;
      
      const sortedRegions = Array.from(svcCounts.entries()).sort(compareRegionEntries);
      
      html += `<div class="filter-group-header">${esc(svc.toUpperCase())}</div>`;
      html += sortedRegions.map(([region, count]) => {
        const val = `${svc}:${region}`;
        return `
          <label class="filter-row filter-row--nested">
            <input type="checkbox" class="flt-region" value="${esc(val)}" checked>
            <span class="filter-label">${esc(region)}</span>
            <span class="filter-count">${count}</span>
          </label>`;
      }).join("");
    }
    document.getElementById("region-filters").innerHTML = html || '<div class="filter-row" style="color:var(--text-dim)">No regions</div>';
  }

  function renderSubServiceFilters(counts) {
    const el = document.getElementById("subsvc-filters");
    if (!el) return;
    const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const html = sorted.map(([svc, count]) => `
      <label class="filter-row">
        <input type="checkbox" class="flt-subsvc" value="${esc(svc)}" checked>
        <span class="filter-label">${esc(svc)}</span>
        <span class="filter-count">${count}</span>
      </label>`).join("");
    el.innerHTML = html || '<div class="filter-row" style="color:var(--text-dim)">No sub-services</div>';
  }

  let customerSearchQuery = "";
  let customerImpactCollapsed = false;

  function renderCustomerImpact(impact) {
    const by = (impact && impact.by_customer) || {};
    const allNames = Object.keys(by).sort((a, b) => (by[b].total || 0) - (by[a].total || 0) || a.localeCompare(b));
    updateCustomerFilterPill();

    const summaryEl = document.getElementById("cust-impact-summary");
    if (summaryEl) {
      summaryEl.textContent = `${allNames.length} affected`;
    }

    const q = (customerSearchQuery || "").trim().toLowerCase();
    const names = q ? allNames.filter(n => n.toLowerCase().includes(q)) : allNames;

    const html = names.map(name => {
      const entry = by[name] || {};
      const svcCounts = entry.by_service || {};
      const breakdown = Object.entries(svcCounts)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([svc, n]) => `<span class="chip-mini chip-mini--inline">${esc(svc)} ${n}</span>`)
        .join(" ");
      
      const sevCounts = entry.by_severity || {};
      let topSev = "info";
      if (sevCounts.critical > 0) topSev = "critical";
      else if (sevCounts.warning > 0) topSev = "warning";
      else if (sevCounts.resolved > 0 && !sevCounts.info) topSev = "resolved";

      const sevBadges = SEVERITIES.map(sev => {
        const count = sevCounts[sev] || 0;
        if (count === 0) return "";
        return `<span class="customer-sev-badge customer-sev-badge--${sev}" title="${count} ${sev} incident${count === 1 ? '' : 's'}">${count}</span>`;
      }).join("");
      const sevBadgesHtml = `<span class="customer-sev-badges">${sevBadges}</span>`;

      const isActive = state.activeCustomers.has(name);
      return `
        <button type="button" class="customer-tile customer-row--filter${isActive ? " active" : ""}"
                data-customer="${esc(name)}"
                aria-pressed="${isActive ? "true" : "false"}"
                title="Click to ${isActive ? "remove" : "add"} ${esc(name)} from the outage filter">
          <div class="customer-tile-top">
            <span class="customer-tile-name" title="${esc(name)}">${esc(name)}</span>
            <span class="customer-tile-total customer-tile-total--${topSev}" title="Total active impact">${entry.total || 0}</span>
          </div>
          <div class="customer-tile-bottom">
            <span class="customer-tile-services">${breakdown || "Impacted"}</span>
            ${sevBadgesHtml}
          </div>
        </button>`;
    }).join("");

    const list = document.getElementById("customer-list");
    if (list) {
      if (allNames.length === 0) {
        list.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px">No customer impact</div>';
      } else if (names.length === 0) {
        list.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:8px">No customers matching "${esc(q)}"</div>`;
      } else {
        list.innerHTML = html;
      }

      list.querySelectorAll(".customer-row--filter").forEach(btn => {
        btn.addEventListener("click", () => {
          const name = btn.dataset.customer;
          if (state.activeCustomers.has(name)) {
            state.activeCustomers.delete(name);
            btn.classList.remove("active");
            btn.setAttribute("aria-pressed", "false");
          } else {
            state.activeCustomers.add(name);
            btn.classList.add("active");
            btn.setAttribute("aria-pressed", "true");
          }
          updateCustomerFilterPill();
          saveFilterState();
          applyFilters();
        });
      });
    }
  }

  function bindCustomerImpactControls() {
    const searchInput = document.getElementById("cust-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        customerSearchQuery = e.target.value;
        if (state.impact) renderCustomerImpact(state.impact);
      });
    }

    const selectAllBtn = document.getElementById("cust-select-all-btn");
    if (selectAllBtn) {
      selectAllBtn.addEventListener("click", () => {
        const by = (state.impact && state.impact.by_customer) || {};
        const q = (customerSearchQuery || "").trim().toLowerCase();
        for (const name of Object.keys(by)) {
          if (!q || name.toLowerCase().includes(q)) {
            state.activeCustomers.add(name);
          }
        }
        updateCustomerFilterPill();
        syncCustomerFilterRows();
        saveFilterState();
        applyFilters();
      });
    }

    const clearBtn = document.getElementById("cust-clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        state.activeCustomers.clear();
        updateCustomerFilterPill();
        syncCustomerFilterRows();
        saveFilterState();
        applyFilters();
      });
    }

    const collapseBtn = document.getElementById("cust-collapse-btn");
    const container = document.getElementById("customer-grid-container");
    if (collapseBtn && container) {
      collapseBtn.addEventListener("click", () => {
        customerImpactCollapsed = !customerImpactCollapsed;
        container.classList.toggle("collapsed", customerImpactCollapsed);
        collapseBtn.textContent = customerImpactCollapsed ? "▼" : "▲";
        collapseBtn.setAttribute("aria-expanded", String(!customerImpactCollapsed));
      });
    }
  }

  // Updates the "N customer(s)" pill next to the "Customer impact" heading.
  // Called from renderCustomerImpact (after a full render) and from the
  // click handler (after a toggle) so the pill stays in sync regardless
  // of which path mutated activeCustomers. (ISS-026)
  function updateCustomerFilterPill() {
    const pill = document.getElementById("cust-filter-pill");
    if (!pill) return;
    const n = state.activeCustomers.size;
    if (n > 0) {
      pill.textContent = n + " customer" + (n === 1 ? "" : "s");
      pill.hidden = false;
    } else {
      pill.hidden = true;
    }
  }

  // Sync the .active class and aria-pressed on every customer filter row
  // to the current state.activeCustomers. Called after bulk state changes
  // (Reset, restoreFilterState) where re-rendering the whole list would
  // be wasteful or could disrupt focus.
  function syncCustomerFilterRows() {
    document.querySelectorAll(".customer-row--filter").forEach(btn => {
      const name = btn.dataset.customer;
      const isActive = state.activeCustomers.has(name);
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
      btn.title = "Click to " + (isActive ? "remove" : "add") + " " + name + " from the outage filter";
    });
    updateCustomerFilterPill();
  }

  // Group the outage cards by service. Each group is a collapsible
  // <details> block — expanded by default if the group has any
  // non-resolved outage, collapsed otherwise. With 336 Zscaler rows
  // and 59 Cloudflare rows in the live dataset, a flat list is
  // impossible to scan. Operators usually only care about one or two
  // services at a time, so grouping matches the mental model. (TODO #22)
  function renderCards(rows, impact) {
    const groups = new Map();
    for (const r of rows) {
      const svc = (r.service || "").toLowerCase() || "other";
      if (!groups.has(svc)) groups.set(svc, []);
      groups.get(svc).push(r);
    }
    const sortedSvcs = Array.from(groups.keys()).sort();
    if (sortedSvcs.length === 0) {
      document.getElementById("card-list").innerHTML =
        '<div class="empty"><div class="big">&#10003;</div><div>No outages</div></div>';
      return;
    }
    const html = sortedSvcs.map(svc => {
      const groupRows = groups.get(svc);
      groupRows.sort((a, b) =>
        (Date.parse(b.published_at) || 0) - (Date.parse(a.published_at) || 0)
      );
      
      const counts = { critical: 0, warning: 0, info: 0, resolved: 0 };
      for (const r of groupRows) {
        const sev = (r.severity || "info").toLowerCase();
        if (sev in counts) {
          counts[sev]++;
        } else {
          counts.info++;
        }
      }
      
      const active = counts.critical + counts.warning + counts.info;
      const hasActive = active > 0;

      const parts = [];
      if (counts.critical > 0) {
        parts.push(`<span style="color:${SEV_COLORS.critical};font-weight:600">${counts.critical} critical</span>`);
      }
      if (counts.warning > 0) {
        parts.push(`<span style="color:${SEV_COLORS.warning};font-weight:600">${counts.warning} warning</span>`);
      }
      if (counts.info > 0) {
        parts.push(`<span style="color:${SEV_COLORS.info};font-weight:600">${counts.info} info</span>`);
      }
      if (counts.resolved > 0) {
        parts.push(`<span style="color:${SEV_COLORS.resolved}">${counts.resolved} resolved</span>`);
      }
      const countsHtml = parts.length > 0 ? parts.join(" · ") : "0 total";

      const cardsHtml = groupRows.map(r => renderCard(r, impact)).join("");
      return `
        <details class="card-group" data-service-group="${esc(svc)}" ${hasActive ? "open" : ""}>
          <summary>
            <span class="card-group-name">${esc(svc.toUpperCase())}</span>
            <span class="card-group-counts">${countsHtml}</span>
          </summary>
          <div class="card-group-cards">${cardsHtml}</div>
        </details>`;
    }).join("");
    document.getElementById("card-list").innerHTML = html;
  }

  // Build a single card article. Kept as a helper so renderCards can
  // stay focused on grouping. (TODO #22)
  function renderCard(r, impact) {
    const sev = (r.severity || "info").toLowerCase();
    const color = SEV_COLORS[sev] || SEV_COLORS.info;
    const regionChips = (r.regions || []).map(rg => `<span class="chip-mini">${esc(rg)}</span>`).join("");
    const customerChips = customersForRow(r, impact).map(c =>
      `<span class="chip-mini chip-customer">${esc(c)}</span>`).join("");
    const link = r.link ? `<a class="card-link" href="${esc(r.link)}" target="_blank" rel="noopener">status page →</a>` : "";
    const desc = r.description || "";
    return `
      <article class="card sev-${esc(sev)}"
               data-service="${esc(r.service || "")}"
               data-severity="${esc(sev)}"
               data-regions="${esc((r.regions || []).join("|"))}"
               data-sub-services="${esc((r.sub_services || []).join("|"))}"
               data-customers="${esc(customersForRow(r, impact).join("|"))}">
        <div class="card-sev-bar" style="background:${color}"></div>
        <div class="card-body">
          <div class="card-head">
            <span class="card-service">${esc((r.service || "").toUpperCase())}</span>
            ${r.raw_id ? `<span class="card-id">#${esc(r.raw_id)}</span>` : ""}
            <span class="card-sev" style="color:${color}">${esc(sev)}</span>
            <span class="card-time">${esc(relTime(r.published_at))}</span>
          </div>
          <h3 class="card-title">${esc(r.title || "")}</h3>
          ${desc ? `<p class="card-desc">${esc(desc)}</p>` : ""}
          <div class="card-chips">${regionChips}${customerChips}</div>
          ${link}
        </div>
      </article>`;
  }

  // Post-render pass: attach a "Show more" / "Show less" toggle to any
  // card description whose content overflows the CSS -webkit-line-clamp
  // (currently 2 lines). Short descriptions that fit in 1 line get no
  // button at all, so card heights stay compact. The CSS `.card-desc`
  // uses -webkit-line-clamp: 2 — `.expanded` switches to `display: block`
  // and unsets the clamp, so layout flows naturally when expanded.
  function setupDescriptionToggles() {
    const descs = document.querySelectorAll(".card-desc");
    descs.forEach(desc => {
      if (desc.scrollHeight <= desc.clientHeight + 1) return;
      if (desc.nextElementSibling && desc.nextElementSibling.classList.contains("card-more")) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card-more";
      btn.textContent = "Show more";
      btn.setAttribute("aria-expanded", "false");
      btn.addEventListener("click", () => {
        const expanded = desc.classList.toggle("expanded");
        btn.textContent = expanded ? "Show less" : "Show more";
        btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
      desc.insertAdjacentElement("afterend", btn);
    });
  }

  // Single-source-of-truth filter predicate. Used by the main apply
  // loop and by the empty-state branch in applyFilters (ISS-027).
  // Resolved rows are filtered via the regular Severity checkbox — there
  // is no separate "show resolved" toggle.
  function cardMatchesActiveFilters(card, ctx) {
    const svc = card.dataset.service;
    const sev = card.dataset.severity;
    const cardRegions = new Set((card.dataset.regions || "").split("|").filter(Boolean));
    const cardSubs = new Set((card.dataset.subServices || "").split("|").filter(Boolean));
    const cardCusts = new Set((card.dataset.customers || "").split("|").filter(Boolean));
    const regionOk = ctx.regions.size === 0
      || cardRegions.size === 0
      || Array.from(cardRegions).some(rg => ctx.regions.has(`${svc}:${rg}`));
    // Sub-service filter (additive semantic):
    //   * No checkboxes ticked  → no filter (all rows pass)
    //   * Card has no sub_services (e.g. cloudflare rows, or generic
    //     zscaler maintenance events) → passes regardless of selection
    //   * Card has sub_services → must intersect the selected set
    // This means "all 4 sub-service checkboxes ticked" is a no-op (the
    // user is saying "I'm interested in sub-service filtering" but
    // hasn't narrowed anything), and unchecking a box narrows by
    // excluding rows that have *only* the unchecked sub-service.
    const subsvcOk = ctx.subServices.size === 0
      || cardSubs.size === 0
      || Array.from(ctx.subServices).some(s => cardSubs.has(s));
    // customer filter: empty set = no filter; non-empty = card's customer
    // list must overlap with the selected set. (ISS-026/029: customer
    // filtering is now click-to-toggle, default empty.)
    const custOk = state.activeCustomers.size === 0
      || (cardCusts.size > 0 && Array.from(state.activeCustomers).some(x => cardCusts.has(x)));
    return ctx.svcs.has(svc) && ctx.sevs.has(sev) && regionOk && subsvcOk && custOk;
  }

  function applyFilters() {
    const ctx = {
      svcs: new Set(Array.from(document.querySelectorAll(".flt-svc:checked")).map(c => c.value)),
      sevs: new Set(Array.from(document.querySelectorAll(".flt-sev:checked")).map(c => c.value)),
      regions: new Set(Array.from(document.querySelectorAll(".flt-region:checked")).map(c => c.value)),
      subServices: new Set(Array.from(document.querySelectorAll(".flt-subsvc:checked")).map(c => c.value)),
    };
    const cards = Array.from(document.querySelectorAll(".card"));
    let visible = 0;
    for (const c of cards) {
      const show = cardMatchesActiveFilters(c, ctx);
      c.classList.toggle("hidden", !show);
      if (show) visible++;
    }
    // Hide any service group whose cards all got filtered out — keeps
    // the Outages list from filling with empty collapsible groups when
    // the operator narrows by service / region / sub-service.
    // (TODO #22)
    document.querySelectorAll(".card-group").forEach(g => {
      const groupCards = g.querySelectorAll(".card");
      const anyVisible = Array.from(groupCards).some(c => !c.classList.contains("hidden"));
      g.classList.toggle("group-hidden", !anyVisible);
    });
    document.getElementById("visible-count").textContent =
      visible + " of " + cards.length;
    const listEl = document.getElementById("card-list");
    if (cards.length > 0 && visible === 0) {
      // Check whether the would-be-visible set (treating "resolved" as
      // eligible under Severity) is entirely resolved. If so, the helpful
      // "all resolved" hint is more useful than the generic "no match"
      // message. (ISS-027)
      const wouldBeVisible = cards.filter(c => {
        const relaxed = { ...ctx, sevs: new Set([...ctx.sevs, "resolved"]) };
        return cardMatchesActiveFilters(c, relaxed);
      });
      const allResolved = wouldBeVisible.length > 0
        && wouldBeVisible.every(c => c.dataset.severity === "resolved");
      const msg = allResolved
        ? `<div class="empty" id="empty-filters"><div class="big">&#10003;</div><div>All outages are resolved. Tick 'resolved' under Severity to display them.</div></div>`
        : `<div class="empty" id="empty-filters"><div class="big">&#128269;</div><div>No outages match current filters</div></div>`;
      listEl.insertAdjacentHTML("beforeend", msg);
    } else {
      const e = document.getElementById("empty-filters");
      if (e) e.remove();
    }
  }

  function saveFilterState() {
    try {
      const s = {
        svcs: Array.from(document.querySelectorAll(".flt-svc")).map(c => ({ v: c.value, checked: c.checked })),
        sevs: Array.from(document.querySelectorAll(".flt-sev")).map(c => ({ v: c.value, checked: c.checked })),
        regions: Array.from(document.querySelectorAll(".flt-region")).map(c => ({ v: c.value, checked: c.checked })),
        sub_services: Array.from(document.querySelectorAll(".flt-subsvc")).map(c => ({ v: c.value, checked: c.checked })),
        customers: Array.from(state.activeCustomers),
      };
      localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(s));
    } catch (e) { /* ignore */ }
  }

  function restoreFilterState() {
    try {
      const raw = localStorage.getItem(FILTER_STORAGE_KEY);
      if (!raw) return;
      const s = JSON.parse(raw);
      (s.svcs || []).forEach(({ v, checked }) => {
        const el = document.querySelector('.flt-svc[value="' + CSS.escape(v) + '"]');
        if (el) el.checked = checked;
      });
      (s.sevs || []).forEach(({ v, checked }) => {
        const el = document.querySelector('.flt-sev[value="' + CSS.escape(v) + '"]');
        if (el) el.checked = checked;
      });
      (s.regions || []).forEach(({ v, checked }) => {
        const el = document.querySelector('.flt-region[value="' + CSS.escape(v) + '"]');
        if (el) el.checked = checked;
      });
      (s.sub_services || []).forEach(({ v, checked }) => {
        const el = document.querySelector('.flt-subsvc[value="' + CSS.escape(v) + '"]');
        if (el) el.checked = checked;
      });
      // Customer filter: a Set of customer names. Empty set = no filter.
      // The .active class is reapplied on every render via
      // renderCustomerImpact, so no DOM touch needed here. (ISS-029)
      state.activeCustomers = new Set(s.customers || []);
    } catch (e) { /* ignore */ }
  }

  // Bind filter changes once the layout is in place.
  document.addEventListener("change", e => {
    if (e.target && (e.target.classList.contains("flt-svc") ||
                     e.target.classList.contains("flt-sev") ||
                     e.target.classList.contains("flt-region") ||
                     e.target.classList.contains("flt-subsvc"))) {
      saveFilterState();
      applyFilters();
    }
  });

  // ===== All Outages Tab State & Logic =============================
  const allOutagesState = {
    search: "",
    selectedServices: new Set(SERVICES),
    selectedSeverities: new Set(SEVERITIES),
    customer: "",
    sortBy: "published_at",
    sortDir: "desc",
    page: 1,
    pageSize: 50,
    expandedRows: new Set(),
  };

  function bindAllOutagesEvents() {
    const searchInput = document.getElementById("all-search");
    if (searchInput) {
      searchInput.addEventListener("input", e => {
        allOutagesState.search = e.target.value.trim().toLowerCase();
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    // Provider dropdown toggle
    const svcBtn = document.getElementById("all-svc-btn");
    const svcMenu = document.getElementById("all-svc-menu");
    const sevBtn = document.getElementById("all-sev-btn");
    const sevMenu = document.getElementById("all-sev-menu");

    if (svcBtn && svcMenu) {
      svcBtn.addEventListener("click", e => {
        e.stopPropagation();
        const willOpen = svcMenu.classList.contains("hidden");
        svcMenu.classList.toggle("hidden", !willOpen);
        svcBtn.classList.toggle("open", willOpen);
        if (sevMenu) {
          sevMenu.classList.add("hidden");
          if (sevBtn) sevBtn.classList.remove("open");
        }
      });
      svcMenu.addEventListener("click", e => e.stopPropagation());
    }

    // Severity dropdown toggle
    if (sevBtn && sevMenu) {
      sevBtn.addEventListener("click", e => {
        e.stopPropagation();
        const willOpen = sevMenu.classList.contains("hidden");
        sevMenu.classList.toggle("hidden", !willOpen);
        sevBtn.classList.toggle("open", willOpen);
        if (svcMenu) {
          svcMenu.classList.add("hidden");
          if (svcBtn) svcBtn.classList.remove("open");
        }
      });
      sevMenu.addEventListener("click", e => e.stopPropagation());
    }

    // Close dropdowns on click outside or escape
    document.addEventListener("click", () => {
      if (svcMenu) {
        svcMenu.classList.add("hidden");
        if (svcBtn) svcBtn.classList.remove("open");
      }
      if (sevMenu) {
        sevMenu.classList.add("hidden");
        if (sevBtn) sevBtn.classList.remove("open");
      }
    });

    document.addEventListener("keydown", e => {
      if (e.key === "Escape") {
        if (svcMenu) {
          svcMenu.classList.add("hidden");
          if (svcBtn) svcBtn.classList.remove("open");
        }
        if (sevMenu) {
          sevMenu.classList.add("hidden");
          if (sevBtn) sevBtn.classList.remove("open");
        }
      }
    });

    // Provider search within dropdown
    const svcSearch = document.getElementById("all-svc-search");
    if (svcSearch) {
      svcSearch.addEventListener("input", e => {
        const query = e.target.value.trim().toLowerCase();
        const items = document.querySelectorAll("#all-svc-list .svc-item");
        items.forEach(it => {
          const s = it.dataset.svc || "";
          it.style.display = (!query || s.includes(query)) ? "flex" : "none";
        });
      });
    }

    // Provider "All Providers" master checkbox
    const svcCheckAll = document.getElementById("all-svc-check-all");
    if (svcCheckAll) {
      svcCheckAll.addEventListener("change", e => {
        if (e.target.checked) {
          allOutagesState.selectedServices = new Set(SERVICES);
        } else {
          allOutagesState.selectedServices.clear();
        }
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    // Provider "Select all" / "Clear" buttons
    const svcSelectAllBtn = document.getElementById("all-svc-select-all");
    if (svcSelectAllBtn) {
      svcSelectAllBtn.addEventListener("click", () => {
        allOutagesState.selectedServices = new Set(SERVICES);
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }
    const svcClearAllBtn = document.getElementById("all-svc-clear-all");
    if (svcClearAllBtn) {
      svcClearAllBtn.addEventListener("click", () => {
        allOutagesState.selectedServices.clear();
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    // Severity "All Statuses" master checkbox
    const sevCheckAll = document.getElementById("all-sev-check-all");
    if (sevCheckAll) {
      sevCheckAll.addEventListener("change", e => {
        if (e.target.checked) {
          allOutagesState.selectedSeverities = new Set(SEVERITIES);
        } else {
          allOutagesState.selectedSeverities.clear();
        }
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    // Severity presets inside dropdown
    const sevPresetCritWarn = document.getElementById("all-sev-preset-crit-warn");
    if (sevPresetCritWarn) {
      sevPresetCritWarn.addEventListener("click", () => {
        allOutagesState.selectedSeverities = new Set(["critical", "warning"]);
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    const sevPresetActive = document.getElementById("all-sev-preset-active");
    if (sevPresetActive) {
      sevPresetActive.addEventListener("click", () => {
        allOutagesState.selectedSeverities = new Set(["critical", "warning", "info"]);
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    const sevSelectAll = document.getElementById("all-sev-select-all");
    if (sevSelectAll) {
      sevSelectAll.addEventListener("click", () => {
        allOutagesState.selectedSeverities = new Set(SEVERITIES);
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    // Quick toolbar button: [⚠️ Critical & Warning]
    const quickCritWarnBtn = document.getElementById("all-quick-crit-warn");
    if (quickCritWarnBtn) {
      quickCritWarnBtn.addEventListener("click", () => {
        const isCurrentlyCritWarn = (
          allOutagesState.selectedSeverities.size === 2 &&
          allOutagesState.selectedSeverities.has("critical") &&
          allOutagesState.selectedSeverities.has("warning") &&
          allOutagesState.selectedServices.size === SERVICES.length
        );
        if (isCurrentlyCritWarn) {
          allOutagesState.selectedSeverities = new Set(SEVERITIES);
        } else {
          allOutagesState.selectedServices = new Set(SERVICES);
          allOutagesState.selectedSeverities = new Set(["critical", "warning"]);
        }
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    const custFilter = document.getElementById("all-cust-filter");
    if (custFilter) {
      custFilter.addEventListener("change", e => {
        allOutagesState.customer = e.target.value;
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    const pageSizeSelect = document.getElementById("all-page-size");
    if (pageSizeSelect) {
      pageSizeSelect.addEventListener("change", e => {
        const val = e.target.value;
        allOutagesState.pageSize = val === "all" ? 999999 : (parseInt(val, 10) || 50);
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    }

    const prevBtn = document.getElementById("all-page-prev");
    if (prevBtn) {
      prevBtn.addEventListener("click", () => {
        if (allOutagesState.page > 1) {
          allOutagesState.page--;
          renderAllOutagesView();
          const wrap = document.getElementById("all-table-wrap");
          if (wrap) wrap.scrollTop = 0;
        }
      });
    }

    const nextBtn = document.getElementById("all-page-next");
    if (nextBtn) {
      nextBtn.addEventListener("click", () => {
        allOutagesState.page++;
        renderAllOutagesView();
        const wrap = document.getElementById("all-table-wrap");
        if (wrap) wrap.scrollTop = 0;
      });
    }
  }

  function formatAllOutageTime(isoStr) {
    if (!isoStr) return "—";
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return String(isoStr);
    const pad = n => String(n).padStart(2, "0");
    const yr = d.getUTCFullYear();
    const mo = pad(d.getUTCMonth() + 1);
    const day = pad(d.getUTCDate());
    const hr = pad(d.getUTCHours());
    const min = pad(d.getUTCMinutes());
    return `${yr}-${mo}-${day} ${hr}:${min} UTC`;
  }

  function renderAllOutagesView() {
    const wrap = document.getElementById("all-table-wrap");
    if (!wrap) return;

    const rows = state.rows || [];
    const impact = state.impact || { by_customer: {}, by_outage: {} };

    // Update Provider dropdown UI
    const svcBtnLabel = document.getElementById("all-svc-btn-label");
    const svcCountSelected = allOutagesState.selectedServices.size;
    if (svcBtnLabel) {
      if (svcCountSelected === SERVICES.length) {
        svcBtnLabel.textContent = `Providers: All (${SERVICES.length})`;
      } else if (svcCountSelected === 0) {
        svcBtnLabel.textContent = "Providers: None (0)";
      } else if (svcCountSelected === 1) {
        svcBtnLabel.textContent = `Provider: ${Array.from(allOutagesState.selectedServices)[0].toUpperCase()}`;
      } else if (svcCountSelected <= 2) {
        svcBtnLabel.textContent = `Providers: ${Array.from(allOutagesState.selectedServices).map(s => s.toUpperCase()).join(", ")}`;
      } else {
        svcBtnLabel.textContent = `Providers: ${svcCountSelected} of ${SERVICES.length}`;
      }
    }

    const svcCheckAll = document.getElementById("all-svc-check-all");
    if (svcCheckAll) {
      svcCheckAll.checked = (svcCountSelected === SERVICES.length);
      svcCheckAll.indeterminate = (svcCountSelected > 0 && svcCountSelected < SERVICES.length);
    }

    // Populate provider items list
    const svcListEl = document.getElementById("all-svc-list");
    if (svcListEl) {
      const counts = Object.fromEntries(SERVICES.map(s => [s, 0]));
      for (const r of rows) if (r.service && counts[r.service] != null) counts[r.service]++;

      svcListEl.innerHTML = SERVICES.map(s => {
        const count = counts[s] || 0;
        const color = PROVIDER_COLORS[s] || "#38bdf8";
        const checked = allOutagesState.selectedServices.has(s);
        return `
          <label class="multi-menu-item svc-item" data-svc="${esc(s)}">
            <input type="checkbox" class="all-svc-cb" value="${esc(s)}" ${checked ? "checked" : ""}>
            <span class="badge-service" style="color:${color}; background:${color}18; border:1px solid ${color}44;">
              ${esc(s.toUpperCase())}
            </span>
            <span class="multi-item-count">${count}</span>
          </label>
        `;
      }).join("");

      // Bind individual provider checkbox changes
      svcListEl.querySelectorAll(".all-svc-cb").forEach(cb => {
        cb.addEventListener("change", e => {
          e.stopPropagation();
          const svcVal = cb.value;
          if (cb.checked) {
            allOutagesState.selectedServices.add(svcVal);
          } else {
            allOutagesState.selectedServices.delete(svcVal);
          }
          allOutagesState.page = 1;
          renderAllOutagesView();
        });
      });
    }

    // Update Severity dropdown UI
    const sevBtnLabel = document.getElementById("all-sev-btn-label");
    const sevCountSelected = allOutagesState.selectedSeverities.size;
    if (sevBtnLabel) {
      if (sevCountSelected === SEVERITIES.length) {
        sevBtnLabel.textContent = `Status: All (${SEVERITIES.length})`;
      } else if (sevCountSelected === 0) {
        sevBtnLabel.textContent = "Status: None (0)";
      } else if (sevCountSelected === 2 && allOutagesState.selectedSeverities.has("critical") && allOutagesState.selectedSeverities.has("warning")) {
        sevBtnLabel.textContent = "Status: Critical, Warning";
      } else if (sevCountSelected === 1) {
        sevBtnLabel.textContent = `Status: ${Array.from(allOutagesState.selectedSeverities)[0].toUpperCase()}`;
      } else {
        sevBtnLabel.textContent = `Status: ${sevCountSelected} selected`;
      }
    }

    const sevCheckAll = document.getElementById("all-sev-check-all");
    if (sevCheckAll) {
      sevCheckAll.checked = (sevCountSelected === SEVERITIES.length);
      sevCheckAll.indeterminate = (sevCountSelected > 0 && sevCountSelected < SEVERITIES.length);
    }

    // Populate severity items list
    const sevListEl = document.getElementById("all-sev-list");
    if (sevListEl) {
      const sevCounts = Object.fromEntries(SEVERITIES.map(s => [s, 0]));
      for (const r of rows) {
        const s = (r.severity || "info").toLowerCase();
        if (sevCounts[s] != null) sevCounts[s]++;
      }

      sevListEl.innerHTML = SEVERITIES.map(sev => {
        const count = sevCounts[sev] || 0;
        const color = SEV_COLORS[sev] || SEV_COLORS.info;
        const checked = allOutagesState.selectedSeverities.has(sev);
        return `
          <label class="multi-menu-item sev-item" data-sev="${esc(sev)}">
            <input type="checkbox" class="all-sev-cb" value="${esc(sev)}" ${checked ? "checked" : ""}>
            <span class="badge-sev" style="color:${color}; background:${color}18; border:1px solid ${color}44;">
              ${esc(sev.toUpperCase())}
            </span>
            <span class="multi-item-count">${count}</span>
          </label>
        `;
      }).join("");

      // Bind individual severity checkbox changes
      sevListEl.querySelectorAll(".all-sev-cb").forEach(cb => {
        cb.addEventListener("change", e => {
          e.stopPropagation();
          const sevVal = cb.value;
          if (cb.checked) {
            allOutagesState.selectedSeverities.add(sevVal);
          } else {
            allOutagesState.selectedSeverities.delete(sevVal);
          }
          allOutagesState.page = 1;
          renderAllOutagesView();
        });
      });
    }

    // Update Quick Critical & Warning toolbar button state
    const quickCritWarnBtn = document.getElementById("all-quick-crit-warn");
    if (quickCritWarnBtn) {
      const isCritWarnActive = (
        allOutagesState.selectedSeverities.size === 2 &&
        allOutagesState.selectedSeverities.has("critical") &&
        allOutagesState.selectedSeverities.has("warning") &&
        allOutagesState.selectedServices.size === SERVICES.length
      );
      quickCritWarnBtn.classList.toggle("active", isCritWarnActive);
    }

    // Populate customer dropdown if needed
    const custFltEl = document.getElementById("all-cust-filter");
    if (custFltEl && custFltEl.options.length <= 2) {
      const custNames = Object.keys(impact.by_customer || {}).sort();
      const opts = [
        '<option value="">All Customers</option>',
        '<option value="_impacted">Impacted Customers Only</option>'
      ];
      for (const c of custNames) {
        const count = (impact.by_customer[c] || []).length;
        opts.push(`<option value="${esc(c)}">${esc(c)} (${count})</option>`);
      }
      custFltEl.innerHTML = opts.join("");
      custFltEl.value = allOutagesState.customer;
    }

    // Filter rows
    const needle = allOutagesState.search;
    const filterCust = allOutagesState.customer;

    const filtered = rows.filter(r => {
      const svc = (r.service || "").toLowerCase();
      const sev = (r.severity || "info").toLowerCase();
      const custs = customersForRow(r, impact);

      // Multi-select providers filter
      if (allOutagesState.selectedServices.size > 0 && !allOutagesState.selectedServices.has(svc)) {
        return false;
      }
      if (allOutagesState.selectedServices.size === 0) {
        return false;
      }

      // Multi-select severity filter
      if (allOutagesState.selectedSeverities.size > 0 && !allOutagesState.selectedSeverities.has(sev)) {
        return false;
      }
      if (allOutagesState.selectedSeverities.size === 0) {
        return false;
      }

      if (filterCust) {
        if (filterCust === "_impacted" && custs.length === 0) return false;
        if (filterCust !== "_impacted" && !custs.includes(filterCust)) return false;
      }

      if (needle) {
        const title = (r.title || "").toLowerCase();
        const desc = (r.description || "").toLowerCase();
        const rawId = (r.raw_id || "").toLowerCase();
        const regions = (r.regions || []).join(" ").toLowerCase();
        const subSvcs = (r.sub_services || []).join(" ").toLowerCase();
        const custsStr = custs.join(" ").toLowerCase();
        if (
          !title.includes(needle) &&
          !svc.includes(needle) &&
          !desc.includes(needle) &&
          !rawId.includes(needle) &&
          !regions.includes(needle) &&
          !subSvcs.includes(needle) &&
          !custsStr.includes(needle)
        ) {
          return false;
        }
      }
      return true;
    });

    // Sort rows
    const sortBy = allOutagesState.sortBy;
    const sortDir = allOutagesState.sortDir;
    const sevRank = { critical: 4, warning: 3, info: 2, resolved: 1 };

    filtered.sort((a, b) => {
      let cmp = 0;
      if (sortBy === "published_at") {
        cmp = (Date.parse(a.published_at) || 0) - (Date.parse(b.published_at) || 0);
      } else if (sortBy === "service") {
        cmp = (a.service || "").localeCompare(b.service || "");
      } else if (sortBy === "severity") {
        const rA = sevRank[(a.severity || "info").toLowerCase()] || 0;
        const rB = sevRank[(b.severity || "info").toLowerCase()] || 0;
        cmp = rA - rB;
      } else if (sortBy === "title") {
        cmp = (a.title || "").localeCompare(b.title || "");
      }
      return sortDir === "desc" ? -cmp : cmp;
    });

    // Pagination
    const totalCount = filtered.length;
    const pageSize = allOutagesState.pageSize;
    const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
    if (allOutagesState.page > totalPages) allOutagesState.page = totalPages;
    const page = allOutagesState.page;
    const startIdx = (page - 1) * pageSize;
    const pageRows = filtered.slice(startIdx, startIdx + pageSize);

    // Update Pagination UI
    const prevBtn = document.getElementById("all-page-prev");
    const nextBtn = document.getElementById("all-page-next");
    const pageIndicator = document.getElementById("all-page-indicator");
    if (prevBtn) prevBtn.disabled = page <= 1;
    if (nextBtn) nextBtn.disabled = page >= totalPages;
    if (pageIndicator) pageIndicator.textContent = `${page} / ${totalPages}`;

    const summaryEl = document.getElementById("all-count-summary");
    if (summaryEl) {
      if (totalCount === 0) {
        summaryEl.textContent = "No outages match the selected criteria.";
      } else {
        const fromNum = startIdx + 1;
        const toNum = Math.min(startIdx + pageSize, totalCount);
        summaryEl.textContent = `Showing ${fromNum}–${toNum} of ${totalCount.toLocaleString()} outages (${activeCount(rows)} active across 26 monitored services)`;
      }
    }

    if (totalCount === 0) {
      wrap.innerHTML = '<div class="empty"><div class="big">∅</div><div>No outages match the selected search and filter criteria.</div></div>';
      return;
    }

    // Sort indicators
    const indicatorFor = col => {
      if (sortBy !== col) return "";
      return `<span class="sort-indicator">${sortDir === "desc" ? "▼" : "▲"}</span>`;
    };

    const tableHtml = `
      <table class="table all-outages-table">
        <thead>
          <tr>
            <th class="sortable" data-sort="published_at" title="Click to sort by date">Date / Time (UTC)${indicatorFor("published_at")}</th>
            <th class="sortable" data-sort="service" title="Click to sort by service">Service${indicatorFor("service")}</th>
            <th class="sortable" data-sort="severity" title="Click to sort by severity">Severity${indicatorFor("severity")}</th>
            <th class="sortable" data-sort="title" title="Click to sort by title">Title & Summary${indicatorFor("title")}</th>
            <th>Regions / Sub-Services</th>
            <th>Customer Impact</th>
            <th style="text-align:right">Link</th>
          </tr>
        </thead>
        <tbody>
          ${pageRows.map((r, idx) => {
            const svc = (r.service || "").toLowerCase();
            const sev = (r.severity || "info").toLowerCase();
            const color = PROVIDER_COLORS[svc] || "#38bdf8";
            const sevColor = SEV_COLORS[sev] || SEV_COLORS.info;
            const timeFormatted = formatAllOutageTime(r.published_at);
            const timeAgoStr = relTime(r.published_at);
            const titleTooltip = r.published_at ? `${r.published_at} (${timeAgoStr})` : "";
            const custs = customersForRow(r, impact);
            const rowKey = r.raw_id || `${svc}-${r.published_at}-${idx}`;
            const isExpanded = allOutagesState.expandedRows.has(rowKey);

            const regionChips = (r.regions || []).map(rg => `<span class="chip-mini">${esc(rg)}</span>`).join("");
            const subChips = (r.sub_services || []).map(sb => `<span class="chip-mini" style="border:1px solid rgba(56,189,248,0.3)">${esc(sb)}</span>`).join("");
            const custChips = custs.length > 0
              ? custs.map(c => `<span class="chip-mini chip-customer">${esc(c)}</span>`).join("")
              : `<span style="color:var(--text-dim)">—</span>`;

            const linkHtml = r.link
              ? `<a class="card-link" href="${esc(r.link)}" target="_blank" rel="noopener" title="Open provider status page">↗</a>`
              : "";

            const hasDesc = !!(r.description && r.description.trim());

            return `
              <tr class="table-outage-row">
                <td class="table-time" title="${esc(titleTooltip)}">${esc(timeFormatted)}</td>
                <td>
                  <span class="badge-service" style="color:${color}; background:${color}18; border:1px solid ${color}44;">
                    ${esc(svc.toUpperCase())}
                  </span>
                </td>
                <td>
                  <span class="badge-sev" style="color:${sevColor}; background:${sevColor}18; border:1px solid ${sevColor}44;">
                    ${esc(sev)}
                  </span>
                </td>
                <td>
                  <div class="all-title-cell">
                    <div class="all-title-row">
                      <span class="all-title-text">${esc(r.title || "(Untitled incident)")}</span>
                      ${hasDesc ? `<button type="button" class="all-desc-toggle" data-row-key="${esc(rowKey)}">${isExpanded ? "Details ▲" : "Details ▼"}</button>` : ""}
                    </div>
                    ${hasDesc ? `
                      <div class="all-desc-box" id="desc-box-${esc(rowKey)}" ${isExpanded ? "" : "hidden"}>
                        <pre class="all-desc-pre">${esc(r.description)}</pre>
                        <div class="all-meta-tags">
                          ${r.raw_id ? `<span>ID: ${esc(r.raw_id)}</span>` : ""}
                          ${r.last_seen_at ? `<span>Last seen: ${esc(formatAllOutageTime(r.last_seen_at))}</span>` : ""}
                        </div>
                      </div>
                    ` : ""}
                  </div>
                </td>
                <td>
                  <div style="display:flex; flex-wrap:wrap; gap:3px;">
                    ${regionChips}
                    ${subChips}
                    ${!regionChips && !subChips ? '<span style="color:var(--text-dim)">—</span>' : ""}
                  </div>
                </td>
                <td>
                  <div style="display:flex; flex-wrap:wrap; gap:3px;">
                    ${custChips}
                  </div>
                </td>
                <td style="text-align:right; font-size:14px;">
                  ${linkHtml}
                </td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    `;

    wrap.innerHTML = tableHtml;

    // Attach sort click handlers
    wrap.querySelectorAll("th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const sortCol = th.dataset.sort;
        if (allOutagesState.sortBy === sortCol) {
          allOutagesState.sortDir = allOutagesState.sortDir === "desc" ? "asc" : "desc";
        } else {
          allOutagesState.sortBy = sortCol;
          allOutagesState.sortDir = (sortCol === "title" || sortCol === "service") ? "asc" : "desc";
        }
        allOutagesState.page = 1;
        renderAllOutagesView();
      });
    });

    // Attach description toggle click handlers
    wrap.querySelectorAll(".all-desc-toggle").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        const rowKey = btn.dataset.rowKey;
        const box = document.getElementById("desc-box-" + rowKey);
        if (!box) return;
        if (allOutagesState.expandedRows.has(rowKey)) {
          allOutagesState.expandedRows.delete(rowKey);
          box.setAttribute("hidden", "");
          btn.textContent = "Details ▼";
        } else {
          allOutagesState.expandedRows.add(rowKey);
          box.removeAttribute("hidden");
          btn.textContent = "Details ▲";
        }
      });
    });
  }

  // ----- Tabs + view switching -------------------------------------
  // Module-level view state. Mutated only by setView / restoreActiveView
  // so all callers can trust it as the source of truth.
  let currentView = "outages";
  let customersData = [];
  let regionsData = [];
  // Active edit modal handles. When non-null, the matching backdrop is
  // open and editorIsOpen() returns true. Cleared by closeCustomerEditor /
  // closeRegionEditor.
  let editCustomerModal = null;   // { mode: "create"|"edit", id?, data? } | null
  let editRegionModal = null;     // { mode, svc?, code?, data? } | null

  function bindTabs() {
    document.querySelectorAll("#tabs-nav .tab").forEach(btn => {
      btn.addEventListener("click", () => setView(btn.dataset.view));
    });
  }

  function setView(view) {
    if (!VIEWS.includes(view)) return;
    currentView = view;
    document.querySelectorAll("#tabs-nav .tab").forEach(b => {
      const active = b.dataset.view === view;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    for (const v of VIEWS) {
      const el = document.getElementById("view-" + v);
      if (el) el.hidden = (v !== view);
    }
    try { localStorage.setItem(VIEW_STORAGE_KEY, view); } catch (e) { /* ignore */ }
    if (view === "customers") void loadCustomers();
    else if (view === "regions") void loadRegions();
    else if (view === "graphs") void loadGraphsView();
    else if (view === "all") {
      if (!state.rows || state.rows.length === 0) void refresh();
      else renderAllOutagesView();
    }
    else if (view === "outages") void refresh();
  }

  function restoreActiveView() {
    try {
      const v = localStorage.getItem(VIEW_STORAGE_KEY);
      if (v && VIEWS.includes(v) && v !== "outages") {
        setView(v);
        // setView already called the loader, so just return.
        return;
      }
    } catch (e) { /* ignore */ }
    // Default: mark Outages active so the tab UI is correct on first
    // load, but don't trigger a redundant refresh — init() will do it.
    document.querySelectorAll("#tabs-nav .tab").forEach(b => {
      const active = b.dataset.view === "outages";
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  // ----- Toast + error helpers -------------------------------------
  function showToast(msg, kind) {
    const t = document.createElement("div");
    t.className = "toast toast-" + (kind || "info");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  // Render server validation errors in the form's error region.
  // `body` is the JSON response from a 4xx endpoint:
  //   {"error": "...", "details": [{"path": "...", "message": "..."}, ...]}
  // The first error is shown inline; the rest are inside a <details>
  // block so a customer with many bad regions doesn't fill the screen.
  function showFormErrors(prefix, body) {
    const firstEl = document.getElementById(prefix + "-form-error");
    const listEl = document.getElementById(prefix + "-form-errors");
    const details = (body && body.details) || [];
    if (details.length === 0) {
      if (firstEl) {
        let msg = (body && body.error) || "Save failed";
        if (body && body.detail) {
          msg += ": " + body.detail;
        }
        firstEl.textContent = msg;
        firstEl.hidden = false;
      }
      if (listEl) listEl.hidden = true;
      return;
    }
    const first = details[0];
    if (firstEl) {
      firstEl.textContent = (first.path ? first.path + ": " : "") + first.message;
      firstEl.hidden = false;
    }
    if (listEl) {
      if (details.length <= 1) {
        listEl.hidden = true;
      } else {
        listEl.innerHTML = "<summary>" + (details.length - 1) + " more error" +
          (details.length - 1 === 1 ? "" : "s") + "</summary><ul>" +
          details.slice(1).map(d => "<li><code>" + esc(d.path) + "</code>: " + esc(d.message) + "</li>").join("") +
          "</ul>";
        listEl.hidden = false;
      }
    }
  }

  function clearFormErrors(prefix) {
    const firstEl = document.getElementById(prefix + "-form-error");
    const listEl = document.getElementById(prefix + "-form-errors");
    if (firstEl) { firstEl.textContent = ""; firstEl.hidden = true; }
    if (listEl) { listEl.hidden = true; }
  }

  function editorIsOpen() {
    return !!editCustomerModal || !!editRegionModal;
  }

  // ----- Customers tab --------------------------------------------
  async function loadCustomers() {
    const list = document.getElementById("cust-list");
    if (!list) return;
    list.innerHTML = '<div style="color:var(--text-dim);font-size:12px">Loading…</div>';
    try {
      const [rCusts, rRegs] = await Promise.all([
        fetch("/api/customers", { cache: "no-store" }),
        fetch("/api/regions", { cache: "no-store" })
      ]);
      if (!rCusts.ok) throw new Error("HTTP " + rCusts.status);
      if (!rRegs.ok) throw new Error("HTTP " + rRegs.status);
      const [bodyCusts, bodyRegs] = await Promise.all([
        rCusts.json(),
        rRegs.json()
      ]);
      customersData = Array.isArray(bodyCusts) ? bodyCusts : [];
      regionsData = (bodyRegs && bodyRegs.regions) || [];
      renderCustomerList();
    } catch (e) {
      list.innerHTML = '<div class="empty"><div class="big">!</div><div>Failed to load customers: ' + esc(e.message) + '</div></div>';
    }
  }

  function renderCustomerList() {
    const list = document.getElementById("cust-list");
    const search = (document.getElementById("cust-search") || {}).value || "";
    const needle = search.trim().toLowerCase();
    const rows = customersData
      .slice()
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
      .filter(c => {
        if (!needle) return true;
        if ((c.name || "").toLowerCase().includes(needle) || (c.customer_id || "").toLowerCase().includes(needle)) {
          return true;
        }
        const subs = c.subscriptions || {};
        for (const [svc, regs] of Object.entries(subs)) {
          if (svc.toLowerCase().includes(needle)) return true;
          if ((regs || []).some(r => String(r).toLowerCase().includes(needle))) return true;
        }
        const subSvcs = c.sub_services || {};
        for (const [svc, codes] of Object.entries(subSvcs)) {
          if ((codes || []).some(cd => String(cd).toLowerCase().includes(needle))) return true;
        }
        return false;
      });
    if (rows.length === 0) {
      list.innerHTML = '<div class="empty"><div class="big">' +
        (customersData.length === 0 ? "+" : "∅") + '</div><div>' +
        (customersData.length === 0 ? "No customers yet. Click “Add customer” to create one." : "No customers match your filter.") +
        '</div></div>';
      return;
    }
    list.innerHTML = rows.map(renderCustomerRow).join("");
    list.querySelectorAll("[data-cust-edit]").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.custEdit;
        const c = customersData.find(x => x.customer_id === id);
        if (c) openCustomerEditor("edit", c);
      });
    });
    list.querySelectorAll("[data-cust-del]").forEach(btn => {
      btn.addEventListener("click", () => deleteCustomer(btn.dataset.custDel));
    });
  }

  function renderCustomerRow(c) {
    const subs = c.subscriptions || {};
    const subSvcs = c.sub_services || {};
    const subKeys = Object.keys(subs).sort();
    const subsHtml = subKeys.length === 0
      ? '<span style="color:var(--text-dim);font-size:11px">No subscriptions</span>'
      : subKeys.map(svc => {
          const regs = (subs[svc] || []).map(r =>
            '<span class="chip">' + esc(r) + '</span>'
          ).join(" ");
          // Sub-services are shown only when the customer has any
          // non-empty ones for this service — empty (the default
          // "match all sub-services") is invisible to keep the row
          // compact. (TODO #21)
          const svcSubs = (subSvcs[svc] || []);
          const subsChips = svcSubs.length === 0 ? "" :
            '<span class="chip-label" style="margin-left:8px">sub:</span>' +
            svcSubs.map(s => '<span class="chip chip-subsvc">' + esc(s) + '</span>').join(" ");
          return '<span class="cust-sub-group"><span class="chip-label">' + esc(svc) + '</span>' + regs + subsChips + '</span>';
        }).join("");
    return `
<div class="customer-editor-row">
  <div class="cust-row-head">
    <span class="cust-row-name">${esc(c.name)}</span>
    <span class="cust-row-id">${esc(c.customer_id)}</span>
    <span class="cust-row-actions">
      <button type="button" class="btn-secondary" data-cust-edit="${esc(c.customer_id)}">Edit</button>
      <button type="button" class="btn-danger" data-cust-del="${esc(c.customer_id)}">Delete</button>
    </span>
  </div>
  <div class="cust-row-subs">${subsHtml}</div>
</div>`;
  }

  function bindCustomerEditor() {
    const add = document.getElementById("cust-add");
    if (add) add.addEventListener("click", () => openCustomerEditor("create", null));
    const search = document.getElementById("cust-search");
    if (search) search.addEventListener("input", renderCustomerList);
    const close = document.getElementById("cust-edit-close");
    const backdrop = document.getElementById("cust-edit-backdrop");
    if (close) close.addEventListener("click", closeCustomerEditor);
    if (backdrop) backdrop.addEventListener("click", e => {
      if (e.target === backdrop) closeCustomerEditor();
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && editCustomerModal) closeCustomerEditor();
    });
  }

  function openCustomerEditor(mode, customer) {
    editCustomerModal = { mode, id: customer ? customer.customer_id : null, data: customer };
    const body = document.getElementById("cust-edit-body");
    const title = document.getElementById("cust-edit-title");
    title.textContent = mode === "create" ? "Add customer" : "Edit customer · " + (customer.name || customer.customer_id);
    body.innerHTML = renderCustomerForm(customer, regionsData);
    const form = document.getElementById("cust-form");
    form.addEventListener("submit", e => { e.preventDefault(); submitCustomer(); });
    document.getElementById("cust-cancel").addEventListener("click", closeCustomerEditor);
    // Service enable/disable toggles: when off, hide the region picker
    // AND the sub-service picker (both keyed by data-svc). (TODO #21)
    body.querySelectorAll(".svc-toggle").forEach(cb => {
      const svc = cb.dataset.svc;
      const regionPicker = body.querySelector('.region-picker[data-svc="' + svc + '"]');
      const subsvcPicker = body.querySelector('.subsvc-picker[data-svc="' + svc + '"]');
      const update = () => {
        if (regionPicker) regionPicker.style.display = cb.checked ? "" : "none";
        if (subsvcPicker) subsvcPicker.style.display = cb.checked ? "" : "none";
      };
      cb.addEventListener("change", update);
      update();
    });
    // Update the customer_id placeholder as the user types the name.
    const nameInput = document.getElementById("cust-name");
    const idInput = document.getElementById("cust-cid");
    if (nameInput && idInput) {
      nameInput.addEventListener("input", () => {
        if (mode === "create" && !idInput.dataset.touched) {
          idInput.placeholder = "auto: " + slugifyClient(nameInput.value);
        }
      });
      idInput.addEventListener("input", () => { idInput.dataset.touched = "1"; });
    }
    clearFormErrors("cust");
    document.getElementById("cust-edit-backdrop").classList.remove("hidden");
    document.getElementById("cust-edit-modal").focus();
    if (nameInput) nameInput.focus();
  }

  function renderCustomerForm(customer, regions) {
    const subs = (customer && customer.subscriptions) || {};
    const subSvcs = (customer && customer.sub_services) || {};
    const regionsByService = {};
    for (const r of regions || []) {
      (regionsByService[r.service] = regionsByService[r.service] || []).push(r);
    }
    const services = SERVICES;
    // Sub-service codes are currently populated for Chorus (fibre, copper).
    // Zscaler sub-products are no longer parsed.
    const SUB_SERVICE_CODES = {
      chorus: ["fibre", "copper"],
      vector: ["power", "hv", "lv"],
      m365: [
        "exchange", "teams", "sharepoint", "onedrive", "intune",
        "defender-endpoint", "teams-calling", "azure-ad"
      ],
      kaseya: [
        "it-glue", "datto", "autotask", "vsa", "bms", "spanning", "dark-web-id", "rocketcyber"
      ],
      duo: ["push", "telephony", "sso", "admin-panel"],
      myob: ["accountright", "business", "practice", "payglobal", "greentree", "exo"],
      atlassian: [
        "jira",
        "jira-service-management",
        "jira-work-management",
        "jira-product-discovery",
        "jira-align",
        "confluence",
        "bitbucket",
        "trello",
        "opsgenie",
        "guard",
        "statuspage",
        "compass",
        "atlas",
        "analytics",
        "rovo",
        "talent",
        "csm",
      ],
    };
    const subsHtml = services.map(svc => {
      const checked = svc in subs;
      const currentRegions = new Set((subs[svc] || []).map(x => String(x).toLowerCase()));
      // Region options: every approved region for this service, plus
      // the literal "global" (which the lint accepts even without a
      // CSV row, but we always show it for UX consistency).
      const opts = (regionsByService[svc] || []).slice();
      if (!opts.some(o => o.region_code === "global")) {
        opts.push({ service: svc, region_code: "global", display_name: "Global" });
      }
      opts.sort((a, b) => compareRegionCodes(a.region_code, b.region_code));
      const optHtml = opts.map(o => {
        const isOn = currentRegions.has(o.region_code);
        return `<label class="region-pick">
          <input type="checkbox" class="region-check" data-svc="${esc(svc)}" value="${esc(o.region_code)}" ${isOn ? "checked" : ""}>
          <span class="region-code">${esc(o.region_code)}</span>
          <span class="region-display">${esc(o.display_name || o.region_code)}</span>
        </label>`;
      }).join("");
      
      let subsvcBlock = "";
      if (SUB_SERVICE_CODES[svc]) {
        const currentSubSvcs = new Set((subSvcs[svc] || []).map(x => String(x).toLowerCase()));
        const subsvcHtml = SUB_SERVICE_CODES[svc].map(code => {
          const isOn = currentSubSvcs.has(code);
          return `<label class="subsvc-pick" style="margin-right:12px;cursor:pointer">
            <input type="checkbox" class="subsvc-check" data-svc="${esc(svc)}" value="${esc(code)}" ${isOn ? "checked" : ""}>
            <span class="subsvc-code">${esc(code)}</span>
          </label>`;
        }).join("");
        subsvcBlock = `<div class="subsvc-picker" data-svc="${esc(svc)}" style="margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);${checked ? "" : "display:none"}">
          <span style="font-size:12px;color:var(--text-dim);margin-right:8px">Sub-services:</span>
          ${subsvcHtml}
        </div>`;
      }

      return `<fieldset class="form-subs">
        <legend>
          <label style="cursor:pointer">
            <input type="checkbox" class="svc-toggle" data-svc="${esc(svc)}" ${checked ? "checked" : ""}>
            ${esc(svc)}
          </label>
        </legend>
        <div class="region-picker" data-svc="${esc(svc)}" ${checked ? "" : "style='display:none'"}>${optHtml}</div>
        ${subsvcBlock}
      </fieldset>`;
    }).join("");

    return `<form id="cust-form" class="form" autocomplete="off">
      <div class="form-row">
        <label class="form-label" for="cust-name">Name</label>
        <input type="text" class="form-input" id="cust-name" required value="${esc((customer && customer.name) || "")}" maxlength="120">
      </div>
      <div class="form-row">
        <label class="form-label" for="cust-cid">Customer ID</label>
        <input type="text" class="form-input" id="cust-cid" value="${esc((customer && customer.customer_id) || "")}" ${customer ? "disabled" : ""} maxlength="80">
        <span class="field-help">${customer ? "Customer ID is immutable; delete + recreate to rename." : "Leave blank to auto-generate from the name (kebab-case). Must be unique."}</span>
      </div>
      <div class="form-row">
        <span class="form-label">Subscriptions</span>
         <span class="field-help">Tick a service to enable it, then check one or more regions. "global" matches any outage for that service.</span>
        ${subsHtml}
      </div>
      <div class="form-error" id="cust-form-error" hidden></div>
      <details class="form-error-details" id="cust-form-errors" hidden></details>
      <div class="form-actions">
        <button type="button" class="btn-secondary" id="cust-cancel">Cancel</button>
        <button type="submit" class="btn-primary" id="cust-save">${customer ? "Save changes" : "Create customer"}</button>
      </div>
    </form>`;
  }

  // Lightweight client-side slug preview, mirrors server-side
  // regions_store.slugify. Used only as a placeholder hint — the server
  // is the source of truth.
  function slugifyClient(s) {
    return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "customer";
  }

  // Collects per-service regions and sub-services from the form.
  // Returns `{ subscriptions, sub_services }`:
  //   - `subscriptions` is `{service: [regions]}` — omitted services
  //     (toggle off) don't appear in the result.
  //   - `sub_services` is `{service: [codes]}` — only includes services
  //     that have a sub_service picker AND at least one checked code.
  //     Services with all-unchecked sub-service pickers are absent
  //     from the dict, which the server treats as "no sub-service
  //     filter" (backward-compatible). (TODO #21)
  function collectFormSubscriptions() {
    const subs = {};
    const subSvcs = {};
    document.querySelectorAll(".svc-toggle").forEach(cb => {
      if (!cb.checked) return;
      const svc = cb.dataset.svc;
      const regions = Array.from(
        document.querySelectorAll('.region-check[data-svc="' + svc + '"]:checked')
      ).map(c => c.value);
      if (regions.length > 0) subs[svc] = regions;
      const subsvcChecks = document.querySelectorAll(
        '.subsvc-check[data-svc="' + svc + '"]:checked'
      );
      if (subsvcChecks.length > 0) {
        subSvcs[svc] = Array.from(subsvcChecks).map(c => c.value);
      }
    });
    return { subscriptions: subs, sub_services: subSvcs };
  }

  async function submitCustomer() {
    if (!editCustomerModal) return;
    const mode = editCustomerModal.mode;
    const id = editCustomerModal.id;
    const name = document.getElementById("cust-name").value.trim();
    if (!name) {
      showFormErrors("cust", { error: "invalid", details: [{ path: "name", message: "required" }] });
      return;
    }
    const body = { name };
    if (mode === "create") {
      const cid = document.getElementById("cust-cid").value.trim();
      if (cid) body.customer_id = cid;
    }
    const { subscriptions, sub_services } = collectFormSubscriptions();
    body.subscriptions = subscriptions;
    body.sub_services = sub_services;
    clearFormErrors("cust");
    const saveBtn = document.getElementById("cust-save");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const method = mode === "create" ? "POST" : "PUT";
      const url = mode === "create"
        ? "/api/customers"
        : "/api/customers/" + encodeURIComponent(id);
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const resp = r.status === 204 ? null : await r.json().catch(() => null);
      if (!r.ok) {
        showFormErrors("cust", resp || { error: "HTTP " + r.status });
        return;
      }
      customersData = (resp && resp.customers) || customersData;
      showToast(mode === "create" ? "Customer created" : "Customer updated", "success");
      closeCustomerEditor();
      renderCustomerList();
    } catch (e) {
      showFormErrors("cust", { error: "network error", details: [{ path: "", message: e.message }] });
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = editCustomerModal ? (editCustomerModal.mode === "create" ? "Create customer" : "Save changes") : "Save";
    }
  }

  function closeCustomerEditor() {
    editCustomerModal = null;
    document.getElementById("cust-edit-backdrop").classList.add("hidden");
  }

  async function deleteCustomer(id) {
    const c = customersData.find(x => x.customer_id === id);
    const label = c ? c.name : id;
    if (!confirm('Delete customer "' + label + '"? This cannot be undone.')) return;
    try {
      const r = await fetch("/api/customers/" + encodeURIComponent(id), { method: "DELETE" });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        showToast("Delete failed: " + ((body && body.error) || ("HTTP " + r.status)), "error");
        return;
      }
      customersData = (body && body.customers) || customersData;
      showToast("Customer deleted", "success");
      renderCustomerList();
    } catch (e) {
      showToast("Delete failed: " + e.message, "error");
    }
  }

  // ----- Regions tab ----------------------------------------------
  async function loadRegions() {
    const wrap = document.getElementById("reg-table-wrap");
    if (!wrap) return;
    wrap.innerHTML = '<div style="padding:20px;color:var(--text-dim);font-size:12px">Loading…</div>';
    try {
      const r = await fetch("/api/regions", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const body = await r.json();
      regionsData = (body && body.regions) || [];
      renderRegionTable();
    } catch (e) {
      wrap.innerHTML = '<div class="empty"><div class="big">!</div><div>Failed to load regions: ' + esc(e.message) + '</div></div>';
    }
  }

  function renderRegionTable() {
    const wrap = document.getElementById("reg-table-wrap");
    const search = (document.getElementById("reg-search") || {}).value || "";
    const needle = search.trim().toLowerCase();
    const rows = regionsData
      .slice()
      .sort((a, b) => (a.service || "").localeCompare(b.service || "") || compareRegionCodes(a.region_code, b.region_code))
      .filter(r => !needle ||
        (r.service || "").toLowerCase().includes(needle) ||
        (r.region_code || "").toLowerCase().includes(needle) ||
        (r.display_name || "").toLowerCase().includes(needle));
    if (rows.length === 0) {
      wrap.innerHTML = '<div class="empty"><div class="big">' +
        (regionsData.length === 0 ? "+" : "∅") + '</div><div>' +
        (regionsData.length === 0 ? "No regions yet. Click “Add region” to add a (service, region_code) pair." : "No regions match your filter.") +
        '</div></div>';
      return;
    }
    const html = `<table class="table">
      <thead><tr><th>Service</th><th>Region code</th><th>Display name</th><th></th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${esc(r.service)}</td>
          <td class="table-code">${esc(r.region_code)}</td>
          <td>${esc(r.display_name || "")}</td>
          <td class="table-actions">
            <button type="button" class="btn-secondary" data-reg-edit='${esc(JSON.stringify({ svc: r.service, code: r.region_code }))}'>Edit</button>
            <button type="button" class="btn-danger" data-reg-del='${esc(JSON.stringify({ svc: r.service, code: r.region_code }))}'>Delete</button>
          </td>
        </tr>`).join("")}
      </tbody>
    </table>`;
    wrap.innerHTML = html;
    wrap.querySelectorAll("[data-reg-edit]").forEach(btn => {
      btn.addEventListener("click", () => {
        const { svc, code } = JSON.parse(btn.dataset.regEdit);
        const row = regionsData.find(x => x.service === svc && x.region_code === code);
        if (row) openRegionEditor("edit", row);
      });
    });
    wrap.querySelectorAll("[data-reg-del]").forEach(btn => {
      btn.addEventListener("click", () => {
        const { svc, code } = JSON.parse(btn.dataset.regDel);
        deleteRegion(svc, code);
      });
    });
  }

  function bindRegionEditor() {
    const add = document.getElementById("reg-add");
    if (add) add.addEventListener("click", () => openRegionEditor("create", null));
    const search = document.getElementById("reg-search");
    if (search) search.addEventListener("input", renderRegionTable);
    const close = document.getElementById("reg-edit-close");
    const backdrop = document.getElementById("reg-edit-backdrop");
    if (close) close.addEventListener("click", closeRegionEditor);
    if (backdrop) backdrop.addEventListener("click", e => {
      if (e.target === backdrop) closeRegionEditor();
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && editRegionModal) closeRegionEditor();
    });
  }

  function openRegionEditor(mode, row) {
    editRegionModal = { mode, svc: row && row.service, code: row && row.region_code, data: row };
    const body = document.getElementById("reg-edit-body");
    const title = document.getElementById("reg-edit-title");
    title.textContent = mode === "create"
      ? "Add region"
      : "Edit region · " + row.service + "/" + row.region_code;
    body.innerHTML = renderRegionForm(row, mode);
    const form = document.getElementById("reg-form");
    form.addEventListener("submit", e => { e.preventDefault(); submitRegion(); });
    document.getElementById("reg-cancel").addEventListener("click", closeRegionEditor);
    clearFormErrors("reg");
    document.getElementById("reg-edit-backdrop").classList.remove("hidden");
    document.getElementById("reg-edit-modal").focus();
    const first = document.getElementById(mode === "create" ? "reg-svc" : "reg-display");
    if (first) first.focus();
  }

  function renderRegionForm(row, mode) {
    const services = SERVICES;
    const svcOptions = services.map(s => `<option value="${esc(s)}" ${row && row.service === s ? "selected" : ""}>${esc(s)}</option>`).join("");
    return `<form id="reg-form" class="form" autocomplete="off">
      <div class="form-row">
        <label class="form-label" for="reg-svc">Service</label>
        <select class="form-select" id="reg-svc" ${mode === "edit" ? "disabled" : ""}>
          ${mode === "create" ? '<option value="" disabled selected>Select a service…</option>' : ""}
          ${svcOptions}
        </select>
      </div>
      <div class="form-row">
        <label class="form-label" for="reg-code">Region code</label>
        <input type="text" class="form-input" id="reg-code" required value="${esc((row && row.region_code) || "")}" ${mode === "edit" ? "disabled" : ""} maxlength="80" pattern="[a-z0-9][a-z0-9\\-]*" title="lowercase kebab-case (e.g. ap-southeast-2), or the literal 'global'">
        <span class="field-help">Lowercase kebab-case (e.g. <code>ap-southeast-2</code>) or the literal <code>global</code>. Use <code>global</code> to match any outage for that service.</span>
      </div>
      <div class="form-row">
        <label class="form-label" for="reg-display">Display name</label>
        <input type="text" class="form-input" id="reg-display" required value="${esc((row && row.display_name) || "")}" maxlength="120">
        <span class="field-help">Friendly name shown in the customer editor's region picker (e.g. "Sydney").</span>
      </div>
      <div class="form-error" id="reg-form-error" hidden></div>
      <details class="form-error-details" id="reg-form-errors" hidden></details>
      <div class="form-actions">
        <button type="button" class="btn-secondary" id="reg-cancel">Cancel</button>
        <button type="submit" class="btn-primary" id="reg-save">${row ? "Save changes" : "Add region"}</button>
      </div>
    </form>`;
  }

  async function submitRegion() {
    if (!editRegionModal) return;
    const mode = editRegionModal.mode;
    const body = {};
    if (mode === "create") {
      const svc = document.getElementById("reg-svc").value;
      const code = document.getElementById("reg-code").value.trim().toLowerCase();
      if (!svc) {
        showFormErrors("reg", { error: "invalid", details: [{ path: "service", message: "required" }] });
        return;
      }
      if (!code) {
        showFormErrors("reg", { error: "invalid", details: [{ path: "region_code", message: "required" }] });
        return;
      }
      body.service = svc;
      body.region_code = code;
    }
    body.display_name = document.getElementById("reg-display").value.trim();
    if (!body.display_name) {
      showFormErrors("reg", { error: "invalid", details: [{ path: "display_name", message: "required" }] });
      return;
    }
    clearFormErrors("reg");
    const saveBtn = document.getElementById("reg-save");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const method = mode === "create" ? "POST" : "PUT";
      const url = mode === "create"
        ? "/api/regions"
        : "/api/regions/" + encodeURIComponent(editRegionModal.svc) + "/" + encodeURIComponent(editRegionModal.code);
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const resp = await r.json().catch(() => null);
      if (!r.ok) {
        showFormErrors("reg", resp || { error: "HTTP " + r.status });
        return;
      }
      regionsData = (resp && resp.regions) || regionsData;
      showToast(mode === "create" ? "Region added" : "Region updated", "success");
      closeRegionEditor();
      renderRegionTable();
    } catch (e) {
      showFormErrors("reg", { error: "network error", details: [{ path: "", message: e.message }] });
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = editRegionModal ? (editRegionModal.mode === "create" ? "Add region" : "Save changes") : "Save";
    }
  }

  function closeRegionEditor() {
    editRegionModal = null;
    document.getElementById("reg-edit-backdrop").classList.add("hidden");
  }

  async function deleteRegion(svc, code) {
    const row = regionsData.find(x => x.service === svc && x.region_code === code);
    const label = row ? (row.service + "/" + row.region_code) : (svc + "/" + code);
    if (!confirm('Delete region "' + label + '"? This cannot be undone.')) return;
    let force = false;
    let resp;
    try {
      const r = await fetch("/api/regions/" + encodeURIComponent(svc) + "/" + encodeURIComponent(code), { method: "DELETE" });
      resp = await r.json().catch(() => null);
      if (r.status === 409) {
        const count = (resp && resp.customer_count) || 0;
        const customers = (resp && resp.customers) || [];
        const sample = customers.slice(0, 3).join(", ") + (customers.length > 3 ? ", …" : "");
        if (!confirm(
          "Region " + label + " is used by " + count + " customer" + (count === 1 ? "" : "s") +
          " (" + sample + ").\n\n" +
          "Deleting it will leave those customers referencing a region " +
          "that is no longer in the allowlist, and `lint-customers` will start failing.\n\n" +
          "Continue anyway?"
        )) return;
        force = true;
        const r2 = await fetch("/api/regions/" + encodeURIComponent(svc) + "/" + encodeURIComponent(code) + "?force=true", { method: "DELETE" });
        const resp2 = await r2.json().catch(() => null);
        if (!r2.ok) {
          showToast("Delete failed: " + ((resp2 && resp2.error) || ("HTTP " + r2.status)), "error");
          return;
        }
        regionsData = (resp2 && resp2.regions) || regionsData;
        showToast("Region force-deleted (" + count + " customer subscription" + (count === 1 ? "" : "s") + " now reference a non-approved region)", "success");
      } else if (!r.ok) {
        showToast("Delete failed: " + ((resp && resp.error) || ("HTTP " + r.status)), "error");
        return;
      } else {
        regionsData = (resp && resp.regions) || regionsData;
        showToast("Region deleted", "success");
      }
      renderRegionTable();
    } catch (e) {
      showToast("Delete failed: " + e.message, "error");
    }
  }

  // =========================================================================
  // Graphs View & Native SVG Analytics Engine
  // =========================================================================

  const graphState = {
    chartType: "stacked-bar", // "stacked-bar" | "grouped-bar" | "line" | "area"
    range: "30d",             // "24h" | "7d" | "30d" | "90d" | "all"
    xDim: "time",             // "time" | "provider" | "severity" | "customer"
    yMetric: "count",         // "count" | "impacted_customers" | "severity_score"
    zGroup: "provider",       // "provider" | "severity" | "customer" | "none"
    providers: new Set(SERVICES),
    severities: new Set(SEVERITIES),
    customerFilter: "",
    hiddenSeries: new Set(),
  };

  const PROVIDER_COLORS = {
    atlassian: "#0052cc",
    aws: "#ff9900",
    azure: "#0078d4",
    cloudflare: "#f38020",
    connectwise: "#22c55e",
    googleworkspace: "#ea4335",
    m365: "#d83b01",
    zscaler: "#00a3e0",
    chorus: "#a855f7",
    vector: "#eab308",
    wiz: "#ff4f5e",
    xero: "#13b5ea",
    halopsa: "#385170",
    ninjaone: "#2563eb",
    duo: "#10b981",
    kaseya: "#0284c7",
    enable: "#0d9488",
    mimecast: "#1d4ed8",
    lightwire: "#059669",
    myob: "#6366f1",
    rewst: "#8b5cf6",
    crowdstrike: "#dc2626",
    kordia: "#475569",
    pia: "#7c3aed",
    nbn: "#0284c7",
    avepoint: "#0ea5e9",
    wasabi: "#10b981",
    tenable: "#0284c7",
    eset: "#06b6d4",
    googleai: "#4285f4",
  };

  const GRAPH_PALETTE = [
    "#38bdf8", "#f43f5e", "#10b981", "#8b5cf6", "#f59e0b",
    "#06b6d4", "#ec4899", "#84cc16", "#a855f7", "#64748b"
  ];

  function getGraphColor(key, idx) {
    const k = String(key || "").toLowerCase();
    if (PROVIDER_COLORS[k]) return PROVIDER_COLORS[k];
    if (SEV_COLORS[k]) return SEV_COLORS[k];
    let hash = 0;
    for (let i = 0; i < k.length; i++) hash = (hash << 5) - hash + k.charCodeAt(i);
    const pos = Math.abs(hash) % GRAPH_PALETTE.length;
    return GRAPH_PALETTE[(pos + (idx || 0)) % GRAPH_PALETTE.length];
  }

  let graphsBound = false;

  async function loadGraphsView() {
    if (!state.payload || !state.rows || state.rows.length === 0) {
      await refresh();
    }
    if (customersData.length === 0) {
      try {
        const r = await fetch("/api/customers");
        if (r.ok) customersData = await r.json();
      } catch (e) { /* ignore */ }
    }
    populateGraphCustomerDropdown();
    renderGraphFilterCheckboxes();
    renderGraphs();
  }

  function bindGraphsEvents() {
    if (graphsBound) return;
    graphsBound = true;

    const chartTypeSel = document.getElementById("graph-chart-type");
    if (chartTypeSel) {
      chartTypeSel.addEventListener("change", e => {
        graphState.chartType = e.target.value;
        renderGraphs();
      });
    }

    const rangeBtns = document.getElementById("graph-range-btns");
    if (rangeBtns) {
      rangeBtns.addEventListener("click", e => {
        const btn = e.target.closest(".graph-pill-btn");
        if (!btn) return;
        rangeBtns.querySelectorAll(".graph-pill-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        graphState.range = btn.dataset.range;
        renderGraphs();
      });
    }

    const xDimSel = document.getElementById("graph-x-dim");
    if (xDimSel) {
      xDimSel.addEventListener("change", e => {
        graphState.xDim = e.target.value;
        renderGraphs();
      });
    }

    const yMetricSel = document.getElementById("graph-y-metric");
    if (yMetricSel) {
      yMetricSel.addEventListener("change", e => {
        graphState.yMetric = e.target.value;
        renderGraphs();
      });
    }

    const zGroupSel = document.getElementById("graph-z-group");
    if (zGroupSel) {
      zGroupSel.addEventListener("change", e => {
        graphState.zGroup = e.target.value;
        graphState.hiddenSeries.clear();
        renderGraphs();
      });
    }

    const custSel = document.getElementById("graph-customer-filter");
    if (custSel) {
      custSel.addEventListener("change", e => {
        graphState.customerFilter = e.target.value;
        renderGraphs();
      });
    }

    const pFilters = document.getElementById("graph-provider-filters");
    if (pFilters) {
      pFilters.addEventListener("change", () => {
        const checked = new Set();
        pFilters.querySelectorAll("input[type=checkbox]:checked").forEach(cb => checked.add(cb.value));
        graphState.providers = checked;
        renderGraphs();
      });
    }

    const sFilters = document.getElementById("graph-severity-filters");
    if (sFilters) {
      sFilters.addEventListener("change", () => {
        const checked = new Set();
        sFilters.querySelectorAll("input[type=checkbox]:checked").forEach(cb => checked.add(cb.value));
        graphState.severities = checked;
        renderGraphs();
      });
    }

    const splitter = document.getElementById("vSplitterGraphs");
    if (splitter) {
      let isDragging = false;
      splitter.addEventListener("mousedown", () => {
        isDragging = true;
        splitter.classList.add("dragging");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      });
      document.addEventListener("mousemove", e => {
        if (!isDragging) return;
        const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, e.clientX));
        document.documentElement.style.setProperty("--sidebar-w", w + "px");
      });
      document.addEventListener("mouseup", () => {
        if (isDragging) {
          isDragging = false;
          splitter.classList.remove("dragging");
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        }
      });
    }

    window.addEventListener("resize", () => {
      if (currentView === "graphs") {
        renderGraphs();
      }
    });
  }

  function populateGraphCustomerDropdown() {
    const sel = document.getElementById("graph-customer-filter");
    if (!sel) return;
    const current = graphState.customerFilter;
    let html = '<option value="">All Customers</option>';
    const namesSet = new Set();
    for (const c of customersData) {
      if (c && c.name) namesSet.add(c.name);
    }
    if (state.impact && state.impact.by_customer) {
      for (const k of Object.keys(state.impact.by_customer)) {
        if (k) namesSet.add(k);
      }
    }
    const list = Array.from(namesSet).sort((a, b) => a.localeCompare(b));
    for (const name of list) {
      const selected = name === current ? "selected" : "";
      html += `<option value="${esc(name)}" ${selected}>${esc(name)}</option>`;
    }
    sel.innerHTML = html;
  }

  function renderGraphFilterCheckboxes() {
    const pContainer = document.getElementById("graph-provider-filters");
    if (pContainer) {
      pContainer.innerHTML = SERVICES.map(s => {
        const checked = graphState.providers.has(s) ? "checked" : "";
        const color = PROVIDER_COLORS[s] || "#94a3b8";
        return `<label class="filter-row">
          <input type="checkbox" value="${esc(s)}" ${checked}>
          <span class="graph-tooltip-swatch" style="background:${color}"></span>
          <span class="filter-label">${esc(s.toUpperCase())}</span>
        </label>`;
      }).join("");
    }

    const sContainer = document.getElementById("graph-severity-filters");
    if (sContainer) {
      sContainer.innerHTML = SEVERITIES.map(s => {
        const checked = graphState.severities.has(s) ? "checked" : "";
        const color = SEV_COLORS[s] || "#94a3b8";
        return `<label class="filter-row">
          <input type="checkbox" value="${esc(s)}" ${checked}>
          <span class="graph-tooltip-swatch" style="background:${color}"></span>
          <span class="filter-label">${esc(s.charAt(0).toUpperCase() + s.slice(1))}</span>
        </label>`;
      }).join("");
    }
  }

  function buildGraphDataset() {
    const rows = (state.rows || []).slice();
    const now = Date.now();

    let cutoffMs = 0;
    if (graphState.range === "24h") cutoffMs = now - 24 * 3600 * 1000;
    else if (graphState.range === "7d") cutoffMs = now - 7 * 86400 * 1000;
    else if (graphState.range === "30d") cutoffMs = now - 30 * 86400 * 1000;
    else if (graphState.range === "90d") cutoffMs = now - 90 * 86400 * 1000;

    const filteredRows = rows.filter(r => {
      if (!r || !r.raw_id) return false;
      const svc = (r.service || "").toLowerCase();
      if (!graphState.providers.has(svc)) return false;
      const sev = (r.severity || "").toLowerCase();
      if (!graphState.severities.has(sev)) return false;

      const pubTime = Date.parse(r.published_at || r.last_seen_at || "");
      if (cutoffMs > 0 && Number.isFinite(pubTime) && pubTime < cutoffMs) return false;

      if (graphState.customerFilter) {
        const impacted = customersForRow(r, state.impact);
        const match = impacted.some(c => {
          const name = typeof c === "string" ? c : (c.name || c.customer_id);
          return name === graphState.customerFilter;
        });
        if (!match) return false;
      }
      return true;
    });

    let buckets = [];
    const seriesSet = new Set();

    function getMetricValue(r) {
      if (graphState.yMetric === "impacted_customers") {
        return customersForRow(r, state.impact).length;
      }
      if (graphState.yMetric === "severity_score") {
        const sev = (r.severity || "").toLowerCase();
        if (sev === "critical") return 4;
        if (sev === "warning") return 2;
        if (sev === "info") return 1;
        return 0.5;
      }
      return 1;
    }

    function getSeriesKeys(r) {
      if (graphState.zGroup === "provider") return [(r.service || "unknown").toLowerCase()];
      if (graphState.zGroup === "severity") return [(r.severity || "unknown").toLowerCase()];
      if (graphState.zGroup === "customer") {
        const list = customersForRow(r, state.impact);
        if (list.length === 0) return ["No Customer Match"];
        return list.map(c => typeof c === "string" ? c : (c.name || c.customer_id));
      }
      return ["Total"];
    }

    if (graphState.xDim === "time") {
      if (graphState.range === "24h") {
        const startHour = new Date(now - 23 * 3600 * 1000);
        startHour.setMinutes(0, 0, 0);
        for (let i = 0; i < 24; i++) {
          const d = new Date(startHour.getTime() + i * 3600 * 1000);
          const key = d.toISOString().slice(0, 13);
          const label = String(d.getHours()).padStart(2, "0") + ":00";
          const fullLabel = d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
          buckets.push({ key, label, fullLabel, seriesMap: {}, total: 0 });
        }
      } else if (graphState.range === "7d" || graphState.range === "30d") {
        const numDays = graphState.range === "7d" ? 7 : 30;
        const startDate = new Date(now - (numDays - 1) * 86400 * 1000);
        startDate.setHours(0, 0, 0, 0);
        for (let i = 0; i < numDays; i++) {
          const d = new Date(startDate.getTime() + i * 86400 * 1000);
          const key = d.toISOString().slice(0, 10);
          const label = d.toLocaleDateString([], { month: "short", day: "numeric" });
          const fullLabel = d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: "numeric" });
          buckets.push({ key, label, fullLabel, seriesMap: {}, total: 0 });
        }
      } else if (graphState.range === "90d") {
        const numWeeks = 13;
        const startWeek = new Date(now - (numWeeks - 1) * 7 * 86400 * 1000);
        for (let i = 0; i < numWeeks; i++) {
          const d = new Date(startWeek.getTime() + i * 7 * 86400 * 1000);
          const key = d.toISOString().slice(0, 10);
          const label = "W " + d.toLocaleDateString([], { month: "numeric", day: "numeric" });
          const fullLabel = "Week of " + d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
          buckets.push({ key, label, fullLabel, seriesMap: {}, total: 0 });
        }
      } else {
        const monthsMap = new Map();
        for (const r of filteredRows) {
          const t = Date.parse(r.published_at || r.last_seen_at || "");
          if (!Number.isFinite(t)) continue;
          const d = new Date(t);
          const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
          if (!monthsMap.has(key)) {
            monthsMap.set(key, {
              key,
              label: d.toLocaleDateString([], { month: "short", year: "2-digit" }),
              fullLabel: d.toLocaleDateString([], { month: "long", year: "numeric" }),
              seriesMap: {},
              total: 0,
            });
          }
        }
        buckets = Array.from(monthsMap.values()).sort((a, b) => a.key.localeCompare(b.key));
        if (buckets.length === 0) {
          const d = new Date();
          buckets.push({ key: "current", label: d.toLocaleDateString([], { month: "short" }), fullLabel: "Current Month", seriesMap: {}, total: 0 });
        }
      }

      for (const r of filteredRows) {
        const t = Date.parse(r.published_at || r.last_seen_at || "");
        if (!Number.isFinite(t)) continue;
        const d = new Date(t);
        const val = getMetricValue(r);
        const seriesList = getSeriesKeys(r);

        let targetBucket = null;
        if (graphState.range === "24h") {
          const key = d.toISOString().slice(0, 13);
          targetBucket = buckets.find(b => b.key === key);
        } else if (graphState.range === "7d" || graphState.range === "30d") {
          const key = d.toISOString().slice(0, 10);
          targetBucket = buckets.find(b => b.key === key);
        } else if (graphState.range === "90d") {
          targetBucket = buckets.find(b => Math.abs(Date.parse(b.key) - t) < 7 * 86400 * 1000);
        } else {
          const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
          targetBucket = buckets.find(b => b.key === key);
        }

        if (targetBucket) {
          for (const sKey of seriesList) {
            seriesSet.add(sKey);
            targetBucket.seriesMap[sKey] = (targetBucket.seriesMap[sKey] || 0) + val;
            targetBucket.total += val;
          }
        }
      }
    } else if (graphState.xDim === "provider") {
      for (const p of SERVICES) {
        if (!graphState.providers.has(p)) continue;
        const b = { key: p, label: p.toUpperCase(), fullLabel: `Provider: ${p.toUpperCase()}`, seriesMap: {}, total: 0 };
        buckets.push(b);
      }
      for (const r of filteredRows) {
        const p = (r.service || "").toLowerCase();
        const b = buckets.find(x => x.key === p);
        if (!b) continue;
        const val = getMetricValue(r);
        const sKeys = getSeriesKeys(r);
        for (const sk of sKeys) {
          seriesSet.add(sk);
          b.seriesMap[sk] = (b.seriesMap[sk] || 0) + val;
          b.total += val;
        }
      }
    } else if (graphState.xDim === "severity") {
      for (const s of SEVERITIES) {
        if (!graphState.severities.has(s)) continue;
        const b = { key: s, label: s.toUpperCase(), fullLabel: `Severity: ${s.toUpperCase()}`, seriesMap: {}, total: 0 };
        buckets.push(b);
      }
      for (const r of filteredRows) {
        const s = (r.severity || "").toLowerCase();
        const b = buckets.find(x => x.key === s);
        if (!b) continue;
        const val = getMetricValue(r);
        const sKeys = getSeriesKeys(r);
        for (const sk of sKeys) {
          seriesSet.add(sk);
          b.seriesMap[sk] = (b.seriesMap[sk] || 0) + val;
          b.total += val;
        }
      }
    } else if (graphState.xDim === "customer") {
      const custMap = new Map();
      for (const r of filteredRows) {
        const impacted = customersForRow(r, state.impact);
        const val = getMetricValue(r);
        const sKeys = getSeriesKeys(r);
        const names = impacted.length > 0 ? impacted.map(c => typeof c === "string" ? c : (c.name || c.customer_id)) : ["Unassigned"];
        for (const name of names) {
          if (!custMap.has(name)) {
            custMap.set(name, { key: name, label: name, fullLabel: `Customer: ${name}`, seriesMap: {}, total: 0 });
          }
          const b = custMap.get(name);
          for (const sk of sKeys) {
            seriesSet.add(sk);
            b.seriesMap[sk] = (b.seriesMap[sk] || 0) + val;
            b.total += val;
          }
        }
      }
      buckets = Array.from(custMap.values()).sort((a, b) => b.total - a.total).slice(0, 15);
    }

    return {
      filteredRows,
      buckets,
      allSeries: Array.from(seriesSet).sort(),
    };
  }

  function drawSvgChart(data) {
    const svg = document.getElementById("graph-svg");
    const legendEl = document.getElementById("graph-legend");
    const tooltipEl = document.getElementById("graph-tooltip");
    if (!svg || !legendEl) return;

    const { buckets, allSeries } = data;
    const activeSeries = allSeries.filter(s => !graphState.hiddenSeries.has(s));

    legendEl.innerHTML = allSeries.map((s, i) => {
      const color = getGraphColor(s, i);
      const isDimmed = graphState.hiddenSeries.has(s) ? "dimmed" : "";
      return `<div class="graph-legend-item ${isDimmed}" data-series="${esc(s)}">
        <span class="graph-legend-dot" style="background:${color}"></span>
        <span>${esc(s.toUpperCase())}</span>
      </div>`;
    }).join("");

    legendEl.querySelectorAll(".graph-legend-item").forEach(item => {
      item.addEventListener("click", () => {
        const s = item.dataset.series;
        if (graphState.hiddenSeries.has(s)) {
          graphState.hiddenSeries.delete(s);
        } else {
          if (graphState.hiddenSeries.size < allSeries.length - 1) {
            graphState.hiddenSeries.add(s);
          }
        }
        renderGraphs();
      });
    });

    const W = 960;
    const H = 400;
    const M = { top: 25, right: 30, bottom: 50, left: 55 };
    const plotW = W - M.left - M.right;
    const plotH = H - M.top - M.bottom;
    const baseline = M.top + plotH;

    if (buckets.length === 0 || activeSeries.length === 0) {
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      svg.innerHTML = `
        <rect x="0" y="0" width="${W}" height="${H}" fill="transparent" />
        <text x="${W/2}" y="${H/2}" fill="var(--text-dim)" text-anchor="middle" font-size="14">No outage data matching active filters</text>
      `;
      return;
    }

    let maxVal = 0;
    for (const b of buckets) {
      if (graphState.chartType === "stacked-bar" || graphState.chartType === "area") {
        let stack = 0;
        for (const s of activeSeries) {
          stack += (b.seriesMap[s] || 0);
        }
        if (stack > maxVal) maxVal = stack;
      } else {
        for (const s of activeSeries) {
          const v = b.seriesMap[s] || 0;
          if (v > maxVal) maxVal = v;
        }
      }
    }

    let yMax = Math.max(5, Math.ceil(maxVal * 1.15));
    if (yMax <= 10) yMax = 10;
    else if (yMax <= 25) yMax = 25;
    else if (yMax <= 50) yMax = 50;
    else if (yMax <= 100) yMax = 100;
    else if (yMax <= 250) yMax = 250;
    else if (yMax <= 500) yMax = 500;
    else if (yMax <= 1000) yMax = 1000;
    else yMax = Math.ceil(yMax / 500) * 500;

    let svgInner = "";

    const numGridLines = 5;
    for (let g = 0; g <= numGridLines; g++) {
      const fraction = g / numGridLines;
      const yVal = Math.round(fraction * yMax);
      const yPos = baseline - fraction * plotH;
      svgInner += `
        <line x1="${M.left}" y1="${yPos}" x2="${W - M.right}" y2="${yPos}" class="graph-grid-line" />
        <text x="${M.left - 10}" y="${yPos + 3.5}" class="graph-axis-text" text-anchor="end">${yVal}</text>
      `;
    }

    svgInner += `<line x1="${M.left}" y1="${baseline}" x2="${W - M.right}" y2="${baseline}" class="graph-axis-line" />`;
    svgInner += `<line x1="${M.left}" y1="${M.top}" x2="${M.left}" y2="${baseline}" class="graph-axis-line" />`;

    const N = buckets.length;
    const step = plotW / N;
    const labelStep = N > 20 ? Math.ceil(N / 10) : (N > 10 ? 2 : 1);

    for (let i = 0; i < N; i++) {
      const cx = M.left + (i + 0.5) * step;
      if (i % labelStep === 0 || i === N - 1) {
        svgInner += `
          <text x="${cx}" y="${baseline + 18}" class="graph-axis-text" text-anchor="middle">${esc(buckets[i].label)}</text>
        `;
      }
    }

    if (graphState.chartType === "stacked-bar") {
      const bw = Math.min(48, Math.max(6, step * 0.7));
      for (let i = 0; i < N; i++) {
        const b = buckets[i];
        const cx = M.left + (i + 0.5) * step;
        const bx = cx - bw / 2;
        let currY = baseline;
        for (let sIdx = 0; sIdx < activeSeries.length; sIdx++) {
          const s = activeSeries[sIdx];
          const val = b.seriesMap[s] || 0;
          if (val <= 0) continue;
          const h = (val / yMax) * plotH;
          const y = currY - h;
          const color = getGraphColor(s, sIdx);
          svgInner += `<rect x="${bx}" y="${y}" width="${bw}" height="${h}" fill="${color}" rx="1.5" opacity="0.9" />`;
          currY = y;
        }
      }
    } else if (graphState.chartType === "grouped-bar") {
      const S = activeSeries.length;
      const groupW = Math.min(60, Math.max(8, step * 0.8));
      const subW = Math.max(2, groupW / S);
      for (let i = 0; i < N; i++) {
        const b = buckets[i];
        const cx = M.left + (i + 0.5) * step;
        const startX = cx - groupW / 2;
        for (let sIdx = 0; sIdx < S; sIdx++) {
          const s = activeSeries[sIdx];
          const val = b.seriesMap[s] || 0;
          if (val <= 0) continue;
          const h = (val / yMax) * plotH;
          const y = baseline - h;
          const x = startX + sIdx * subW;
          const color = getGraphColor(s, sIdx);
          svgInner += `<rect x="${x}" y="${y}" width="${Math.max(1, subW - 1)}" height="${h}" fill="${color}" rx="1" opacity="0.9" />`;
        }
      }
    } else if (graphState.chartType === "line" || graphState.chartType === "area") {
      for (let sIdx = 0; sIdx < activeSeries.length; sIdx++) {
        const s = activeSeries[sIdx];
        const color = getGraphColor(s, sIdx);
        const pts = [];
        for (let i = 0; i < N; i++) {
          const cx = M.left + (i + 0.5) * step;
          const val = buckets[i].seriesMap[s] || 0;
          const cy = baseline - (val / yMax) * plotH;
          pts.push({ x: cx, y: cy, val });
        }

        if (pts.length > 0) {
          const pathD = pts.map((p, idx) => (idx === 0 ? `M ${p.x} ${p.y}` : `L ${p.x} ${p.y}`)).join(" ");
          if (graphState.chartType === "area") {
            const polyPts = `${pts[0].x},${baseline} ` + pts.map(p => `${p.x},${p.y}`).join(" ") + ` ${pts[pts.length - 1].x},${baseline}`;
            svgInner += `<polygon points="${polyPts}" fill="${color}" fill-opacity="0.16" />`;
          }
          svgInner += `<path d="${pathD}" stroke="${color}" stroke-width="2.5" fill="none" stroke-linejoin="round" stroke-linecap="round" />`;
          for (const p of pts) {
            svgInner += `<circle cx="${p.x}" cy="${p.y}" r="3.5" fill="${color}" stroke="#0f172a" stroke-width="1.5" />`;
          }
        }
      }
    }

    svgInner += `<line id="graph-crosshair-line" x1="0" y1="${M.top}" x2="0" y2="${baseline}" class="graph-crosshair hidden" />`;
    svgInner += `<rect id="graph-hit-box" x="${M.left}" y="${M.top}" width="${plotW}" height="${plotH}" fill="transparent" style="cursor:crosshair" />`;

    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = svgInner;

    const hitBox = document.getElementById("graph-hit-box");
    const crosshair = document.getElementById("graph-crosshair-line");

    if (hitBox && crosshair && tooltipEl) {
      hitBox.addEventListener("mousemove", e => {
        const rect = svg.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const normX = (mouseX / rect.width) * W;
        if (normX < M.left || normX > W - M.right) {
          crosshair.classList.add("hidden");
          tooltipEl.classList.add("hidden");
          return;
        }

        const bucketIdx = Math.min(N - 1, Math.max(0, Math.floor((normX - M.left) / step)));
        const b = buckets[bucketIdx];
        if (!b) return;

        const cx = M.left + (bucketIdx + 0.5) * step;
        crosshair.setAttribute("x1", cx);
        crosshair.setAttribute("x2", cx);
        crosshair.classList.remove("hidden");

        let rowsHtml = "";
        let totalVal = 0;
        for (let sIdx = 0; sIdx < activeSeries.length; sIdx++) {
          const s = activeSeries[sIdx];
          const val = b.seriesMap[s] || 0;
          if (val > 0) {
            totalVal += val;
            const color = getGraphColor(s, sIdx);
            rowsHtml += `
              <div class="graph-tooltip-row">
                <span><span class="graph-tooltip-swatch" style="background:${color}"></span>${esc(s.toUpperCase())}</span>
                <strong>${val}</strong>
              </div>
            `;
          }
        }

        const metricLabel = graphState.yMetric === "impacted_customers" ? "Impacted Cust" : (graphState.yMetric === "severity_score" ? "Score" : "Outages");
        tooltipEl.innerHTML = `
          <div class="graph-tooltip-title">${esc(b.fullLabel)}</div>
          <div class="graph-tooltip-row" style="border-bottom:1px solid var(--border);margin-bottom:4px;padding-bottom:3px">
            <span style="color:var(--text-dim)">Total ${metricLabel}</span>
            <strong style="color:var(--accent)">${totalVal}</strong>
          </div>
          ${rowsHtml || '<div style="color:var(--text-dim);font-size:11px">No incidents</div>'}
        `;

        tooltipEl.classList.remove("hidden");

        const wrapRect = svg.parentElement.getBoundingClientRect();
        let tipX = e.clientX - wrapRect.left + 15;
        let tipY = e.clientY - wrapRect.top - 10;
        if (tipX + 220 > wrapRect.width) tipX = e.clientX - wrapRect.left - 230;
        if (tipY + 150 > wrapRect.height) tipY = wrapRect.height - 160;
        if (tipY < 10) tipY = 10;

        tooltipEl.style.left = tipX + "px";
        tooltipEl.style.top = tipY + "px";
      });

      hitBox.addEventListener("mouseleave", () => {
        crosshair.classList.add("hidden");
        tooltipEl.classList.add("hidden");
      });
    }
  }

  function renderGraphs() {
    const dataset = buildGraphDataset();
    const { filteredRows, buckets } = dataset;

    const titleEl = document.getElementById("graph-main-title");
    const subTitleEl = document.getElementById("graph-main-subtitle");
    if (titleEl && subTitleEl) {
      const metricName = graphState.yMetric === "impacted_customers" ? "Customer Impact" : (graphState.yMetric === "severity_score" ? "Severity Weighted Outages" : "Outages / Incidents");
      const dimName = graphState.xDim === "time" ? `Timeline (${graphState.range})` : `by ${graphState.xDim.toUpperCase()}`;
      titleEl.textContent = `${metricName} ${dimName}`;
      subTitleEl.textContent = `Grouped by ${graphState.zGroup.toUpperCase()} · ${filteredRows.length} total events matching filters`;
    }

    const summaryBar = document.getElementById("graphs-summary-bar");
    if (summaryBar) {
      const totalCount = filteredRows.length;
      const unresCount = filteredRows.filter(r => (r.severity || "").toLowerCase() !== "resolved").length;
      const resCount = totalCount - unresCount;
      
      let maxBucket = null;
      for (const b of buckets) {
        if (!maxBucket || b.total > maxBucket.total) maxBucket = b;
      }

      summaryBar.innerHTML = `
        <div class="graph-stat-card">
          <span class="graph-stat-label">Filtered Incidents</span>
          <span class="graph-stat-val">${totalCount}</span>
          <span class="graph-stat-sub">${unresCount} active · ${resCount} resolved</span>
        </div>
        <div class="graph-stat-card">
          <span class="graph-stat-label">Active / Unresolved</span>
          <span class="graph-stat-val" style="color:var(--accent)">${unresCount}</span>
          <span class="graph-stat-sub">${totalCount > 0 ? Math.round((unresCount / totalCount) * 100) : 0}% of filtered total</span>
        </div>
        <div class="graph-stat-card">
          <span class="graph-stat-label">Peak Period</span>
          <span class="graph-stat-val" style="font-size:16px">${maxBucket && maxBucket.total > 0 ? esc(maxBucket.label) : "None"}</span>
          <span class="graph-stat-sub">${maxBucket && maxBucket.total > 0 ? maxBucket.total + " events" : "No peaks"}</span>
        </div>
        <div class="graph-stat-card">
          <span class="graph-stat-label">Active Providers</span>
          <span class="graph-stat-val">${graphState.providers.size}</span>
          <span class="graph-stat-sub">of ${SERVICES.length} total feeds</span>
        </div>
      `;
    }

    drawSvgChart(dataset);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
