"""Async tests for deploy_to_pipedream with mocked Playwright."""

import asyncio  # noqa: F401
from pathlib import Path  # noqa: F401
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock  # noqa: F401

import pytest

from src.deploy.config import DeployConfig, DeploySettings, StepConfig, WorkflowConfig
from src.deploy.exceptions import (  # noqa: F401
    AuthenticationError,
    CodeUpdateError,
    NavigationError,
    StepNotFoundError,
)
from src.deploy.deploy_to_pipedream import (  # noqa: F401
    PipedreamSyncer,
    PlaywrightTimeout,
    StepResult,
    WorkflowResult,
)


@pytest.fixture
def mock_config():
    """Create a mock DeployConfig for testing."""
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
            )
        },
        settings=DeploySettings(
            step_timeout=60,
            max_retries=3,
            autosave_wait=1.0,
            screenshot_on_failure=True,
            screenshot_path=".tmp/screenshots",
        ),
        pipedream_username="testuser",
        pipedream_project_id="proj_test",
    )


class TestSyncerWithMockedPage:
    """Tests for syncer methods with mocked page object."""

    @pytest.mark.asyncio
    async def test_take_screenshot_with_page(self, mock_config, tmp_path):
        """Test take_screenshot saves file when page is available."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.config.settings.screenshot_path = str(tmp_path)

        # Mock the page
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        syncer.page = mock_page

        result = await syncer.take_screenshot("test_screenshot")

        assert result is not None
        assert "test_screenshot" in result
        mock_page.screenshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_take_screenshot_handles_error(self, mock_config, tmp_path):
        """Test take_screenshot handles errors gracefully."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.config.settings.screenshot_path = str(tmp_path)

        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock(side_effect=Exception("Screenshot failed"))
        syncer.page = mock_page

        result = await syncer.take_screenshot("error_test")
        assert result is None

    @pytest.mark.asyncio
    async def test_click_code_tab_clicks_code(self, mock_config):
        """Test click_code_tab clicks CODE section."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.click = AsyncMock()
        # Mock evaluate to return False (editor not visible), so click proceeds
        mock_page.evaluate = AsyncMock(return_value=False)

        # Mock the locator chain for clicking CODE
        mock_locator = MagicMock()
        mock_locator.first = MagicMock()
        mock_locator.first.count = AsyncMock(return_value=1)
        mock_locator.first.scroll_into_view_if_needed = AsyncMock()
        mock_locator.first.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        syncer.page = mock_page

        await syncer.click_code_tab()

        # Should have called locator to find CODE element
        mock_page.locator.assert_called()

    @pytest.mark.asyncio
    async def test_click_code_tab_handles_timeout(self, mock_config):
        """Test click_code_tab handles timeout gracefully."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.click = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        syncer.page = mock_page

        # Should not raise, just log and continue
        await syncer.click_code_tab()


class TestUpdateCodeWithMockedPage:
    """Tests for update_code with mocked visible editor detection."""

    @pytest.mark.asyncio
    async def test_update_code_success(self, mock_config):
        """Test update_code successfully updates code via visible editor click, select, paste."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()

        # Mock evaluate to return different values for different calls:
        # 1. First call: editor info debug
        # 2. Second call: find visible editor (returns selector)
        # 3. Third call: cleanup marker
        # 4+: clipboard write calls (with argument for code)
        call_count = 0

        async def mock_evaluate(script, *args):
            nonlocal call_count
            call_count += 1
            if "selectors.forEach" in script:
                # Debug info call
                return {".cm-editor": 1, "visible": 1}
            elif "data-sync-target" in script and "setAttribute" in script:
                # Find visible editor call
                return ".cm-editor"
            elif "removeAttribute" in script:
                # Cleanup call
                return None
            elif "clipboard.writeText" in script:
                # Clipboard write call (may have args for code parameter)
                return None
            return None

        mock_page.evaluate = mock_evaluate

        # Mock the locator for [data-sync-target="true"]
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        mock_page.keyboard = AsyncMock()
        mock_page.keyboard.press = AsyncMock()

        syncer.page = mock_page

        await syncer.update_code("def handler(pd): pass")

        # Verify keyboard shortcuts were used
        mock_page.keyboard.press.assert_any_call("ControlOrMeta+KeyA")
        mock_page.keyboard.press.assert_any_call("ControlOrMeta+KeyV")

    @pytest.mark.asyncio
    async def test_update_code_no_editor_found(self, mock_config):
        """Test update_code raises error when no visible editor found."""
        syncer = PipedreamSyncer(config=mock_config)

        mock_page = AsyncMock()

        async def mock_evaluate(script):
            if "selectors.forEach" in script:
                return {".cm-editor": 0, "visible": 0}
            elif "data-sync-target" in script and "setAttribute" in script:
                # No visible editor found
                return None
            return None

        mock_page.evaluate = mock_evaluate
        syncer.page = mock_page

        with pytest.raises(CodeUpdateError, match="No visible editor"):
            await syncer.update_code("test code")

    @pytest.mark.asyncio
    async def test_update_code_clipboard_paste(self, mock_config):
        """Test update_code uses clipboard paste."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        clipboard_written = []

        async def mock_evaluate(script, *args):
            if "selectors.forEach" in script:
                return {".cm-editor": 1, "visible": 1}
            elif "data-sync-target" in script and "setAttribute" in script:
                return ".cm-editor"
            elif "removeAttribute" in script:
                return None
            elif "clipboard.writeText" in script:
                # Capture the code argument if provided
                if args:
                    clipboard_written.append(args[0])
                else:
                    clipboard_written.append(script)
                return None
            return None

        mock_page.evaluate = mock_evaluate

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        mock_page.keyboard = AsyncMock()
        mock_page.keyboard.press = AsyncMock()

        syncer.page = mock_page

        test_code = "print('hello')"
        await syncer.update_code(test_code)

        # Verify clipboard write was called with the code
        assert len(clipboard_written) > 0
        # First clipboard write should be the code, second is the empty string to clear
        assert "hello" in clipboard_written[0]


class TestNavigateToWorkflow:
    """Tests for navigate_to_workflow with mocked page."""

    @pytest.mark.asyncio
    async def test_navigate_success(self, mock_config):
        """Test successful navigation to workflow."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        syncer.page = mock_page

        await syncer.navigate_to_workflow("test-workflow-p_abc")

        mock_page.goto.assert_called_once()
        assert "build" in str(mock_page.goto.call_args)

    @pytest.mark.asyncio
    async def test_navigate_timeout(self, mock_config):
        """Test navigation timeout raises error."""
        syncer = PipedreamSyncer(config=mock_config, screenshot_always=False)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        syncer.page = mock_page

        with pytest.raises(NavigationError, match="Timeout"):
            await syncer.navigate_to_workflow("test-p_abc")


class TestDeployWorkflow:
    """Tests for deploy_workflow with mocked page."""

    @pytest.mark.asyncio
    async def test_deploy_success(self, mock_config):
        """Test successful deployment."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_button = AsyncMock()
        mock_button.click = AsyncMock()

        mock_page = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(return_value=mock_button)
        # Mock evaluate to return False for DEPLOY PENDING check (no pending)
        mock_page.evaluate = AsyncMock(return_value=False)
        mock_page.goto = AsyncMock()
        syncer.page = mock_page

        result = await syncer.deploy_workflow("Test Workflow")

        assert result is True
        mock_button.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_deploy_button_not_found(self, mock_config):
        """Test deploy returns False when button not found."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        syncer.page = mock_page

        result = await syncer.deploy_workflow()

        assert result is False


class TestWaitForSave:
    """Tests for wait_for_save with mocked page."""

    @pytest.mark.asyncio
    async def test_wait_for_save_success(self, mock_config):
        """Test wait_for_save detects save completion."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        syncer.page = mock_page

        result = await syncer.wait_for_save()

        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_save_timeout(self, mock_config):
        """Test wait_for_save returns True even on timeout (verification catches issues)."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        syncer.page = mock_page

        result = await syncer.wait_for_save()

        # Should return True - verification will catch any save failures
        assert result is True


class TestFindAndClickStep:
    """Tests for find_and_click_step with mocked page."""

    @pytest.mark.asyncio
    async def test_find_step_success(self, mock_config):
        """Test successfully finding and clicking a step."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()

        # Mock the locator chain for finding the step text
        mock_text_locator = MagicMock()
        mock_text_locator.count = AsyncMock(return_value=1)
        mock_text_locator.first = MagicMock()
        mock_text_locator.first.dblclick = AsyncMock()

        # Mock parent locator that fails (so it falls through to text_el.dblclick)
        mock_parent_locator = MagicMock()
        mock_parent_locator.first = MagicMock()
        mock_parent_locator.first.count = AsyncMock(return_value=0)

        def locator_side_effect(selector):
            if "text=" in selector:
                return mock_text_locator
            return mock_parent_locator

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        syncer.page = mock_page

        await syncer.find_and_click_step("test_step")

        # Should have double-clicked the text element
        mock_text_locator.first.dblclick.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_step_not_found(self, mock_config):
        """Test StepNotFoundError when step doesn't exist."""
        syncer = PipedreamSyncer(config=mock_config)

        mock_page = AsyncMock()
        mock_page.click = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        syncer.page = mock_page

        with pytest.raises(StepNotFoundError):
            await syncer.find_and_click_step("nonexistent_step")


class TestSyncStepDryRun:
    """Tests for sync_step in dry run mode."""

    @pytest.mark.asyncio
    async def test_sync_step_dry_run(self, mock_config, tmp_path):
        """Test sync_step in dry run mode."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        step = StepConfig(step_name="test", script_path="test.py")
        result = await syncer.sync_step("workflow_id", step, tmp_path)

        assert result.status == "skipped"
        assert result.message == "Dry run"


class TestSyncWorkflowDryRun:
    """Tests for sync_workflow in dry run mode."""

    @pytest.mark.asyncio
    async def test_sync_workflow_dry_run(self, mock_config, tmp_path, capsys):
        """Test sync_workflow in dry run mode."""
        syncer = PipedreamSyncer(config=mock_config, dry_run=True)

        result = await syncer.sync_workflow("test_workflow", tmp_path)

        assert result.status == "skipped"
        assert len(result.steps) == 2  # Two steps in mock config

        captured = capsys.readouterr()
        assert "dry-run" in captured.out


class TestWaitForLogin:
    """Tests for wait_for_login with mocked page."""

    @pytest.mark.asyncio
    async def test_already_logged_in(self, mock_config):
        """Test wait_for_login when already logged in."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()  # Returns immediately (logged in)
        syncer.page = mock_page

        result = await syncer.wait_for_login()

        assert result is True

    @pytest.mark.asyncio
    async def test_login_via_url_check(self, mock_config):
        """Test wait_for_login detects login via URL change."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        # First call: not logged in, second call: times out but URL check passes
        mock_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        # Simulate being on workflows page (logged in)
        type(mock_page).url = PropertyMock(return_value="https://pipedream.com/workflows")
        syncer.page = mock_page

        result = await syncer.wait_for_login()

        assert result is True


class TestVerifyCodeUpdate:
    """Tests for verify_code_update method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={
                "test_workflow": WorkflowConfig(
                    id="test-workflow-p_abc123",
                    name="Test Workflow",
                    steps=[
                        StepConfig(step_name="step1", script_path="src/steps/step1.py"),
                    ],
                )
            },
            settings=DeploySettings(
                step_timeout=60,
                max_retries=3,
                autosave_wait=1.0,
            ),
            pipedream_username="testuser",
            pipedream_project_id="proj_test",
        )

    @pytest.mark.asyncio
    async def test_verify_code_update_without_page(self, mock_config):
        """Test verify_code_update returns False when page not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        result = await syncer.verify_code_update("test code", "test_step")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_code_update_with_matching_handler(self, mock_config):
        """Test verify_code_update returns True when handler functions match."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        expected_code = "def handler_test_step(pd): pass"
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="def handler_test_step(pd): pass")
        syncer.page = mock_page

        result = await syncer.verify_code_update(expected_code, "test_step")
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_code_update_handler_mismatch(self, mock_config):
        """Test verify_code_update returns False when handler functions don't match."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        expected_code = "def handler_expected(pd): pass"
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="def handler_different(pd): pass")
        syncer.page = mock_page

        result = await syncer.verify_code_update(expected_code, "test_step")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_code_update_empty_actual_code(self, mock_config):
        """Test verify_code_update returns False when editor is empty."""
        syncer = PipedreamSyncer(config=mock_config, verbose=True)

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="")
        syncer.page = mock_page

        result = await syncer.verify_code_update("def handler(pd): pass", "test_step")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_code_update_handles_exception(self, mock_config):
        """Test verify_code_update handles exceptions gracefully."""
        syncer = PipedreamSyncer(config=mock_config)

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("Evaluation failed"))
        syncer.page = mock_page

        result = await syncer.verify_code_update("test code", "test_step")
        assert result is False


class TestWorkflowEditUrl:
    """Tests for workflow_edit_url function from selectors module."""

    def test_workflow_edit_url_with_username_and_project(self):
        """Test building URL with username and project ID."""
        from src.deploy.selectors import workflow_edit_url

        url = workflow_edit_url(
            base_url="https://pipedream.com",
            workflow_id="my-workflow-p_xyz789",
            username="testuser",
            project_id="proj_abc123",
        )

        assert "testuser" in url
        assert "proj_abc123" in url
        assert "my-workflow-p_xyz789" in url
        assert "build" in url

    def test_workflow_edit_url_legacy_format(self):
        """Test building URL without username (legacy format)."""
        from src.deploy.selectors import workflow_edit_url

        url = workflow_edit_url(
            base_url="https://pipedream.com",
            workflow_id="p_xyz789",
            username="",
            project_id="",
        )

        assert "pipedream.com" in url
        assert "p_xyz789" in url
        assert "edit" in url


class TestSyncStepWithScriptFile:
    """Tests for sync_step with actual script file handling."""

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
                    steps=[
                        StepConfig(step_name="test_step", script_path="src/steps/test.py"),
                    ],
                )
            },
            settings=DeploySettings(autosave_wait=0.1),
            pipedream_username="testuser",
            pipedream_project_id="proj_test",
        )

    @pytest.mark.asyncio
    async def test_sync_step_script_not_found(self, mock_config, tmp_path):
        """Test sync_step handles missing script file."""
        syncer = PipedreamSyncer(config=mock_config)

        step = StepConfig(step_name="test", script_path="nonexistent.py")
        result = await syncer.sync_step("p_abc", step, tmp_path)

        assert result.status == "failed"
        assert "not found" in result.message.lower() or "error" in result.message.lower()


class TestLogLevels:
    """Tests for log method with different levels."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    def test_log_success_level(self, mock_config, capsys):
        """Test logging with success level."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Success message", "success")

        captured = capsys.readouterr()
        assert "Success message" in captured.out

    def test_log_unknown_level_defaults_to_info(self, mock_config, capsys):
        """Test logging with unknown level uses default behavior."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.log("Unknown level message", "unknown")

        captured = capsys.readouterr()
        assert "Unknown level message" in captured.out


class TestScreenshotAlways:
    """Tests for screenshot_always functionality."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(screenshot_path=".tmp/screenshots"),
        )

    @pytest.mark.asyncio
    async def test_take_screenshot_with_screenshot_always(self, mock_config, tmp_path):
        """Test take_screenshot works with screenshot_always enabled."""
        mock_config.settings.screenshot_path = str(tmp_path)
        syncer = PipedreamSyncer(config=mock_config, screenshot_always=True)

        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock()
        syncer.page = mock_page

        result = await syncer.take_screenshot("always_test")

        assert result is not None
        assert "always_test" in result
        mock_page.screenshot.assert_called_once()


class TestResultsTracking:
    """Tests for results tracking in syncer."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    def test_results_empty_on_init(self, mock_config):
        """Test that results list is empty on initialization."""
        syncer = PipedreamSyncer(config=mock_config)
        assert syncer.results == []

    def test_results_can_be_appended(self, mock_config):
        """Test that results can be appended."""
        syncer = PipedreamSyncer(config=mock_config)
        result = WorkflowResult(
            workflow_key="test",
            workflow_id="p_test",
            workflow_name="Test",
            status="success",
        )
        syncer.results.append(result)
        assert len(syncer.results) == 1
        assert syncer.results[0].status == "success"


class TestGetUniqueMarker:
    """Tests for _get_unique_marker method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    def test_finds_default_max_results_marker(self, mock_config):
        """Test finding DEFAULT_MAX_RESULTS marker."""
        syncer = PipedreamSyncer(config=mock_config)
        code = "# Gmail fetcher\nDEFAULT_MAX_RESULTS = 50\ndef handler(pd): pass"

        marker = syncer._get_unique_marker(code)

        assert "DEFAULT_MAX_RESULTS" in marker

    def test_finds_label_name_marker(self, mock_config):
        """Test finding LABEL_NAME_TO_ADD marker."""
        syncer = PipedreamSyncer(config=mock_config)
        code = "# Label step\nLABEL_NAME_TO_ADD = 'notiontaskcreated'\ndef handler(pd): pass"

        marker = syncer._get_unique_marker(code)

        assert "LABEL_NAME_TO_ADD" in marker

    def test_finds_previous_step_gmail_marker(self, mock_config):
        """Test finding PREVIOUS_STEP_NAME=gmail marker."""
        syncer = PipedreamSyncer(config=mock_config)
        code = "# Notion step\nPREVIOUS_STEP_NAME = 'gmail'\ndef handler(pd): pass"

        marker = syncer._get_unique_marker(code)

        assert "PREVIOUS_STEP_NAME" in marker

    def test_finds_gcal_event_marker(self, mock_config):
        """Test finding gcal_event_to_notion marker."""
        syncer = PipedreamSyncer(config=mock_config)
        code = "# GCal step\ndef gcal_event_to_notion(event): pass"

        marker = syncer._get_unique_marker(code)

        assert "gcal_event_to_notion" in marker

    def test_fallback_to_config_line(self, mock_config):
        """Test fallback to a config line when no known markers found."""
        syncer = PipedreamSyncer(config=mock_config)
        # Build code with enough lines before the config section
        code = "\n".join(["# import line"] * 12) + "\nSOME_CONFIG = 'value'\ndef handler(pd): pass"

        marker = syncer._get_unique_marker(code)

        assert "SOME_CONFIG" in marker

    def test_fallback_to_first_100_chars(self, mock_config):
        """Test fallback to first 100 chars when no markers or config lines."""
        syncer = PipedreamSyncer(config=mock_config)
        code = "def simple_function():\n    pass"

        marker = syncer._get_unique_marker(code)

        assert len(marker) <= 100
        assert "def simple_function" in marker


class TestTeardownBrowser:
    """Tests for teardown_browser method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    @pytest.mark.asyncio
    async def test_teardown_browser_closes_context_and_playwright(self, mock_config):
        """Test teardown properly closes context and stops playwright."""
        syncer = PipedreamSyncer(config=mock_config)

        mock_context = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        mock_playwright = AsyncMock()
        syncer.context = mock_context
        syncer.playwright = mock_playwright

        await syncer.teardown_browser()

        mock_context.close.assert_called_once()
        mock_playwright.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_teardown_browser_handles_no_context(self, mock_config):
        """Test teardown handles case when context is None."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.context = None
        syncer.playwright = AsyncMock()

        # Should not raise
        await syncer.teardown_browser()

    @pytest.mark.asyncio
    async def test_teardown_browser_handles_no_playwright(self, mock_config):
        """Test teardown handles case when playwright is None."""
        syncer = PipedreamSyncer(config=mock_config)
        mock_context = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        syncer.context = mock_context
        syncer.playwright = None

        # Should not raise
        await syncer.teardown_browser()


class TestNavigationErrors:
    """Tests for navigation error handling."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(screenshot_on_failure=True),
        )

    @pytest.mark.asyncio
    async def test_navigate_to_workflow_no_page(self, mock_config):
        """Test navigate_to_workflow raises when page not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = None

        with pytest.raises(NavigationError, match="Browser not initialized"):
            await syncer.navigate_to_workflow("p_test123")


class TestClickCodeTab:
    """Tests for click_code_tab method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(),
        )

    @pytest.mark.asyncio
    async def test_click_code_tab_returns_early_when_no_page(self, mock_config):
        """Test click_code_tab returns early when page not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = None

        # Should return early without raising
        result = await syncer.click_code_tab()
        assert result is None


class TestUpdateCode:
    """Tests for update_code method."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock DeployConfig for testing."""
        return DeployConfig(
            version="1.0",
            pipedream_base_url="https://pipedream.com",
            workflows={},
            settings=DeploySettings(autosave_wait=0.1),
        )

    @pytest.mark.asyncio
    async def test_update_code_no_page(self, mock_config):
        """Test update_code raises when page not initialized."""
        syncer = PipedreamSyncer(config=mock_config)
        syncer.page = None

        with pytest.raises(CodeUpdateError, match="Browser not initialized"):
            await syncer.update_code("def handler(pd): pass")
