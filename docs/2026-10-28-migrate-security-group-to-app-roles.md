# Migrate project access from a security group to app roles (2026-10-28)

Status: planned, not started. Do this before adding a second project.

## Today (working)

| Where | Setting | Value |
|---|---|---|
| Signup app | `OAUTH_CLAIMS_SOURCE` | `id_token` |
| Signup app | `GROUPS_FIELD` | `groups` |
| Signup app | `TEAM_GROUP_PREFIX` | object ID of the `genesis-mag-users` security group |
| Entra ID | Token configuration | groups claim: security groups, Group ID, in the ID token |
| LiteLLM team | `team_id` / `team_alias` / `models` | the object ID / `genesis-mag-users` / `claude-sonnet-4.5` |

The ID token carries the group's object ID, which equals both the prefix and
the team's `team_id`, so members of `genesis-mag-users` get that project.

## Why change

- **Only one project can work this way.** `TEAM_GROUP_PREFIX` is a single
  "starts with" value, and object IDs share no common start.
- **People in many groups get none.** Entra leaves the groups claim out of
  the token for people in more than about 200 groups.
- **App roles fix both**: the token's `roles` claim carries readable names
  (`genesis-mag-users`, `genesis-<next project>`), all starting with
  `genesis-`, and only roles on this app appear. See the option A doc
  (PR #73) and the multiple-projects plan (PR #72).

## Target

| Where | Setting | Value |
|---|---|---|
| Signup app | `GROUPS_FIELD` | `roles` |
| Signup app | `TEAM_GROUP_PREFIX` | `genesis-` |
| Entra ID | App role on the signup app | value `genesis-mag-users`, assigned to the `genesis-mag-users` security group |
| LiteLLM team | unchanged | matches by its name, `genesis-mag-users` |

The existing LiteLLM team needs **no change**: the app matches a team by its
`team_id` or its `team_alias`, and with roles the alias matches.

## Steps

Steps 1 and 2 change nothing for users: the app keeps reading `groups` until
step 3.

1. **Entra ID: create the app role.** App registrations, the signup app,
   App roles, Create app role:
   - Display name: `genesis-mag-users` (free text)
   - Allowed member types: Users/Groups
   - Value: `genesis-mag-users`
   - Enabled
2. **Entra ID: assign the security group to the role.** Enterprise
   applications, the signup app, Users and groups, Add user/group: the group
   `genesis-mag-users`, role `genesis-mag-users`. (Assigning groups needs
   Entra ID P1 or P2.)
3. **Check the token carries the role.** Sign in through the test below and
   confirm the ID token shows `roles = ['genesis-mag-users']`.
4. **Switch the signup app** and redeploy:
   ```yaml
   - name: GROUPS_FIELD
     value: "roles"
   - name: TEAM_GROUP_PREFIX
     value: "genesis-"
   ```
5. **Verify.** Sign out of the app, sign back in, hard-refresh, and open
   Create API Key: the project `genesis-mag-users` with `claude-sonnet-4.5`.
   Someone outside the group still sees "You aren't in any project".
6. **If the key reconciler is on** (PR #74): it follows `GROUPS_FIELD`
   automatically and reads app-role members instead of group members, which
   needs the Graph permission `Application.Read.All` as well. Set
   `RECONCILE_DRY_RUN=true` for the first runs after the switch and check the
   `reconcile_would_block` lines.
7. **Optional cleanup**, once nothing else relies on it: remove the groups
   claim from the app registration's Token configuration.

## Rollback

Set `GROUPS_FIELD=groups` and `TEAM_GROUP_PREFIX=<object ID>` again and
redeploy. The app role can stay; it does no harm while the app reads groups.

## Adding the next project (after the migration)

1. Entra ID: a security group, an app role `genesis-<project>`, and the group
   assigned to it.
2. LiteLLM: a team with `team_alias` = `genesis-<project>` and its models.
3. People sign out and back in. No change to the signup app.

## Token check (step 3)

Run from the jump host. Step A prints a sign-in link; open it, sign in, and
copy the whole address of the error page you land on (the app rejects this
hand-made sign-in, which is expected). Within a minute, run step B with that
address. It prints the `roles` and `groups` in the ID token.

```bash
# A: the sign-in link
kubectl exec deploy/signup-main -c main -- python -c '
import os, urllib.parse
p = {"response_type": "code", "client_id": os.environ["OAUTH_CLIENT_ID"],
     "redirect_uri": os.environ["OAUTH_REDIRECT_URL"],
     "scope": os.environ.get("OAUTH_SCOPES") or "openid email profile",
     "state": "roles-test", "nonce": "roles-test"}
print(os.environ["OAUTH_AUTHORIZE_URL"] + "?" + urllib.parse.urlencode(p))'

# B: the claims (paste the whole address in place of ADDRESS)
kubectl exec deploy/signup-main -c main -- python -c '
import os, sys, json, base64, urllib.request, urllib.parse
code = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[1]).query)["code"][0]
data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
    "redirect_uri": os.environ["OAUTH_REDIRECT_URL"], "client_id": os.environ["OAUTH_CLIENT_ID"],
    "client_secret": os.environ["OAUTH_CLIENT_SECRET"]}).encode()
tok = json.load(urllib.request.urlopen(urllib.request.Request(os.environ["OAUTH_TOKEN_URL"], data=data)))
part = tok["id_token"].split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
print("roles  =", claims.get("roles")); print("groups =", claims.get("groups"))' "ADDRESS"
```

## Checklist

- [ ] App role `genesis-mag-users` created on the signup app's registration
- [ ] Security group `genesis-mag-users` assigned to the role
- [ ] Token check shows `roles = ['genesis-mag-users']`
- [ ] Signup app switched to `GROUPS_FIELD=roles`, `TEAM_GROUP_PREFIX=genesis-`
- [ ] Project visible for a member, not for a non-member
- [ ] Reconciler (if on) dry-run output checked after the switch
