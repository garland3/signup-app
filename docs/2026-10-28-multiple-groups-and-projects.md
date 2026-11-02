# Multiple security groups and projects (2026-10-28)

## Goal

Several projects, each tied to one Entra ID security group and its own list of
models. A person in several of those groups sees all of their projects in
Create API Key, picks one per key, and the key can only use that project's
models. Adding a project should not need a code change.

## What already works

With `FEATURE_TEAMS_ENABLED=true` and `TEAM_SOURCE=claims`:

- **One person, several projects.** At sign-in the app keeps every group in
  the token that starts with `TEAM_GROUP_PREFIX`, and every one that names a
  LiteLLM team (by `team_alias` or `team_id`) becomes a project. With one
  project the dialog picks it; with several it shows a project dropdown
  (`static/app.js`, `renderTeamSelect`).
- **Each project has its own models.** The model list in the dialog follows
  the chosen project, and the server checks it on create and change
  (PR #70).
- **Membership is re-checked** with the identity provider before a key is
  made or changed, so leaving a group stops new keys there.
- **One cap across projects.** `MAX_ACTIVE_KEYS_PER_USER` counts a person's
  active keys in all projects together (blank: no cap).

## The gap

`TEAM_GROUP_PREFIX` is one string, and a group only counts if it **starts
with** it. That works for group names (`genesis-mag-users`,
`genesis-other`, all starting with `genesis-`). It does not work for Entra
group **object IDs**: they are random GUIDs with no common start. Today's
deployment sets the prefix to the one object ID of `genesis-mag-users`, so a
second project's group can never match.

A second Entra limit: when someone is in more than about 200 groups, Entra
leaves the `groups` claim out of the token. The app then can't tell which
projects they have and refuses key changes (403) rather than guess.

## Options

### A. Entra app roles (no code change; recommended to try first)

Each project is an **app role** on the signup app's Entra registration, and
the role is assigned to that project's security group. The token's `roles`
claim then lists role **values** (names), not object IDs.

- Entra (per project): App registrations, then the signup app, then App
  roles: create a role, allowed member types Users/Groups, value
  `genesis-<project>`. Enterprise applications, then the signup app, then
  Users and groups: assign the project's security group to that role.
- LiteLLM (per project): a team with `team_alias` equal to the role value,
  for example `genesis-mag-users`, and its `models`.
- Signup app (once): `GROUPS_FIELD=roles`, `TEAM_GROUP_PREFIX=genesis-`,
  `HIDE_TEAM_NAME_PREFIX` as preferred.
- Pros: no code change, no redeploy per project, readable names, roles are
  not subject to the 200-group limit, and only what is assigned to this app
  appears in the token.
- Cons: someone with Entra admin rights creates a role per project.

### B. Match group object IDs to LiteLLM team IDs (small code change)

Keep the `groups` claim with object IDs, and let a group count when it is
exactly a LiteLLM team's `team_id`, with no prefix.

- New setting, for example `TEAM_GROUP_MATCH=team_id` (default `prefix`,
  today's behaviour). In `team_id` mode, `TEAM_GROUP_PREFIX` is not needed
  and a group matches only a team whose `team_id` equals it (aliases are
  ignored, so no team name can capture a group's members).
- LiteLLM (per project): a team with `team_id` = the group's object ID,
  `team_alias` = the name people see, and its `models` (as done for
  `genesis-mag-users`).
- Signup app (once): `TEAM_GROUP_MATCH=team_id`; no redeploy per project.
- Entra: groups claim set to "Groups assigned to the application", with
  each project group assigned to the app, to stay clear of the 200-group
  limit.
- Pros: works with the object IDs already in use; one setting.
- Cons: a code change, tests, a release.

### C. A list of prefixes (smallest code change)

Let `TEAM_GROUP_PREFIX` take a comma-separated list, each a prefix or a full
object ID.

- Cons: every new project means editing the list and redeploying. Kept as
  an option, not recommended.

## Recommendation

1. Fix today's sign-in first: with `OAUTH_CLAIMS_SOURCE=id_token`, sign-in
   fails with "Sign-in could not be verified". The app log line
   `ID token verification failed: ...` names the cause (usually the issuer).
2. Try option A for the second project. If app roles are not workable for
   the Entra admins, implement option B.

## Supporting changes (optional)

- **Log the groups received at sign-in** (`team_claims_sign_in`: what the
  provider sent and what matched), so "You aren't in any project" can be
  traced without access to Entra or Keycloak.
- **Versioned static file URLs** (`style.css?v=<hash>`), so browsers load
  each release's stylesheet instead of a cached one.
- **Document `TRUSTED_ORIGINS`** in `.env.example`.

## Adding a project, end to end (with option A)

1. Entra: create the security group, add people.
2. Entra: create the app role `genesis-<project>` and assign the group to it.
3. LiteLLM: create the team (`team_alias` = `genesis-<project>`, `models` =
   that project's models). Today by hand; with the `ensure` command or
   automatic creation proposed below, without `curl`.
4. People sign out and back in; the project appears in Create API Key.

## Creating LiteLLM teams for project groups

Today a LiteLLM admin creates each project's team by hand with `curl` against
LiteLLM's `/team/new`. The first project showed how that goes wrong:

- **Wrong value pasted.** A team was created with the literal text
  `<OBJECT-ID>` as its `team_id`, so it matched nobody.
- **Wrong address.** From the jump host, `LITELLM_BASE_URL` pointed at a
  different service, which answered HTTP 200 with unrelated content.
- **Duplicates.** Running `/team/new` twice creates two teams with the same
  name, and the app then treats that name as matching no project.
- **Model names.** Nothing checks that the models listed exist on the proxy.

### Which groups are meant for LiteLLM

Before automating anything, there has to be one clear signal that a group is
a LiteLLM project. Options:

| Signal | How | Notes |
|---|---|---|
| **Assigned to the signup app** (recommended) | With option A: the group is assigned an app role whose value starts with `genesis-`. Without app roles: the group is assigned to the enterprise application | Entra admins already control assignments; it's the same step that gives people access |
| Name convention | Group display name starts with `genesis-` | Easy, but anyone who can create or rename groups can create a project |
| A list in the signup app's settings | `PROJECT_GROUPS=<id>,<id>` | Explicit, but every project needs a redeploy |

### Step 1: a safe "ensure team" command (small)

A command in the signup app image that creates a project's team correctly,
run from the app's pod (so it uses the app's own LiteLLM address and key):

```bash
# groups claim (today): the group's object ID
kubectl exec deploy/signup-main -c main -- \
  python -m app.core.team_provisioning ensure \
    --group <object ID> --models claude-sonnet-4.5,gpt-4o

# app roles (option A): the role's value
kubectl exec deploy/signup-main -c main -- \
  python -m app.core.team_provisioning ensure \
    --role genesis-fusion-sim --models claude-sonnet-4.5,gpt-4o
```

It would:

- look the group or app role up in Microsoft Graph and refuse if it doesn't
  exist, so a typo or a placeholder can't create a dead team;
- name the team the way the app matches it: `team_id` = the group's object
  ID (groups claim), or `team_alias` = the role value (option A); for a
  group, the group's display name becomes the alias, so people see a
  readable project name;
- refuse a name that doesn't start with `TEAM_GROUP_PREFIX`, since the app
  would never treat that team as a project;
- refuse a name already used by any team as its `team_alias` **or** its
  `team_id` (the app matches either, and a name that could mean two teams
  matches none); if the team already exists exactly as asked, do nothing and
  say so, so running it twice is safe;
- require `--models`, and refuse model names the proxy doesn't list
  (`/v1/models`). An empty list is refused: in LiteLLM a team with no
  models may use **every** model on the proxy. Giving a project every model
  needs an explicit `--all-models`;
- set no members: with projects from sign-in, the app adds people to the
  team when they make their first key there;
- print what it did, and log `team_provisioned` in the audit log;
- support `--dry-run` to show what it would create.

It does not create the Entra group or app role, or assign them: that needs
Graph write permissions (`Application.ReadWrite.All` to add roles,
`AppRoleAssignment.ReadWrite.All` to assign them), which the app shouldn't
hold. Entra admins keep doing those steps.

### Step 2: create missing teams automatically (opt-in)

Extend the reconciler (PR #74), which already reads Graph on a schedule:

- For every group or app role designated as a project (signal above) with no
  LiteLLM team, create one, using the same rules as step 1.
- Models for a new team come from `PROJECT_DEFAULT_MODELS` (for example
  `claude-sonnet-4.5`), optionally overridden per project by a small mapping
  (`PROJECT_MODELS={"genesis-fusion-sim": ["claude-sonnet-4.5","gpt-4o"]}`).
  Admins can still change a team's models in LiteLLM afterwards; the
  reconciler only sets them on creation and never overwrites them.
- Off by default (`PROVISION_TEAMS=false`), with its own dry run, logging
  `team_would_create` until switched on.
- Never deletes teams. A team whose group or role is gone is logged
  (`team_orphaned`) for an admin to decide; its members' keys are already
  blocked by the reconciler.
- Optional team settings on creation: a budget (`max_budget`,
  `budget_duration`) from `PROJECT_DEFAULT_BUDGET`, so a new project can't
  spend without limit.

With this, adding a project becomes: create the group (and app role), assign
it, and wait for the next run. No `curl`, no LiteLLM admin step.

### Step 3: an admin page (optional, later)

A page in the signup app, shown only to holders of an admin app role (for
example `signup-admin`), listing the designated groups and roles, which have
teams, and their models, with a button to create a missing team and pick its
models (the same picker as Create API Key). Only worth building if steps 1
and 2 leave too much for people to do from the command line.

### Gaps and risks

- **Depends on PR #74.** Steps 1 and 2 reuse its Microsoft Graph client and
  Graph read permissions (`GroupMember.Read.All`, `User.Read.All`, and
  `Application.Read.All` for app roles). Without PR #74 merged, step 1 needs
  its own Graph code.
- **With the groups claim, only one project still works.**
  `TEAM_GROUP_PREFIX` holds one object ID, so step 1 can create teams for
  other groups, but nobody would get those projects until option A (or B)
  is in place. Use the command for the current project now, and for new
  projects after the migration.
- **"Every model" by accident.** Covered above: an empty models list is
  refused unless `--all-models` is given. Step 2 needs the same guard: no
  `PROJECT_DEFAULT_MODELS` and no per-project entry means log and skip, not
  create.
- **Spending.** Automatic creation (step 2) turns "someone assigned a role"
  into "a project that can spend". `PROJECT_DEFAULT_BUDGET` should be set
  before step 2 is switched on, and step 2 should refuse to run without it.
- **Renames.** Renaming a role value (option A) looks like a new project:
  step 2 would create a new team, and the old team's keys would be blocked
  as their owners no longer match it. Rename by updating the team's
  `team_alias` in LiteLLM at the same time (`/team/update`), and document
  that procedure.
- **Two creators at once.** The command and step 2 could both create the
  same team in the same minute. Each re-reads the team list right before
  creating, and step 2 runs in one place (one replica, or one CronJob), so
  this is unlikely; if it happens, the app treats the shared name as no
  project and the logs show both teams, for an admin to delete one.
- **Undoing a mistake.** A team created wrongly and still without keys can
  be deleted (`/team/delete`); the command could offer `remove` that refuses
  when the team has keys.
- **Keycloak.** Graph is Entra-only. A deployment whose groups come from
  Keycloak would need the same steps against Keycloak's admin API; out of
  scope.

### Testing (steps 1 and 2)

- A group or role that doesn't exist, a name without the prefix, a name
  another team uses as alias or ID, an unknown model, and an empty models
  list are each refused, and nothing is created.
- Running `ensure` twice creates one team.
- Step 2 creates teams only for designated groups or roles, skips those with
  no models or budget configured, never changes an existing team's models,
  and logs orphaned teams without deleting them.
- Dry run creates nothing.

### Recommendation

Do step 1 with the option A migration (PR #75): it removes the copy-paste
risks right away and is small, but it needs PR #74's Graph client first.
Add step 2 once the reconciler is running in dry run, so both read Graph with
the same credentials, and only with a default budget set. Leave step 3 until
there's a need.

## Testing (for option B, if chosen)

- A person in two project groups sees both projects; keys in each are
  limited to that project's models.
- A group ID with no matching team is ignored; an alias equal to a group ID
  does not match in `team_id` mode.
- Leaving one group removes only that project.
- Group overage still refuses (403).
- `prefix` mode behaves exactly as today.

## Open questions

- Can the Entra admins create and assign app roles (option A)?
- Should project names shown to people be the role value, or a friendlier
  team alias (option B allows any alias)?
- Who creates LiteLLM teams for new projects, and with which models?
  (Steps 1 and 2 under "Creating LiteLLM teams" propose an answer.)
- Which signal marks a group as a LiteLLM project: assignment to the signup
  app (recommended), a name convention, or a list in the app's settings?
- What default models and budget should a new project's team get?
