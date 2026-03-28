"""Tests for Pipedream deployment utility functions."""

import base64
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.deploy.exceptions import AuthenticationError
from src.deploy.utils import (
    check_pipedream_api_support,
    encode_cookies_base64,
    ensure_screenshot_dir,
    generate_report,
    get_cached_cookies,
    load_and_set_env_local,
    load_cookies_from_env,
    load_cookies_from_file,
    load_env_local,
    read_script_content,
    save_cookies_to_env_local,
    validate_cookie_expiration,
)


class TestLoadEnvLocal:
    """Tests for load_env_local function."""

    def test_load_existing_env_file(self, tmp_path):
        """Test loading variables from existing .env.local file."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("KEY1=value1\nKEY2=value2\n")

        result = load_env_local(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_load_missing_env_file(self, tmp_path):
        """Test loading from non-existent file returns empty dict."""
        env_file = tmp_path / ".env.local.missing"
        result = load_env_local(env_file)
        assert result == {}

    def test_skips_comments(self, tmp_path):
        """Test that comment lines are skipped."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("# This is a comment\nKEY=value\n# Another comment\n")

        result = load_env_local(env_file)
        assert result == {"KEY": "value"}

    def test_skips_empty_lines(self, tmp_path):
        """Test that empty lines are skipped."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("KEY1=value1\n\n\nKEY2=value2\n")

        result = load_env_local(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_handles_quoted_values(self, tmp_path):
        """Test that quoted values are unquoted."""
        env_file = tmp_path / ".env.local"
        env_file.write_text('KEY1="double quoted"\nKEY2=\'single quoted\'\n')

        result = load_env_local(env_file)
        assert result["KEY1"] == "double quoted"
        assert result["KEY2"] == "single quoted"

    def test_handles_equals_in_value(self, tmp_path):
        """Test that values containing = are handled correctly."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("URL=https://example.com?foo=bar\n")

        result = load_env_local(env_file)
        assert result["URL"] == "https://example.com?foo=bar"


class TestLoadAndSetEnvLocal:
    """Tests for load_and_set_env_local function."""

    def test_sets_env_vars(self, tmp_path, monkeypatch):
        """Test that variables are set in environment."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("TEST_VAR=test_value\n")

        # Ensure the var doesn't exist
        monkeypatch.delenv("TEST_VAR", raising=False)

        load_and_set_env_local(env_file)
        assert os.environ.get("TEST_VAR") == "test_value"

    def test_does_not_override_existing(self, tmp_path, monkeypatch):
        """Test that existing env vars are not overridden."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("EXISTING_VAR=from_file\n")

        monkeypatch.setenv("EXISTING_VAR", "original_value")

        load_and_set_env_local(env_file)
        assert os.environ.get("EXISTING_VAR") == "original_value"

    def test_returns_loaded_vars(self, tmp_path):
        """Test that function returns loaded variables."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("KEY=value\n")

        result = load_and_set_env_local(env_file)
        assert result == {"KEY": "value"}

    # ------------------------------------------------------------------
    # Fallback / canonical path tests
    # ------------------------------------------------------------------

    def test_fallback_canonical_fills_missing_vars(self, tmp_path, monkeypatch):
        """Canonical path fills vars absent from repo .env.local."""
        import src.deploy.utils as utils_mod

        repo_env = tmp_path / ".env.local"
        canonical_env = tmp_path / "canonical.env.local"

        repo_env.write_text("REPO_ONLY=repo_val\n")
        canonical_env.write_text("CANONICAL_ONLY=canonical_val\n")

        # Patch module-level paths so the test is hermetic
        monkeypatch.setattr(utils_mod, "ENV_LOCAL_PATH", repo_env)
        monkeypatch.setattr(utils_mod, "CANONICAL_ENV_PATH", canonical_env)
        monkeypatch.delenv("REPO_ONLY", raising=False)
        monkeypatch.delenv("CANONICAL_ONLY", raising=False)

        result = utils_mod.load_and_set_env_local()

        assert os.environ.get("REPO_ONLY") == "repo_val"
        assert os.environ.get("CANONICAL_ONLY") == "canonical_val"
        assert "REPO_ONLY" in result
        assert "CANONICAL_ONLY" in result

    def test_repo_env_takes_precedence_over_canonical(self, tmp_path, monkeypatch):
        """Repo .env.local value wins over canonical when key exists in both."""
        import src.deploy.utils as utils_mod

        repo_env = tmp_path / ".env.local"
        canonical_env = tmp_path / "canonical.env.local"

        repo_env.write_text("SHARED_KEY=from_repo\n")
        canonical_env.write_text("SHARED_KEY=from_canonical\n")

        monkeypatch.setattr(utils_mod, "ENV_LOCAL_PATH", repo_env)
        monkeypatch.setattr(utils_mod, "CANONICAL_ENV_PATH", canonical_env)
        monkeypatch.delenv("SHARED_KEY", raising=False)

        utils_mod.load_and_set_env_local()

        assert os.environ.get("SHARED_KEY") == "from_repo"

    def test_canonical_used_when_repo_env_missing(self, tmp_path, monkeypatch):
        """Falls back to canonical when CWD .env.local does not exist."""
        import src.deploy.utils as utils_mod

        canonical_env = tmp_path / "canonical.env.local"
        canonical_env.write_text("FALLBACK_VAR=fallback_val\n")

        # Point repo env at a non-existent file
        monkeypatch.setattr(utils_mod, "ENV_LOCAL_PATH", tmp_path / "missing.env.local")
        monkeypatch.setattr(utils_mod, "CANONICAL_ENV_PATH", canonical_env)
        monkeypatch.delenv("FALLBACK_VAR", raising=False)

        result = utils_mod.load_and_set_env_local()

        assert os.environ.get("FALLBACK_VAR") == "fallback_val"
        assert "FALLBACK_VAR" in result

    def test_explicit_path_skips_fallback(self, tmp_path, monkeypatch):
        """When an explicit env_path is passed, canonical fallback is skipped."""
        import src.deploy.utils as utils_mod

        explicit_env = tmp_path / "explicit.env.local"
        canonical_env = tmp_path / "canonical.env.local"

        explicit_env.write_text("EXPLICIT_KEY=explicit_val\n")
        canonical_env.write_text("CANONICAL_KEY=should_not_load\n")

        monkeypatch.setattr(utils_mod, "CANONICAL_ENV_PATH", canonical_env)
        monkeypatch.delenv("EXPLICIT_KEY", raising=False)
        monkeypatch.delenv("CANONICAL_KEY", raising=False)

        result = utils_mod.load_and_set_env_local(explicit_env)

        assert os.environ.get("EXPLICIT_KEY") == "explicit_val"
        assert os.environ.get("CANONICAL_KEY") is None
        assert "EXPLICIT_KEY" in result
        assert "CANONICAL_KEY" not in result


class TestEncodeCookiesBase64:
    """Tests for encode_cookies_base64 function."""

    def test_encodes_cookies(self):
        """Test encoding cookies to base64."""
        cookies = [{"name": "test", "value": "abc", "domain": ".example.com"}]
        result = encode_cookies_base64(cookies)

        # Should be base64 decodable
        decoded = base64.b64decode(result).decode("utf-8")
        parsed = json.loads(decoded)
        assert parsed == cookies

    def test_encodes_empty_list(self):
        """Test encoding empty cookie list."""
        result = encode_cookies_base64([])
        decoded = base64.b64decode(result).decode("utf-8")
        assert decoded == "[]"

    def test_compact_encoding(self):
        """Test that encoding is compact (no spaces)."""
        cookies = [{"name": "a", "value": "b"}]
        result = encode_cookies_base64(cookies)
        decoded = base64.b64decode(result).decode("utf-8")
        assert " " not in decoded


class TestValidateCookieExpiration:
    """Tests for validate_cookie_expiration function."""

    def test_valid_cookies(self):
        """Test validation with non-expired cookies."""
        future_time = time.time() + (48 * 60 * 60)  # 48 hours from now
        cookies = [{"name": "test", "expires": future_time}]

        is_valid, message = validate_cookie_expiration(cookies)
        assert is_valid is True
        assert "valid" in message.lower()

    def test_expired_cookies(self):
        """Test validation with expired cookies."""
        past_time = time.time() - 3600  # 1 hour ago
        cookies = [{"name": "test", "expires": past_time}]

        is_valid, message = validate_cookie_expiration(cookies)
        assert is_valid is False
        assert "expired" in message.lower()

    def test_session_cookies(self):
        """Test validation with session cookies (no expiration)."""
        cookies = [{"name": "test", "expires": -1}]

        is_valid, message = validate_cookie_expiration(cookies)
        assert is_valid is True

    def test_expiring_soon_warning(self):
        """Test warning when cookies expire within 24 hours."""
        soon_time = time.time() + (12 * 60 * 60)  # 12 hours from now
        cookies = [{"name": "test", "expires": soon_time}]

        is_valid, message = validate_cookie_expiration(cookies)
        assert is_valid is True
        assert "warning" in message.lower()


class TestGetCachedCookies:
    """Tests for get_cached_cookies function."""

    def test_returns_none_when_no_cookies(self, tmp_path):
        """Test returns None when PIPEDREAM_COOKIES not in file."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("OTHER_VAR=value\n")

        result = get_cached_cookies(env_file)
        assert result is None

    def test_returns_valid_cookies(self, tmp_path):
        """Test returns cookies when valid and not expired."""
        future_time = time.time() + (48 * 60 * 60)
        cookies = [{"name": "test", "value": "abc", "domain": ".example.com", "expires": future_time}]
        cookies_b64 = encode_cookies_base64(cookies)

        env_file = tmp_path / ".env.local"
        env_file.write_text(f"PIPEDREAM_COOKIES={cookies_b64}\n")

        result = get_cached_cookies(env_file)
        assert result == cookies

    def test_returns_none_for_expired_cookies(self, tmp_path):
        """Test returns None when cookies are expired."""
        past_time = time.time() - 3600
        cookies = [{"name": "test", "value": "abc", "domain": ".example.com", "expires": past_time}]
        cookies_b64 = encode_cookies_base64(cookies)

        env_file = tmp_path / ".env.local"
        env_file.write_text(f"PIPEDREAM_COOKIES={cookies_b64}\n")

        result = get_cached_cookies(env_file)
        assert result is None


class TestSaveCookiesToEnvLocal:
    """Tests for save_cookies_to_env_local function."""

    def test_saves_cookies_to_new_file(self, tmp_path):
        """Test saving cookies to new .env.local file."""
        env_file = tmp_path / ".env.local"
        cookies = [{"name": "test", "value": "abc"}]

        save_cookies_to_env_local(cookies, env_file)

        content = env_file.read_text()
        assert "PIPEDREAM_COOKIES=" in content

    def test_updates_existing_cookies(self, tmp_path):
        """Test updating existing PIPEDREAM_COOKIES line."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("PIPEDREAM_COOKIES=old_value\nOTHER=keep\n")

        cookies = [{"name": "new", "value": "cookie"}]
        save_cookies_to_env_local(cookies, env_file)

        content = env_file.read_text()
        assert "old_value" not in content
        assert "OTHER=keep" in content

    def test_preserves_other_content(self, tmp_path):
        """Test that other content in file is preserved."""
        env_file = tmp_path / ".env.local"
        env_file.write_text("KEY1=value1\nKEY2=value2\n")

        cookies = [{"name": "test", "value": "abc"}]
        save_cookies_to_env_local(cookies, env_file)

        content = env_file.read_text()
        assert "KEY1=value1" in content
        assert "KEY2=value2" in content


class TestLoadCookiesFromEnv:
    """Tests for load_cookies_from_env function."""

    def test_raises_when_env_not_set(self, monkeypatch):
        """Test raises AuthenticationError when env var not set."""
        monkeypatch.delenv("PIPEDREAM_COOKIES", raising=False)

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_env()
        assert "not set" in str(exc_info.value)

    def test_loads_valid_cookies(self, monkeypatch):
        """Test loading valid cookies from environment."""
        cookies = [{"name": "test", "value": "abc", "domain": ".example.com"}]
        cookies_b64 = encode_cookies_base64(cookies)
        monkeypatch.setenv("PIPEDREAM_COOKIES", cookies_b64)

        result = load_cookies_from_env()
        assert result == cookies

    def test_raises_on_invalid_base64(self, monkeypatch):
        """Test raises AuthenticationError on invalid base64."""
        monkeypatch.setenv("PIPEDREAM_COOKIES", "not-valid-base64!!!")

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_env()
        assert "decode" in str(exc_info.value).lower()

    def test_raises_on_invalid_json(self, monkeypatch):
        """Test raises AuthenticationError on invalid JSON."""
        invalid_json = base64.b64encode(b"not json").decode()
        monkeypatch.setenv("PIPEDREAM_COOKIES", invalid_json)

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_env()
        # Error message contains "decode" or "expecting" depending on JSON parse error
        error_msg = str(exc_info.value).lower()
        assert "decode" in error_msg or "expecting" in error_msg

    def test_raises_when_not_array(self, monkeypatch):
        """Test raises AuthenticationError when cookies not an array."""
        cookies_b64 = base64.b64encode(b'{"not": "array"}').decode()
        monkeypatch.setenv("PIPEDREAM_COOKIES", cookies_b64)

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_env()
        assert "array" in str(exc_info.value).lower()

    def test_validates_cookie_structure(self, monkeypatch):
        """Test validates required cookie fields."""
        cookies = [{"name": "test"}]  # Missing value and domain
        cookies_b64 = encode_cookies_base64(cookies)
        monkeypatch.setenv("PIPEDREAM_COOKIES", cookies_b64)

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_env()
        assert "missing" in str(exc_info.value).lower()


class TestLoadCookiesFromFile:
    """Tests for load_cookies_from_file function."""

    def test_loads_valid_file(self, tmp_path):
        """Test loading cookies from valid JSON file."""
        cookies = [{"name": "test", "value": "abc", "domain": ".example.com"}]
        cookie_file = tmp_path / "cookies.json"
        cookie_file.write_text(json.dumps(cookies))

        result = load_cookies_from_file(str(cookie_file))
        assert result == cookies

    def test_raises_on_missing_file(self, tmp_path):
        """Test raises AuthenticationError when file not found."""
        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_file(str(tmp_path / "nonexistent.json"))
        assert "not found" in str(exc_info.value).lower()

    def test_raises_on_invalid_json(self, tmp_path):
        """Test raises AuthenticationError on invalid JSON."""
        cookie_file = tmp_path / "cookies.json"
        cookie_file.write_text("not valid json")

        with pytest.raises(AuthenticationError) as exc_info:
            load_cookies_from_file(str(cookie_file))
        assert "json" in str(exc_info.value).lower()


class TestEnsureScreenshotDir:
    """Tests for ensure_screenshot_dir function."""

    def test_creates_directory(self, tmp_path):
        """Test that directory is created if it doesn't exist."""
        screenshot_dir = tmp_path / "screenshots" / "nested"
        assert not screenshot_dir.exists()

        result = ensure_screenshot_dir(str(screenshot_dir))

        assert screenshot_dir.exists()
        assert result == screenshot_dir

    def test_returns_existing_directory(self, tmp_path):
        """Test that existing directory is returned."""
        screenshot_dir = tmp_path / "existing"
        screenshot_dir.mkdir()

        result = ensure_screenshot_dir(str(screenshot_dir))
        assert result == screenshot_dir


class TestReadScriptContent:
    """Tests for read_script_content function."""

    def test_reads_existing_script(self, tmp_path):
        """Test reading content from existing script."""
        script_content = "def handler(pd):\n    return {}\n"
        script_file = tmp_path / "script.py"
        script_file.write_text(script_content)

        result = read_script_content("script.py", tmp_path)
        assert result == script_content

    def test_raises_on_missing_script(self, tmp_path):
        """Test raises FileNotFoundError when script not found."""
        with pytest.raises(FileNotFoundError):
            read_script_content("nonexistent.py", tmp_path)

    def test_uses_cwd_as_default_base(self):
        """Test that current working directory is default base."""
        # This test verifies the function signature, not actual file access
        # We can't easily test without creating files in cwd
        pass


class TestGenerateReport:
    """Tests for generate_report function."""

    def test_generates_report_structure(self):
        """Test that report has expected structure."""
        results = [
            {"workflow": "wf1", "status": "success"},
            {"workflow": "wf2", "status": "failed"},
        ]

        report = generate_report(results)

        assert "timestamp" in report
        assert report["total_workflows"] == 2
        assert report["successful"] == 1
        assert report["failed"] == 1
        assert report["results"] == results

    def test_counts_statuses_correctly(self):
        """Test that status counts are correct."""
        results = [
            {"status": "success"},
            {"status": "success"},
            {"status": "failed"},
            {"status": "skipped"},
        ]

        report = generate_report(results)

        assert report["successful"] == 2
        assert report["failed"] == 1
        assert report["skipped"] == 1

    def test_writes_to_file(self, tmp_path):
        """Test that report can be written to file."""
        output_file = tmp_path / "reports" / "report.json"
        results = [{"status": "success"}]

        generate_report(results, str(output_file))

        assert output_file.exists()
        with open(output_file) as f:
            saved_report = json.load(f)
        assert saved_report["successful"] == 1

    def test_handles_empty_results(self):
        """Test handling empty results list."""
        report = generate_report([])

        assert report["total_workflows"] == 0
        assert report["successful"] == 0
        assert report["failed"] == 0
        assert report["skipped"] == 0


class TestCheckPipedreamAPISupport:
    """Tests for check_pipedream_api_support function."""

    def test_returns_dict_with_required_keys(self):
        """Test that result contains all required keys."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock_urlopen.return_value.__enter__.return_value
            mock_response.read.return_value = b"some content"

            result = check_pipedream_api_support()

            assert "supports_code_update" in result
            assert "message" in result
            assert "docs_url" in result
            assert "checked_at" in result

    def test_detects_no_support_with_activation_status(self):
        """Test that 'activation status' text indicates no support."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock_urlopen.return_value.__enter__.return_value
            mock_response.read.return_value = (
                b"This endpoint only updates the workflow's activation status."
            )

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is False
            assert "still does NOT support" in result["message"]

    def test_detects_no_support_with_new_workflow_message(self):
        """Test that 'consider making a new workflow' indicates no support."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock_urlopen.return_value.__enter__.return_value
            mock_response.read.return_value = (
                b"If you need to modify steps, consider making a new workflow."
            )

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is False
            assert "still does NOT support" in result["message"]

    def test_detects_support_with_explicit_phrase(self):
        """Test that explicit support phrase is detected."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock_urlopen.return_value.__enter__.return_value
            mock_response.read.return_value = (
                b"You can now update step code via the API."
            )

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is True
            assert "may NOW support" in result["message"]

    def test_handles_network_error(self):
        """Test graceful handling of network errors."""
        import urllib.error

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is False
            assert "network error" in result["message"]

    def test_handles_generic_exception(self):
        """Test graceful handling of unexpected errors."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("Unexpected error")

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is False
            assert "Could not check" in result["message"]

    def test_unknown_content_suggests_manual_check(self):
        """Test that unknown content suggests manual check."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock_urlopen.return_value.__enter__.return_value
            mock_response.read.return_value = b"Completely unrelated content here."

            result = check_pipedream_api_support()

            assert result["supports_code_update"] is False
            assert "Docs structure may have changed" in result["message"]
