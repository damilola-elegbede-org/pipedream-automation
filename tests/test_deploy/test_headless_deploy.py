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
    syncer = PipedreamSyncer(deploy_config)
    page = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(
        side_effect=[PlaywrightTimeout("not logged in"), None]
    )
    type(page).url = "https://pipedream.com/"
    syncer.page = page

    assert await syncer.wait_for_login() is True
    assert page.goto.call_count == 2
    assert page.goto.call_args_list[1].args[0].endswith("/login")


@pytest.mark.asyncio
async def test_headless_unseeded_profile_fails_immediately_with_seeding_command(deploy_config):
    syncer = PipedreamSyncer(deploy_config, headless=True)
    page = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeout("not logged in"))
    type(page).url = "https://pipedream.com/"
    syncer.page = page

    started = time.monotonic()
    with pytest.raises(HeadlessAuthenticationError, match="--seed-login"):
        await syncer.wait_for_login()
    assert time.monotonic() - started < 10
    assert page.goto.call_count == 1


@pytest.mark.asyncio
async def test_main_returns_distinct_exit_code_for_unseeded_headless_profile(
    deploy_config, tmp_path, monkeypatch
):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("workflows: {}\n")
    args = argparse.Namespace(
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
    args = argparse.Namespace(
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
