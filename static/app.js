var currentKeys = [];
var appConfig = {
    app_name: "API Keys",
    required_metadata: [],
    max_active_keys: null,
    nav_links: [],
    show_spend: true,
};
var rootPathMeta = document.querySelector('meta[name="app-root-path"]');
var ROOT_PATH = rootPathMeta ? rootPathMeta.getAttribute("content") : "";
// Exposed for any external tooling/tests that still read window.APP_ROOT_PATH.
window.APP_ROOT_PATH = ROOT_PATH;
var API_BASE = ROOT_PATH + "/api";

async function loadConfig() {
    var r = await fetch(API_BASE + "/config");
    if (!r.ok) return;
    appConfig = await r.json();
    if (appConfig.show_spend == null) appConfig.show_spend = true;
    var title = appConfig.app_name || "API Keys";
    document.getElementById("app-title").textContent = title;
    document.title = title;
    renderNavLinks();
    renderMetadataInputs();
    renderSpendHeader();
    renderMetadataHeaders();
}

function renderSpendHeader() {
    var th = document.getElementById("spend-col-header");
    if (!th) return;
    if (appConfig.show_spend) {
        th.classList.remove("hidden");
    } else {
        th.classList.add("hidden");
    }
}

function renderNavLinks() {
    var nav = document.getElementById("nav-links");
    if (!nav) return;
    var links = appConfig.nav_links || [];
    nav.textContent = "";
    if (links.length === 0) {
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

function renderMetadataHeaders() {
    var head = document.getElementById("keys-table-head");
    // Remove previously-added metadata headers (marked with data-meta)
    Array.prototype.slice.call(head.querySelectorAll("th[data-meta]")).forEach(function(th) {
        th.parentNode.removeChild(th);
    });
    // Insert metadata headers before the last (actions) column
    var fields = appConfig.required_metadata || [];
    var lastTh = head.lastElementChild;
    fields.forEach(function(f) {
        var th = document.createElement("th");
        th.setAttribute("data-meta", f);
        th.textContent = prettyLabel(f);
        head.insertBefore(th, lastTh);
    });
}

function renderMetadataInputs() {
    var container = document.getElementById("required-metadata-fields");
    container.textContent = "";
    var fields = appConfig.required_metadata || [];
    fields.forEach(function(f) {
        var label = document.createElement("label");
        label.setAttribute("for", "meta-" + f);
        label.textContent = prettyLabel(f) + " (required)";
        var input = document.createElement("input");
        input.type = "text";
        input.id = "meta-" + f;
        input.setAttribute("data-meta-field", f);
        container.appendChild(label);
        container.appendChild(input);
    });
}

function prettyLabel(field) {
    return field.replace(/_/g, " ").replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

function showToast(message, type) {
    var container = document.getElementById("toast-container");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + (type || "info");
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    // textContent (not innerHTML) so server-supplied error strings can
    // never escape into markup.
    toast.textContent = message;
    container.appendChild(toast);
    // Force a reflow so the initial (hidden) state is committed before
    // we add the visible class, allowing the CSS transition to run.
    void toast.offsetWidth;
    toast.classList.add("toast-visible");
    setTimeout(function() {
        toast.classList.remove("toast-visible");
        setTimeout(function() {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 250);
    }, 4000);
}

async function loadUser() {
    var r = await fetch(API_BASE + "/me");
    if (r.ok) {
        var data = await r.json();
        document.getElementById("user-email").textContent = data.email;
    }
}

async function loadKeys() {
    var r = await fetch(API_BASE + "/keys");
    if (!r.ok) return;
    currentKeys = await r.json();
    renderKeys();
}

function renderKeys() {
    var tbody = document.getElementById("keys-body");
    var noKeys = document.getElementById("no-keys");
    var table = document.getElementById("keys-table");

    if (currentKeys.length === 0) {
        table.classList.add("hidden");
        noKeys.classList.remove("hidden");
        return;
    }

    table.classList.remove("hidden");
    noKeys.classList.add("hidden");

    var metaFields = appConfig.required_metadata || [];

    // Rebuild rows using DOM APIs rather than HTML concatenation so no
    // untrusted value ever enters an HTML/JS parsing context.
    tbody.textContent = "";
    currentKeys.forEach(function(k) {
        var tr = document.createElement("tr");
        appendCell(tr, k.name);

        var prefixCell = document.createElement("td");
        var prefixSpan = document.createElement("span");
        prefixSpan.className = "key-prefix";
        prefixSpan.textContent = k.prefix || "";
        prefixCell.appendChild(prefixSpan);
        tr.appendChild(prefixCell);

        appendCell(tr, formatDate(k.created_at));
        appendCell(tr, k.duration || "-");

        if (appConfig.show_spend) {
            var spend = k.spend != null ? "$" + Number(k.spend).toFixed(2) : "-";
            appendCell(tr, spend);
        }

        var statusCell = document.createElement("td");
        var statusSpan = document.createElement("span");
        statusSpan.className = k.is_active ? "status-active" : "status-revoked";
        statusSpan.textContent = k.is_active ? "Active" : "Inactive";
        statusCell.appendChild(statusSpan);
        tr.appendChild(statusCell);

        metaFields.forEach(function(f) {
            var v = (k.metadata && k.metadata[f]) ? k.metadata[f] : "-";
            appendCell(tr, v);
        });

        var actionsCell = document.createElement("td");
        if (k.is_active) {
            var btn = document.createElement("button");
            btn.className = "danger";
            btn.textContent = "Delete";
            btn.addEventListener("click", function() { deleteKey(k.id); });
            actionsCell.appendChild(btn);
        }
        tr.appendChild(actionsCell);

        tbody.appendChild(tr);
    });
}

function appendCell(tr, text) {
    var td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    tr.appendChild(td);
}

function showCreateModal() {
    document.getElementById("key-name").value = "";
    document.getElementById("key-duration").value = "";
    document.getElementById("key-budget").value = "";
    (appConfig.required_metadata || []).forEach(function(f) {
        var el = document.getElementById("meta-" + f);
        if (el) el.value = "";
    });
    clearCreateError();
    document.getElementById("create-modal").classList.remove("hidden");
    document.getElementById("key-name").focus();
}

function hideCreateModal() {
    document.getElementById("create-modal").classList.add("hidden");
    clearCreateError();
}

function showCreateError(message) {
    var el = document.getElementById("create-error");
    if (!el) return;
    // Use textContent so any server-supplied error string is treated as
    // plain text and can never escape into markup.
    el.textContent = message;
    el.classList.remove("hidden");
}

function clearCreateError() {
    var el = document.getElementById("create-error");
    if (!el) return;
    el.textContent = "";
    el.classList.add("hidden");
}

function setCreateSubmitBusy(busy) {
    var btn = document.getElementById("create-submit-btn");
    var cancel = document.getElementById("create-cancel-btn");
    if (!btn) return;
    if (busy) {
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        // Stash the label so we can restore it after the request lands;
        // any text we set here is just a visible hint that the click
        // was received and we're waiting on the server.
        if (!btn.dataset.originalLabel) {
            btn.dataset.originalLabel = btn.textContent;
        }
        btn.textContent = "Creating\u2026";
        if (cancel) cancel.disabled = true;
    } else {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        if (btn.dataset.originalLabel) {
            btn.textContent = btn.dataset.originalLabel;
            delete btn.dataset.originalLabel;
        }
        if (cancel) cancel.disabled = false;
    }
}

async function createKey() {
    var nameEl = document.getElementById("key-name");
    var name = nameEl.value.trim();
    if (!name) return;
    // The input has a pattern="[A-Za-z0-9\-]+" constraint; surface the
    // browser's native validation message so the user knows why the
    // submission is blocked rather than silently sending bad input the
    // server would then have to sanitize.
    if (!nameEl.checkValidity()) {
        nameEl.reportValidity();
        return;
    }

    clearCreateError();

    var body = {name: name};

    var duration = document.getElementById("key-duration").value.trim();
    if (duration) body.duration = duration;

    var budget = document.getElementById("key-budget").value.trim();
    if (budget) body.max_budget = parseFloat(budget);

    var metadata = {};
    var missing = [];
    (appConfig.required_metadata || []).forEach(function(f) {
        var el = document.getElementById("meta-" + f);
        var v = el ? el.value.trim() : "";
        if (!v) {
            missing.push(prettyLabel(f));
        } else {
            metadata[f] = v;
        }
    });
    if (missing.length > 0) {
        showCreateError("Please fill in required fields: " + missing.join(", "));
        return;
    }
    if (Object.keys(metadata).length > 0) body.metadata = metadata;

    setCreateSubmitBusy(true);
    var r;
    try {
        r = await fetch(API_BASE + "/keys", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
        });
    } catch (e) {
        setCreateSubmitBusy(false);
        showCreateError("Network error. Please try again.");
        return;
    }

    if (!r.ok) {
        var err = await r.json().catch(function() { return {}; });
        // 409 = duplicate key name. Keep the modal open, surface the
        // error inline, and focus the name field so the user can pick a
        // different name without retyping anything else.
        if (r.status === 409) {
            setCreateSubmitBusy(false);
            showCreateError(err.detail || "A key with this name already exists. Please choose a different name.");
            nameEl.focus();
            nameEl.select();
            return;
        }
        setCreateSubmitBusy(false);
        showCreateError("Failed to create key: " + (err.detail || r.statusText));
        return;
    }

    var data = await r.json();
    setCreateSubmitBusy(false);
    hideCreateModal();

    document.getElementById("full-key").textContent = data.key;
    document.getElementById("show-key-modal").classList.remove("hidden");

    await loadKeys();
}

function hideShowKeyModal() {
    document.getElementById("show-key-modal").classList.add("hidden");
    document.getElementById("full-key").textContent = "";
}

async function copyKey() {
    var key = document.getElementById("full-key").textContent;
    await navigator.clipboard.writeText(key);
}

async function deleteKey(token) {
    if (!confirm("Delete this API key? The key will be expired immediately.")) return;
    var r = await fetch(API_BASE + "/keys/" + encodeURIComponent(token), {method: "DELETE"});
    if (r.ok) await loadKeys();
}

function formatDate(dateStr) {
    if (!dateStr) return "-";
    return new Date(dateStr).toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
    });
}

document.getElementById("create-btn").addEventListener("click", showCreateModal);
document.getElementById("create-cancel-btn").addEventListener("click", hideCreateModal);
document.getElementById("create-submit-btn").addEventListener("click", createKey);
document.getElementById("copy-key-btn").addEventListener("click", copyKey);
document.getElementById("show-key-done-btn").addEventListener("click", hideShowKeyModal);

loadConfig().then(function() {
    loadUser();
    loadKeys();
});
