"""Tests for basic deploy_to_pipedream components (dataclasses, non-async methods)."""

import asyncio
from dataclasses import fields
from io import StringIO
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.deploy.config import DeployConfig, DeploySettings, StepConfig, WorkflowConfig
from src.deploy.exceptions import CodeUpdateError, NavigationError, StepNotFoundError
from src.deploy.deploy_to_pipedream import (
    BROWSER_PROFILE_DIR,
    PipedreamSyncer,
    StepResult,
    WorkflowResult,
)


class TestStepResult:
    """Tests for StepResult dataclass."""

    def test_creation_with_required_fields(self):
        """Test creating StepResult with required fields."""
        result = StepResult(
            step_name="fetch_emails",
            script_path="src/steps/fetch_emails.py",
            status="success",
        )
        assert result.step_name == "fetch_emails"
        assert result.script_path == "src/steps/fetch_emails.py"
        assert result.status == "success"

    def test_creation_with_all_fields(self):
        """Test creating StepResult with all fields."""
        result = StepResult(
            step_name="create_task",
            script_path="src/steps/create_task.py",
            status="failed",
            message="Editor not found",
            duration_seconds=5.5,
        )
        assert result.message == "Editor not found"
        assert result.duration_seconds == 5.5

    def test_default_values(self):
        """Test default values for optional fields."""
        result = StepResult(
            step_name="test",
            script_path="test.py",
            status="success",
        )
        assert result.message == ""
        assert result.duration_seconds == 0.0

    def test_status_values(self):
        """Test that various status values are accepted."""
        for status in ["success", "failed", "skipped"]:
            result = StepResult(
                step_name="test",
                script_path="test.py",
                status=status,
            )
            assert result.status == status

    def test_is_dataclass(self):
        """Test that StepResult is a proper dataclass."""
        field_names = {f.name for f in fields(StepResult)}
        expected_fields = {"step_name", "script_path", "status", "message", "duration_seconds"}
        assert field_names == expected_fields


class TestWorkflowResult:
    """Tests for WorkflowResult dataclass."""

    def test_creation_with_required_fields(self):
        """Test creating WorkflowResult with required fields."""
        result = WorkflowResult(
            workflow_key="gmail_to_notion",
            workflow_id="p_abc123",
            workflow_name="Gmail to Notion",
            status="success",
        )
        assert result.workflow_key == "gmail_to_notion"
        assert result.workflow_id == "p_abc123"
        assert result.workflow_name == "Gmail to Notion"
        assert result.status == "success"

    def test_creation_with_all_fields(self):
        """Test creating WorkflowResult with all fields."""
        step = StepResult("step1", "path.py", "success")
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test Workflow",
            status="partial",
            steps=[step],
            error="Some error",
        )
        assert len(result.steps) == 1
        assert result.error == "Some error"

    def test_default_values(self):
        """Test default values for optional fields."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="success",
        )
        assert result.steps == []
        assert result.error is None

    def test_status_values(self):
        """Test that various status values are accepted."""
        for status in ["success", "partial", "failed", "skipped"]:
            result = WorkflowResult(
                workflow_key="test",
                workflow_id="p_test",
                workflow_name="Test",
                status=status,
            )
            assert result.status == status

    def test_steps_list_is_mutable(self):
        """Test that steps list can be modified."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="success",
        )
        step = StepResult("new_step", "path.py", "success")
        result.steps.append(step)
        assert len(result.steps) == 1

    def test_is_dataclass(self):
        """Test that WorkflowResult is a proper dataclass."""
        field_names = {f.name for f in fields(WorkflowResult)}
        expected_fields = {"workflow_key", "workflow_id", "workflow_name", "status", "steps", "error"}
        assert field_names == expected_fields


class TestPipedreamSyncerInit:
    """Tests for PipedreamSyncer initialization."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
            pipedream_username="testuser",
            pipedream_project_id="proj_test",
        )

    def test_initialization_with_required_args(self, mock_config):
        """Test initializing syncer with required arguments."""
        syncer = PipedreamSyncer(config=mock_config)

        assert syncer.config == mock_config
        assert syncer.dry_run is False
        assert syncer.verbose is False
        assert syncer.screenshot_always is False

    def test_initialization_with_all_args(self, mock_config):
        """Test initializing syncer with all arguments."""
        syncer = PipedreamSyncer(
            config=mock_config,
            dry_run=True,
            verbose=True,
            screenshot_always=True,
        )

        assert syncer.dry_run is True
        assert syncer.verbose is True
        assert syncer.screenshot_always is True

    def test_initial_state(self, mock_config):
        """Test that syncer has correct initial state."""
        syncer = PipedreamSyncer(config=mock_config)

        assert syncer.playwright is None
        assert syncer.context is None
        assert syncer.page is None
        assert syncer.results == []


class TestPipedreamSyncerLog:
    """Tests for PipedreamSyncer.log method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    def test_log_info(self, mock_config, capsys):
        """Test logging info message."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Test message", "info")

        captured = capsys.readouterr()
        assert "Test message" in captured.out

    def test_log_warning(self, mock_config, capsys):
        """Test logging warning message."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Warning message", "warn")

        captured = capsys.readouterr()
        assert "WARNING:" in captured.out
        assert "Warning message" in captured.out

    def test_log_error(self, mock_config, capsys):
        """Test logging error message."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Error message", "error")

        captured = capsys.readouterr()
        assert "ERROR:" in captured.out
        assert "Error message" in captured.out

    def test_log_debug_when_verbose(self, mock_config, capsys):
        """Test logging debug message when verbose is True."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)
        syncer.log("Debug message", "debug")

        captured = capsys.readouterr()
        assert "Debug message" in captured.out

    def test_log_debug_when_not_verbose(self, mock_config, capsys):
        """Test debug message is suppressed when verbose is False."""
        syncer = PipedreamSyncer(config=mock_config, verbose=False)
        syncer.log("Debug message", "debug")

        captured = capsys.readouterr()
        assert "Debug message" not in captured.out

    def test_log_default_level(self, mock_config, capsys):
        """Test that default log level is info."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Default level message")

        captured = capsys.readouterr()
        assert "Default level message" in captured.out
        # Should not have any prefix for info level
        assert "WARNING" not in captured.out
        assert "ERROR" not in captured.out


class TestWorkflowResultWithSteps:
    """Tests for WorkflowResult interaction with StepResult."""

    def test_adding_successful_steps(self):
        """Test adding successful steps to workflow result."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="success",
        )

        result.steps.append(StepResult("step1", "path1.py", "success", duration_seconds=1.0))
        result.steps.append(StepResult("step2", "path2.py", "success", duration_seconds=2.0))

        assert len(result.steps) == 2
        assert all(s.status == "success" for s in result.steps)

    def test_calculating_total_duration(self):
        """Test calculating total duration from steps."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="success",
            steps=[
                StepResult("step1", "path1.py", "success", duration_seconds=1.5),
                StepResult("step2", "path2.py", "success", duration_seconds=2.5),
                StepResult("step3", "path3.py", "success", duration_seconds=3.0),
            ],
        )

        total_duration = sum(s.duration_seconds for s in result.steps)
        assert total_duration == 7.0

    def test_counting_failed_steps(self):
        """Test counting failed steps in workflow result."""
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="partial",
            steps=[
                StepResult("step1", "path1.py", "success"),
                StepResult("step2", "path2.py", "failed", message="Error"),
                StepResult("step3", "path3.py", "success"),
            ],
        )

        failed_count = sum(1 for s in result.steps if s.status == "failed")
        assert failed_count == 1


class TestBrowserProfileDir:
    """Tests for browser profile directory constant."""

    def test_browser_profile_dir_is_path(self):
        """Test that BROWSER_PROFILE_DIR is a Path object."""
        from pathlib import Path
        assert isinstance(BROWSER_PROFILE_DIR, Path)

    def test_browser_profile_dir_location(self):
        """Test that BROWSER_PROFILE_DIR is in .tmp directory."""
        assert ".tmp" in str(BROWSER_PROFILE_DIR)
        assert "browser_profile" in str(BROWSER_PROFILE_DIR)


class TestAsyncMethods:
    """Tests for async methods using mocks."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test_workflow": WorkflowConfig(
                    id="test-p_abc123",
                    name="Test Workflow",
                    steps=[StepConfig(step_name="test_step", script_path="test.py")],
                )
            },
            settings=DeploySettings(),
            pipedream_username="testuser",
            pipedream_project_id="proj_test",
        )

    @pytest.mark.asyncio
    async def test_take_screenshot_without_page(self, mock_config):
        """Test take_screenshot returns None when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        result = await syncer.take_screenshot("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_for_save_without_page(self, mock_config):
        """Test wait_for_save returns False when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        result = await syncer.wait_for_save()
        assert result is False

    @pytest.mark.asyncio
    async def test_deploy_workflow_without_page(self, mock_config):
        """Test deploy_workflow returns False when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        result = await syncer.deploy_workflow()
        assert result is False

    @pytest.mark.asyncio
    async def test_update_code_without_page(self, mock_config):
        """Test update_code raises error when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        with pytest.raises(CodeUpdateError, match="not initialized"):
            await syncer.update_code("test code")

    @pytest.mark.asyncio
    async def test_navigate_to_workflow_without_page(self, mock_config):
        """Test navigate_to_workflow raises error when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        with pytest.raises(NavigationError, match="not initialized"):
            await syncer.navigate_to_workflow("test-p_abc")

    @pytest.mark.asyncio
    async def test_find_and_click_step_without_page(self, mock_config):
        """Test find_and_click_step raises error when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        with pytest.raises(StepNotFoundError):
            await syncer.find_and_click_step("test_step")

    @pytest.mark.asyncio
    async def test_click_code_tab_without_page(self, mock_config):
        """Test click_code_tab does nothing when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        # Should not raise, just return early
        await syncer.click_code_tab()

    @pytest.mark.asyncio
    async def test_wait_for_login_without_page(self, mock_config):
        """Test wait_for_login returns False when page is not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        result = await syncer.wait_for_login()
        assert result is False


class TestDryRunBehavior:
    """Tests for dry run mode behavior."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test_workflow": WorkflowConfig(
                    id="test-p_abc123",
                    name="Test Workflow",
                    steps=[StepConfig(step_name="test_step", script_path="test.py")],
                )
            },
            settings=DeploySettings(),
        )

    def test_dry_run_flag_set(self, mock_config):
        """Test dry_run flag is set correctly."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)
        assert syncer.dry_run is True

    def test_dry_run_flag_default_false(self, mock_config):
        """Test dry_run flag defaults to False."""
        syncer = PipedreamSyncer(config=mock_config)
        assert syncer.dry_run is False

    @pytest.mark.asyncio
    async def test_dry_run_does_not_launch_browser(self, mock_config, tmp_path):
        """Dry-run sync_all must NOT call setup_browser_interactive (Fix #2).

        Before this fix, sync_all() launched a real browser even for dry-run,
        causing the process to hang waiting for interactive Google SSO.
        """
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        browser_launched = []

        async def fake_setup():
            browser_launched.append(True)

        syncer.setup_browser_interactive = fake_setup

        # We need a valid script path for sync_workflow to work in dry-run mode.
        # Patch validate_config to skip file checks, or supply a real temp script.
        script_file = tmp_path / "test.py"
        script_file.write_text("def handler(pd):\n    return {}\n")

        # Point the step at our temp script
        mock_config.workflows["test_workflow"].steps[0].script_path = str(script_file)

        results = await syncer.sync_all(base_path=tmp_path)

        # Browser must NOT have been launched
        assert browser_launched == [], "setup_browser_interactive was called in dry-run mode"
        # Results should have skipped status
        assert len(results) == 1
        assert results[0].status == "skipped"
