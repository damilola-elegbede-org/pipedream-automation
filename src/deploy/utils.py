"""Utility functions for Pipedream deployment."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Optional

from .exceptions import AuthenticationError


# Default path for .env.local file
ENV_LOCAL_PATH = Path(".env.local")


def load_env_local(env_path: Optional[Path] = None) -> dict[str, str]:
    """
    Load environment variables from .env.local file.

    Args:
        env_path: Path to env file (defaults to .env.local)

    Returns:
        Dictionary of environment variables
    """
    path = env_path or ENV_LOCAL_PATH
    env_vars = {}

    if not path.exists():
        return env_vars

    with open(path) as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Parse KEY=VALUE
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if value and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                env_vars[key] = value

    return env_vars


def _validate_credential_file_security(path: Path) -> None:
    """
    Validate that a credential file is safe to load.

    Checks:
    1. File ownership matches the current process UID.
    2. File permissions are 0o600 or stricter (no group/world read).
    3. Parent directory is not world-writable.

    Raises:
        PermissionError: If any security check fails. This is a hard error —
                         no fallback, no silent skip.
    """
    file_stat = os.stat(path)
    parent_stat = os.stat(path.parent)

    # 1. File must be owned by the current process user
    if file_stat.st_uid != os.getuid():
        raise PermissionError(
            f"Credential file {path} is owned by UID {file_stat.st_uid}, "
            f"but the current process UID is {os.getuid()}. "
            "Refusing to load — possible credential injection attack."
        )

    # 2. File must not be group-readable or world-readable (require 0o600 or stricter)
    if file_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        octal_mode = oct(stat.S_IMODE(file_stat.st_mode))
        raise PermissionError(
            f"Credential file {path} has unsafe permissions {octal_mode}. "
            "Expected 0o600 or stricter (no group/world read). "
            f"Fix with: chmod 600 {path}"
        )

    # 3. Parent directory must not be world-writable
    if parent_stat.st_mode & stat.S_IWOTH:
        octal_mode = oct(stat.S_IMODE(parent_stat.st_mode))
        raise PermissionError(
            f"Parent directory {path.parent} is world-writable (permissions {octal_mode}). "
            "Refusing to load credential file — directory is unsafe. "
            f"Fix with: chmod o-w {path.parent}"
        )


def load_and_set_env_local(env_path: Optional[Path] = None) -> dict[str, str]:
    """
    Load .env.local and set as environment variables.

    Priority order (later sources override earlier ones, env already set always wins):
    1. ~/.openclaw-dara/credentials/pipedream/.env.local (canonical — loaded first as base)
    2. ./env.local (repo root — loaded second, overrides canonical for same keys)
    3. env_path parameter (if provided — loaded exclusively, skips auto-discovery)

    This layered approach ensures workflow IDs from the canonical credentials file are
    always present even when the repo .env.local only contains PIPEDREAM_COOKIES.

    Args:
        env_path: Path to env file. If provided, only this file is loaded (no fallback).
                  If omitted, canonical + repo .env.local are merged.

    Returns:
        Dictionary of all loaded environment variables (merged)
    """
    merged_vars: dict[str, str] = {}

    if env_path:
        # Explicit path: load only that file, no auto-discovery
        merged_vars = load_env_local(env_path)
    else:
        # Auto-discovery: load canonical first (base), then repo .env.local (overlay)
        canonical_env = Path.home() / ".openclaw-dara/credentials/pipedream/.env.local"
        if canonical_env.exists():
            _validate_credential_file_security(canonical_env)
            canonical_vars = load_env_local(canonical_env)
            merged_vars.update(canonical_vars)

        # Repo .env.local overlays canonical (repo-specific values take priority)
        repo_env = Path(".env.local")
        if repo_env.exists():
            repo_vars = load_env_local(repo_env)
            merged_vars.update(repo_vars)

    for key, value in merged_vars.items():
        if key not in os.environ:  # Don't override already-set env vars
            os.environ[key] = value
    return merged_vars


def save_cookies_to_env_local(
    cookies: list[dict],
    env_path: Optional[Path] = None
) -> None:
    """
    Save cookies to .env.local file.

    Args:
        cookies: List of cookie dictionaries
        env_path: Path to env file (defaults to .env.local)
    """
    path = env_path or ENV_LOCAL_PATH
    cookies_b64 = encode_cookies_base64(cookies)

    # Read existing content
    existing_lines = []
    if path.exists():
        with open(path) as f:
            existing_lines = f.readlines()

    # Update or add PIPEDREAM_COOKIES line
    found = False
    new_lines = []
    for line in existing_lines:
        if line.strip().startswith("PIPEDREAM_COOKIES=") or \
           line.strip().startswith("# PIPEDREAM_COOKIES="):
            new_lines.append(f"PIPEDREAM_COOKIES={cookies_b64}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        # Add at the end
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"PIPEDREAM_COOKIES={cookies_b64}\n")

    with open(path, "w") as f:
        f.writelines(new_lines)


def get_cached_cookies(env_path: Optional[Path] = None) -> Optional[list[dict]]:
    """
    Get cached cookies from .env.local if they exist and are valid.

    Args:
        env_path: Path to env file (defaults to .env.local)

    Returns:
        List of cookies if valid, None otherwise
    """
    env_vars = load_env_local(env_path)
    cookies_b64 = env_vars.get("PIPEDREAM_COOKIES")

    if not cookies_b64:
        return None

    try:
        cookies_json = base64.b64decode(cookies_b64).decode("utf-8")
        cookies = json.loads(cookies_json)

        # Check if expired
        is_valid, _ = validate_cookie_expiration(cookies)
        if not is_valid:
            return None

        return cookies
    except Exception:
        return None


def load_cookies_from_env() -> list[dict]:
    """
    Load Pipedream OAuth cookies from environment variable.

    The PIPEDREAM_COOKIES environment variable should contain
    a base64-encoded JSON array of cookie objects.

    Returns:
        List of cookie dictionaries

    Raises:
        AuthenticationError: If cookies are missing or invalid
    """
    cookies_b64 = os.environ.get("PIPEDREAM_COOKIES")
    if not cookies_b64:
        raise AuthenticationError(
            "PIPEDREAM_COOKIES environment variable not set. "
            "Run 'python scripts/extract_cookies.py' to get cookies."
        )

    try:
        cookies_json = base64.b64decode(cookies_b64).decode("utf-8")
        cookies = json.loads(cookies_json)
    except (base64.binascii.Error, ValueError) as e:
        raise AuthenticationError(f"Failed to decode cookies: {e}")
    except json.JSONDecodeError as e:
        raise AuthenticationError(f"Invalid JSON in cookies: {e}")

    if not isinstance(cookies, list):
        raise AuthenticationError("Cookies must be a JSON array")

    # Validate cookie structure
    required_fields = {"name", "value", "domain"}
    for i, cookie in enumerate(cookies):
        if not isinstance(cookie, dict):
            raise AuthenticationError(f"Cookie {i} must be an object")
        missing = required_fields - set(cookie.keys())
        if missing:
            raise AuthenticationError(
                f"Cookie '{cookie.get('name', i)}' missing fields: {missing}"
            )

    return cookies


def load_cookies_from_file(file_path: str) -> list[dict]:
    """
    Load cookies from a JSON file (for local testing).

    Args:
        file_path: Path to the cookies JSON file

    Returns:
        List of cookie dictionaries
    """
    path = Path(file_path)
    if not path.exists():
        raise AuthenticationError(f"Cookie file not found: {file_path}")

    try:
        with open(path) as f:
            cookies = json.load(f)
    except json.JSONDecodeError as e:
        raise AuthenticationError(f"Invalid JSON in {file_path}: {e}")

    return cookies


def validate_cookie_expiration(cookies: list[dict]) -> tuple[bool, str]:
    """
    Check if cookies are expired or about to expire.

    Args:
        cookies: List of cookie dictionaries

    Returns:
        (is_valid, message) tuple
    """
    now = time.time()
    warning_threshold = 24 * 60 * 60  # 24 hours

    for cookie in cookies:
        expires = cookie.get("expires", -1)
        if expires == -1:
            # Session cookie, no expiration
            continue

        if expires < now:
            return False, f"Cookie '{cookie['name']}' has expired"

        if expires < now + warning_threshold:
            hours_left = (expires - now) / 3600
            return True, (
                f"Warning: Cookie '{cookie['name']}' expires in "
                f"{hours_left:.1f} hours. Consider refreshing."
            )

    return True, "All cookies are valid"


def encode_cookies_base64(cookies: list[dict]) -> str:
    """
    Encode cookies as base64 for storage in environment variable.

    Args:
        cookies: List of cookie dictionaries

    Returns:
        Base64-encoded JSON string
    """
    cookies_json = json.dumps(cookies, separators=(",", ":"))
    return base64.b64encode(cookies_json.encode()).decode()


def ensure_screenshot_dir(path: str) -> Path:
    """
    Ensure the screenshot directory exists.

    Args:
        path: Path to screenshot directory

    Returns:
        Path object
    """
    screenshot_dir = Path(path)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    return screenshot_dir


def read_script_content(script_path: str, base_path: Optional[Path] = None) -> str:
    """
    Read the content of a Python script file.

    Args:
        script_path: Relative path to the script
        base_path: Base directory (defaults to cwd)

    Returns:
        Script content as string

    Raises:
        ValueError: If path traversal is detected
        FileNotFoundError: If script doesn't exist
    """
    base = base_path or Path.cwd()
    full_path = (base / script_path).resolve()
    base_resolved = base.resolve()

    # Security: Prevent path traversal attacks using proper path containment check
    try:
        full_path.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"Path traversal detected: {script_path}") from None

    if not full_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    with open(full_path) as f:
        return f.read()


def generate_report(results: list[dict], output_path: Optional[str] = None) -> dict:
    """
    Generate a deployment report from sync results.

    Args:
        results: List of sync result dictionaries
        output_path: Optional path to write JSON report

    Returns:
        Report dictionary
    """
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_workflows": len(results),
        "successful": sum(1 for r in results if r.get("status") == "success"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "results": results,
    }

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)

    return report


def check_pipedream_api_support() -> dict:
    """
    Check if Pipedream API now supports updating workflow step code.

    This check fetches the Pipedream API docs to see if step code updates
    are now supported, which would allow us to switch from browser automation
    to direct API calls.

    Returns:
        dict with keys:
            - supports_code_update: bool
            - message: str
            - docs_url: str
    """
    import urllib.request
    import urllib.error

    API_DOCS_URL = "https://pipedream.com/docs/rest-api/api-reference/workflows/update-a-workflow"

    result = {
        "supports_code_update": False,
        "message": "",
        "docs_url": API_DOCS_URL,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Keywords that indicate it's NOT supported (current state as of Dec 2024)
    # These are checked FIRST - if found, we know code updates are not supported
    # Using shorter, simpler phrases to handle HTML formatting variations
    negative_indicators = [
        "activation status",  # "updates the workflow's activation status"
        "consider making a new workflow",  # limitation message
        "does not support updating step code",
        "cannot update step code",
    ]

    # Keywords that would indicate step code update support (explicit phrases only)
    # Only checked if no negative indicators found
    positive_indicators = [
        "update step code",
        "modify step code",
        "change step code",
        "edit step code via api",
        "update workflow step code",
    ]

    try:
        # Fetch the Pipedream docs page directly
        req = urllib.request.Request(
            API_DOCS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode("utf-8").lower()

            # Check for negative indicators FIRST (API doesn't support code updates)
            # This is the expected current state, so check it first
            for indicator in negative_indicators:
                if indicator.lower() in content:
                    result["message"] = (
                        "Pipedream API still does NOT support updating step code. "
                        "Browser automation remains required."
                    )
                    return result

            # Check for positive indicators (API now supports code updates!)
            # Only reached if no negative indicators found
            for indicator in positive_indicators:
                if indicator.lower() in content:
                    result["supports_code_update"] = True
                    result["message"] = (
                        "Pipedream API may NOW support updating step code! "
                        f"Check docs: {API_DOCS_URL}"
                    )
                    return result

            # No clear indicators found - docs may have changed
            result["message"] = (
                "Docs structure may have changed. "
                f"Manually check: {API_DOCS_URL}"
            )

    except urllib.error.URLError as e:
        result["message"] = f"Could not check API docs (network error): {e}"
    except Exception as e:
        result["message"] = f"Could not check API docs: {e}"

    return result
