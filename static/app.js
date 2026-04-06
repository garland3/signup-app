var currentKeys = [];
var appConfig = {app_name: "API Keys", required_metadata: [], max_active_keys: null};

async function loadConfig() {
    var r = await fetch("/api/config");
    if (!r.ok) return;
    appConfig = await r.json();
    var title = appConfig.app_name || "API Keys";
    document.getElementById("app-title").textContent = title;
    document.title = title;
    renderMetadataInputs();
    renderMetadataHeaders();
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
    container.innerHTML = "";
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

function previewKeyName(name) {
    if (!name) return "";
    if (name.length <= 10) return name;
    return name.slice(0, 5) + "..." + name.slice(-5);
}

function prettyLabel(field) {
    return field.replace(/_/g, " ").replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

async function loadUser() {
    var r = await fetch("/api/me");
    if (r.ok) {
        var data = await r.json();
        document.getElementById("user-email").textContent = data.email;
    }
}

async function loadKeys() {
    var r = await fetch("/api/keys");
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

    tbody.innerHTML = currentKeys.map(function(k) {
        var status = k.is_active ? "Active" : "Inactive";
        var statusClass = k.is_active ? "status-active" : "status-revoked";
        var spend = k.spend != null ? "$" + Number(k.spend).toFixed(2) : "-";
        var duration = k.duration || "-";
        var actions = k.is_active
            ? '<button class="danger" onclick="deleteKey(\'' + escapeAttr(k.id) + '\')">Delete</button>'
            : '';

        var metaCells = metaFields.map(function(f) {
            var v = (k.metadata && k.metadata[f]) ? k.metadata[f] : "-";
            return '<td>' + escapeHtml(v) + '</td>';
        }).join("");

        return '<tr>' +
            '<td><span class="key-prefix">' + escapeHtml(previewKeyName(k.name)) + '</span></td>' +
            '<td>' + formatDate(k.created_at) + '</td>' +
            '<td>' + escapeHtml(duration) + '</td>' +
            '<td>' + spend + '</td>' +
            '<td><span class="' + statusClass + '">' + status + '</span></td>' +
            metaCells +
            '<td>' + actions + '</td>' +
        '</tr>';
    }).join("");
}

function showCreateModal() {
    document.getElementById("key-name").value = "";
    document.getElementById("key-duration").value = "";
    document.getElementById("key-budget").value = "";
    (appConfig.required_metadata || []).forEach(function(f) {
        var el = document.getElementById("meta-" + f);
        if (el) el.value = "";
    });
    document.getElementById("create-modal").classList.remove("hidden");
    document.getElementById("key-name").focus();
}

function hideCreateModal() {
    document.getElementById("create-modal").classList.add("hidden");
}

async function createKey() {
    var name = document.getElementById("key-name").value.trim();
    if (!name) return;

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
        alert("Please fill in required fields: " + missing.join(", "));
        return;
    }
    if (Object.keys(metadata).length > 0) body.metadata = metadata;

    var r = await fetch("/api/keys", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
    });

    if (!r.ok) {
        var err = await r.json().catch(function() { return {}; });
        alert("Failed to create key: " + (err.detail || r.statusText));
        return;
    }

    var data = await r.json();
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
    var r = await fetch("/api/keys/" + encodeURIComponent(token), {method: "DELETE"});
    if (r.ok) await loadKeys();
}

function formatDate(dateStr) {
    if (!dateStr) return "-";
    return new Date(dateStr).toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
    });
}

function escapeHtml(str) {
    if (str == null) return "";
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    if (!str) return "";
    return str.replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

loadConfig().then(function() {
    loadUser();
    loadKeys();
});
