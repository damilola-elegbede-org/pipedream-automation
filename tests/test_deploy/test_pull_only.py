"""Hermetic tests for the read-only deployed-code pull mode."""

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.deploy.config import DeployConfig, DeploySettings, StepConfig, WorkflowConfig
from src.deploy.deploy_to_pipedream import (
    PullResult,
    PipedreamSyncer,
    build_parser,
    main_async,
)
from src.deploy.exceptions import HeadlessAuthenticationError


def pull_config(script_path: str = "scripts/step.py") -> DeployConfig:
    """A one-step config used by pull tests."""
    return DeployConfig(
        version="1.0",
        pipedream_base_url="https://pipedream.com",
        workflows={
            "workflow": WorkflowConfig(
                id="workflow-p_123",
                name="Workflow",
                steps=[StepConfig(step_name="step", script_path=script_path)],
            )
        },
        settings=DeploySettings(),
    )


def make_local_script(tmp_path: Path, code: str) -> Path:
    script = tmp_path / "scripts" / "step.py"
    script.parent.mkdir()
    script.write_text(code, encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_pull_only_matching_content_writes_artifact_without_a_diff(tmp_path, capsys):
    local_code = "def handler(pd):\n    return {'ok': True}\n"
    make_local_script(tmp_path, local_code)
    syncer = PipedreamSyncer(pull_config(), headless=True)

    # The real read_code method evaluates this mocked Playwright page.  The
    # navigation helpers are mocked because their selectors are covered by
    # their own tests and are not relevant to comparing pulled code.
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=local_code)
    syncer.page = page

    with patch.object(syncer, "setup_browser_interactive", new_callable=AsyncMock), \
         patch.object(syncer, "wait_for_login", new_callable=AsyncMock, return_value=True), \
         patch.object(syncer, "navigate_to_workflow", new_callable=AsyncMock), \
         patch.object(syncer, "close_step_panel", new_callable=AsyncMock), \
         patch.object(syncer, "find_and_click_step", new_callable=AsyncMock), \
         patch.object(syncer, "click_code_tab", new_callable=AsyncMock), \
         patch.object(syncer, "sync_step", new_callable=AsyncMock) as sync_step, \
         patch.object(syncer, "update_code", new_callable=AsyncMock) as update_code, \
         patch.object(syncer, "wait_for_save", new_callable=AsyncMock) as wait_for_save:
        results = await syncer.pull_all(tmp_path, ["workflow"])

    assert [result.status for result in results] == ["match"]
    assert (tmp_path / ".tmp" / "pulled" / "step.py").read_text(encoding="utf-8") == local_code
    output = capsys.readouterr().out
    assert "--- scripts/step.py" not in output
    assert "+++ .tmp/pulled/step.py" not in output
    page.evaluate.assert_awaited_once()
    # These are mutation guards: changing the pull path to delegate to a sync
    # method makes this test fail rather than merely relying on code review.
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


@pytest.mark.asyncio
async def test_pull_only_different_content_prints_unified_diff_and_exits_nonzero(tmp_path, capsys):
    local_code = "def handler(pd):\n    return 'local'\n"
    deployed_code = "def handler(pd):\n    return 'deployed'\n"
    make_local_script(tmp_path, local_code)
    syncer = PipedreamSyncer(pull_config(), headless=True)
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=deployed_code)
    syncer.page = page

    with patch.object(syncer, "setup_browser_interactive", new_callable=AsyncMock), \
         patch.object(syncer, "wait_for_login", new_callable=AsyncMock, return_value=True), \
         patch.object(syncer, "navigate_to_workflow", new_callable=AsyncMock), \
         patch.object(syncer, "close_step_panel", new_callable=AsyncMock), \
         patch.object(syncer, "find_and_click_step", new_callable=AsyncMock), \
         patch.object(syncer, "click_code_tab", new_callable=AsyncMock), \
         patch.object(syncer, "sync_step", new_callable=AsyncMock) as sync_step, \
         patch.object(syncer, "update_code", new_callable=AsyncMock) as update_code, \
         patch.object(syncer, "wait_for_save", new_callable=AsyncMock) as wait_for_save:
        results = await syncer.pull_all(tmp_path, ["workflow"])

    assert [result.status for result in results] == ["different"]
    assert (tmp_path / ".tmp" / "pulled" / "step.py").read_text(encoding="utf-8") == deployed_code
    output = capsys.readouterr().out
    assert "--- scripts/step.py" in output
    assert "+++ .tmp/pulled/step.py" in output
    assert "-    return 'local'" in output
    assert "+    return 'deployed'" in output
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


@pytest.mark.asyncio
async def test_pull_only_headless_unseeded_profile_raises_without_writing(tmp_path):
    syncer = PipedreamSyncer(pull_config(), headless=True)
    page = AsyncMock()
    page.goto = AsyncMock()
    page.inner_text = AsyncMock(return_value="Sign up Start for free")
    type(page).url = "https://pipedream.com/"
    syncer.page = page

    with patch.object(syncer, "setup_browser_interactive", new_callable=AsyncMock), \
         patch.object(syncer, "teardown_browser", new_callable=AsyncMock) as teardown, \
         patch.object(syncer, "sync_step", new_callable=AsyncMock) as sync_step, \
         patch.object(syncer, "update_code", new_callable=AsyncMock) as update_code, \
         patch.object(syncer, "wait_for_save", new_callable=AsyncMock) as wait_for_save:
        with pytest.raises(HeadlessAuthenticationError, match="--seed-login"):
            await syncer.pull_all(tmp_path, ["workflow"])

    teardown.assert_awaited_once_with(cache_cookies=False)
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pull_status", "expected_exit"), [("match", 0), ("different", 1)]
)
async def test_main_pull_only_uses_documented_drift_exit_codes(
    tmp_path, monkeypatch, pull_status, expected_exit
):
    config = pull_config()
    syncer = MagicMock()
    syncer.pull_all = AsyncMock(return_value=[
        PullResult("step", "scripts/step.py", ".tmp/pulled/step.py", pull_status)
    ])
    args = argparse.Namespace(
        login_timeout=60,
        config=str(tmp_path / "config.yaml"),
        workflow=None,
        dry_run=False,
        verbose=False,
        screenshot_always=False,
        base_path=str(tmp_path),
        headless=False,
        seed_login=False,
        pull_only=True,
    )
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.load_and_set_env_local", lambda: None)
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr("src.deploy.deploy_to_pipedream.validate_config", lambda *_args: True)
    with patch("src.deploy.deploy_to_pipedream.PipedreamSyncer", return_value=syncer) as syncer_type:
        assert await main_async(args) == expected_exit

    assert syncer_type.call_args.kwargs["headless"] is True
    syncer.pull_all.assert_awaited_once()


def test_pull_only_is_documented_as_headless_and_read_only():
    help_text = build_parser().format_help()

    assert "--pull-only" in help_text
    assert "never updates Pipedream" in help_text
