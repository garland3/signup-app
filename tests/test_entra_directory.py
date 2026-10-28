"""Reading group and app-role members from Microsoft Graph (entra_directory.py)."""
import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.core.entra_directory import DirectoryError, EntraDirectory

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
GROUP = "11111111-2222-3333-4444-555555555555"
ALICE = {"id": "a-oid", "mail": "alice@example.com", "userPrincipalName": "alice@example.com"}
BOB = {"id": "b-oid", "mail": "bob@example.com", "userPrincipalName": "bob@example.com"}


def _settings() -> Settings:
    return Settings(GRAPH_TENANT_ID="tenant-1", OAUTH_CLIENT_ID="app-1", OAUTH_CLIENT_SECRET="s3cret",
                    LITELLM_BASE_URL="http://x", LITELLM_ADMIN_KEY="k")


def _token():
    return respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "t", "expires_in": 3600}))


@pytest.mark.asyncio
@respx.mock
async def test_group_members_by_object_id_follow_pages_with_one_token():
    token = _token()
    members = f"{GRAPH}/groups/{GROUP}/transitiveMembers/microsoft.graph.user"
    respx.get(members, params={"$top": "999"}).mock(return_value=Response(200, json={
        "value": [ALICE], "@odata.nextLink": f"{members}?$skiptoken=2"}))
    respx.get(members, params={"$skiptoken": "2"}).mock(return_value=Response(200, json={"value": [BOB]}))
    async with EntraDirectory(_settings()) as d:
        assert await d.group_members(GROUP) == [ALICE, BOB]
    assert token.call_count == 1
    assert b"scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in token.calls.last.request.content


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("found, expected", [([{"id": GROUP}], [ALICE]), ([], None), ([{"id": "g1"}, {"id": "g2"}], None)])
async def test_group_members_by_name_need_exactly_one_group(found, expected):
    _token()
    respx.get(f"{GRAPH}/groups").mock(return_value=Response(200, json={"value": found}))
    respx.get(f"{GRAPH}/groups/{GROUP}/transitiveMembers/microsoft.graph.user").mock(
        return_value=Response(200, json={"value": [ALICE]}))
    async with EntraDirectory(_settings()) as d:
        assert await d.group_members("genesis-mag-users") == expected


@pytest.mark.asyncio
@respx.mock
async def test_a_missing_group_is_none_and_a_graph_error_raises():
    _token()
    url = f"{GRAPH}/groups/{GROUP}/transitiveMembers/microsoft.graph.user"
    respx.get(url).mock(side_effect=[Response(404), Response(500)])
    async with EntraDirectory(_settings()) as d:
        assert await d.group_members(GROUP) is None
        with pytest.raises(DirectoryError):
            await d.group_members(GROUP)


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_token_raises():
    respx.post(TOKEN_URL).mock(return_value=Response(401, json={"error": "invalid_client"}))
    async with EntraDirectory(_settings()) as d:
        with pytest.raises(DirectoryError, match="token"):
            await d.group_members(GROUP)


def _service_principal(roles):
    respx.get(f"{GRAPH}/servicePrincipals").mock(return_value=Response(200, json={
        "value": [{"id": "sp-1", "appRoles": roles}]}))


@pytest.mark.asyncio
@respx.mock
async def test_app_role_members_through_groups_and_directly():
    _token()
    _service_principal([{"id": "r-mag", "value": "genesis-mag-users", "isEnabled": True},
                        {"id": "r-other", "value": "genesis-other", "isEnabled": True}])
    respx.get(f"{GRAPH}/servicePrincipals/sp-1/appRoleAssignedTo").mock(return_value=Response(200, json={"value": [
        {"principalId": GROUP, "principalType": "Group", "appRoleId": "r-mag"},
        {"principalId": "b-oid", "principalType": "User", "appRoleId": "r-mag"},
        {"principalId": "c-oid", "principalType": "User", "appRoleId": "r-other"},
    ]}))
    respx.get(f"{GRAPH}/groups/{GROUP}/transitiveMembers/microsoft.graph.user").mock(
        return_value=Response(200, json={"value": [ALICE]}))
    respx.get(f"{GRAPH}/users/b-oid").mock(return_value=Response(200, json=BOB))
    async with EntraDirectory(_settings()) as d:
        assert await d.app_role_members("genesis-mag-users") == [ALICE, BOB]
    assert respx.calls[1].request.url.params["$filter"] == "appId eq 'app-1'"


@pytest.mark.asyncio
@respx.mock
async def test_an_unknown_or_disabled_role_is_none():
    _token()
    _service_principal([{"id": "r-mag", "value": "genesis-mag-users", "isEnabled": False}])
    async with EntraDirectory(_settings()) as d:
        assert await d.app_role_members("genesis-mag-users") is None
        assert await d.app_role_members("genesis-nope") is None


@pytest.mark.asyncio
@respx.mock
async def test_an_assigned_group_that_cant_be_read_raises():
    _token()
    _service_principal([{"id": "r-mag", "value": "genesis-mag-users"}])
    respx.get(f"{GRAPH}/servicePrincipals/sp-1/appRoleAssignedTo").mock(return_value=Response(200, json={"value": [
        {"principalId": GROUP, "principalType": "Group", "appRoleId": "r-mag"}]}))
    respx.get(f"{GRAPH}/groups/{GROUP}/transitiveMembers/microsoft.graph.user").mock(return_value=Response(404))
    async with EntraDirectory(_settings()) as d:
        with pytest.raises(DirectoryError):
            await d.app_role_members("genesis-mag-users")
