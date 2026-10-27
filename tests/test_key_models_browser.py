"""The create dialog's model picker (static/key-models.js) in a real browser.

Runs the app and the mock LiteLLM on local ports and drives Chromium with
Playwright. Skipped when no Chromium can be launched. Set
PLAYWRIGHT_CHROMIUM_PATH to use a particular binary.
"""
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

ADMIN = {"Authorization": "Bearer sk-mock-admin-key"}
USER = "test@test.com"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server didn't start")


@pytest.fixture(scope="module")
def browser():
    path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or (
        "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
    )
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=path)
        except Exception as e:
            pytest.skip(f"no Chromium to launch: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def app_url():
    import app.core.config as config_mod
    from app.core.config import Settings
    from app.main import create_app
    from mocks import litellm_mock

    mock_port, app_port = _free_port(), _free_port()
    mock = _serve(litellm_mock.app, mock_port)
    for team in ("team-alpha", "team-beta"):
        httpx.post(f"http://127.0.0.1:{mock_port}/team/member_add", headers=ADMIN, json={
            "team_id": team, "member": {"user_id": USER, "user_email": USER, "role": "user"},
        }).raise_for_status()
    previous = config_mod.settings
    config_mod.settings = Settings(
        DEBUG_MODE=True, AUTH_MODE="proxy", ALLOW_TEST_USER=True, TEST_USER=USER,
        FEATURE_PROXY_SECRET_ENABLED=False, FEATURE_TEAMS_ENABLED=True, TEAM_LABEL="Project",
        LITELLM_BASE_URL=f"http://127.0.0.1:{mock_port}", LITELLM_ADMIN_KEY="sk-mock-admin-key",
    )
    server = _serve(create_app(), app_port)
    yield f"http://127.0.0.1:{app_port}"
    server.should_exit = mock.should_exit = True
    config_mod.settings = previous


@pytest.fixture
def page(browser, app_url):
    from app.core.rate_limit import limiter

    limiter.reset()
    page = browser.new_page()
    page.goto(app_url + "/")
    page.wait_for_selector("#create-btn")
    page.click("#create-btn")
    page.fill("#key-name", "k")
    yield page
    page.close()


def _models(page):
    return page.locator("#key-models-list input")


def _posts(page):
    sent = []
    page.on("request", lambda r: sent.append(r) if r.method == "POST" and r.url.endswith("/api/keys") else None)
    return sent


def test_offers_the_projects_models_and_reloads_on_switch(page):
    page.select_option("#key-team", label="Alpha Team")
    expect(_models(page)).to_have_count(2)
    page.select_option("#key-team", label="Beta Team")
    expect(_models(page)).to_have_count(4)  # no list: every proxy model


def test_a_failed_load_keeps_all_unchecked_and_try_again_reloads(page):
    page.select_option("#key-team", label="Alpha Team")
    expect(_models(page)).to_have_count(2)
    page.uncheck("#key-models-all")
    fail = {"on": True}
    page.route("**/api/models*", lambda route: route.fulfill(status=502, body="{}")
               if fail["on"] else route.continue_())
    sent = _posts(page)
    page.select_option("#key-team", label="Beta Team")
    expect(page.locator("#key-models-retry")).to_be_visible()
    assert not page.is_checked("#key-models-all")
    page.click("#create-submit-btn")
    expect(page.locator("#create-error")).to_contain_text("Pick at least one model")
    assert not sent
    fail["on"] = False
    page.click("#key-models-retry")
    expect(_models(page)).to_have_count(4)
    expect(page.locator("#key-models-retry")).to_be_hidden()
    assert not page.is_checked("#key-models-all")


def test_a_project_without_models_keeps_all_unchecked(page):
    page.select_option("#key-team", label="Alpha Team")
    expect(_models(page)).to_have_count(2)
    page.uncheck("#key-models-all")
    page.route("**/api/models*team_id=team-beta*", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"team_id": "team-beta", "models": []}'))
    sent = _posts(page)
    page.select_option("#key-team", label="Beta Team")
    expect(page.locator("#key-models-hint")).to_contain_text("no models yet")
    assert not page.is_checked("#key-models-all")
    page.click("#create-submit-btn")
    expect(page.locator("#create-error")).to_contain_text("Pick at least one model")
    assert not sent


def test_a_slow_answer_for_the_previous_project_is_ignored(page):
    held = []
    page.route("**/api/models*team_id=team-alpha*", lambda route: held.append(route))
    page.select_option("#key-team", label="Alpha Team")
    page.wait_for_timeout(100)
    page.select_option("#key-team", label="Beta Team")
    expect(_models(page)).to_have_count(4)
    assert held, "the Alpha request never went out"
    held[0].fulfill(status=200, content_type="application/json",
                    body='{"team_id": "team-alpha", "models": ["claude-sonnet"]}')
    page.wait_for_timeout(200)
    expect(_models(page)).to_have_count(4)  # still Beta's list
