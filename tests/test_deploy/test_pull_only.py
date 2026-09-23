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
from src.deploy.exceptions import HeadlessAuthenticationError, NavigationError
from src.deploy.utils import build_deploy_header, strip_for_deploy


def deployed_form(local_source: str) -> str:
    """What actually lands in the Pipedream editor for a given local script:
    the deploy header (with its per-deploy timestamp) plus the comment/
    docstring-stripped payload. Tests must diff against THIS, matching what
    sync_step actually pastes — not the raw local source — or a test can
    pass while --pull-only reports drift on every real deployed step."""
    return build_deploy_header() + strip_for_deploy(local_source)


def evaluate_side_effect(deployed_code: str):
    """Route the real read_code method's several page.evaluate calls: the
    editor-locate probe gets a truthy marker-found result, the clipboard
    read gets the deployed code, and cleanup calls (remove attribute, clear
    clipboard) get None — the same shape a real Playwright page returns."""

    async def _side_effect(script, *args, **kwargs):
        if "clipboard.readText" in script:
            return deployed_code
        if "querySelectorAll" in script:
            return True
        return None

    return _side_effect


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

    # The real read_code method evaluates this mocked Playwright page. Feed it
    # what a real deploy actually pastes (header + stripped payload) — not the
    # raw local source — or this test cannot catch a comparison that diffs
    # against the wrong baseline (it did not, until this fixture was fixed).
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect(deployed_form(local_code)))
    # Real Playwright's page.locator() is synchronous (only the returned
    # locator's actions are awaitable) — AsyncMock would wrongly make the
    # call itself a coroutine.
    page.locator = MagicMock(return_value=AsyncMock())
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
    output = capsys.readouterr().out
    assert "--- scripts/step.py" not in output
    assert "+++ .tmp/pulled/step.py" not in output
    page.evaluate.assert_any_await("navigator.clipboard.readText()")
    # Mutation guards: read_code must select-all + copy (reads the editor's
    # document model, unaffected by virtualization) and must never paste —
    # a switch to paste would make this a write path, not a read.
    pressed_keys = [call.args[0] for call in page.keyboard.press.await_args_list]
    assert "ControlOrMeta+KeyA" in pressed_keys
    assert "ControlOrMeta+KeyC" in pressed_keys
    assert "ControlOrMeta+KeyV" not in pressed_keys
    # These are mutation guards: changing the pull path to delegate to a sync
    # method makes this test fail rather than merely relying on code review.
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


@pytest.mark.asyncio
async def test_pull_only_different_content_prints_unified_diff_and_exits_nonzero(tmp_path, capsys):
    local_code = "def handler(pd):\n    return 'local'\n"
    deployed_source = "def handler(pd):\n    return 'deployed'\n"
    make_local_script(tmp_path, local_code)
    syncer = PipedreamSyncer(pull_config(), headless=True)
    page = AsyncMock()
    # Realistic: the live step also went through the deploy transform, it's
    # just genuinely different code underneath it (a real drift case, not an
    # artifact of the header/strip transform itself).
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect(deployed_form(deployed_source)))
    page.locator = MagicMock(return_value=AsyncMock())
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
    output = capsys.readouterr().out
    assert "--- scripts/step.py" in output
    # workflow-scoped: pull_config()'s workflow id is "workflow-p_123"
    assert "+++ .tmp/pulled/workflow-p_123/step.py" in output
    assert "-    return 'local'" in output
    assert "+    return 'deployed'" in output
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


def stale_clipboard_side_effect():
    """Simulate a keyboard copy that never fires: read_code's sentinel
    writeText() lands, but the follow-up readText() still returns that same
    sentinel instead of newly-copied editor text -- exactly what a real
    Playwright page returns when the copy keypress silently no-ops."""
    seeded = {"value": None}

    async def _side_effect(script, *args, **kwargs):
        if "clipboard.writeText" in script:
            seeded["value"] = args[0] if args else None
            return None
        if "clipboard.readText" in script:
            return seeded["value"]
        if "querySelectorAll" in script:
            return True
        return None

    return _side_effect


@pytest.mark.asyncio
async def test_pull_only_stale_clipboard_is_rejected_not_written_to_disk(tmp_path, capsys):
    local_code = "def handler(pd):\n    return {'ok': True}\n"
    make_local_script(tmp_path, local_code)
    syncer = PipedreamSyncer(pull_config(), headless=True)

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=stale_clipboard_side_effect())
    page.locator = MagicMock(return_value=AsyncMock())
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

    assert [result.status for result in results] == ["failed"]
    assert "clipboard" in results[0].message.lower()
    # The whole point: nothing -- stale or otherwise -- gets written to disk
    # for a copy that never happened.
    pulled_path = tmp_path / ".tmp" / "pulled" / "workflow-p_123" / "step.py"
    assert not pulled_path.exists()
    sync_step.assert_not_called()
    update_code.assert_not_called()
    wait_for_save.assert_not_called()


@pytest.mark.asyncio
async def test_pull_only_navigation_failure_on_one_workflow_does_not_abort_the_rest(tmp_path, capsys):
    local_code = "def handler(pd):\n    return {'ok': True}\n"
    make_local_script(tmp_path, local_code)
    config = DeployConfig(
        version="1.0",
        pipedream_base_url="https://pipedream.com",
        workflows={
            "broken": WorkflowConfig(
                id="workflow-p_broken",
                name="Broken",
                steps=[StepConfig(step_name="broken-step", script_path="scripts/step.py")],
            ),
            "workflow": WorkflowConfig(
                id="workflow-p_123",
                name="Workflow",
                steps=[StepConfig(step_name="step", script_path="scripts/step.py")],
            ),
        },
        settings=DeploySettings(),
    )
    syncer = PipedreamSyncer(config, headless=True)
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect(deployed_form(local_code)))
    page.locator = MagicMock(return_value=AsyncMock())
    syncer.page = page

    async def navigate_side_effect(workflow_id):
        if workflow_id == "workflow-p_broken":
            raise NavigationError(f"Timeout loading workflow {workflow_id}")

    with patch.object(syncer, "setup_browser_interactive", new_callable=AsyncMock), \
         patch.object(syncer, "wait_for_login", new_callable=AsyncMock, return_value=True), \
         patch.object(
             syncer, "navigate_to_workflow", new_callable=AsyncMock, side_effect=navigate_side_effect
         ) as navigate_to_workflow, \
         patch.object(syncer, "close_step_panel", new_callable=AsyncMock), \
         patch.object(syncer, "find_and_click_step", new_callable=AsyncMock), \
         patch.object(syncer, "click_code_tab", new_callable=AsyncMock), \
         patch.object(syncer, "sync_step", new_callable=AsyncMock), \
         patch.object(syncer, "update_code", new_callable=AsyncMock), \
         patch.object(syncer, "wait_for_save", new_callable=AsyncMock):
        results = await syncer.pull_all(tmp_path, ["broken", "workflow"])

    # The broken workflow's step is recorded failed, not silently dropped, and
    # the second workflow still gets pulled and read — the whole run does not
    # abort on the first workflow's navigation failure.
    assert [r.status for r in results] == ["failed", "match"]
    assert "Navigation failed" in results[0].message
    assert results[1].step_name == "step"
    assert navigate_to_workflow.await_count == 2


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
