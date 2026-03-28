#!/usr/bin/env python3
"""
Pipedream Workflow Deploy Script

Uses Playwright to automate updating Python code steps in Pipedream workflows.
Supports interactive Google SSO login with persistent browser profile.

Usage:
    python -m src.deploy.deploy_to_pipedream                    # Interactive login
    python -m src.deploy.deploy_to_pipedream --workflow gmail_to_notion
    python -m src.deploy.deploy_to_pipedream --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import (
        async_playwright,
        Browser,
        BrowserContext,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeout,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    # Define stub types for type hints when Playwright not installed
    async_playwright = None
    Browser = None
    BrowserContext = None
    Page = None
    Playwright = None
    PlaywrightTimeout = Exception

from .config import DeployConfig, load_config, validate_config, StepConfig
from .exceptions import (
    AuthenticationError,
    CodeUpdateError,
    NavigationError,
    PipedreamSyncError,
    SaveError,
    StepNotFoundError,
)
from .selectors import (
    CODE_EDITOR,
    LOGGED_IN_INDICATOR,
    LOGIN_BUTTON,
    SAVED_INDICATOR,
    SAVING_INDICATOR,
    STEP_CONFIG_PANEL,
    TAB_CODE,
    step_by_name,
    workflow_edit_url,
)
from .utils import (
    ensure_screenshot_dir,
    generate_report,
    get_cached_cookies,
    load_and_set_env_local,
    read_script_content,
    save_cookies_to_env_local,
    validate_cookie_expiration,
)


# Browser profile directory for persistent sessions
BROWSER_PROFILE_DIR = Path(".tmp/browser_profile")


@dataclass
class StepResult:
    """Result of syncing a single step."""
    step_name: str
    script_path: str
    status: str  # "success", "failed", "skipped"
    message: str = ""
    duration_seconds: float = 0.0


@dataclass
class WorkflowResult:
    """Result of syncing a workflow."""
    workflow_key: str
    workflow_id: str
    workflow_name: str
    status: str  # "success", "partial", "failed", "skipped"
    steps: list[StepResult] = field(default_factory=list)
    error: Optional[str] = None


class PipedreamSyncer:
    """Main orchestrator for Pipedream code sync operations."""

    def __init__(
        self,
        config: DeployConfig,
        dry_run: bool = False,
        verbose: bool = False,
        screenshot_always: bool = False,
    ):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose
        self.screenshot_always = screenshot_always
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.results: list[WorkflowResult] = []

    async def __aenter__(self) -> "PipedreamSyncer":
        """Async context manager entry - setup browser."""
        await self.setup_browser_interactive()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - guaranteed cleanup."""
        await self.teardown_browser()

    def log(self, message: str, level: str = "info") -> None:
        """Print log message."""
        prefix = {"info": "", "warn": "WARNING: ", "error": "ERROR: ", "debug": "  "}
        if level == "debug" and not self.verbose:
            return
        print(f"{prefix.get(level, '')}{message}")

    async def setup_browser_interactive(self) -> None:
        """
        Initialize browser with persistent context for Google SSO.

        Uses a persistent browser profile that:
        - Persists login state between runs
        - Removes automation detection flags for Google SSO
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright not available. This should not happen if running via __main__. "
                "Try: python -m src.deploy.deploy_to_pipedream"
            )

        self.log("Starting browser...", "debug")

        # Ensure profile directory exists
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        self.playwright = await async_playwright().start()

        # Use persistent context for Google SSO compatibility
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,  # Always headed for interactive login
            viewport={
                "width": self.config.settings.viewport_width,
                "height": self.config.settings.viewport_height,
            },
            # Remove automation detection for Google SSO
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
            # Grant clipboard permissions for code paste
            permissions=["clipboard-read", "clipboard-write"],
        )

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Grant clipboard permissions to the context
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.log("Browser ready", "debug")

    async def teardown_browser(self) -> None:
        """Close browser and clean up."""
        if self.context:
            # Save cookies before closing
            try:
                cookies = await self.context.cookies()
                pipedream_cookies = [
                    c for c in cookies
                    if "pipedream.com" in c.get("domain", "")
                ]
                if pipedream_cookies:
                    save_cookies_to_env_local(pipedream_cookies)
                    self.log("Cookies cached for future use", "debug")
            except Exception as e:
                self.log(f"Failed to cache cookies: {e}", "debug")

            await self.context.close()

        if self.playwright:
            await self.playwright.stop()

        self.log("Browser closed", "debug")

    async def take_screenshot(self, name: str) -> Optional[str]:
        """Take a screenshot for debugging."""
        if not self.page:
            return None

        screenshot_dir = ensure_screenshot_dir(self.config.settings.screenshot_path)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}-{name}.png"
        path = screenshot_dir / filename

        try:
            await self.page.screenshot(path=str(path), full_page=True)
            self.log(f"Screenshot saved: {path}", "debug")
            return str(path)
        except Exception as e:
            self.log(f"Failed to take screenshot: {e}", "warn")
            return None

    async def wait_for_login(self) -> bool:
        """
        Wait for user to complete login via Google SSO.

        Returns True when logged in, False on timeout.
        """
        if not self.page:
            return False

        # IMPORTANT: Inject cached cookies BEFORE first navigation to avoid SSO redirect loop
        try:
            cached_cookies = get_cached_cookies()
            if cached_cookies:
                self.log("Injecting cached Pipedream cookies into context...", "debug")
                await self.context.add_cookies(cached_cookies)
                self.log("Cookies injected successfully", "debug")
            else:
                self.log("No cached cookies found, will require interactive login", "debug")
        except Exception as e:
            self.log(f"Failed to inject cookies: {e}", "warn")

        # First, check if already logged in by navigating to the base URL
        self.log("Checking login status...")
        await self.page.goto(self.config.pipedream_base_url, wait_until="networkidle")

        # Check if already logged in - by selector OR by URL redirect
        # When logged in, Pipedream often redirects to /workflows or /projects
        current_url = self.page.url
        if "/workflows" in current_url or "/projects" in current_url:
            self.log("Already logged in (redirected to dashboard)!")
            return True

        try:
            await self.page.wait_for_selector(LOGGED_IN_INDICATOR, timeout=3000)
            self.log("Already logged in!")
            return True
        except PlaywrightTimeout:
            pass

        # Not logged in - navigate to LOGIN PAGE (not landing page!)
        # This ensures the login form is visible on fresh machines
        login_url = f"{self.config.pipedream_base_url}/login"
        self.log(f"Not logged in. Navigating to {login_url}...")
        await self.page.goto(login_url, wait_until="networkidle")

        # Prompt user
        print("\n" + "=" * 50)
        print("PIPEDREAM LOGIN REQUIRED")
        print("=" * 50)
        print("1. Click 'Sign in with Google' in the browser")
        print("2. Complete the Google SSO login")
        print("3. Wait for the Pipedream dashboard to load")
        print("=" * 50)
        print("\nWaiting for login (5 minute timeout)...")

        # Wait for login to complete (check every 2 seconds)
        max_wait = 300  # 5 minutes
        waited = 0

        while waited < max_wait:
            try:
                # Check for logged-in indicator
                await self.page.wait_for_selector(LOGGED_IN_INDICATOR, timeout=2000)
                print("\nLogin successful!")
                return True
            except PlaywrightTimeout:
                pass

            # Also check URL - if we're on dashboard/workflows, we're logged in
            current_url = self.page.url
            if "/workflows" in current_url or "/projects" in current_url:
                print("\nLogin successful!")
                return True

            waited += 2
            if waited % 30 == 0:
                print(f"  Still waiting... ({waited}s elapsed)")

        print("\nLogin timeout! Please try again.")
        return False

    async def navigate_to_workflow(self, workflow_id: str) -> None:
        """Navigate to a workflow's edit page."""
        if not self.page:
            raise NavigationError("Browser not initialized")

        url = workflow_edit_url(
            self.config.pipedream_base_url,
            workflow_id,
            self.config.pipedream_username,
            self.config.pipedream_project_id,
        )
        self.log(f"Navigating to {url}", "debug")

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)  # Give time for JS to render
        except PlaywrightTimeout:
            if self.config.settings.screenshot_on_failure:
                await self.take_screenshot(f"timeout-{workflow_id}")
            raise NavigationError(f"Timeout loading workflow {workflow_id}")

        # Wait for build page to load - look for Deploy button
        try:
            await self.page.wait_for_selector("text=Deploy", timeout=15000)
            self.log("Build page loaded", "debug")
        except PlaywrightTimeout:
            if self.config.settings.screenshot_on_failure:
                await self.take_screenshot(f"nav-failed-{workflow_id}")
            raise NavigationError(f"Build page not loaded for {workflow_id}")

        if self.screenshot_always:
            await self.take_screenshot(f"workflow-{workflow_id}")

    async def find_and_click_step(self, step_name: str) -> None:
        """Find a step by name and click to open its editor."""
        if not self.page:
            raise StepNotFoundError(step_name, "unknown")

        self.log(f"Looking for step: {step_name}", "debug")

        # In Pipedream, steps are displayed as cards in the workflow canvas.
        # We need to click the CARD (parent container), not just the text.
        # The step cards typically have the step name as text inside them.

        clicked = False

        # Strategy 1: Find text, then click its ancestor step card
        try:
            # Find the text element containing the step name
            text_locator = self.page.locator(f"text='{step_name}'")
            count = await text_locator.count()
            self.log(f"  Found {count} text element(s) for '{step_name}'", "debug")

            if count > 0:
                # Get the bounding box of the text and click slightly above/on the card
                # Or use locator to find parent and click it
                text_el = text_locator.first

                # Try clicking the text's parent elements (step card)
                # In Pipedream, the clickable area is usually a few levels up
                for parent_selector in [
                    f"text='{step_name}' >> xpath=ancestor::div[contains(@class, 'step') or contains(@class, 'node') or @role='button'][1]",
                    f"div:has(> div:has-text('{step_name}'))",
                    f"div:has-text('{step_name}'):not(:has(div:has-text('{step_name}')))",
                ]:
                    try:
                        parent = self.page.locator(parent_selector).first
                        if await parent.count() > 0:
                            # Double-click to ensure panel switches to this step
                            await parent.dblclick(timeout=3000)
                            self.log(f"Double-clicked parent card for {step_name}", "debug")
                            clicked = True
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue

                # Fallback: double-click the text element to force panel switch
                if not clicked:
                    await text_el.dblclick(timeout=3000)
                    self.log(f"Double-clicked text element for {step_name}", "debug")
                    clicked = True
                    await asyncio.sleep(1)

        except Exception as e:
            self.log(f"  Strategy 1 failed: {e}", "debug")

        # Strategy 2: Try CSS selectors for step containers
        if not clicked:
            selectors_to_try = [
                f"[data-step-name='{step_name}']",
                f"[data-testid='step']:has-text('{step_name}')",
                f".step-container:has-text('{step_name}')",
                f".workflow-step:has-text('{step_name}')",
            ]
            for selector in selectors_to_try:
                try:
                    locator = self.page.locator(selector)
                    count = await locator.count()
                    if count > 0:
                        await locator.first.click(timeout=3000)
                        self.log(f"Clicked with selector: {selector[:50]}", "debug")
                        clicked = True
                        await asyncio.sleep(1)
                        break
                except Exception as e:
                    self.log(f"  Selector '{selector[:30]}' failed: {e}", "debug")
                    continue

        if not clicked:
            self.log(f"Could not find step {step_name} with any selector", "error")
            raise StepNotFoundError(step_name, "workflow")

        # Wait for step panel to open and switch to this step
        await asyncio.sleep(1)  # Give panel time to switch

        # Pipedream uses a TABBED interface - when multiple steps are open, they appear as tabs
        # We need to click on the correct TAB in the panel header to see that step's code
        try:
            # Find and click the tab for this step in the panel header
            # The tab contains the step name and is in the header area (right side panel)
            tab_selectors = [
                f"button:has-text('{step_name}')",
                f"[role='tab']:has-text('{step_name}')",
                f"div[class*='tab']:has-text('{step_name}')",
                # Partial match for truncated names
                f"button:has-text('{step_name[:15]}')",
                f"[role='tab']:has-text('{step_name[:15]}')",
            ]

            tab_clicked = False
            for tab_selector in tab_selectors:
                try:
                    tab = self.page.locator(tab_selector).first
                    if await tab.count() > 0 and await tab.is_visible():
                        await tab.click(timeout=2000)
                        self.log(f"Clicked tab for {step_name}", "debug")
                        tab_clicked = True
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue

            if not tab_clicked:
                self.log(f"Could not find tab for {step_name}, panel should already show it", "debug")

        except Exception as e:
            self.log(f"Tab click failed: {e}", "debug")

        # Verify the panel shows the correct step by checking the header
        try:
            await self.page.wait_for_selector(f"text={step_name}", timeout=5000)
            self.log(f"Panel header shows {step_name}", "debug")
        except PlaywrightTimeout:
            self.log(f"Panel may not have switched to {step_name}", "warn")

        # Wait for CODE section to appear
        try:
            await self.page.wait_for_selector("text=CODE", timeout=5000)
            self.log(f"CODE section visible for {step_name}", "debug")
        except PlaywrightTimeout:
            self.log(f"CODE section not visible after clicking {step_name}", "warn")

    async def click_code_tab(self) -> None:
        """Ensure the code editor is visible by expanding the CODE section."""
        if not self.page:
            return

        self.log("Expanding CODE section...", "debug")

        # Check if editor is already visible (has size and not hidden)
        try:
            visible_editor = await self.page.evaluate("""
                () => {
                    const editors = document.querySelectorAll('.monaco-editor, .cm-editor');
                    for (const editor of editors) {
                        const rect = editor.getBoundingClientRect();
                        const style = window.getComputedStyle(editor);
                        // Check if editor has size and is not hidden
                        // Note: Don't require top >= 0 because editors in scrollable
                        // panels can have negative top while still being visible
                        const isVisible = (
                            rect.width > 100 &&
                            rect.height > 100 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                        if (isVisible) return true;
                    }
                    return false;
                }
            """)
            if visible_editor:
                self.log("Editor already visible, skipping CODE click", "debug")
                return
        except Exception:
            pass

        # Try multiple selectors to expand the CODE section
        # The CODE section in Pipedream is a collapsible panel
        code_selectors = [
            # Direct text match
            "text=CODE",
            # Section headers often have these patterns
            "div:has(> span:text('CODE'))",
            "h3:has-text('CODE')",
            "h4:has-text('CODE')",
            # Button/clickable area patterns
            "button:has-text('CODE')",
            "[role='button']:has-text('CODE')",
            # Aria labels
            "[aria-label*='code' i]",
            "[aria-label*='Code' i]",
            # Data attributes
            "[data-section='code']",
            "[data-tab='code']",
        ]

        for selector in code_selectors:
            try:
                locator = self.page.locator(selector).first
                count = await locator.count()
                if count > 0:
                    # First scroll the element into view
                    await locator.scroll_into_view_if_needed(timeout=2000)
                    await asyncio.sleep(0.3)
                    # Use force=True to click even if element is partially obscured
                    await locator.click(timeout=3000, force=True)
                    self.log(f"Clicked CODE with selector: {selector}", "debug")
                    await asyncio.sleep(1.5)

                    # Check if editor appeared
                    editor_visible = await self.page.evaluate("""
                        () => {
                            const editors = document.querySelectorAll('.monaco-editor, .cm-editor');
                            for (const editor of editors) {
                                const rect = editor.getBoundingClientRect();
                                const style = window.getComputedStyle(editor);
                                if (rect.width > 100 && rect.height > 100 &&
                                    style.display !== 'none' && style.visibility !== 'hidden') return true;
                            }
                            return false;
                        }
                    """)
                    if editor_visible:
                        self.log("Editor appeared after CODE click", "debug")
                        return
                    else:
                        self.log("Editor not visible after click, trying next selector", "debug")
            except Exception as e:
                self.log(f"Selector {selector} failed: {e}", "debug")
                continue

        # If still no editor, try double-clicking on CODE text
        try:
            self.log("Trying double-click on CODE", "debug")
            code_text = self.page.locator("text=CODE").first
            await code_text.dblclick(timeout=3000)
            await asyncio.sleep(1.5)
        except Exception:
            pass

        # Final check
        try:
            await self.page.wait_for_selector(
                ".monaco-editor, .cm-editor",
                state="visible",
                timeout=3000
            )
            self.log("Editor visible after CODE expansion attempts", "debug")
        except PlaywrightTimeout:
            self.log("CODE section expansion failed - editor not visible", "warn")
            await self.take_screenshot("code-expansion-failed")

    async def close_step_panel(self) -> None:
        """Close any open step panel by clicking the X button or pressing Escape."""
        if not self.page:
            return

        self.log("Closing any open step panel...", "debug")

        # Method 1: Click the X/close button in the panel header
        # In Pipedream, there's typically an X button in the top-right of the panel
        close_selectors = [
            # X button patterns
            "button[aria-label='Close']",
            "button[aria-label='close']",
            "[data-testid='close-step']",
            "[data-testid='close-panel']",
            ".close-button",
            # Look for X in the panel header area (right side panel)
            "div.absolute button:has(svg)",  # Icon buttons
            "button:has(svg[class*='close'])",
            "button:has(svg[class*='x-'])",
        ]

        panel_closed = False
        for selector in close_selectors:
            try:
                close_btn = self.page.locator(selector).first
                if await close_btn.count() > 0:
                    # Check if it's visible
                    is_visible = await close_btn.is_visible()
                    if is_visible:
                        await close_btn.click(timeout=2000)
                        self.log(f"Clicked close button: {selector}", "debug")
                        await asyncio.sleep(0.5)
                        panel_closed = True
                        break
            except Exception:
                continue

        # Method 2: Press Escape key multiple times
        if not panel_closed:
            self.log("Trying Escape key to close panel", "debug")
            for _ in range(3):
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)

        # Method 3: Click on empty canvas area (left side)
        try:
            # Click on the workflow canvas area, away from steps
            await self.page.click("body", position={"x": 200, "y": 600}, timeout=1000)
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Wait a moment for panel to close
        await asyncio.sleep(0.5)

        # Verify panel closed by checking if CODE section is gone
        try:
            code_visible = await self.page.locator("text=CODE").first.is_visible()
            if code_visible:
                self.log("Panel may still be open (CODE visible)", "debug")
                # Try one more escape
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        except Exception:
            pass

    async def update_code(self, new_code: str) -> None:
        """Replace all code in the editor using click, select-all, and paste."""
        if not self.page:
            raise CodeUpdateError("Browser not initialized")

        self.log("Starting code update...", "info")

        # Debug: Check what editor elements exist on the page
        try:
            editor_info = await self.page.evaluate("""
                () => {
                    const selectors = [
                        '.monaco-editor',
                        '.view-lines',
                        '.CodeMirror',
                        '.cm-content',
                        '.cm-editor'
                    ];
                    const results = {};
                    selectors.forEach(sel => {
                        const els = document.querySelectorAll(sel);
                        results[sel] = els.length;
                    });
                    // Also get detailed rect info for each editor
                    const editors = document.querySelectorAll('.monaco-editor, .cm-editor');
                    const rects = [];
                    editors.forEach((e, i) => {
                        const rect = e.getBoundingClientRect();
                        const style = window.getComputedStyle(e);
                        rects.push({
                            idx: i,
                            w: Math.round(rect.width),
                            h: Math.round(rect.height),
                            top: Math.round(rect.top),
                            left: Math.round(rect.left),
                            display: style.display,
                            visibility: style.visibility
                        });
                    });
                    results['rects'] = rects;
                    results['viewport'] = {w: window.innerWidth, h: window.innerHeight};
                    return results;
                }
            """)
            self.log(f"  Editor elements: {editor_info}", "info")
        except Exception as e:
            self.log(f"  Debug check failed: {e}", "warn")

        # Step 1: Find THE LARGEST visible editor (CODE editor, not config panel!)
        # Pipedream shows multiple editors - config panel (small) and code editor (large)
        # We must target the LARGEST one to avoid pasting into the config panel
        visible_editor = await self.page.evaluate("""
            () => {
                const selectors = ['.monaco-editor', '.cm-editor', '.CodeMirror'];
                let bestEditor = null;
                let bestSel = null;
                let maxHeight = 0;

                for (const sel of selectors) {
                    const editors = document.querySelectorAll(sel);
                    for (const editor of editors) {
                        const rect = editor.getBoundingClientRect();
                        const style = window.getComputedStyle(editor);
                        // Check if visible: has size and not hidden
                        // Note: Don't check top >= 0 because editors in scrollable panels
                        // can have negative top values while still being visible
                        if (rect.width > 100 && rect.height > 100 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden') {
                            // Track the TALLEST editor (code editor > config panel)
                            if (rect.height > maxHeight) {
                                maxHeight = rect.height;
                                bestEditor = editor;
                                bestSel = sel;
                            }
                        }
                    }
                }

                if (bestEditor) {
                    // Mark the largest editor as our target
                    bestEditor.setAttribute('data-sync-target', 'true');
                    return bestSel;
                }
                return null;
            }
        """)

        if not visible_editor:
            self.log("  Step 1 FAILED: No visible editor found", "error")
            await self.take_screenshot("no-visible-editor")
            raise CodeUpdateError("No visible editor found in viewport")

        self.log(f"  Step 1a: Found visible editor ({visible_editor})", "info")

        # Click the marked editor
        try:
            target = self.page.locator('[data-sync-target="true"]')
            count = await target.count()
            if count != 1:
                await self.take_screenshot(f"multiple-targets-{count}")
                raise CodeUpdateError(f"Expected 1 target editor, found {count}")
            await target.click(timeout=5000)
            await asyncio.sleep(0.5)
            self.log(f"  Step 1b: Clicked visible editor", "info")
        except CodeUpdateError:
            raise
        except Exception as e:
            self.log(f"  Step 1 FAILED: Could not click target: {e}", "error")
            raise CodeUpdateError(f"Failed to click visible editor: {e}")
        finally:
            # Clean up the marker
            await self.page.evaluate("""
                () => {
                    const el = document.querySelector('[data-sync-target]');
                    if (el) el.removeAttribute('data-sync-target');
                }
            """)

        # Step 2: Select all with Cmd+A
        try:
            await self.page.keyboard.press("ControlOrMeta+KeyA")
            await asyncio.sleep(0.3)
            self.log("  Step 2: Pressed Cmd+A to select all", "info")
        except Exception as e:
            self.log(f"  Step 2 FAILED: {e}", "error")
            raise CodeUpdateError(f"Select all failed: {e}")

        # Step 3: Copy new code to clipboard and paste
        try:
            # Prepend deploy timestamp to force Pipedream to recognize changes
            # (Pipedream won't update if code is identical to existing)
            # Note: Use timezone.utc for Python 3.8 compatibility (datetime.UTC is 3.11+)
            from datetime import timezone
            deploy_header = (
                f"# Deployed by pipedream-automation\n"
                f"# Timestamp: {datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            )
            code_with_timestamp = deploy_header + new_code

            await self.page.evaluate("(code) => navigator.clipboard.writeText(code)", code_with_timestamp)
            self.log("  Step 3a: Wrote to clipboard (with deploy timestamp)", "info")
            await self.page.keyboard.press("ControlOrMeta+KeyV")
            await asyncio.sleep(0.5)
            self.log("  Step 3b: Pressed Cmd+V to paste", "info")

            # Security: Clear clipboard after paste to prevent exposure
            await self.page.evaluate("() => navigator.clipboard.writeText('')")
            self.log("  Step 3c: Cleared clipboard", "debug")

            # Step 4: Force save with Cmd+S (clipboard paste doesn't trigger autosave)
            await self.page.keyboard.press("ControlOrMeta+KeyS")
            await asyncio.sleep(0.5)
            self.log("  Step 4: Pressed Cmd+S to save", "info")
        except Exception as e:
            self.log(f"  Step 3/4 FAILED: {e}", "error")
            raise CodeUpdateError(f"Clipboard paste/save failed: {e}")

        self.log("Code update complete", "info")

    async def wait_for_save(self) -> bool:
        """Wait for save to complete after Cmd+S."""
        if not self.page:
            return False

        self.log("Waiting for save...", "debug")

        # Wait for saving indicator to appear and disappear (optional)
        try:
            await self.page.wait_for_selector(SAVING_INDICATOR, timeout=2000)
            self.log("Saving in progress...", "debug")
            # Wait for it to disappear
            await self.page.wait_for_selector(SAVING_INDICATOR, state="detached", timeout=10000)
        except PlaywrightTimeout:
            pass

        # Try to find saved indicator, but don't fail if not found
        # Pipedream may not show a visible "Saved" indicator
        try:
            await self.page.wait_for_selector(SAVED_INDICATOR, timeout=3000)
            self.log("  ✓ Save indicator found", "info")
        except PlaywrightTimeout:
            # No indicator found - wait a bit and assume saved
            # Verification will catch any issues
            await asyncio.sleep(2)
            self.log("  Waited for save (no indicator found)", "debug")

        return True

    async def verify_code_update(self, expected_code: str, step_name: str) -> bool:
        """Verify the editor contains the expected code by checking handler function."""
        if not self.page:
            return False

        try:
            # Get editor content via JavaScript - use .last to match the editor we updated
            actual_code = await self.page.evaluate("""
                () => {
                    const editors = document.querySelectorAll('.cm-editor .cm-content');
                    if (editors.length === 0) return '';
                    // Get the last editor (most recently opened)
                    const editor = editors[editors.length - 1];
                    return editor ? editor.textContent : '';
                }
            """)

            if not actual_code:
                self.log(f"  ⚠ Could not read editor content for {step_name}", "warn")
                return False

            # Check if handler function name matches (definitive check)
            # Each script has unique handler: handler_fetch_gmail_emails, handler_create_notion_task, etc.
            expected_handler = re.search(r'def (handler_\w+)', expected_code)
            actual_handler = re.search(r'def (handler_\w+)', actual_code)

            if expected_handler and actual_handler:
                if expected_handler.group(1) == actual_handler.group(1):
                    self.log(f"  ✓ Verified: {step_name} has correct handler ({expected_handler.group(1)})", "info")
                    return True
                else:
                    self.log(f"  ✗ MISMATCH: {step_name} expected {expected_handler.group(1)}, got {actual_handler.group(1)}", "error")
                    return False

            # Fallback: check first 100 chars contain expected content
            expected_start = expected_code[:100].replace(" ", "").replace("\n", "")
            actual_start = actual_code[:100].replace(" ", "").replace("\n", "")

            if expected_start[:50] in actual_start or actual_start[:50] in expected_start:
                self.log(f"  ✓ Verified: {step_name} code looks correct", "info")
                return True

            self.log(f"  ✗ Verification failed for {step_name}", "error")
            self.log(f"    Expected start: {expected_code[:60]}...", "debug")
            self.log(f"    Actual start: {actual_code[:60]}...", "debug")
            return False

        except Exception as e:
            self.log(f"  ⚠ Verification error for {step_name}: {e}", "warn")
            return False

    async def deploy_workflow(self, workflow_name: str = "") -> bool:
        """Click Deploy button and wait for deployment to complete."""
        if not self.page:
            return False

        self.log("Deploying workflow...", "info")

        try:
            # First, close any open step panel by pressing Escape
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(1.0)

            # Debug: Check what elements contain "Deploy" text
            try:
                deploy_elements = await self.page.evaluate("""
                    () => {
                        const elements = [...document.querySelectorAll('*')].filter(el =>
                            el.textContent.trim() === 'Deploy' &&
                            el.children.length === 0
                        );
                        return elements.map(el => ({
                            tag: el.tagName,
                            class: el.className,
                            id: el.id,
                            parent: el.parentElement?.tagName
                        }));
                    }
                """)
                self.log(f"  Deploy text elements: {deploy_elements}", "info")
            except Exception as e:
                self.log(f"  Deploy element scan failed: {e}", "warn")

            # Take screenshot before clicking Deploy to capture current state
            safe_name = workflow_name.replace(" ", "_").replace("/", "_")[:30] if workflow_name else "unknown"
            await self.take_screenshot(f"pre-deploy-click-{safe_name}")

            # Try using Playwright's text locator first (most reliable)
            try:
                deploy_button = self.page.get_by_text("Deploy", exact=True).first
                count = await deploy_button.count()
                if count > 0:
                    await deploy_button.click(timeout=5000)
                    self.log("Clicked Deploy using text locator", "info")

                    # Wait on edit page for deploy API request to complete
                    # This prevents the race condition where navigation aborts the request
                    self.log("  Waiting for deploy request to complete...", "info")
                    await asyncio.sleep(3)

                    # Look for visual confirmation of deploy initiation
                    try:
                        await self.page.wait_for_selector("text=Deploying", timeout=5000)
                        self.log("  Deploy initiated (saw 'Deploying' indicator)", "info")
                    except PlaywrightTimeout:
                        self.log("  No 'Deploying' indicator found, proceeding anyway", "debug")

                    # Take screenshot to capture post-click state for debugging
                    safe_name = workflow_name.replace(" ", "_").replace("/", "_")[:30] if workflow_name else "unknown"
                    await self.take_screenshot(f"post-deploy-click-{safe_name}")

                    # Wait for deployment to complete by checking workflow list page
                    return await self._wait_for_deploy_completion(workflow_name, timeout=30)
            except Exception as e:
                self.log(f"  Text locator failed: {e}", "debug")

            # Try multiple CSS selectors
            deploy_selectors = [
                "text=Deploy",
                "button:has-text('Deploy')",
                "*:has-text('Deploy'):not(:has(*))",  # Leaf element with Deploy text
                "[data-testid='deploy-button']",
                "div:has-text('Deploy')",
                "span:has-text('Deploy')",
            ]

            for selector in deploy_selectors:
                try:
                    deploy_button = await self.page.wait_for_selector(
                        selector,
                        timeout=2000
                    )
                    if deploy_button:
                        await deploy_button.click()
                        self.log(f"Clicked Deploy with: {selector}", "info")

                        # Wait on edit page for deploy API request to complete
                        # This prevents the race condition where navigation aborts the request
                        self.log("  Waiting for deploy request to complete...", "info")
                        await asyncio.sleep(3)

                        # Look for visual confirmation of deploy initiation
                        try:
                            await self.page.wait_for_selector("text=Deploying", timeout=5000)
                            self.log("  Deploy initiated (saw 'Deploying' indicator)", "info")
                        except PlaywrightTimeout:
                            self.log("  No 'Deploying' indicator found, proceeding anyway", "debug")

                        # Take screenshot to capture post-click state for debugging
                        safe_name = workflow_name.replace(" ", "_").replace("/", "_")[:30] if workflow_name else "unknown"
                        await self.take_screenshot(f"post-deploy-click-{safe_name}")

                        # Wait for deployment to complete by checking workflow list page
                        return await self._wait_for_deploy_completion(workflow_name, timeout=30)
                except PlaywrightTimeout:
                    continue

            # Take screenshot to help debug
            await self.take_screenshot("deploy-button-not-found")
            self.log("Deploy button not found with any selector", "warn")
            return False

        except Exception as e:
            self.log(f"Deployment error: {e}", "error")
            return False

    async def _wait_for_deploy_completion(self, workflow_name: str, timeout: int = 45) -> bool:
        """
        Poll workflow list page for deployment completion.

        After clicking Deploy, navigates to the workflow list page and checks
        if "DEPLOY PENDING" appears next to the specific workflow. Waits for it to clear.

        Args:
            workflow_name: Name of the workflow to check (e.g., "Gmail to Notion")
            timeout: Maximum seconds to wait (default 45s for larger workflows)

        Returns:
            True if deployment completed, False if still pending after timeout
        """
        if not self.page:
            return False

        # Build workflow list page URL
        base = self.config.pipedream_base_url.rstrip("/")
        username = self.config.pipedream_username
        project_id = self.config.pipedream_project_id

        if not username or not project_id:
            self.log("  Missing username/project_id, skipping list page check", "debug")
            await asyncio.sleep(3)  # Fallback: just wait a bit longer
            return True

        list_url = f"{base}/@{username}/projects/{project_id}"
        self.log(f"  Checking workflow list at: {list_url}", "info")
        self.log(f"  Looking for workflow: '{workflow_name}'", "info")

        start_time = time.time()
        check_interval = 3.0  # Check every 3 seconds
        first_poll = True

        while time.time() - start_time < timeout:
            try:
                # Navigate to workflow list page
                await self.page.goto(list_url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)  # Wait for JS rendering

                # Take screenshot on first poll to debug workflow list
                if first_poll:
                    safe_name = workflow_name.replace(" ", "_").replace("/", "_")[:30] if workflow_name else "unknown"
                    await self.take_screenshot(f"deploy-list-page-{safe_name}")

                    # Log all visible workflows for debugging
                    try:
                        workflows_on_page = await self.page.evaluate("""
                            () => {
                                const rows = document.querySelectorAll('a[href*="/build"], [class*="workflow"], tr, [role="row"]');
                                return [...rows].slice(0, 10).map(row => {
                                    const text = (row.innerText || row.textContent || '').trim();
                                    return text.substring(0, 100);  // First 100 chars
                                });
                            }
                        """)
                        self.log(f"  Workflows visible on page: {workflows_on_page}", "info")
                    except Exception as e:
                        self.log(f"  Could not list workflows: {e}", "debug")

                    first_poll = False

                # Check if this specific workflow has "DEPLOY PENDING" next to it
                # We look for the workflow name and check if DEPLOY PENDING is nearby
                has_pending = await self.page.evaluate("""
                    (workflowName) => {
                        // Find all workflow rows/cards on the page
                        const rows = document.querySelectorAll('a[href*="/build"], [class*="workflow"], tr, [role="row"]');
                        for (const row of rows) {
                            const text = row.innerText || row.textContent || '';
                            // Check if this row contains both the workflow name and DEPLOY PENDING
                            if (text.includes(workflowName) && text.includes('DEPLOY PENDING')) {
                                return true;
                            }
                        }
                        // Fallback: check if the workflow name appears near DEPLOY PENDING in page text
                        const pageText = document.body.innerText || '';
                        // Look for pattern: workflow name within 200 chars of DEPLOY PENDING
                        const pendingIndex = pageText.indexOf('DEPLOY PENDING');
                        if (pendingIndex === -1) {
                            return false;  // No pending deployments at all
                        }
                        const nameIndex = pageText.indexOf(workflowName);
                        if (nameIndex === -1) {
                            return false;  // Workflow not found, assume OK
                        }
                        // Check if they're close together (same row)
                        return Math.abs(pendingIndex - nameIndex) < 200;
                    }
                """, workflow_name)

                if not has_pending:
                    self.log("  Deploy completed (no pending indicator for this workflow)", "info")
                    return True

                elapsed = int(time.time() - start_time)
                self.log(f"    Deploy pending, waiting... ({elapsed}s)", "debug")

            except Exception as e:
                self.log(f"  Error checking deploy status: {e}", "debug")

            await asyncio.sleep(check_interval)

        self.log(f"  Deploy still pending after {timeout}s timeout", "warning")
        return False

    def _get_unique_marker(self, code: str) -> str:
        """Extract a unique identifying marker from the code.

        Each script has unique constants that identify it:
        - fetch_gmail_emails.py: DEFAULT_MAX_RESULTS = 50
        - create_notion_task.py: PREVIOUS_STEP_NAME = "gmail"
        - label_gmail_processed.py: LABEL_NAME_TO_ADD = "notiontaskcreated"
        """
        markers = [
            (r'DEFAULT_MAX_RESULTS\s*=\s*\d+', "DEFAULT_MAX_RESULTS"),
            (r'LABEL_NAME_TO_ADD\s*=\s*["\'][^"\']+["\']', "LABEL_NAME_TO_ADD"),
            (r'PREVIOUS_STEP_NAME\s*=\s*["\']gmail["\']', "PREVIOUS_STEP_NAME=gmail"),
            (r'PREVIOUS_STEP_NAME\s*=\s*["\']notion["\']', "PREVIOUS_STEP_NAME=notion"),
            (r'GMAIL_MODIFY_URL_BASE', "GMAIL_MODIFY_URL_BASE"),
            (r'HCTI_USER_ID', "HCTI_USER_ID"),
            (r'gcal_event_to_notion', "gcal_event_to_notion"),
            (r'notion_task_to_gcal', "notion_task_to_gcal"),
            (r'notion_update_to_gcal', "notion_update_to_gcal"),
        ]
        for pattern, name in markers:
            match = re.search(pattern, code)
            if match:
                return match.group(0)
        # Fallback: use first unique line after imports
        lines = code.split('\n')
        for line in lines[10:30]:  # Skip imports, look at config section
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                return line[:80]
        return code[:100]

    async def verify_workflow_after_deploy(
        self,
        workflow: "WorkflowConfig",
        base_path: Path,
    ) -> bool:
        """Verify all steps have correct code by reloading the workflow."""
        if not self.page:
            return False

        self.log("Reloading workflow for verification...", "debug")

        # Navigate back to build page (deploy may redirect to inspect)
        build_url = workflow_edit_url(
            self.config.pipedream_base_url,
            workflow.id,
            self.config.pipedream_username,
            self.config.pipedream_project_id,
        )
        await self.page.goto(build_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)  # Wait for JS to render

        # Wait for page to be ready - look for any step name as indicator
        try:
            await self.page.wait_for_selector(f"text={workflow.steps[0].step_name}", timeout=15000)
            self.log("Workflow page loaded for verification", "debug")
        except PlaywrightTimeout:
            self.log("Failed to load workflow page for verification", "error")
            return False

        all_verified = True

        for step in workflow.steps:
            step_name = step.step_name
            script_path = step.script_path

            try:
                # Read expected code from local file
                expected_code = read_script_content(script_path, base_path)

                # Close any open panel first
                await self.close_step_panel()

                # Open the step
                await self.find_and_click_step(step_name)
                await self.click_code_tab()
                await asyncio.sleep(1)

                # Find the CODE editor (largest height = code, not config panel)
                # Mark it and use clipboard to read full content
                editor_found = await self.page.evaluate("""
                    () => {
                        const cmEditors = document.querySelectorAll('.cm-editor');
                        let bestEditor = null;
                        let maxHeight = 0;

                        for (const editor of cmEditors) {
                            const rect = editor.getBoundingClientRect();
                            const style = window.getComputedStyle(editor);
                            if (rect.width > 100 && rect.height > 100 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden') {
                                // The CODE editor is the tallest one
                                if (rect.height > maxHeight) {
                                    maxHeight = rect.height;
                                    bestEditor = editor;
                                }
                            }
                        }

                        if (bestEditor) {
                            bestEditor.setAttribute('data-verify-target', 'true');
                            return true;
                        }
                        return false;
                    }
                """)

                if not editor_found:
                    self.log(f"      ✗ {step_name}: Could not find editor to verify", "error")
                    all_verified = False
                    continue

                # Click the marked editor
                try:
                    target = self.page.locator('[data-verify-target="true"]')
                    await target.click(timeout=5000)
                    await asyncio.sleep(0.3)
                finally:
                    await self.page.evaluate("""
                        () => {
                            const el = document.querySelector('[data-verify-target]');
                            if (el) el.removeAttribute('data-verify-target');
                        }
                    """)

                # Select all and copy to clipboard
                await self.page.keyboard.press("ControlOrMeta+KeyA")
                await asyncio.sleep(0.2)
                await self.page.keyboard.press("ControlOrMeta+KeyC")
                await asyncio.sleep(0.3)

                # Read from clipboard
                actual_code = await self.page.evaluate("navigator.clipboard.readText()")

                if not actual_code:
                    self.log(f"      ✗ {step_name}: Could not read code from visible editor", "error")
                    all_verified = False
                    continue

                # Debug: show what was read
                self.log(f"      Read {len(actual_code)} chars, starts: {actual_code[:80]!r}...", "debug")

                # Use UNIQUE CONTENT MARKERS (not handler names which are all "handler")
                expected_marker = self._get_unique_marker(expected_code)

                if expected_marker in actual_code:
                    self.log(f"      ✓ {step_name}: Verified (found: {expected_marker[:40]}...)", "info")
                else:
                    self.log(f"      ✗ {step_name}: Missing marker '{expected_marker[:40]}...'", "error")
                    self.log(f"        Actual code starts with: {actual_code[:60]}...", "debug")
                    all_verified = False

            except Exception as e:
                self.log(f"      ✗ {step_name}: Verification error - {e}", "error")
                all_verified = False

        return all_verified

    async def sync_step(
        self,
        workflow_id: str,
        step: StepConfig,
        base_path: Path,
    ) -> StepResult:
        """Sync a single step's code."""
        start_time = time.time()
        step_name = step.step_name
        script_path = step.script_path

        self.log(f"    Syncing: {step_name}")

        if self.dry_run:
            return StepResult(
                step_name=step_name,
                script_path=script_path,
                status="skipped",
                message="Dry run",
                duration_seconds=time.time() - start_time,
            )

        try:
            new_code = read_script_content(script_path, base_path)

            # Close any previously open step panel FIRST
            await self.close_step_panel()

            await self.find_and_click_step(step_name)
            await self.click_code_tab()

            if self.screenshot_always:
                await self.take_screenshot(f"before-{step_name}")

            await self.update_code(new_code)
            await self.wait_for_save()

            if self.screenshot_always:
                await self.take_screenshot(f"after-{step_name}")

            return StepResult(
                step_name=step_name,
                script_path=script_path,
                status="success",
                message="Updated",
                duration_seconds=time.time() - start_time,
            )

        except (StepNotFoundError, CodeUpdateError, SaveError) as e:
            if self.config.settings.screenshot_on_failure:
                await self.take_screenshot(f"failed-{step_name}")
            return StepResult(
                step_name=step_name,
                script_path=script_path,
                status="failed",
                message=str(e),
                duration_seconds=time.time() - start_time,
            )

        except Exception as e:
            if self.config.settings.screenshot_on_failure:
                await self.take_screenshot(f"error-{step_name}")
            return StepResult(
                step_name=step_name,
                script_path=script_path,
                status="failed",
                message=f"Error: {e}",
                duration_seconds=time.time() - start_time,
            )

    async def _dry_run_workflow(self, workflow_key: str, base_path: Path) -> WorkflowResult:
        """
        Return a skipped WorkflowResult for dry-run mode — no browser interaction.

        Enumerates every step from config and marks them all as skipped with
        message "Dry run".  Called by sync_workflow and sync_all in dry-run mode
        to make the dry-run path explicit and separately testable.

        Args:
            workflow_key: Key identifying the workflow in config.
            base_path: Base path used to resolve script paths (not accessed here).

        Returns:
            WorkflowResult with status "skipped" and one StepResult per step.
        """
        workflow = self.config.get_workflow(workflow_key)
        result = WorkflowResult(
            workflow_key=workflow_key,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            status="skipped",
        )
        for step in workflow.steps:
            self.log(f"    [dry-run] {step.step_name} <- {step.script_path}")
            result.steps.append(StepResult(
                step_name=step.step_name,
                script_path=step.script_path,
                status="skipped",
                message="Dry run",
            ))
        return result

    async def sync_workflow(self, workflow_key: str, base_path: Path) -> WorkflowResult:
        """Sync all steps in a workflow."""
        workflow = self.config.get_workflow(workflow_key)
        self.log(f"  [{workflow_key}] {workflow.name}")

        if self.dry_run:
            return await self._dry_run_workflow(workflow_key, base_path)

        result = WorkflowResult(
            workflow_key=workflow_key,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            status="success",
        )

        try:
            await self.navigate_to_workflow(workflow.id)

            for step in workflow.steps:
                step_result = await self.sync_step(workflow.id, step, base_path)
                result.steps.append(step_result)

                # Print status
                icon = {"success": "+", "failed": "X", "skipped": "-"}.get(step_result.status, "?")
                print(f"      {icon} {step_result.step_name}")

                if step_result.status == "failed":
                    result.status = "partial"

            failed_count = sum(1 for s in result.steps if s.status == "failed")
            if failed_count == len(result.steps):
                result.status = "failed"
            elif failed_count > 0:
                result.status = "partial"

            # Deploy the workflow after all steps are synced
            if result.status in ["success", "partial"]:
                deployed = await self.deploy_workflow(workflow.name)
                if deployed:
                    print(f"    Deployed {workflow_key}")

                    # Verify by reloading and checking each step
                    print(f"    Verifying deployment...")
                    verified = await self.verify_workflow_after_deploy(workflow, base_path)
                    if verified:
                        print(f"    ✓ All steps verified")
                    else:
                        print(f"    ✗ Verification failed - some steps may not have saved")
                        result.status = "partial"
                else:
                    print(f"    Warning: {workflow_key} may not be deployed")

        except (NavigationError, AuthenticationError) as e:
            result.status = "failed"
            result.error = str(e)
            self.log(f"    Failed: {e}", "error")

        return result

    async def sync_all(self, base_path: Path, workflow_keys: Optional[list[str]] = None) -> list[WorkflowResult]:
        """Sync all (or specified) workflows with interactive login."""
        keys_to_sync = workflow_keys or list(self.config.workflows.keys())

        print(f"\nSyncing {len(keys_to_sync)} workflow(s)...")
        if self.dry_run:
            print("[DRY RUN - No changes will be made]\n")
            # In dry-run mode, skip browser setup entirely — just validate and enumerate.
            # Previously this caused a hang because setup_browser_interactive() launches
            # a headed browser and wait_for_login() blocks for up to 5 minutes.
            # Call _dry_run_workflow directly so no browser paths are reachable.
            for key in keys_to_sync:
                result = await self._dry_run_workflow(key, base_path)
                self.results.append(result)
            return self.results

        try:
            await self.setup_browser_interactive()

            # Wait for login
            if not await self.wait_for_login():
                raise AuthenticationError("Login failed or timed out")

            print("\nStarting sync...\n")

            for key in keys_to_sync:
                result = await self.sync_workflow(key, base_path)
                self.results.append(result)

        finally:
            await self.teardown_browser()

        return self.results


async def main_async(args: argparse.Namespace) -> int:
    """Async main function."""
    # Load .env.local
    load_and_set_env_local()

    # Load configuration
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        return 1

    # Validate configuration
    base_path = Path(args.base_path) if args.base_path else Path.cwd()
    try:
        validate_config(config, str(base_path))
    except Exception as e:
        print(f"ERROR: Invalid config: {e}")
        return 1

    # Determine workflows to sync
    workflow_keys = None
    if args.workflow:
        workflow_keys = [args.workflow]

    # Run sync
    syncer = PipedreamSyncer(
        config=config,
        dry_run=args.dry_run,
        verbose=args.verbose,
        screenshot_always=args.screenshot_always,
    )

    try:
        results = await syncer.sync_all(base_path, workflow_keys)
    except PipedreamSyncError as e:
        print(f"\nERROR: Sync failed: {e}")
        return 1

    # Generate report
    report_data = [
        {
            "workflow": r.workflow_key,
            "id": r.workflow_id,
            "status": r.status,
            "error": r.error,
            "steps": [
                {
                    "name": s.step_name,
                    "status": s.status,
                    "message": s.message,
                    "duration": s.duration_seconds,
                }
                for s in r.steps
            ],
        }
        for r in results
    ]

    report = generate_report(report_data, ".tmp/deploy-report.json")

    # Print summary
    print("\n" + "=" * 50)
    print("SYNC COMPLETE")
    print("=" * 50)
    print(f"Workflows: {report['total_workflows']}")
    print(f"Successful: {report['successful']}")
    print(f"Failed: {report['failed']}")
    print(f"Skipped: {report['skipped']}")

    # Check if Pipedream API now supports code updates (skip in dry-run to avoid
    # unnecessary network requests)
    if not args.dry_run:
        from .utils import check_pipedream_api_support
        print("\n" + "-" * 50)
        print("API STATUS CHECK")
        print("-" * 50)
        api_check = check_pipedream_api_support()
        if api_check["supports_code_update"]:
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print("NOTICE: " + api_check["message"])
            print("Consider switching to API-based sync!")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            print(api_check["message"])

    if report['failed'] > 0:
        return 1
    return 0


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Deploy Python scripts to Pipedream workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Deploy all workflows (interactive login):
    python -m src.deploy.deploy_to_pipedream

  Deploy single workflow:
    python -m src.deploy.deploy_to_pipedream --workflow gmail_to_notion

  Dry run (validate only):
    python -m src.deploy.deploy_to_pipedream --dry-run
        """,
    )

    parser.add_argument(
        "--config",
        default="config/pipedream-mapping.yaml",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--workflow",
        help="Sync only this workflow (by key)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and show what would be synced",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed logging output",
    )
    parser.add_argument(
        "--screenshot-always",
        action="store_true",
        help="Take screenshots at every step",
    )
    parser.add_argument(
        "--base-path",
        help="Base path for resolving script paths (default: cwd)",
    )

    args = parser.parse_args()

    try:
        exit_code = asyncio.run(main_async(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


def ensure_environment() -> bool:
    """
    Ensure venv exists and dependencies are installed.

    Returns True if environment is ready. If we need to re-launch with venv python,
    this function calls sys.exit() directly and never returns.
    """
    import subprocess

    venv_dir = Path("venv")
    # Platform-independent venv python path
    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    # Check if we're already running from venv (check path without resolving symlinks)
    current_python = Path(sys.executable)
    if venv_dir.exists() and str(venv_dir.resolve()) in str(current_python):
        # Already in venv, just ensure playwright browser is installed
        if not PLAYWRIGHT_AVAILABLE:
            print("Installing Playwright browser...")
            try:
                subprocess.run([str(venv_python), "-m", "playwright", "install", "chromium"], check=True)
            except subprocess.CalledProcessError as e:
                print(f"ERROR: Failed to install Playwright browser: {e}")
                print("Try running: python -m playwright install chromium")
                sys.exit(1)
        return True

    try:
        # Create venv if missing
        if not venv_dir.exists():
            print("Creating virtual environment...")
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

        # Install dependencies from requirements.txt
        print("Installing dependencies from requirements.txt...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
            check=True
        )

        # Install Playwright browser
        print("Installing Playwright browser...")
        subprocess.run([str(venv_python), "-m", "playwright", "install", "chromium"], check=True)

    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Environment setup failed: {e}")
        print("\nPossible solutions:")
        print("  1. Ensure Python 3.9+ is installed")
        print("  2. Check that requirements.txt exists")
        print("  3. Try creating venv manually: python -m venv venv")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\nERROR: Command not found: {e}")
        print("Ensure Python is properly installed and in PATH")
        sys.exit(1)

    # Re-execute with venv python using subprocess
    # Use -m to run as module, passing only the CLI args (not sys.argv[0] which is the script path)
    print("Re-launching with virtual environment...\n")
    cmd = [str(venv_python), "-m", "src.deploy.deploy_to_pipedream"] + sys.argv[1:]
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    ensure_environment()
    main()
