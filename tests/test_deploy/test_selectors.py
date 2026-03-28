"""Tests for Pipedream UI DOM selectors."""

import pytest

from src.deploy.selectors import (
    CODE_EDITOR,
    CODEMIRROR6_EDITOR,
    CODEMIRROR6_CONTENT,
    DEPLOY_BUTTON,
    LOGGED_IN_INDICATOR,
    LOGIN_BUTTON,
    MONACO_EDITOR,
    SAVED_INDICATOR,
    STEP_CONTAINER,
    SelectorSets,
    step_by_index,
    step_by_name,
    workflow_edit_url,
    workflow_url,
)


class TestStepByName:
    """Tests for step_by_name selector generator."""

    def test_basic_step_name(self):
        """Test generating selector for a basic step name."""
        selector = step_by_name("fetch_gmail_emails")
        assert "fetch_gmail_emails" in selector
        assert "data-step-name" in selector

    def test_step_name_with_spaces(self):
        """Test generating selector for step name with spaces."""
        selector = step_by_name("My Custom Step")
        assert "My Custom Step" in selector

    def test_returns_multiple_patterns(self):
        """Test that selector includes multiple fallback patterns."""
        selector = step_by_name("test_step")
        # Should contain comma-separated selectors for fallback
        assert "," in selector
        assert "data-step-name" in selector
        assert "data-testid" in selector
        assert "step-container" in selector


class TestStepByIndex:
    """Tests for step_by_index selector generator."""

    def test_index_zero(self):
        """Test generating selector for first step (index 0)."""
        selector = step_by_index(0)
        assert "nth=0" in selector
        assert STEP_CONTAINER in selector

    def test_index_positive(self):
        """Test generating selector for positive index."""
        selector = step_by_index(5)
        assert "nth=5" in selector

    def test_index_format(self):
        """Test the format of the generated selector."""
        selector = step_by_index(3)
        expected = f"{STEP_CONTAINER} >> nth=3"
        assert selector == expected


class TestWorkflowUrl:
    """Tests for workflow_url generator."""

    def test_url_with_username_and_project(self):
        """Test URL generation with username and project ID."""
        url = workflow_url(
            base_url="https://pipedream.com",
            workflow_id="gmail-to-notion-p_abc123",
            username="testuser",
            project_id="proj_xyz"
        )
        assert url == "https://pipedream.com/@testuser/projects/proj_xyz/gmail-to-notion-p_abc123/inspect"

    def test_url_legacy_format(self):
        """Test URL generation in legacy format (no username/project)."""
        url = workflow_url(
            base_url="https://pipedream.com",
            workflow_id="p_abc123",
            username="",
            project_id=""
        )
        assert url == "https://pipedream.com/workflows/p_abc123"

    def test_url_strips_trailing_slash(self):
        """Test that trailing slash is stripped from base URL."""
        url = workflow_url(
            base_url="https://pipedream.com/",
            workflow_id="workflow-p_abc123",
            username="user",
            project_id="proj"
        )
        assert not url.startswith("https://pipedream.com//")
        assert "/@user/projects/proj/workflow-p_abc123/inspect" in url

    def test_url_without_project_uses_legacy(self):
        """Test that missing project uses legacy format."""
        url = workflow_url(
            base_url="https://pipedream.com",
            workflow_id="p_test",
            username="user",
            project_id=""
        )
        assert "/workflows/p_test" in url


class TestWorkflowEditUrl:
    """Tests for workflow_edit_url generator."""

    def test_edit_url_with_username_and_project(self):
        """Test edit URL generation with username and project ID."""
        url = workflow_edit_url(
            base_url="https://pipedream.com",
            workflow_id="gmail-to-notion-p_abc123",
            username="testuser",
            project_id="proj_xyz"
        )
        assert url == "https://pipedream.com/@testuser/projects/proj_xyz/gmail-to-notion-p_abc123/build"

    def test_edit_url_legacy_format(self):
        """Test edit URL generation in legacy format."""
        url = workflow_edit_url(
            base_url="https://pipedream.com",
            workflow_id="p_abc123",
            username="",
            project_id=""
        )
        assert url == "https://pipedream.com/workflows/p_abc123/edit"

    def test_edit_url_strips_trailing_slash(self):
        """Test that trailing slash is stripped from base URL."""
        url = workflow_edit_url(
            base_url="https://pipedream.com/",
            workflow_id="workflow-p_abc123",
            username="user",
            project_id="proj"
        )
        assert not url.startswith("https://pipedream.com//")

    def test_edit_vs_inspect_url(self):
        """Test that edit URL uses /build while inspect uses /inspect."""
        edit_url = workflow_edit_url(
            base_url="https://pipedream.com",
            workflow_id="wf-p_test",
            username="user",
            project_id="proj"
        )
        inspect_url = workflow_url(
            base_url="https://pipedream.com",
            workflow_id="wf-p_test",
            username="user",
            project_id="proj"
        )
        assert "/build" in edit_url
        assert "/inspect" in inspect_url


class TestSelectorSets:
    """Tests for SelectorSets class."""

    def test_page_loaded_selectors_exist(self):
        """Test that PAGE_LOADED selector set exists and has items."""
        assert hasattr(SelectorSets, 'PAGE_LOADED')
        assert len(SelectorSets.PAGE_LOADED) > 0

    def test_step_editor_open_selectors_exist(self):
        """Test that STEP_EDITOR_OPEN selector set exists."""
        assert hasattr(SelectorSets, 'STEP_EDITOR_OPEN')
        assert len(SelectorSets.STEP_EDITOR_OPEN) > 0

    def test_save_complete_selectors_exist(self):
        """Test that SAVE_COMPLETE selector set exists."""
        assert hasattr(SelectorSets, 'SAVE_COMPLETE')
        assert len(SelectorSets.SAVE_COMPLETE) > 0

    def test_auth_failed_selectors_exist(self):
        """Test that AUTH_FAILED selector set exists."""
        assert hasattr(SelectorSets, 'AUTH_FAILED')
        assert len(SelectorSets.AUTH_FAILED) > 0

    def test_page_loaded_contains_expected_selectors(self):
        """Test PAGE_LOADED contains key selectors."""
        selectors = SelectorSets.PAGE_LOADED
        # Should include logged in indicator
        assert LOGGED_IN_INDICATOR in selectors


class TestSelectorConstants:
    """Tests for selector constant definitions."""

    def test_monaco_editor_selector(self):
        """Test Monaco editor selector is defined."""
        assert ".monaco-editor" in MONACO_EDITOR

    def test_code_editor_includes_cm6(self):
        """Test CODE_EDITOR includes CodeMirror 6 selector (current Pipedream editor)."""
        assert ".cm-editor" in CODE_EDITOR

    def test_code_editor_includes_monaco(self):
        """Test CODE_EDITOR includes Monaco selector as fallback."""
        assert "monaco-editor" in CODE_EDITOR.lower() or ".monaco" in CODE_EDITOR

    def test_code_editor_cm6_before_monaco(self):
        """Test CM6 is listed before Monaco in CODE_EDITOR (priority order)."""
        cm6_pos = CODE_EDITOR.index(".cm-editor")
        monaco_pos = CODE_EDITOR.index(".monaco-editor")
        assert cm6_pos < monaco_pos, "CM6 selector should come before Monaco in CODE_EDITOR"

    def test_codemirror6_constants_defined(self):
        """Test CM6 specific constants are defined and non-empty."""
        assert CODEMIRROR6_EDITOR == ".cm-editor"
        assert CODEMIRROR6_CONTENT == ".cm-content"

    def test_deploy_button_selector(self):
        """Test DEPLOY_BUTTON selector includes text match."""
        assert "Deploy" in DEPLOY_BUTTON

    def test_saved_indicator_selector(self):
        """Test SAVED_INDICATOR selector is defined."""
        assert "saved" in SAVED_INDICATOR.lower() or "Saved" in SAVED_INDICATOR

    def test_login_button_selector(self):
        """Test LOGIN_BUTTON selector is defined."""
        assert LOGIN_BUTTON is not None
        assert len(LOGIN_BUTTON) > 0

    def test_logged_in_indicator_no_playwright_combinator(self):
        """LOGGED_IN_INDICATOR must not use >> combinator (invalid in CSS selector strings)."""
        assert ">>" not in LOGGED_IN_INDICATOR, (
            "LOGGED_IN_INDICATOR uses >> which is not valid in CSS selector strings "
            "passed to page.wait_for_selector(); use :has-text() or plain CSS instead"
        )

    def test_logged_in_indicator_selector(self):
        """Test LOGGED_IN_INDICATOR selector is defined and non-empty."""
        assert LOGGED_IN_INDICATOR is not None
        assert len(LOGGED_IN_INDICATOR) > 0

    def test_step_by_name_no_playwright_combinator(self):
        """step_by_name must not use >> combinator (invalid in CSS selector strings)."""
        selector = step_by_name("test_step")
        assert ">>" not in selector, (
            "step_by_name output uses >> which is not valid in CSS selector strings; "
            "use :has-text() or plain CSS instead"
        )
