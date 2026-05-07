var rootPathMeta = document.querySelector('meta[name="app-root-path"]');
var ROOT_PATH = rootPathMeta ? rootPathMeta.getAttribute("content") : "";
window.APP_ROOT_PATH = ROOT_PATH;
var API_BASE = ROOT_PATH + "/api";

var state = {
    period: 30,
    metric: "spend",
    data: null,
};

function $(id) { return document.getElementById(id); }

function fmtMoney(n) {
    if (n == null) return "--";
    var v = Number(n);
    if (!isFinite(v)) return "--";
    return "$" + v.toFixed(v < 1 ? 4 : 2);
}

function fmtInt(n) {
    if (n == null) return "0";
    return Number(n).toLocaleString();
}

function fmtPct(n) {
    if (n == null) return "--";
    return Number(n).toFixed(1) + "%";
}

function fmtDate(s) {
    if (!s) return "";
    try {
        return new Date(s).toLocaleDateString("en-US", {
            year: "numeric", month: "short", day: "numeric",
        });
    } catch (e) { return s; }
}

function showError(message) {
    var el = $("dash-error");
    el.textContent = message;
    el.classList.remove("hidden");
}

function hideError() {
    var el = $("dash-error");
    el.classList.add("hidden");
    el.textContent = "";
}

async function loadConfig() {
    try {
        var r = await fetch(API_BASE + "/config");
        if (!r.ok) return;
        var cfg = await r.json();
        if (cfg.app_name) {
            $("app-title").textContent = cfg.app_name + " - Usage";
            document.title = cfg.app_name + " - Usage";
        }
        renderNavLinks(cfg.nav_links || []);
    } catch (e) { /* config is best-effort */ }
}

function renderNavLinks(links) {
    var nav = $("nav-links");
    nav.textContent = "";
    if (!links.length) {
        nav.classList.add("hidden");
        return;
    }
    links.forEach(function(link) {
        var a = document.createElement("a");
        a.href = link.url;
        a.textContent = link.name;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        nav.appendChild(a);
    });
    nav.classList.remove("hidden");
}

async function loadDashboard() {
    hideError();
    $("dash-loading").classList.remove("hidden");
    $("dash-content").classList.add("hidden");
    var url = API_BASE + "/dashboard?period_days=" + encodeURIComponent(state.period);
    var r;
    try {
        r = await fetch(url);
    } catch (e) {
        $("dash-loading").classList.add("hidden");
        showError("Network error while loading usage data.");
        return;
    }
    if (!r.ok) {
        $("dash-loading").classList.add("hidden");
        var detail = "";
        try { detail = (await r.json()).detail || ""; } catch (e) {}
        showError("Failed to load usage data" + (detail ? ": " + detail : "."));
        return;
    }
    state.data = await r.json();
    $("dash-loading").classList.add("hidden");
    $("dash-content").classList.remove("hidden");
    render();
}

function render() {
    var d = state.data;
    if (!d) return;
    if (d.user_email) {
        $("user-email").textContent = d.user_email;
    }
    renderBudget(d);
    renderTotals(d);
    renderTimeSeries(d);
    renderModels(d);
    renderKeys(d);
    renderProjects(d);
    renderStatusCodes(d);
    renderSchemaHint(d);
}

function renderBudget(d) {
    var b = d.budget || {};
    $("budget-spend").textContent = fmtMoney(b.spend);
    $("budget-period-spend").textContent =
        fmtMoney((d.current_period || {}).spend) +
        " in last " + d.period_days + " days";

    if (b.max_budget == null) {
        $("budget-max").textContent = "Unlimited";
        $("budget-remaining").textContent = "n/a";
        $("budget-pct").textContent = "n/a";
        $("budget-bar").style.width = "0";
    } else {
        $("budget-max").textContent = fmtMoney(b.max_budget);
        $("budget-remaining").textContent = fmtMoney(b.remaining);
        $("budget-pct").textContent = fmtPct(b.consumed_pct);
        var bar = $("budget-bar");
        bar.style.width = Math.min(100, b.consumed_pct || 0) + "%";
        bar.classList.remove("over-soft", "over-hard");
        if (b.consumed_pct != null && b.consumed_pct >= 90) {
            bar.classList.add("over-hard");
        } else if (b.soft_threshold_hit) {
            bar.classList.add("over-soft");
        }
    }

    $("budget-duration").textContent = b.budget_duration
        ? "Window: " + b.budget_duration : "";
    $("budget-reset").textContent = b.budget_reset_at
        ? "Resets " + fmtDate(b.budget_reset_at) : "";

    var warn = $("budget-soft-warning");
    if (b.soft_threshold_hit) {
        warn.classList.remove("hidden");
    } else {
        warn.classList.add("hidden");
    }
}

function renderTotals(d) {
    var life = d.lifetime || {};
    var period = d.current_period || {};
    var tpr = d.tokens_per_request_period || d.tokens_per_request || {};

    $("totals-tokens").textContent = fmtInt(life.total_tokens);
    $("totals-period-tokens").textContent =
        fmtInt(period.total_tokens) + " in last " + d.period_days + " days";

    var p = life.prompt_tokens || 0;
    var c = life.completion_tokens || 0;
    var ratio = (p + c) > 0 ? (p / (p + c) * 100) : 0;
    $("totals-pc-ratio").textContent = ratio.toFixed(0) + "% / " + (100 - ratio).toFixed(0) + "%";
    $("totals-pc-detail").textContent =
        fmtInt(p) + " prompt / " + fmtInt(c) + " completion";

    $("totals-requests").textContent = fmtInt(life.requests);
    var s = life.successful_requests || 0;
    var f = life.failed_requests || 0;
    var total = s + f;
    var successPct = total > 0 ? (s / total * 100).toFixed(1) : "0.0";
    $("totals-success").textContent =
        s + " ok / " + f + " failed (" + successPct + "% success)";

    $("totals-tpr-avg").textContent = fmtInt(Math.round(tpr.avg || 0));
    $("totals-tpr-pct").textContent =
        "p50/p95: " + fmtInt(Math.round(tpr.p50 || 0)) +
        " / " + fmtInt(Math.round(tpr.p95 || 0));
}

function renderTimeSeries(d) {
    var host = $("ts-chart");
    var empty = $("ts-empty");
    var series = d.time_series || [];
    if (!series.length) {
        host.textContent = "";
        empty.classList.remove("hidden");
        return;
    }
    empty.classList.add("hidden");

    var metric = state.metric;
    var values = series.map(function(p) {
        if (metric === "spend") return Number(p.spend) || 0;
        if (metric === "tokens") return Number(p.tokens) || 0;
        return Number(p.requests) || 0;
    });
    var max = values.reduce(function(a, b) { return Math.max(a, b); }, 0);
    if (max === 0) max = 1;

    var width = host.clientWidth || 800;
    var height = 240;
    var padding = {top: 12, right: 16, bottom: 28, left: 56};
    var plotW = width - padding.left - padding.right;
    var plotH = height - padding.top - padding.bottom;

    var n = series.length;
    var stepX = n > 1 ? plotW / (n - 1) : plotW;

    function x(i) { return padding.left + stepX * i; }
    function y(v) { return padding.top + plotH - (v / max) * plotH; }

    var svgNS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("preserveAspectRatio", "none");

    // Horizontal grid lines and y-axis labels (4 ticks)
    for (var t = 0; t <= 4; t++) {
        var yv = max * (t / 4);
        var yy = y(yv);
        var line = document.createElementNS(svgNS, "line");
        line.setAttribute("x1", padding.left);
        line.setAttribute("x2", padding.left + plotW);
        line.setAttribute("y1", yy);
        line.setAttribute("y2", yy);
        line.setAttribute("class", "chart-grid");
        svg.appendChild(line);

        var label = document.createElementNS(svgNS, "text");
        label.setAttribute("x", padding.left - 6);
        label.setAttribute("y", yy + 3);
        label.setAttribute("text-anchor", "end");
        label.setAttribute("class", "chart-label");
        label.textContent = formatAxisValue(yv, metric);
        svg.appendChild(label);
    }

    // Build line + area paths
    var linePoints = [];
    for (var i = 0; i < n; i++) {
        linePoints.push(x(i) + "," + y(values[i]));
    }
    var areaD = "M" + x(0) + "," + (padding.top + plotH) +
        " L" + linePoints.join(" L") +
        " L" + x(n - 1) + "," + (padding.top + plotH) + " Z";
    var area = document.createElementNS(svgNS, "path");
    area.setAttribute("d", areaD);
    area.setAttribute("class", "chart-area");
    svg.appendChild(area);

    var line = document.createElementNS(svgNS, "polyline");
    line.setAttribute("points", linePoints.join(" "));
    line.setAttribute("class", "chart-line");
    svg.appendChild(line);

    // Dots with hover titles
    for (var j = 0; j < n; j++) {
        var dot = document.createElementNS(svgNS, "circle");
        dot.setAttribute("cx", x(j));
        dot.setAttribute("cy", y(values[j]));
        dot.setAttribute("r", 3);
        dot.setAttribute("class", "chart-dot");
        var title = document.createElementNS(svgNS, "title");
        title.textContent = series[j].date + ": " +
            formatAxisValue(values[j], metric);
        dot.appendChild(title);
        svg.appendChild(dot);
    }

    // X-axis date labels (sparse)
    var labelStep = Math.max(1, Math.ceil(n / 6));
    for (var k = 0; k < n; k += labelStep) {
        var xlabel = document.createElementNS(svgNS, "text");
        xlabel.setAttribute("x", x(k));
        xlabel.setAttribute("y", padding.top + plotH + 16);
        xlabel.setAttribute("text-anchor", "middle");
        xlabel.setAttribute("class", "chart-label");
        xlabel.textContent = shortDate(series[k].date);
        svg.appendChild(xlabel);
    }

    host.textContent = "";
    host.appendChild(svg);
}

function formatAxisValue(v, metric) {
    if (metric === "spend") return "$" + Number(v).toFixed(v < 1 ? 3 : 2);
    if (v >= 1000) return (v / 1000).toFixed(1) + "k";
    return String(Math.round(v));
}

function shortDate(s) {
    if (!s) return "";
    try {
        var d = new Date(s + "T00:00:00Z");
        return d.toLocaleDateString("en-US", {month: "short", day: "numeric"});
    } catch (e) { return s; }
}

function renderModels(d) {
    var body = $("models-body");
    body.textContent = "";
    var models = d.models || [];
    if (!models.length) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = 5;
        td.className = "empty-state";
        td.textContent = "No model usage recorded.";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }
    models.forEach(function(m) {
        var tr = document.createElement("tr");
        appendCell(tr, m.key);
        appendCellNum(tr, fmtInt(m.requests));
        appendCellNum(tr, fmtInt(m.total_tokens));
        appendCellNum(tr, fmtMoney(m.spend));
        var perK = m.cost_per_token != null ? "$" + (m.cost_per_token * 1000).toFixed(4) : "--";
        appendCellNum(tr, perK);
        body.appendChild(tr);
    });
}

function renderKeys(d) {
    var body = $("keys-body");
    body.textContent = "";
    var keys = d.keys || [];
    if (!keys.length) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = 5;
        td.className = "empty-state";
        td.textContent = "No spend recorded against any key.";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }
    keys.forEach(function(k) {
        var tr = document.createElement("tr");
        appendCell(tr, k.alias || "(unknown)");
        var prefixCell = document.createElement("td");
        var span = document.createElement("span");
        span.className = "key-prefix";
        span.textContent = k.key_prefix || "";
        prefixCell.appendChild(span);
        tr.appendChild(prefixCell);
        appendCellNum(tr, fmtInt(k.requests));
        appendCellNum(tr, fmtInt(k.total_tokens));
        appendCellNum(tr, fmtMoney(k.spend));
        body.appendChild(tr);
    });
}

function renderProjects(d) {
    var host = $("projects-tree");
    host.textContent = "";
    var projects = d.projects || [];
    if (!projects.length) {
        var p = document.createElement("p");
        p.className = "empty-state";
        p.textContent = "No tagged usage yet.";
        host.appendChild(p);
    } else {
        projects.forEach(function(proj) {
            host.appendChild(buildProjectItem(proj));
        });
    }

    var unattr = d.unattributed || {};
    var card = $("unattributed-card");
    if ((unattr.requests || 0) > 0) {
        card.classList.remove("hidden");
        $("unattributed-stats").textContent =
            fmtInt(unattr.requests) + " requests, " +
            fmtInt(unattr.total_tokens) + " tokens, " +
            fmtMoney(unattr.spend) + " spent";
    } else {
        card.classList.add("hidden");
    }
}

function buildProjectItem(proj) {
    var wrap = document.createElement("div");
    wrap.className = "project-item";

    var row = document.createElement("div");
    row.className = "project-row";

    var twist = document.createElement("span");
    twist.className = "twist";
    twist.textContent = ">";
    row.appendChild(twist);

    var name = document.createElement("span");
    name.className = "project-name";
    name.textContent = "Project " + proj.project;
    row.appendChild(name);

    var reqs = document.createElement("span");
    reqs.className = "project-stat";
    reqs.textContent = fmtInt(proj.requests) + " req";
    row.appendChild(reqs);

    var toks = document.createElement("span");
    toks.className = "project-stat";
    toks.textContent = fmtInt(proj.total_tokens) + " tok";
    row.appendChild(toks);

    var spend = document.createElement("span");
    spend.className = "project-spend";
    spend.textContent = fmtMoney(proj.spend);
    row.appendChild(spend);

    var taskHost = document.createElement("div");
    taskHost.className = "task-list hidden";

    var table = document.createElement("table");
    var tbody = document.createElement("tbody");
    (proj.tasks || []).forEach(function(t) {
        var tr = document.createElement("tr");
        var td1 = document.createElement("td");
        var span = document.createElement("span");
        span.className = "task-name";
        if (t.task === "Unattributed") {
            span.classList.add("task-unattr");
            span.textContent = "(no task tag)";
        } else {
            span.textContent = "Task " + t.task;
        }
        td1.appendChild(span);
        tr.appendChild(td1);
        appendCellNum(tr, fmtInt(t.requests));
        appendCellNum(tr, fmtInt(t.total_tokens));
        appendCellNum(tr, fmtMoney(t.spend));
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    taskHost.appendChild(table);

    row.addEventListener("click", function() {
        if (row.classList.contains("expanded")) {
            row.classList.remove("expanded");
            taskHost.classList.add("hidden");
        } else {
            row.classList.add("expanded");
            taskHost.classList.remove("hidden");
        }
    });

    wrap.appendChild(row);
    wrap.appendChild(taskHost);
    return wrap;
}

function renderStatusCodes(d) {
    var body = $("status-body");
    body.textContent = "";
    var codes = d.status_codes || {};
    var entries = Object.keys(codes).map(function(k) {
        return {code: k, count: codes[k]};
    });
    entries.sort(function(a, b) { return b.count - a.count; });
    var total = entries.reduce(function(s, e) { return s + e.count; }, 0);
    if (!total) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = 3;
        td.className = "empty-state";
        td.textContent = "No requests recorded.";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }
    entries.forEach(function(e) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        var isOk = /^[12]\d\d$/.test(e.code);
        td.className = isOk ? "status-row-success" : "status-row-error";
        td.textContent = e.code;
        tr.appendChild(td);
        appendCellNum(tr, fmtInt(e.count));
        appendCellNum(tr, ((e.count / total) * 100).toFixed(1) + "%");
        body.appendChild(tr);
    });
}

function renderSchemaHint(d) {
    var hint = $("schema-hint");
    var schema = d.tag_schema || {};
    hint.textContent = "";
    var p1 = document.createElement("span");
    p1.textContent = "Tag requests with ";
    var c1 = document.createElement("code");
    c1.textContent = "project:1042";
    var p2 = document.createElement("span");
    p2.textContent = " and ";
    var c2 = document.createElement("code");
    c2.textContent = "task:3.1.2";
    var p3 = document.createElement("span");
    p3.textContent = ". Tasks must accompany a Project; otherwise the request is rolled into Unattributed.";
    hint.appendChild(p1);
    hint.appendChild(c1);
    hint.appendChild(p2);
    hint.appendChild(c2);
    hint.appendChild(p3);
}

function appendCell(tr, text) {
    var td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    tr.appendChild(td);
}

function appendCellNum(tr, text) {
    var td = document.createElement("td");
    td.className = "num";
    td.textContent = text == null ? "" : String(text);
    tr.appendChild(td);
}

// Wire up controls
$("period-select").addEventListener("change", function(e) {
    state.period = parseInt(e.target.value, 10) || 30;
    loadDashboard();
});

["spend", "tokens", "requests"].forEach(function(metric) {
    var btn = $("ts-metric-" + metric);
    if (!btn) return;
    btn.addEventListener("click", function() {
        state.metric = metric;
        document.querySelectorAll(".dash-chart-controls .chip-btn").forEach(function(b) {
            b.classList.remove("chip-btn-active");
        });
        btn.classList.add("chip-btn-active");
        renderTimeSeries(state.data);
    });
});

loadConfig();
loadDashboard();
