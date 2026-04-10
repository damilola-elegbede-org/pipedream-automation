"""
Pipedream UI DOM Selectors

These selectors target the Pipedream workflow editor UI elements.
They may need updates if Pipedream changes their UI structure.

IMPORTANT: These selectors were created based on Pipedream's UI as of December 2024.
If deployment starts failing, inspect the Pipedream UI to update these selectors.
"""

import re

from .exceptions import ValidationError

# Validation patterns for workflow/step identifiers
VALID_STEP_NAME = re.compile(r'^[a-zA-Z0-9_\-\s]{1,100}$')
VALID_WORKFLOW_ID = re.compile(r'^[a-zA-Z0-9_\-]{1,100}$')


def validate_step_name(name: str) -> str:
    """Validate step name format to prevent selector injection."""
    if not VALID_STEP_NAME.match(name):
        raise ValidationError(
            f"Invalid step name format: '{name}'. "
            "Must be 1-100 alphanumeric characters, underscores, hyphens, or spaces."
        )
    return name


def validate_workflow_id(workflow_id: str) -> str:
    """Validate workflow ID format to prevent URL injection."""
    if not VALID_WORKFLOW_ID.match(workflow_id):
        raise ValidationError(
            f"Invalid workflow ID format: '{workflow_id}'. "
            "Must be 1-100 alphanumeric characters, underscores, or hyphens."
        )
    return workflow_id


# =============================================================================
# Authentication Selectors
# =============================================================================

# Login page elements

LOGIN_FORM = "form[action*='login'], .login-form"
LOGIN_BUTTON = "button[type='submit'], button:has-text('Log in'), button:has-text('Sign in')"
USER_MENU = "[data-testid='user-menu'], .user-avatar, .user-dropdown"
# Note: avoid Playwright `>>` combinator in CSS strings passed to wait_for_selector;
# use plain CSS or :has-text() pseudo-class instead.
LOGGED_IN_INDICATOR = "[data-testid='user-menu'], .user-avatar, a[href*='/workflows']"

# =============================================================================
# Navigation Selectors
# =============================================================================

# Top-level navigation
NAV_WORKFLOWS = "a[href*='/workflows']"
NAV_PROJECTS = "a[href*='/projects']"

# Workflow list
WORKFLOW_LIST = "[data-testid='workflow-list'], .workflow-list, .workflows-container"
WORKFLOW_ITEM = "[data-testid='workflow-item'], .workflow-item, .workflow-row"

# =============================================================================
# Workflow Editor Selectors
# =============================================================================

# Main editor canvas
WORKFLOW_CANVAS = "[data-testid='workflow-canvas'], .workflow-canvas, .canvas-container"
WORKFLOW_HEADER = "[data-testid='workflow-header'], .workflow-header, header.workflow"

# Step containers
STEP_CONTAINER = "[data-testid='step'], .step-container, .workflow-step"
STEP_HEADER = "[data-testid='step-header'], .step-header, .step-title"


def step_by_name(name: str) -> str:
    """
    Generate a CSS/Playwright selector for a workflow step card by its name.

    Pipedream renders step names as text inside card-like ``<div>`` elements
    on the workflow canvas.  The returned selector is a *comma-separated list*
    of candidates tried left-to-right by Playwright's ``wait_for_selector`` /
    ``locator``.

    **Important:** Playwright's ``>>`` combinator is **NOT** valid inside a
    CSS selector string passed to ``page.wait_for_selector()``.  This
    function only uses standard CSS4 + Playwright's ``:has-text()``
    pseudo-class.

    Updated 2026-03: prioritise data attributes and :has-text() over raw
    ``div:has-text()`` which can match far too broadly.

    Raises:
        ValidationError: If step name contains invalid characters
    """
    # Validate step name format first
    validate_step_name(name)

    # Escape single quotes for :has-text() selectors
    escaped_single = name.replace("'", "\\'")
    # Escape double quotes for CSS attribute selectors
    escaped_double = name.replace('"', '\\"')

    # Build selector list — ordered from most specific to most general.
    # Avoid Playwright ``>>`` combinator here; callers that need chaining
    # should use ``page.locator(step_by_name(name))`` and chain from there.
    return (
        # 1. Data attribute (best — if Pipedream ever adds this)
        f'[data-step-name="{escaped_double}"], '
        # 2. data-testid with text guard
        f"[data-testid='step']:has-text('{escaped_single}'), "
        # 3. Semantic class names with text guard
        f".step-container:has-text('{escaped_single}'), "
        f".workflow-step:has-text('{escaped_single}'), "
        # 4. Generic node/card class patterns used by common workflow editors
        f"[class*='node']:has-text('{escaped_single}'), "
        f"[class*='card']:has-text('{escaped_single}')"
    )


def step_by_index(index: int) -> str:
    """Generate selector for a step by its index (0-based)."""
    return f"{STEP_CONTAINER} >> nth={index}"


# =============================================================================
# Step Editor Selectors
# =============================================================================

# Step configuration panel (appears when step is clicked)
STEP_CONFIG_PANEL = "[data-testid='step-config'], .step-config, .step-panel, .config-panel"
STEP_CLOSE_BUTTON = "[data-testid='close-step'], .close-button, button[aria-label='Close']"

# Tabs within step configuration
TAB_CODE = "[data-testid='tab-code'], button:has-text('Code'), .tab-code, [role='tab']:has-text('Code')"
TAB_CONFIGURATION = "[data-testid='tab-config'], button:has-text('Configuration'), .tab-config"
TAB_TEST = "[data-testid='tab-test'], button:has-text('Test'), .tab-test"

# =============================================================================
# Code Editor Selectors (Monaco/CodeMirror)
# =============================================================================

# Monaco editor (used by some Pipedream step types)
MONACO_EDITOR = ".monaco-editor"
MONACO_TEXTAREA = ".monaco-editor textarea.inputarea"
MONACO_LINES = ".monaco-editor .view-lines"
MONACO_LINE = ".monaco-editor .view-line"

# CodeMirror 5 (legacy — unlikely in current Pipedream but kept for safety)
CODEMIRROR_EDITOR = ".CodeMirror"
CODEMIRROR_TEXTAREA = ".CodeMirror textarea"
CODEMIRROR_LINES = ".CodeMirror-code"

# CodeMirror 6 — Pipedream's Python step editor as of 2025
# .cm-editor  → outer wrapper element
# .cm-content → the editable content div (contenteditable)
# .cm-scroller → scroll container (useful for scroll_into_view_if_needed)
CODEMIRROR6_EDITOR = ".cm-editor"
CODEMIRROR6_CONTENT = ".cm-content"
CODEMIRROR6_SCROLLER = ".cm-scroller"

# Generic code editor — CM6 first (current Pipedream Python editor), then Monaco, then CM5
CODE_EDITOR = f"{CODEMIRROR6_EDITOR}, {MONACO_EDITOR}, {CODEMIRROR_EDITOR}"
CODE_TEXTAREA = f"{CODEMIRROR6_CONTENT}, {MONACO_TEXTAREA}, {CODEMIRROR_TEXTAREA}"
CODE_CONTENT = f"{CODEMIRROR6_CONTENT}, {MONACO_LINES}, {CODEMIRROR_LINES}"

# =============================================================================
# Save/Status Indicators
# =============================================================================

# Autosave status
SAVE_STATUS = "[data-testid='save-status'], .save-status, .save-indicator"
# :has-text() is Playwright pseudo-class (valid in page.wait_for_selector).
# Using both data attributes and text fallbacks for resilience.
SAVING_INDICATOR = "[data-status='saving'], .saving, [class*='saving']"
SAVED_INDICATOR = "[data-status='saved'], .saved, [class*='saved']"
SAVE_ERROR = "[data-status='error'], .save-error, .error-indicator"

# Deployment status
# Note: Pipedream's Deploy button may render as a <button> or a styled <div>.
# The text-based selector is the most resilient; keep the data-testid as primary.
DEPLOY_BUTTON = "[data-testid='deploy-button'], button:has-text('Deploy')"
DEPLOYED_INDICATOR = "[class*='deployed'], .deployed-badge"

# =============================================================================
# Error and Alert Selectors
# =============================================================================

# Error messages
ERROR_BANNER = "[role='alert'], .error-banner, .alert-error"
ERROR_MESSAGE = ".error-message, [data-testid='error-message']"
TOAST_ERROR = ".toast-error, .notification-error"
TOAST_SUCCESS = ".toast-success, .notification-success"

# Modal dialogs
MODAL_OVERLAY = "[role='dialog'], .modal-overlay, .modal-backdrop"
MODAL_CONTENT = "[role='dialog'] .modal-content, .modal-body"
MODAL_CLOSE = "[role='dialog'] button[aria-label='Close'], .modal-close"

# =============================================================================
# Python-Specific Selectors
# =============================================================================

# Python step indicator
PYTHON_STEP = "[data-language='python'], .python-step, :has-text('Python')"
PYTHON_ICON = "img[alt*='python'], svg[data-icon='python']"

# Language selector (if changing step type)
LANGUAGE_SELECTOR = "[data-testid='language-selector'], .language-picker"
PYTHON_OPTION = "option[value='python'], [data-language='python']"

# =============================================================================
# Utility Functions
# =============================================================================


def workflow_url(
    base_url: str,
    workflow_id: str,
    username: str = "",
    project_id: str = ""
) -> str:
    """Generate the URL for a workflow's page.

    Raises:
        ValidationError: If workflow_id contains invalid characters
    """
    validate_workflow_id(workflow_id)
    base = base_url.rstrip("/")
    if username and project_id:
        # New format: /@username/projects/project_id/workflow-slug/inspect
        return f"{base}/@{username}/projects/{project_id}/{workflow_id}/inspect"
    # Legacy format (may not work)
    return f"{base}/workflows/{workflow_id}"


def workflow_edit_url(
    base_url: str,
    workflow_id: str,
    username: str = "",
    project_id: str = ""
) -> str:
    """Generate the URL for a workflow's build/edit page.

    Raises:
        ValidationError: If workflow_id contains invalid characters
    """
    validate_workflow_id(workflow_id)
    base = base_url.rstrip("/")
    if username and project_id:
        # New format: /@username/projects/project_id/workflow-slug/build
        return f"{base}/@{username}/projects/{project_id}/{workflow_id}/build"
    # Legacy format (may not work)
    return f"{base}/workflows/{workflow_id}/edit"


# =============================================================================
# Selector Sets for Different Operations
# =============================================================================


class SelectorSets:
    """Pre-defined selector combinations for common operations."""

    # Wait for page load
    PAGE_LOADED = [
        WORKFLOW_CANVAS,
        STEP_CONTAINER,
        LOGGED_IN_INDICATOR,
    ]

    # Wait for step editor to open
    STEP_EDITOR_OPEN = [
        STEP_CONFIG_PANEL,
        CODE_EDITOR,
    ]

    # Wait for save to complete
    SAVE_COMPLETE = [
        SAVED_INDICATOR,
    ]

    # Indicators of authentication failure
    AUTH_FAILED = [
        LOGIN_BUTTON,
        LOGIN_FORM,
    ]
