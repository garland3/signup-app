#!/usr/bin/env bash
# Verify that a LiteLLM proxy supports what the teams features and the
# proposed SCIM bridge need, using only LiteLLM's open-source admin API.
#
# Creates throwaway probe resources (a user, teams, and keys named
# "probe-<random uuid>"), checks each call, and deletes only the resources
# this run created, also when interrupted (Ctrl-C) or terminated. Needs curl
# and python3. Runs with the LiteLLM admin key: point it at a staging proxy
# first.
#
# Usage:
#   LITELLM_BASE_URL=https://litellm.example.com \
#   LITELLM_ADMIN_KEY=sk-... \
#   PROBE_MODEL=<a model configured on the proxy> \
#   scripts/verify_litellm_teams_api.sh
#
# PROBE_MODEL is optional. When set, model-access checks make real
# inference calls (up to 8 chat completions with max_tokens=1) and report
# whether each key is authorized for that model. When unset, model-access
# checks are skipped and key blocking and revocation are checked by
# authentication only.
#
# Exit status: 0 all checks passed, 1 a check failed, 2 a check was
# inconclusive (for example a network or upstream error), 3 cleanup failed
# (residual probe resources are listed; takes precedence over 1 and 2),
# 130/143 interrupted/terminated.
#
# See docs/2026-10-08-entra-access-packages-scim-design.md for context.
#
# shellcheck disable=SC2317  # functions reached via traps/indirection after exit paths
set -uo pipefail

# Interrupt handling: INT/TERM only set a flag. Requests run with the
# signals ignored, so a request already sent always completes and its
# resource is recorded; checkpoint then exits before the next step, and the
# EXIT trap cleans up.
INTERRUPTED=0
IN_CLEANUP=0
trap 'INTERRUPTED=130' INT
trap 'INTERRUPTED=143' TERM
checkpoint() {
  if [ "$INTERRUPTED" -ne 0 ] && [ "$IN_CLEANUP" -eq 0 ]; then
    echo
    echo "Interrupted; cleaning up."
    exit "$INTERRUPTED"
  fi
}

: "${LITELLM_BASE_URL:?set LITELLM_BASE_URL}"
: "${LITELLM_ADMIN_KEY:?set LITELLM_ADMIN_KEY}"
PROBE_MODEL="${PROBE_MODEL:-}"

B="${LITELLM_BASE_URL%/}"
JSON="Content-Type: application/json"
ID="probe-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
checkpoint
[ "${#ID}" -eq 38 ] || { echo "could not generate a probe id" >&2; exit 1; }
USER_ID="${ID}@example.invalid"
WORK="$(mktemp -d)"
BODY="$WORK/body.json"

# Resources are recorded when their creation succeeded or its outcome is
# unknown (a timeout or server error after the request was sent; a request
# that never connected created nothing). Only
# recorded resources are deleted during cleanup; they all carry this run's
# unique id, and "not found" during cleanup counts as already gone.
CREATED_USERS=()
CREATED_TEAMS=()
CREATED_KEYS=()
CREATED_KEY_ALIASES=()
FAILS=0
INCONCLUSIVE=0
HTTP_CODE=000
CURL_RC=0

pass() { checkpoint; printf '  PASS  %s\n' "$1"; }
fail() { checkpoint; printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
inconclusive() { checkpoint; printf '  ????  %s\n' "$1"; INCONCLUSIVE=$((INCONCLUSIVE + 1)); }
info() { checkpoint; printf '  INFO  %s\n' "$1"; }
skip() { checkpoint; printf '  SKIP  %s\n' "$1"; }

# shielded CMD... -> run CMD with SIGINT/SIGTERM ignored.
shielded() { ( trap '' INT TERM; exec "$@" ); }

# request METHOD PATH BEARER [curl args...] -> sets HTTP_CODE ("000" on a
# transport failure); the response body is left in $BODY. Runs in the
# current shell (never in $(...)), so callers can record what it created.
request() {
  local method=$1 path=$2 bearer=$3
  shift 3
  checkpoint
  : > "$BODY"
  : > "$WORK/status"
  shielded curl -sS --connect-timeout 10 --max-time 60 -o "$BODY" -w '%{http_code}' \
    -X "$method" "$B$path" -H "Authorization: Bearer $bearer" "$@" \
    > "$WORK/status" 2> "$WORK/curl.err"
  CURL_RC=$?
  HTTP_CODE=""
  IFS= read -r HTTP_CODE < "$WORK/status" || true
  [ -n "$HTTP_CODE" ] || HTTP_CODE=000
}
admin_post() { request POST "$1" "$LITELLM_ADMIN_KEY" -H "$JSON" -d "$2"; }
admin_get() { local p=$1; shift; request GET "$p" "$LITELLM_ADMIN_KEY" -G "$@"; }
# field VAR EXPR -> sets VAR to EXPR evaluated on the last response body (d).
field() {
  shielded python3 -c "import json; d=json.load(open('$BODY')); print($2)" \
    > "$WORK/field" 2>/dev/null || true
  IFS= read -r "$1" < "$WORK/field" || printf -v "$1" '%s' ""
}
# Short error text from the last response (never the request, so keys are
# not echoed).
errmsg() {
  shielded python3 - "$BODY" <<'PY' 2>/dev/null || head -c 160 "$BODY"
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get("error", d.get("detail", d)) if isinstance(d, dict) else d
if isinstance(e, dict):
    e = e.get("message", e.get("error", e))
print(str(e)[:160])
PY
}
# A creation request whose outcome is unknown may still have created the
# resource server side.
outcome_unknown() { [ "$HTTP_CODE" = 000 ] || [ "${HTTP_CODE:0:1}" = 5 ]; }
# maybe_created -> true when the last request may have reached the server
# (curl exit 6/7, could not resolve or connect, means it was never sent).
maybe_created() { outcome_unknown && [ "$CURL_RC" -ne 6 ] && [ "$CURL_RC" -ne 7 ]; }
# Error detail for the last request: curl's message on a transport error,
# otherwise the response's error text.
errdetail() {
  if [ "$HTTP_CODE" = 000 ]; then tr -d '\n' < "$WORK/curl.err" | head -c 120
  else errmsg; fi
}
# problem MSG -> report the last admin call as failed, or as inconclusive when
# it hit a network or server error (the API itself was not shown to fail).
problem() {
  if outcome_unknown; then inconclusive "$1 (HTTP $HTTP_CODE: $(errdetail))"
  else fail "$1 (HTTP $HTTP_CODE: $(errdetail))"; fi
}
# finish -> print the summary and exit with the documented status.
finish() {
  checkpoint
  echo "Summary: $FAILS failed, $INCONCLUSIVE inconclusive"
  if [ "$FAILS" -gt 0 ]; then exit 1; fi
  if [ "$INCONCLUSIVE" -gt 0 ]; then exit 2; fi
  exit 0
}

# classify -> sets RESULT from the last response to a request made with a
# probe key.
classify() {
  local m
  m=$(errmsg | tr '[:upper:]' '[:lower:]')
  case "$HTTP_CODE" in
    000) RESULT="transport error: $(errdetail)" ;;
    200) RESULT=allowed ;;
    401|403)
      if [[ "$m" == *"invalid proxy server token"* ]]; then RESULT=key_rejected
      elif [[ "$m" == *"model"* ]]; then RESULT=model_denied
      elif [[ "$m" == *"authentication error"* ]]; then RESULT=key_rejected
      else RESULT="unexpected HTTP $HTTP_CODE: $m"; fi ;;
    5??) RESULT="upstream/server error HTTP $HTTP_CODE: $m" ;;
    *) RESULT="unexpected HTTP $HTTP_CODE: $m" ;;
  esac
}
# infer KEY -> RESULT for a 1-token chat completion on PROBE_MODEL.
infer() {
  request POST /chat/completions "$1" -H "$JSON" \
    -d "{\"model\":\"$PROBE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}"
  classify
}
# authn KEY -> RESULT for an authentication-only call (the key's own info).
authn() { request GET /key/info "$1"; classify; }

create_team() { # TEAM_ID MODELS_JSON_OR_EMPTY -> 0 on success
  local models=""
  [ -n "$2" ] && models=",\"models\":$2"
  admin_post /team/new "{\"team_id\":\"$1\",\"team_alias\":\"$1\"$models}"
  if [ "$HTTP_CODE" = 200 ]; then CREATED_TEAMS+=("$1"); return 0; fi
  maybe_created && CREATED_TEAMS+=("$1")
  problem "POST /team/new $1"; return 1
}
add_member() { # TEAM_ID -> 0 on success
  admin_post /team/member_add "{\"team_id\":\"$1\",\"member\":{\"user_id\":\"$USER_ID\",\"role\":\"user\"}}"
  [ "$HTTP_CODE" = 200 ] && return 0
  problem "POST /team/member_add $1"; return 1
}
# make_key TEAM_ID ALIAS_SUFFIX -> sets NEW_KEY (empty on failure).
make_key() {
  local alias="$ID-$2"
  NEW_KEY=""
  admin_post /key/generate "{\"user_id\":\"$USER_ID\",\"team_id\":\"$1\",\"key_alias\":\"$alias\"}"
  field NEW_KEY "d.get('key','')"
  if [ "$HTTP_CODE" = 200 ] || maybe_created; then
    CREATED_KEYS+=("$NEW_KEY"); CREATED_KEY_ALIASES+=("$alias")
  fi
  if [ "$HTTP_CODE" != 200 ] || [ -z "$NEW_KEY" ]; then
    NEW_KEY=""
    problem "POST /key/generate for $1"
  fi
}
# verify_block KEY CHECK WHAT -> block KEY with POST /key/block, then use
# CHECK (infer or authn) to confirm the key is rejected.
verify_block() {
  admin_post /key/block "{\"key\":\"$1\"}"
  if [ "$HTTP_CODE" != 200 ]; then problem "POST /key/block"; return; fi
  "$2" "$1"
  case "$RESULT" in
    key_rejected) pass "POST /key/block revoked the key ($3 now gets HTTP 401)" ;;
    allowed) fail "POST /key/block returned 200 but the key is still accepted for $3" ;;
    *) inconclusive "key block check: $RESULT" ;;
  esac
}

# delete_one PATH JSON LABEL -> 0 if deleted or already gone (404).
delete_one() {
  admin_post "$1" "$2"
  case "$HTTP_CODE" in
    200) printf '  removed %s\n' "$3"; return 0 ;;
    404) printf '  already gone: %s\n' "$3"; return 0 ;;
    *) printf '  CLEANUP FAILED for %s (HTTP %s: %s)\n' "$3" "$HTTP_CODE" "$(errdetail)"; return 1 ;;
  esac
}
cleanup() {
  local residual=() i k a
  echo "Cleanup (resources created by this run only):"
  for i in "${!CREATED_KEY_ALIASES[@]}"; do
    k=${CREATED_KEYS[$i]} a=${CREATED_KEY_ALIASES[$i]}
    if [ -n "$k" ]; then
      delete_one /key/delete "{\"keys\":[\"$k\"]}" "key alias $a"
    else
      delete_one /key/delete "{\"key_aliases\":[\"$a\"]}" "key alias $a"
    fi || residual+=("key with alias $a  (POST /key/delete {\"key_aliases\":[\"$a\"]})")
  done
  for i in "${!CREATED_TEAMS[@]}"; do
    delete_one /team/delete "{\"team_ids\":[\"${CREATED_TEAMS[$i]}\"]}" "team ${CREATED_TEAMS[$i]}" \
      || residual+=("team ${CREATED_TEAMS[$i]}  (POST /team/delete {\"team_ids\":[\"${CREATED_TEAMS[$i]}\"]})")
  done
  for i in "${!CREATED_USERS[@]}"; do
    delete_one /user/delete "{\"user_ids\":[\"${CREATED_USERS[$i]}\"]}" "user ${CREATED_USERS[$i]}" \
      || residual+=("user ${CREATED_USERS[$i]}  (POST /user/delete {\"user_ids\":[\"${CREATED_USERS[$i]}\"]})")
  done
  if [ "${#residual[@]}" -gt 0 ]; then
    echo "Residual probe resources: remove these manually with the admin key:"
    printf '  - %s\n' "${residual[@]}"
    return 1
  fi
  return 0
}

on_exit() {
  local rc=$?
  # Ignore further interrupts so a second Ctrl-C cannot abandon cleanup
  # halfway (child processes inherit the ignored signals).
  trap - EXIT
  trap '' INT TERM
  IN_CLEANUP=1
  # Cleanup failure overrides check results so a wrapper can act on
  # residual resources; an interrupt status is kept.
  if ! cleanup && [ "$rc" -ne 130 ] && [ "$rc" -ne 143 ]; then
    rc=3
  fi
  rm -rf "$WORK"
  exit "$rc"
}
trap on_exit EXIT

echo "LiteLLM at $B (probe id $ID)"

echo "1. Built-in SCIM endpoint"
admin_get /scim/v2/ServiceProviderConfig
case "$HTTP_CODE" in
  200) info "built-in /scim/v2 is enabled (licensed proxy)" ;;
  403) info "built-in /scim/v2 returns 403: needs a LiteLLM Enterprise license" ;;
  000) inconclusive "built-in /scim/v2: transport error (proxy unreachable?)" ;;
  *) info "built-in /scim/v2 returned HTTP $HTTP_CODE" ;;
esac

echo "2. Team and membership calls (open-source admin API)"
admin_post /user/new "{\"user_id\":\"$USER_ID\",\"user_email\":\"$USER_ID\",\"user_role\":\"internal_user\",\"auto_create_key\":false}"
if [ "$HTTP_CODE" = 200 ]; then CREATED_USERS+=("$USER_ID"); pass "POST /user/new"
else
  maybe_created && CREATED_USERS+=("$USER_ID")
  problem "POST /user/new"; finish
fi

LOCKED="$ID-locked"
if create_team "$LOCKED" '["no-default-models"]'; then pass "POST /team/new with models=[\"no-default-models\"]"; else finish; fi
if add_member "$LOCKED"; then pass "POST /team/member_add by user_id"; else finish; fi

admin_get /team/list --data-urlencode "user_id=$USER_ID"
in_team=""
field in_team "any(t.get('team_id')=='$LOCKED' for t in (d if isinstance(d,list) else d.get('teams',[])))"
if [ "$HTTP_CODE" = 200 ] && [ "$in_team" = True ]; then pass "GET /team/list?user_id= shows the membership"
else fail "GET /team/list?user_id= does not show the membership (HTTP $HTTP_CODE)"; fi

admin_post /team/update "{\"team_id\":\"$LOCKED\",\"team_alias\":\"$LOCKED-renamed\"}"
if [ "$HTTP_CODE" = 200 ]; then pass "POST /team/update (rename)"
else problem "POST /team/update"; fi

make_key "$LOCKED" locked; LOCKED_KEY=$NEW_KEY
[ -n "$LOCKED_KEY" ] && pass "POST /key/generate with team_id"

echo "3. Model access (inference authorization)"
BASELINE_OK=0
ALLOWED_KEY=""
if [ -z "$PROBE_MODEL" ]; then
  skip "set PROBE_MODEL=<a model configured on this proxy> to test whether keys are"
  skip "authorized for inference; without it no model-access conclusion is drawn"
else
  ALLOWED="$ID-allowed"
  if create_team "$ALLOWED" "[\"$PROBE_MODEL\"]" && add_member "$ALLOWED"; then
    make_key "$ALLOWED" allowed; ALLOWED_KEY=$NEW_KEY
  fi
  if [ -n "$ALLOWED_KEY" ]; then
    infer "$ALLOWED_KEY"
    if [ "$RESULT" = allowed ]; then
      BASELINE_OK=1; pass "baseline: a key in a team whose models include $PROBE_MODEL can call it (HTTP 200)"
    else
      inconclusive "baseline call to $PROBE_MODEL did not succeed ($RESULT); model-access results below are not conclusive"
    fi
  fi
  if [ "$BASELINE_OK" -eq 1 ] && [ -n "$LOCKED_KEY" ]; then
    infer "$LOCKED_KEY"
    case "$RESULT" in
      model_denied) pass "models=[\"no-default-models\"] denies inference on $PROBE_MODEL (HTTP 401/403)" ;;
      allowed) fail "models=[\"no-default-models\"] did NOT deny inference on $PROBE_MODEL" ;;
      *) inconclusive "no-default-models check: $RESULT" ;;
    esac
    OPEN="$ID-open"
    if create_team "$OPEN" "" && add_member "$OPEN"; then
      make_key "$OPEN" open
      if [ -n "$NEW_KEY" ]; then
        infer "$NEW_KEY"
        case "$RESULT" in
          allowed) info "a team created WITHOUT a models list was authorized for $PROBE_MODEL: always set models explicitly" ;;
          model_denied) info "a team created without a models list was denied $PROBE_MODEL on this proxy" ;;
          *) inconclusive "team-without-models check: $RESULT" ;;
        esac
      fi
    fi
  fi
fi

if [ "$BASELINE_OK" -eq 1 ]; then
  REVOKE_TEAM="$ALLOWED"; REVOKE_KEY="$ALLOWED_KEY"; check=infer; what="inference on $PROBE_MODEL"
else
  REVOKE_TEAM="$LOCKED"; REVOKE_KEY="$LOCKED_KEY"; check=authn; what="authentication (GET /key/info with the key)"
fi

echo "4. Key blocking (the bridge's fallback revocation)"
make_key "$REVOKE_TEAM" block
if [ -n "$NEW_KEY" ]; then
  BLOCK_KEY=$NEW_KEY
  $check "$BLOCK_KEY"
  if [ "$RESULT" = allowed ]; then
    verify_block "$BLOCK_KEY" "$check" "$what"
  else
    inconclusive "key not accepted before blocking ($RESULT); /key/block not tested"
  fi
fi

echo "5. Revocation on team member removal"
if [ -n "$REVOKE_KEY" ]; then
  $check "$REVOKE_KEY"
  if [ "$RESULT" = allowed ]; then
    pass "before removal the key is accepted for $what"
    admin_post /team/member_delete "{\"team_id\":\"$REVOKE_TEAM\",\"user_id\":\"$USER_ID\"}"
    if [ "$HTTP_CODE" = 200 ]; then
      pass "POST /team/member_delete"
      $check "$REVOKE_KEY"
      case "$RESULT" in
        key_rejected) pass "removing the member revoked their team key ($what now gets HTTP 401)" ;;
        allowed)
          info "the team key still works after member removal: the bridge must block keys itself"
          verify_block "$REVOKE_KEY" "$check" "$what" ;;
        *) inconclusive "revocation check: $RESULT" ;;
      esac
    else
      fail "POST /team/member_delete (HTTP $HTTP_CODE: $(errmsg))"
    fi
  else
    inconclusive "key not accepted before removal ($RESULT); revocation not tested"
  fi
fi

finish
