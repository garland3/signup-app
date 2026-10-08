#!/usr/bin/env bash
# Verify that a LiteLLM proxy supports what the teams features and the
# proposed SCIM bridge need, using only LiteLLM's open-source admin API.
#
# Creates a throwaway team, user, and keys (ids prefixed "probe-"), checks
# each call, then deletes them. Needs curl and python3.
#
# Usage:
#   LITELLM_BASE_URL=https://litellm.example.com \
#   LITELLM_ADMIN_KEY=sk-... \
#   scripts/verify_litellm_teams_api.sh
#
# See docs/2026-10-08-entra-access-packages-scim-design.md for context.
set -uo pipefail

: "${LITELLM_BASE_URL:?set LITELLM_BASE_URL}"
: "${LITELLM_ADMIN_KEY:?set LITELLM_ADMIN_KEY}"

B="${LITELLM_BASE_URL%/}"
AUTH="Authorization: Bearer ${LITELLM_ADMIN_KEY}"
JSON="Content-Type: application/json"
ID="probe-$(date +%s)"
TEAM="${ID}-team"
OPEN_TEAM="${ID}-open-team"
USER_ID="${ID}@example.invalid"
FAILS=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
info() { printf '  INFO  %s\n' "$1"; }

# post PATH BODY -> prints HTTP status, response body in $BODY_FILE
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT
post() { curl -s -o "$BODY_FILE" -w '%{http_code}' -X POST "$B$1" -H "$AUTH" -H "$JSON" -d "$2"; }
get() { curl -s -o "$BODY_FILE" -w '%{http_code}' -G "$B$1" -H "$AUTH" "${@:2}"; }
field() { python3 -c "import json,sys; d=json.load(open('$BODY_FILE')); print($1)" 2>/dev/null; }
# key_works KEY -> HTTP status of an authenticated call made with KEY itself
key_works() { curl -s -o /dev/null -w '%{http_code}' "$B/v1/models" -H "Authorization: Bearer $1"; }

cleanup() {
  post /team/delete "{\"team_ids\":[\"$TEAM\",\"$OPEN_TEAM\"]}" >/dev/null
  post /user/delete "{\"user_ids\":[\"$USER_ID\"]}" >/dev/null
}

echo "LiteLLM at $B (probe id $ID)"

echo "1. Built-in SCIM endpoint"
code=$(get /scim/v2/ServiceProviderConfig)
case "$code" in
  200) info "built-in /scim/v2 is enabled (licensed proxy)";;
  403) info "built-in /scim/v2 returns 403: needs a LiteLLM Enterprise license";;
  *)   info "built-in /scim/v2 returned HTTP $code";;
esac

echo "2. Team and membership calls (open-source admin API)"
code=$(post /team/new "{\"team_id\":\"$TEAM\",\"team_alias\":\"$TEAM\",\"models\":[\"no-default-models\"]}")
[ "$code" = 200 ] && pass "POST /team/new with models=[\"no-default-models\"]" || { fail "POST /team/new (HTTP $code)"; cleanup; exit 1; }

code=$(post /user/new "{\"user_id\":\"$USER_ID\",\"user_email\":\"$USER_ID\",\"user_role\":\"internal_user\",\"auto_create_key\":false}")
[ "$code" = 200 ] && pass "POST /user/new" || fail "POST /user/new (HTTP $code)"

code=$(post /team/member_add "{\"team_id\":\"$TEAM\",\"member\":{\"user_id\":\"$USER_ID\",\"role\":\"user\"}}")
[ "$code" = 200 ] && pass "POST /team/member_add by user_id" || fail "POST /team/member_add (HTTP $code)"

get /team/list --data-urlencode "user_id=$USER_ID" >/dev/null
in_team=$(field "any(t.get('team_id')=='$TEAM' for t in (d if isinstance(d,list) else d.get('teams',[])))")
[ "$in_team" = True ] && pass "GET /team/list?user_id= shows the membership" || fail "GET /team/list?user_id= does not show the membership"

code=$(post /team/update "{\"team_id\":\"$TEAM\",\"team_alias\":\"$TEAM-renamed\"}")
[ "$code" = 200 ] && pass "POST /team/update (rename)" || fail "POST /team/update (HTTP $code)"

echo "3. Team-scoped keys and revocation"
code=$(post /key/generate "{\"user_id\":\"$USER_ID\",\"team_id\":\"$TEAM\",\"key_alias\":\"$ID-key\"}")
KEY=$(field "d.get('key','')")
if [ "$code" = 200 ] && [ -n "$KEY" ]; then pass "POST /key/generate with team_id"; else fail "POST /key/generate (HTTP $code)"; fi

if [ -n "${KEY:-}" ]; then
  [ "$(key_works "$KEY")" = 200 ] && pass "team key authenticates" || fail "team key does not authenticate"
  code=$(post /team/member_delete "{\"team_id\":\"$TEAM\",\"user_id\":\"$USER_ID\"}")
  [ "$code" = 200 ] && pass "POST /team/member_delete" || fail "POST /team/member_delete (HTTP $code)"
  after=$(key_works "$KEY")
  if [ "$after" = 401 ] || [ "$after" = 403 ]; then
    pass "removing the member revoked their team key (HTTP $after)"
  else
    info "team key still works after member removal (HTTP $after): block keys explicitly with /key/block"
  fi
fi

echo "4. Default model access of a team created without a models list"
post /team/new "{\"team_id\":\"$OPEN_TEAM\",\"team_alias\":\"$OPEN_TEAM\"}" >/dev/null
post /team/member_add "{\"team_id\":\"$OPEN_TEAM\",\"member\":{\"user_id\":\"$USER_ID\",\"role\":\"user\"}}" >/dev/null
post /key/generate "{\"user_id\":\"$USER_ID\",\"team_id\":\"$OPEN_TEAM\"}" >/dev/null
OPEN_KEY=$(field "d.get('key','')")
if [ -n "$OPEN_KEY" ]; then
  n=$(curl -s "$B/v1/models" -H "Authorization: Bearer $OPEN_KEY" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null)
  info "a team created WITHOUT models can see ${n:-?} model(s); always set models explicitly"
fi

cleanup
echo "Cleaned up probe team, user, and keys."
if [ "$FAILS" -eq 0 ]; then echo "Result: all required checks passed"; else echo "Result: $FAILS required check(s) failed"; fi
exit $(( FAILS > 0 ))
