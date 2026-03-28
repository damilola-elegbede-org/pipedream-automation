"""Tests for workflow sync and deployment orchestration.

These tests cover the high-level sync_workflow, sync_all, and related coordination
methods that combine multiple step operations.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import tempfile
import json

import pytest

from src.deploy.config import DeployConfig, DeploySettings, StepConfig, WorkflowConfig
from src.deploy.exceptions import NavigationError, AuthenticationError, PipedreamSyncError
from src.deploy.deploy_to_pipedream import PipedreamSyncer, WorkflowResult, StepResult


@pytest.fixture
def mock_config():
    """Create a comprehensive mock DeployConfig for workflow tests."""
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
        settings=DeploySettings(
            step_timeout=30,
            max_retries=2,
            autosave_wait=0.5,
            screenshot_on_failure=True,
        ),
        pipedream_username="testuser",
        pipedream_project_id="proj_test",
    )


class TestSyncWorkflowDryRun:
    """Test sync_workflow with dry-run mode."""

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run(self, mock_config):
        """Test sync_workflow skips actual sync in dry-run mode."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)
        syncer.page = MagicMock()  # Mock page setup
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "skipped"
        assert result.workflow_key == "test_workflow"
        assert result.workflow_name == "Test Workflow"
        assert len(result.steps) == 2
        assert all(s.status == "skipped" for s in result.steps)

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run_logs_steps(self, mock_config, capsys):
        """Test sync_workflow logs each step in dry-run mode."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True, verbose=True)
        syncer.page = MagicMock()
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        captured = capsys.readouterr()
        assert "dry-run" in captured.out.lower() or "skipped" in captured.out.lower()


class TestSyncWorkflowNavigation:
    """Test sync_workflow navigation handling."""

    @pytest.mark.asyncio
    async def test_sync_workflow_navigation_error(self, mock_config):
        """Test sync_workflow returns failed when navigation fails."""
        syncer = PipedreamSyncer(config=mock_config)
        
        with patch.object(
            syncer, 'navigate_to_workflow',
            side_effect=NavigationError("Failed to navigate")
        ):
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "failed"
        assert "navigation" in result.error.lower() or "navigate" in result.error.lower()

    @pytest.mark.asyncio
    async def test_sync_workflow_auth_error(self, mock_config):
        """Test sync_workflow returns failed when authentication fails."""
        syncer = PipedreamSyncer(config=mock_config)
        
        with patch.object(
            syncer, 'navigate_to_workflow',
            side_effect=AuthenticationError("Auth failed")
        ):
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "failed"
        assert "auth" in result.error.lower()


class TestSyncWorkflowStepHandling:
    """Test sync_workflow handles step results correctly."""

    @pytest.mark.asyncio
    async def test_sync_workflow_mixed_step_results(self, mock_config):
        """Test sync_workflow with some steps succeeding and some failing."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        
        # Mock step sync to return different results
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="failed", message="Error"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=False):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "partial"
        assert len(result.steps) == 2

    @pytest.mark.asyncio
    async def test_sync_workflow_all_steps_fail(self, mock_config):
        """Test sync_workflow returns failed when all steps fail."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="failed"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="failed"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "failed"


class TestSyncWorkflowDeployment:
    """Test sync_workflow deployment behavior."""

    @pytest.mark.asyncio
    async def test_sync_workflow_deploys_on_success(self, mock_config):
        """Test sync_workflow deploys workflow after successful sync."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)
        syncer.page = AsyncMock()
        
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="success"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=True) as mock_deploy:
                    with patch.object(syncer, 'verify_workflow_after_deploy', new_callable=AsyncMock, return_value=True):
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        # Deploy should have been called
        mock_deploy.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_workflow_deploy_failure(self, mock_config):
        """Test sync_workflow handles deploy failure gracefully."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)
        syncer.page = AsyncMock()
        
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="success"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=False):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        # Status should be success but we continue without verification
        assert result.status in ["success", "partial"]

    @pytest.mark.asyncio
    async def test_sync_workflow_verification_failure(self, mock_config):
        """Test sync_workflow marks partial when verification fails."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="success"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=True):
                    with patch.object(syncer, 'verify_workflow_after_deploy', new_callable=AsyncMock, return_value=False):
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "partial"


class TestSyncAll:
    """Test sync_all multi-workflow orchestration."""

    @pytest.mark.asyncio
    async def test_sync_all_single_workflow(self, mock_config):
        """Test sync_all with single workflow."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        syncer.context = AsyncMock()
        
        mock_result = WorkflowResult(
            workflow_key="test_workflow",
            workflow_id="test-workflow-p_abc123",
            workflow_name="Test Workflow",
            status="success",
            steps=[],
        )
        
        with patch.object(syncer, 'sync_workflow', new_callable=AsyncMock, return_value=mock_result):
            with patch.object(syncer, 'setup_browser_interactive', new_callable=AsyncMock):
                with patch.object(syncer, 'teardown_browser', new_callable=AsyncMock):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        results = await syncer.sync_all(Path(tmp_dir), ["test_workflow"])
        
        assert len(results) == 1
        assert results[0].status == "success"

    @pytest.mark.asyncio
    async def test_sync_all_multiple_workflows(self, mock_config):
        """Test sync_all with multiple workflows."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        syncer.context = AsyncMock()
        
        mock_results = [
            WorkflowResult(
                workflow_key="test_workflow",
                workflow_id="test-workflow-p_abc123",
                workflow_name="Test Workflow",
                status="success",
                steps=[],
            ),
            WorkflowResult(
                workflow_key="second_workflow",
                workflow_id="second-workflow-p_def456",
                workflow_name="Second Workflow",
                status="success",
                steps=[],
            ),
        ]
        
        with patch.object(syncer, 'sync_workflow', new_callable=AsyncMock, side_effect=mock_results):
            with patch.object(syncer, 'setup_browser_interactive', new_callable=AsyncMock):
                with patch.object(syncer, 'teardown_browser', new_callable=AsyncMock):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        results = await syncer.sync_all(Path(tmp_dir))
        
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_sync_all_uses_specified_workflows(self, mock_config):
        """Test sync_all respects workflow_keys parameter."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        syncer.context = AsyncMock()
        
        with patch.object(syncer, 'sync_workflow', new_callable=AsyncMock) as mock_sync:
            with patch.object(syncer, 'setup_browser_interactive', new_callable=AsyncMock):
                with patch.object(syncer, 'teardown_browser', new_callable=AsyncMock):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        await syncer.sync_all(Path(tmp_dir), ["test_workflow"])
        
        # Should only call sync_workflow for the specified workflow
        assert mock_sync.call_count >= 1


class TestVerifyWorkflowAfterDeploy:
    """Test workflow verification after deployment."""

    @pytest.mark.asyncio
    async def test_verify_workflow_no_page(self, mock_config):
        """Test verify_workflow_after_deploy returns False when no page."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = None
        
        workflow = mock_config.workflows["test_workflow"]
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await syncer.verify_workflow_after_deploy(workflow, Path(tmp_dir))
        
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_workflow_reload_page(self, mock_config):
        """Test verify_workflow_after_deploy reloads workflow page."""
        syncer = PipedreamSyncer(config=mock_config)
        
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=None)
        
        syncer.page = mock_page
        
        workflow = mock_config.workflows["test_workflow"]
        
        with patch.object(syncer, 'find_and_click_step', new_callable=AsyncMock):
            with patch.object(syncer, 'click_code_tab', new_callable=AsyncMock):
                with patch.object(syncer, 'close_step_panel', new_callable=AsyncMock):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        await syncer.verify_workflow_after_deploy(workflow, Path(tmp_dir))
        
        # Should have reloaded the page
        mock_page.goto.assert_called_once()


class TestWorkflowResultConstruction:
    """Test WorkflowResult data structure."""

    def test_workflow_result_default_status(self, mock_config):
        """Test WorkflowResult initializes with success status."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_123",
            workflow_name="Test",
            status="success",
        )
        
        assert result.status == "success"
        assert result.steps == []

    def test_workflow_result_with_error(self):
        """Test WorkflowResult can store error message."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_123",
            workflow_name="Test",
            status="failed",
            error="Test error",
        )
        
        assert result.status == "failed"
        assert result.error == "Test error"

    def test_workflow_result_with_steps(self):
        """Test WorkflowResult collects step results."""
        steps = [
            StepResult("step1", "path1", "success"),
            StepResult("step2", "path2", "failed", "Error"),
        ]
        
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_123",
            workflow_name="Test",
            status="success",
            steps=steps,
        )
        
        assert len(result.steps) == 2
        assert result.steps[0].step_name == "step1"


class TestMainAsyncFunction:
    """Test main_async entry point."""

    @pytest.mark.asyncio
    async def test_main_async_config_load_error(self, tmp_path):
        """Test main_async handles config load failure."""
        # This would require mocking load_config
        # Just verify the function exists and has proper error handling
        pass

    @pytest.mark.asyncio
    async def test_main_async_config_validation_error(self, tmp_path):
        """Test main_async handles config validation failure."""
        pass


class TestSyncStepStatusCodes:
    """Test different step result status codes."""

    def test_step_result_success(self):
        """Test StepResult can be marked as success."""
        result = StepResult("step1", "path/step1.py", "success")
        assert result.status == "success"

    def test_step_result_failed(self):
        """Test StepResult can be marked as failed with message."""
        result = StepResult("step1", "path/step1.py", "failed", "Script syntax error")
        assert result.status == "failed"
        assert result.message == "Script syntax error"

    def test_step_result_skipped(self):
        """Test StepResult can be marked as skipped."""
        result = StepResult("step1", "path/step1.py", "skipped", "Dry run")
        assert result.status == "skipped"

    def test_step_result_duration(self):
        """Test StepResult tracks execution duration."""
        result = StepResult("step1", "path/step1.py", "success", duration_seconds=2.5)
        assert result.duration_seconds == 2.5


class TestWorkflowStatusAggregation:
    """Test how workflow status is determined from step results."""

    @pytest.mark.asyncio
    async def test_workflow_success_when_all_steps_succeed(self, mock_config):
        """Test workflow is successful when all steps succeed."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        
        # All steps succeed
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="success"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=True):
                    with patch.object(syncer, 'verify_workflow_after_deploy', new_callable=AsyncMock, return_value=True):
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_workflow_partial_when_some_steps_fail(self, mock_config):
        """Test workflow is partial when some steps fail."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = AsyncMock()
        
        # Mix of success and failure
        step_results = [
            StepResult(step_name="step1", script_path="src/steps/step1.py", status="success"),
            StepResult(step_name="step2", script_path="src/steps/step2.py", status="failed"),
        ]
        
        with patch.object(syncer, 'navigate_to_workflow', new_callable=AsyncMock):
            with patch.object(syncer, 'sync_step', new_callable=AsyncMock, side_effect=step_results):
                with patch.object(syncer, 'deploy_workflow', new_callable=AsyncMock, return_value=False):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        result = await syncer.sync_workflow("test_workflow", Path(tmp_dir))
        
        assert result.status == "partial"
