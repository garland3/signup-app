let currentKeys = [];

async function loadUser() {
    const r = await fetch("/api/me");
    if (r.ok) {
        const data = await r.json();
        document.getElementById("user-email").textContent = data.email;
    }
}

async function loadKeys() {
    const r = await fetch("/api/keys");
    if (!r.ok) return;
    currentKeys = await r.json();
    renderKeys();
}

function renderKeys() {
    const tbody = document.getElementById("keys-body");
    const noKeys = document.getElementById("no-keys");
    const table = document.getElementById("keys-table");

    if (currentKeys.length === 0) {
        table.classList.add("hidden");
        noKeys.classList.remove("hidden");
        return;
    }

    table.classList.remove("hidden");
    noKeys.classList.add("hidden");

    tbody.innerHTML = currentKeys.map(function(k) {
        return '<tr>' +
            '<td>' + escapeHtml(k.name) + '</td>' +
            '<td><span class="key-prefix">' + escapeHtml(k.prefix) + '...</span></td>' +
            '<td>' + formatDate(k.created_at) + '</td>' +
            '<td><span class="' + (k.is_active ? 'status-active' : 'status-revoked') + '">' +
                (k.is_active ? 'Active' : 'Revoked') +
            '</span></td>' +
            '<td>' +
                (k.is_active
                    ? '<button class="danger" onclick="revokeKey(\'' + k.id + '\')">Revoke</button>'
                    : '') +
            '</td>' +
        '</tr>';
    }).join("");
}

function showCreateModal() {
    document.getElementById("key-name").value = "";
    document.getElementById("create-modal").classList.remove("hidden");
    document.getElementById("key-name").focus();
}

function hideCreateModal() {
    document.getElementById("create-modal").classList.add("hidden");
}

async function createKey() {
    var name = document.getElementById("key-name").value.trim();
    if (!name) return;

    var r = await fetch("/api/keys", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name: name}),
    });

    if (!r.ok) return;

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

async function revokeKey(id) {
    if (!confirm("Revoke this API key? This cannot be undone.")) return;
    var r = await fetch("/api/keys/" + id, {method: "DELETE"});
    if (r.ok) await loadKeys();
}

function formatDate(iso) {
    return new Date(iso).toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
    });
}

function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

loadUser();
loadKeys();
