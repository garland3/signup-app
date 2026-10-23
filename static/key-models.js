// The create-key dialog's model picker and the keys table's Models column.
// Loaded after app.js and uses its state (teamsEnabled, userTeams, API_BASE)
// and helpers (teamWord).

// A key's models: the ones it was limited to, or "All" (every model its team
// allows, or the proxy's without teams).
function modelsLabel(k) {
    return (k.models && k.models.length) ? k.models.join(", ") : "All";
}

// The team a new key goes to: the only one, or the one picked.
function selectedTeamId() {
    if (!teamsEnabled) return null;
    if (userTeams.length === 1) return userTeams[0].team_id;
    var sel = document.getElementById("key-team");
    return sel && sel.value ? sel.value : null;
}

var modelsRequest = 0;

// Fill the create dialog's model checkboxes with the models the selected team
// allows (GET /api/models). "All models" stays the default; the server checks
// a picked subset again.
async function loadModelOptions() {
    var allBox = document.getElementById("key-models-all");
    var list = document.getElementById("key-models-list");
    var hint = document.getElementById("key-models-hint");
    document.getElementById("key-models-all-text").textContent =
        teamsEnabled ? "All models in this " + teamWord(false) : "All models";
    list.textContent = "";
    hint.textContent = "";
    document.getElementById("key-models-retry").classList.add("hidden");
    allBox.disabled = true;
    var teamId = selectedTeamId();
    if (teamsEnabled && !teamId) return;
    var seq = ++modelsRequest;
    var models;
    try {
        var url = API_BASE + "/models" + (teamId ? "?team_id=" + encodeURIComponent(teamId) : "");
        var r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        models = (await r.json()).models || [];
    } catch (e) {
        if (seq !== modelsRequest) return;
        // Leave "All" as the user set it: a key meant to be limited mustn't
        // quietly become unrestricted. Unchecked, with nothing to pick,
        // Create asks for a choice; they can choose all models or try again.
        allBox.disabled = false;
        syncModelList();
        hint.textContent = "Couldn't load the list of models.";
        document.getElementById("key-models-retry").classList.remove("hidden");
        return;
    }
    if (seq !== modelsRequest) return;  // a newer request (team changed) wins
    if (models.length === 0) {
        allBox.checked = true;
        syncModelList();
        hint.textContent = teamsEnabled
            ? "This " + teamWord(false) + " has no models yet. Ask your administrators."
            : "No models are available yet.";
        return;
    }
    models.forEach(function(m) {
        var label = document.createElement("label");
        label.className = "checkbox-label";
        var box = document.createElement("input");
        box.type = "checkbox";
        box.value = m;
        box.setAttribute("data-model", "");
        label.appendChild(box);
        label.appendChild(document.createTextNode(m));
        list.appendChild(label);
    });
    allBox.disabled = false;
    syncModelList();
}

function syncModelList() {
    var allBox = document.getElementById("key-models-all");
    document.getElementById("key-models-list").classList.toggle("hidden", allBox.checked);
}

// null for "all models", else the models picked.
function selectedModels() {
    var allBox = document.getElementById("key-models-all");
    if (!allBox || allBox.checked) return null;
    return Array.prototype.slice.call(
        document.querySelectorAll("#key-models-list input[data-model]:checked")
    ).map(function(b) { return b.value; });
}

document.getElementById("key-team").addEventListener("change", loadModelOptions);
document.getElementById("key-models-all").addEventListener("change", syncModelList);
document.getElementById("key-models-retry").addEventListener("click", loadModelOptions);
