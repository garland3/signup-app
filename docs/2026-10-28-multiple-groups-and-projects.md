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
   that project's models).
4. People sign out and back in; the project appears in Create API Key.

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
