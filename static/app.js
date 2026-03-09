var currentKeys = [];

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

    tbody.innerHTML = currentKeys.map(function(k) {
        var status = k.is_active ? "Active" : "Blocked";
        var statusClass = k.is_active ? "status-active" : "status-revoked";
        var spend = k.spend != null ? "$" + Number(k.spend).toFixed(2) : "-";
        var actions = "";

        if (k.is_active) {
            actions += '<button class="danger" onclick="deleteKey(\'' + escapeAttr(k.id) + '\')">Delete</button>';
        }

        return '<tr>' +
            '<td>' + escapeHtml(k.name) + '</td>' +
            '<td><span class="key-prefix">' + escapeHtml(k.prefix) + '</span></td>' +
            '<td>' + formatDate(k.created_at) + '</td>' +
            '<td>' + spend + '</td>' +
            '<td><span class="' + statusClass + '">' + status + '</span></td>' +
            '<td>' + actions + '</td>' +
        '</tr>';
    }).join("");
}

function showCreateModal() {
    document.getElementById("key-name").value = "";
    document.getElementById("key-duration").value = "";
    document.getElementById("key-budget").value = "";
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
    if (!confirm("Delete this API key? This cannot be undone.")) return;
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
    if (!str) return "";
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    if (!str) return "";
    return str.replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

loadUser();
loadKeys();
