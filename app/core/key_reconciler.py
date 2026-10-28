"""Block the keys of people who are no longer in a project's group.

With TEAM_SOURCE=claims a person's projects come from their sign-in, so the app
only notices that someone left a group when they next use it. Their keys go
straight to LiteLLM and keep working. With RECONCILE_ENABLED, every
RECONCILE_INTERVAL_SECONDS the app reads each project team's current members
from Entra ID (entra_directory.py) and blocks the keys in the team whose owner
isn't one of them. Blocked keys stop working at once; a person back in the group
can unblock theirs in the app (unblocking re-checks their groups).

Anything uncertain changes nothing:
- a team whose group or role can't be read, isn't found, has no members, or
  whose name two teams share is skipped;
- a run that would block more than RECONCILE_MAX_BLOCKS_PER_RUN keys blocks none;
- with RECONCILE_DRY_RUN (the default) it only logs what it would block.

People are matched by the LiteLLM user ID the app gives them: their email (or,
with OAUTH_USER_ID_FIELD=oid, their object ID), compared without case to each
member's mail, userPrincipalName and object ID. Disabled accounts aren't members.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.audit import audit
from app.core.config import Settings
from app.core.entra_directory import DirectoryError, EntraDirectory
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

FIRST_RUN_DELAY_SECONDS = 30


def _project_name(team: dict, prefix: str) -> str | None:
    """The name a team is a project by (its team_id or team_alias with the
    prefix, as at sign-in), or None for teams that aren't projects."""
    for name in (team.get("team_id"), team.get("team_alias")):
        if isinstance(name, str) and name.startswith(prefix):
            return name
    return None


def _identities(members: list[dict], strip_domain: bool) -> set[str]:
    ids: set[str] = set()
    for m in members:
        if m.get("accountEnabled") is False:
            continue
        for value in (m.get("id"), m.get("mail"), m.get("userPrincipalName")):
            if isinstance(value, str) and value:
                ids.add(value.lower())
                if strip_domain and "@" in value:
                    ids.add(value.split("@", 1)[0].lower())
    return ids


def _should_block(key: object, allowed: set[str]) -> bool:
    """An unblocked key whose owner isn't allowed. Keys with no owner (service
    keys made in LiteLLM itself) are left alone."""
    if not isinstance(key, dict) or key.get("blocked") or not key.get("token"):
        return False
    owner = key.get("user_id")
    return isinstance(owner, str) and bool(owner) and owner.lower() not in allowed


async def _members(directory: EntraDirectory, name: str, settings: Settings) -> list[dict] | None:
    if settings.GROUPS_FIELD == "roles":
        return await directory.app_role_members(name)
    return await directory.group_members(name)


async def reconcile_once(
    settings: Settings, client: LiteLLMClient, directory: EntraDirectory
) -> dict:
    """One pass over every project team. Returns a summary (also logged)."""
    dry_run = settings.RECONCILE_DRY_RUN
    teams = await client.list_all_teams()
    by_name: dict[str, list[dict]] = {}
    for t in teams:
        name = _project_name(t, settings.TEAM_GROUP_PREFIX)
        if name:
            by_name.setdefault(name, []).append(t)

    to_block: list[tuple[str | None, dict]] = []
    everyone: set[str] = set()  # members of any project, for keys without a team
    complete = True  # every project's members were read
    skipped = 0
    for name, same in sorted(by_name.items()):
        if len(same) > 1:
            audit("reconcile_skip_team", team=name, reason="name shared by several teams")
            skipped += 1
            complete = False
            continue
        team = same[0]
        try:
            members = await _members(directory, name, settings)
        except DirectoryError as e:
            members, reason = None, str(e)
        else:
            reason = "group or role not found" if members is None else "no members"
        allowed = _identities(members or [], settings.STRIP_USER_DOMAIN)
        if not allowed:
            audit("reconcile_skip_team", team=name, reason=reason)
            skipped += 1
            complete = False
            continue
        everyone |= allowed
        for key in (await client.list_keys(team_id=team["team_id"]))["keys"]:
            if _should_block(key, allowed):
                to_block.append((team["team_id"], key))

    if settings.RECONCILE_KEYS_WITHOUT_TEAM and complete and everyone:
        for key in (await client.list_keys())["keys"]:
            if isinstance(key, dict) and not key.get("team_id") and _should_block(key, everyone):
                to_block.append((None, key))

    summary = {"projects": len(by_name), "skipped": skipped, "to_block": len(to_block), "dry_run": dry_run}
    if len(to_block) > settings.RECONCILE_MAX_BLOCKS_PER_RUN:
        audit("reconcile_refused", limit=settings.RECONCILE_MAX_BLOCKS_PER_RUN, **summary)
        return {**summary, "blocked": 0, "refused": True}

    blocked = 0
    for team_id, key in to_block:
        fields = {"user": key.get("user_id"), "team_id": team_id, "key_alias": key.get("key_alias")}
        if dry_run:
            audit("reconcile_would_block", **fields)
            continue
        try:
            await client.block_key(key["token"])
        except Exception as e:
            logger.warning("reconciler couldn't block a key of %s: %s", fields["user"], e)
            continue
        blocked += 1
        audit("reconcile_block", **fields)
    audit("reconcile_run", blocked=blocked, **summary)
    return {**summary, "blocked": blocked, "refused": False}


async def run_periodically(settings: Settings) -> None:
    """The app's background loop (started in main.create_app)."""
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
    client = LiteLLMClient(settings)
    while True:
        try:
            async with EntraDirectory(settings) as directory:
                await reconcile_once(settings, client, directory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("key reconciliation failed; nothing was changed by this run")
        await asyncio.sleep(settings.RECONCILE_INTERVAL_SECONDS)


async def run_once_from_settings(settings: Settings) -> dict:
    """One pass outside the app, e.g. from a Kubernetes CronJob:
    ``python -m app.core.key_reconciler``."""
    async with EntraDirectory(settings) as directory:
        return await reconcile_once(settings, LiteLLMClient(settings), directory)


if __name__ == "__main__":
    from app.core.config import get_settings

    print(asyncio.run(run_once_from_settings(get_settings())))
