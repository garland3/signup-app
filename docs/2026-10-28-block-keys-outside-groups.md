# Block keys of people outside the security groups (2026-10-28)

## Problem

Projects come from people's sign-in (`TEAM_SOURCE=claims`), so the app only
learns that someone left a project's group when they next use the app. Their
keys go straight to LiteLLM and keep working, and keep spending tokens, until
they expire or someone deletes them.

## What this adds

A background check inside the signup app, off by default
(`RECONCILE_ENABLED`), that runs every `RECONCILE_INTERVAL_SECONDS` (15 minutes
by default):

1. Lists LiteLLM teams and keeps the projects: `team_id` or `team_alias`
   starting with `TEAM_GROUP_PREFIX`, as at sign-in.
2. For each project, reads its current members from Microsoft Graph: the
   group (by object ID, or exact name) or, with `GROUPS_FIELD=roles`, the
   people holding that app role on the sign-in app (directly or through an
   assigned group). Disabled accounts don't count.
3. Lists the keys in the team and **blocks** each key whose owner isn't a
   member. Blocking stops the key at once and can be undone; a person back in
   the group unblocks their key in the app (which re-checks their groups).
4. Optionally (`RECONCILE_KEYS_WITHOUT_TEAM`), blocks keys made before teams
   whose owner is in no project.

## Why this lives in the signup app

- **It already owns the rules.** The app decides which LiteLLM teams are
  projects (`TEAM_GROUP_PREFIX`, matching by `team_id` or `team_alias`) and
  which LiteLLM user ID a person gets (email, `oid`, `STRIP_USER_DOMAIN`). A
  separate reconciler would copy those rules, and any drift means blocking
  the wrong people or missing the right ones. Here both use the same code
  and the same settings.
- **No new service to run.** It reuses the app's LiteLLM admin key and its
  Entra app registration. There's no extra image to build, scan for CVEs and
  patch, no second set of secrets, no extra deployment. Turning it on is a
  setting and a redeploy of the app you already deploy.
- **One place to look.** Its `reconcile_*` lines go to the same audit log
  as sign-ins and key changes, and its settings are checked at startup with
  the rest (missing Graph tenant, too short an interval, an ID field that
  can't be matched).
- **Blocking and unblocking stay consistent.** The reconciler blocks; the
  app is where a person back in the group unblocks, and it re-checks their
  groups first. Both sides apply the same membership rules.
- **Small and tested together.** About 300 lines, with tests in the same
  suite and against the same LiteLLM mock as the rest of the key code.

Trade-offs, and how they're handled:

- **One replica.** A loop inside the app runs once per replica. The app
  already needs a single replica (sessions are in memory). If it's ever
  scaled out, run the same code as a Kubernetes CronJob instead
  (`python -m app.core.key_reconciler` does one pass) and leave
  `RECONCILE_ENABLED=false` in the app.
- **Broader Entra permissions for the app.** Reading group members needs
  Graph application permissions. To keep the sign-in registration minimal,
  give a separate registration those permissions and set
  `GRAPH_CLIENT_ID`/`GRAPH_CLIENT_SECRET`.
- **Can't take the app down.** It runs as a background task, and any error
  in a run is logged and the next run tries again; requests are unaffected.
- **Paused while the app is down.** Keys keep working in LiteLLM during an
  outage either way; the next run after startup catches up.

## Safety

- **Dry run by default** (`RECONCILE_DRY_RUN=true`): logs
  `reconcile_would_block` lines and changes nothing.
- **Nothing changes for a project that can't be checked**: Graph errors, a
  group or role that isn't found, an empty group, or a name two teams share.
- **A cap per run** (`RECONCILE_MAX_BLOCKS_PER_RUN`, default 50): a run that
  would block more blocks none and logs `reconcile_refused`, so a
  misconfiguration can't block everyone.
- Keys with no owner (service keys made in LiteLLM) are never touched.
- Startup refuses unsafe settings: no Graph tenant or credentials, an
  interval under 60 seconds, teams not from claims, or
  `OAUTH_USER_ID_FIELD=sub` (Entra's `sub` can't be matched to directory
  users).

## Setup

1. Entra ID: give the app registration the **application** permissions
   `GroupMember.Read.All` and `User.Read.All` (plus `Application.Read.All`
   with app roles), and grant admin consent.
2. Signup app: `RECONCILE_ENABLED=true`, `GRAPH_TENANT_ID=<tenant>`, keep
   `RECONCILE_DRY_RUN=true`, and redeploy.
3. Check the log: `kubectl logs deploy/<app> | grep reconcile_` shows each
   run's counts and every key it would block.
4. When the list is right, set `RECONCILE_DRY_RUN=false` and redeploy.

## Not included

- Deleting keys, or removing people from LiteLLM teams
  (`/team/member_delete`). Blocking is reversible; deletion could follow
  once the blocking has run for a while.
- Unblocking automatically when someone rejoins (they unblock in the app).
- Directories other than Entra ID (Keycloak would need its admin API).

## Open questions

- Interval: is 15 minutes right for how quickly access must stop?
- Should keys without a team be blocked once projects are rolled out to
  everyone?
