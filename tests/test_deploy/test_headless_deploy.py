"""Hermetic coverage for unattended Pipedream deployment modes."""

import argparse
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.deploy.config import DeployConfig, DeploySettings, WorkflowConfig
from src.deploy.deploy_to_pipedream import (
    HEADLESS_AUTH_EXIT_CODE,
    PipedreamSyncer,
    PlaywrightTimeout,
    WorkflowResult,
    build_parser,
    main_async,
)
from src.deploy.exceptions import HeadlessAuthenticationError


@pytest.fixture
def deploy_config():
    return DeployConfig(
        version="1.0",
        pipedream_base_url="https://pipedream.com",
        workflows={
            "workflow": WorkflowConfig(id="workflow-p_123", name="Workflow", steps=[]),
        },
        settings=DeploySettings(),
    )


def fake_playwright(page):
    """Build the small Playwright surface setup_browser_interactive needs."""
    context = MagicMock()
    context.pages = [page]
    context.grant_permissions = AsyncMock()
    chromium = MagicMock()
    chromium.launch_persistent_context = AsyncMock(return_value=context)
    playwright = MagicMock(chromium=chromium)
    manager = MagicMock(start=AsyncMock(return_value=playwright))
    return manager, chromium


def test_help_documents_unattended_modes_and_exit_code():
    help_text = build_parser().format_help()

    assert "--headless" in help_text
    assert "--seed-login" in help_text
    assert "exits 2" in help_text


@pytest.mark.asyncio
async def test_headless_authenticated_profile_launches_headless_and_deploys(deploy_config):
    page = AsyncMock()
    # An authenticated settings page. Note the URL still says /workflows — the
    # point of the probe is that the URL is not what decides.
    page.inner_text = AsyncMock(return_value="Workspace settings Membership API")
    type(page).url = "https://pipedream.com/workflows"
    manager, chromium = fake_playwright(page)
    syncer = PipedreamSyncer(deploy_config, headless=True)
    result = WorkflowResult("workflow", "workflow-p_123", "Workflow", "success")

    with patch("src.deploy.deploy_to_pipedream.PLAYWRIGHT_AVAILABLE", True), \
         patch("src.deploy.deploy_to_pipedream.async_playwright", return_value=manager), \
         patch("src.deploy.deploy_to_pipedream.assert_profile_is_safe"), \
         patch.object(syncer, "sync_workflow", new_callable=AsyncMock, return_value=result), \
         patch.object(syncer, "teardown_browser", new_callable=AsyncMock):
        results = await syncer.sync_all(Path.cwd(), ["workflow"])

    assert results == [result]
    assert chromium.launch_persistent_context.call_args.kwargs["headless"] is True


@pytest.mark.asyncio
async def test_default_mode_remains_headed_and_uses_interactive_login(deploy_config):
    page = AsyncMock()
    type(page).url = "https://pipedream.com/workflows"
    manager, chromium = fake_playwright(page)
    syncer = PipedreamSyncer(deploy_config)

    with patch("src.deploy.deploy_to_pipedream.PLAYWRIGHT_AVAILABLE", True), \
         patch("src.deploy.deploy_to_pipedream.async_playwright", return_value=manager), \
         patch("src.deploy.deploy_to_pipedream.assert_profile_is_safe"):
        await syncer.setup_browser_interactive()

    assert chromium.launch_persistent_context.call_args.kwargs["headless"] is False


@pytest.mark.asyncio
async def test_default_mode_falls_back_to_interactive_login_wait(deploy_config):
    # A signed-out probe must send the human to /login and then WAIT, not
    # declare success. login_timeout is squeezed so the wait exits promptly.
    syncer = PipedreamSyncer(deploy_config, login_timeout_sec=2)
    page = AsyncMock()
    page.goto = AsyncMock()
    page.inner_text = AsyncMock(return_value="Sign up Start for free")
    page.is_closed = MagicMock(return_value=False)
    type(page).url = "https://pipedream.com/"
    syncer.page = page
    syncer.context = MagicMock(pages=[page])

    assert await syncer.wait_for_login() is False
    visited = [c.args[0] for c in page.goto.call_args_list]
    assert any(u.endswith("/login") for u in visited)


@pytest.mark.asyncio
async def test_headless_unseeded_profile_fails_immediately_with_seeding_command(deploy_config):
    syncer = PipedreamSyncer(deploy_config, headless=True)
    page = AsyncMock()
    page.goto = AsyncMock()
    page.inner_text = AsyncMock(return_value="Sign up Start for free")
    type(page).url = "https://pipedream.com/"
    syncer.page = page

    started = time.monotonic()
    with pytest.raises(HeadlessAuthenticationError, match="--seed-login"):
        await syncer.wait_for_login()
    assert time.monotonic() - started < 10
    assert page.goto.call_count <= 2  # base URL + the auth probe


@pytest.mark.asyncio
async def test_main_returns_distinct_exit_code_for_unseeded_headless_profile(
    deploy_config, tmp_path, monkeypatch
):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("workflows: {}\n")
    args = argparse.Namespace(login_timeout=7200, 
        config=str(config_file), workflow=None, dry_run=False, verbose=False,
        screenshot_always=False, base_path=None, headless=True, seed_login=False,
    )
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.load_and_set_env_local", lambda: None)
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.load_config", lambda *_args, **_kwargs: deploy_config)
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.validate_config", lambda *_args: True)
    monkeypatch.setattr(
        "src.deploy.deploy_to_pipedream.PipedreamSyncer.sync_all",
        AsyncMock(side_effect=HeadlessAuthenticationError("run --seed-login")),
    )

    # Assert the LITERAL value, not the constant. `== HEADLESS_AUTH_EXIT_CODE`
    # is tautological: it holds no matter what the constant is set to, so it
    # cannot catch the one regression that matters here. Verified by mutation —
    # flipping the constant to 1 left the whole suite green.
    assert await main_async(args) == 2


def test_unseeded_exit_code_is_distinct_from_generic_failure():
    """The hook contract: "not logged in" must be distinguishable from "deploy failed".

    A git hook branches on this. If the unseeded code ever collapses into the
    generic failure code, the hook silently stops being able to tell a human
    "go run --seed-login" and starts reporting a deploy error instead.
    """
    assert HEADLESS_AUTH_EXIT_CODE == 2
    assert HEADLESS_AUTH_EXIT_CODE != 1, "must not collide with the generic failure code"
    assert HEADLESS_AUTH_EXIT_CODE != 0, "must not collide with success"


@pytest.mark.asyncio
async def test_preflight_lists_all_missing_workflow_ids_before_browser(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "workflows:\n"
        "  first: {id: '${PIPEDREAM_WORKFLOW_FIRST}'}\n"
        "  second: {id: '${PIPEDREAM_WORKFLOW_SECOND}'}\n"
    )
    args = argparse.Namespace(login_timeout=7200, 
        config=str(config_file), workflow=None, dry_run=False, verbose=False,
        screenshot_always=False, base_path=None, headless=False, seed_login=False,
    )
    monkeypatch.delenv("PIPEDREAM_WORKFLOW_FIRST", raising=False)
    monkeypatch.delenv("PIPEDREAM_WORKFLOW_SECOND", raising=False)
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.load_and_set_env_local", lambda: None)
    browser = MagicMock()
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.PipedreamSyncer", browser)

    assert await main_async(args) == 1
    output = capsys.readouterr().out
    assert "PIPEDREAM_WORKFLOW_FIRST" in output
    assert "PIPEDREAM_WORKFLOW_SECOND" in output
    browser.assert_not_called()


class TestAuthProbeIsContentBased:
    """pipedream.com/workflows is a MARKETING page.

    It renders the title "Workflows" and a URL containing /workflows whether or
    not you have a session. The old check keyed off exactly that, so --seed-login
    declared success against a logged-out browser and closed the window on a
    human who had not logged in yet. These pin the decision to page CONTENT.
    """

    @staticmethod
    def _syncer(body_text):
        cfg = DeployConfig(version="1.0", pipedream_base_url="https://pipedream.com",
                           pipedream_username="u", pipedream_project_id="p",
                           workflows={}, settings=DeploySettings())
        s = PipedreamSyncer(cfg, headless=True)
        page = MagicMock()
        page.goto = AsyncMock()
        page.inner_text = AsyncMock(return_value=body_text)
        page.url = "https://pipedream.com/workflows"   # the deceptive part
        s.page = page
        return s

    @pytest.mark.asyncio
    async def test_marketing_page_is_not_a_session(self):
        marketing = ("Pipedream has joined Workday Sign in Sign up "
                     "Connect apps, databases, and more Start for free")
        assert await self._syncer(marketing)._is_authenticated() is False

    @pytest.mark.asyncio
    async def test_authenticated_settings_page_is_a_session(self):
        real = ("Workspace settings General Membership Authentication "
                "Environment Variables API Billing and Usage")
        assert await self._syncer(real)._is_authenticated() is True

    @pytest.mark.asyncio
    async def test_probe_targets_an_authenticated_route_not_the_marketing_one(self):
        s = self._syncer("Workspace settings")
        await s._is_authenticated()
        assert "/workflows" not in s.page.goto.call_args[0][0]
        assert s.AUTH_PROBE_URL in s.page.goto.call_args[0][0]

    @pytest.mark.asyncio
    async def test_unreadable_body_is_not_a_session(self):
        s = self._syncer(None)
        assert await s._is_authenticated() is False

    @pytest.mark.asyncio
    async def test_inner_text_failure_is_not_a_session(self):
        s = self._syncer("unused")
        s.page.inner_text = AsyncMock(side_effect=RuntimeError("boom"))
        assert await s._is_authenticated() is False

    @pytest.mark.asyncio
    async def test_goto_failure_is_not_a_session(self):
        s = self._syncer("unused")
        s.page.goto = AsyncMock(side_effect=RuntimeError("boom"))
        assert await s._is_authenticated() is False
