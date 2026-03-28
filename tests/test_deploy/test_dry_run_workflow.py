"""Tests for dry-run short-circuit in sync_all (Issue 2 fix).

Verifies that --dry-run does NOT launch a browser: setup_browser_interactive,
wait_for_login, and teardown_browser must all be skipped when dry_run=True.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.deploy.config import DeployConfig, DeploySettings, StepConfig, WorkflowConfig
from src.deploy.deploy_to_pipedream import PipedreamSyncer, StepResult, WorkflowResult


@pytest.fixture
def mock_config():
    """Create a minimal DeployConfig for dry-run tests."""
    return DeployConfig(
        version="1.0",
        pipedream_base_url="https://pipedream.com",
        workflows={
            "test_workflow": WorkflowConfig(
                id="test-workflow-p_abc123",
                name="Test Workflow",
                steps=[
                    StepConfig(step_name="step1", script_path="src/steps/step1.py"),
                    StepConfig(step_name="step2", script_path="src/steps/step2.py"),
                ],
            ),
            "second_workflow": WorkflowConfig(
                id="second-workflow-p_def456",
                name="Second Workflow",
                steps=[
                    StepConfig(step_name="step3", script_path="src/steps/step3.py"),
                ],
            ),
        },
        settings=DeploySettings(),
        pipedream_username="testuser",
        pipedream_project_id="proj_test",
    )


class TestSyncWorkflowDryRun:
    """Tests for sync_workflow dry-run behaviour."""

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run_returns_skipped(self, mock_config):
        """sync_workflow in dry-run returns status=skipped without browser."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))

        assert result.workflow_key == "test_workflow"
        assert result.status == "skipped"
        assert len(result.steps) == 2
        for step in result.steps:
            assert step.status == "skipped"
            assert step.message == "Dry run"

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run_enumerates_all_steps(self, mock_config):
        """sync_workflow dry-run lists every step from config."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))

        step_names = [s.step_name for s in result.steps]
        assert "step1" in step_names
        assert "step2" in step_names

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run_captures_script_paths(self, mock_config):
        """sync_workflow dry-run records script_path for each step."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))

        script_paths = [s.script_path for s in result.steps]
        assert "src/steps/step1.py" in script_paths
        assert "src/steps/step2.py" in script_paths

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run_single_step(self, mock_config):
        """sync_workflow dry-run works for single-step workflows."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("second_workflow", Path(tmp_dir))

        assert result.status == "skipped"
        assert len(result.steps) == 1
        assert result.steps[0].step_name == "step3"


class TestSyncAllDryRunShortCircuit:
    """Tests for the dry-run short-circuit in sync_all (Issue 2).

    The key requirement: when dry_run=True, sync_all must NOT invoke
    setup_browser_interactive, wait_for_login, or teardown_browser.
    Previously these were called unconditionally, causing --dry-run to hang.
    """

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_skips_browser_setup(self, mock_config):
        """sync_all in dry-run mode must NOT call setup_browser_interactive."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with patch.object(
            syncer, 'setup_browser_interactive', new_callable=AsyncMock
        ) as mock_setup:
            with tempfile.TemporaryDirectory() as tmp_dir:
                await syncer.sync_all(Path(tmp_dir))

        mock_setup.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_skips_teardown_browser(self, mock_config):
        """sync_all dry-run mode does not call teardown_browser."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with patch.object(
            syncer, 'teardown_browser', new_callable=AsyncMock
        ) as mock_teardown:
            with tempfile.TemporaryDirectory() as tmp_dir:
                await syncer.sync_all(Path(tmp_dir))

        mock_teardown.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_skips_login(self, mock_config):
        """sync_all dry-run mode does not call wait_for_login."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with patch.object(
            syncer, 'wait_for_login', new_callable=AsyncMock
        ) as mock_login:
            with tempfile.TemporaryDirectory() as tmp_dir:
                await syncer.sync_all(Path(tmp_dir))

        mock_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_returns_all_workflows(self, mock_config):
        """sync_all in dry-run mode returns results for all workflows."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            results = await syncer.sync_all(Path(tmp_dir))

        assert len(results) == len(mock_config.workflows)
        for result in results:
            assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_respects_workflow_keys(self, mock_config):
        """sync_all dry-run mode respects workflow_keys filter."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            results = await syncer.sync_all(Path(tmp_dir), ["test_workflow"])

        assert len(results) == 1
        assert results[0].workflow_key == "test_workflow"

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_accumulates_results(self, mock_config):
        """sync_all dry-run populates syncer.results."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            await syncer.sync_all(Path(tmp_dir))

        assert len(syncer.results) == len(mock_config.workflows)

    @pytest.mark.asyncio
    async def test_sync_all_dry_run_returns_list(self, mock_config):
        """sync_all dry-run returns a list (not raises), confirming exit-0 path."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            results = await syncer.sync_all(Path(tmp_dir))

        assert isinstance(results, list)
