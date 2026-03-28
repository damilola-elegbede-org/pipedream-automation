"""
Tests for create_notion_task.py Pipedream step.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.create_notion_task import (
    handler,
    extract_email,
    build_notion_properties,
    check_existing_task,
    build_page_content_blocks
)


class TestExtractEmail:
    """Tests for the extract_email helper function."""

    def test_extracts_from_angle_brackets(self):
        assert extract_email("John Doe <john@example.com>") == "john@example.com"

    def test_handles_plain_email(self):
        assert extract_email("john@example.com") == "john@example.com"

    def test_handles_email_with_spaces(self):
        result = extract_email("  john@example.com  ")
        assert result == "john@example.com"

    def test_returns_none_for_invalid(self):
        assert extract_email("not an email") is None
        assert extract_email("") is None
        assert extract_email(None) is None


class TestBuildNotionProperties:
    """Tests for the build_notion_properties function."""

    def test_includes_message_id(self, sample_email):
        """Verify Message ID is included in properties (bug fix)."""
        props = build_notion_properties(sample_email, "msg_abc123")
        assert "Message ID" in props
        assert props["Message ID"]["rich_text"][0]["text"]["content"] == "msg_abc123"

    def test_includes_task_name(self, sample_email):
        props = build_notion_properties(sample_email, "msg_abc123")
        assert "Task name" in props
        assert props["Task name"]["title"][0]["text"]["content"] == "Test Email Subject"

    def test_includes_original_email_link(self, sample_email):
        props = build_notion_properties(sample_email, "msg_abc123")
        assert "Original Email Link" in props
        assert props["Original Email Link"]["url"] == sample_email["url"]

    def test_extracts_sender_email(self, sample_email):
        props = build_notion_properties(sample_email, "msg_abc123")
        assert "Sender" in props
        assert props["Sender"]["email"] == "john@example.com"

    def test_extracts_receiver_email(self, sample_email):
        props = build_notion_properties(sample_email, "msg_abc123")
        assert "To" in props
        assert props["To"]["email"] == "jane@example.com"


class TestCheckExistingTask:
    """Tests for duplicate detection via check_existing_task."""

    @patch('steps.create_notion_task.requests.post')
    def test_returns_existing_page_if_found(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"id": "existing_page_123", "properties": {}}]
        }
        mock_post.return_value = mock_response

        headers = {"Authorization": "Bearer test"}
        result = check_existing_task(headers, "db_123", "msg_abc")

        assert result is not None
        assert result["id"] == "existing_page_123"

    @patch('steps.create_notion_task.requests.post')
    def test_returns_none_if_not_found(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_post.return_value = mock_response

        headers = {"Authorization": "Bearer test"}
        result = check_existing_task(headers, "db_123", "msg_abc")

        assert result is None

    @patch('steps.create_notion_task.requests.post')
    def test_handles_api_error_gracefully(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.RequestException("API Error")

        headers = {"Authorization": "Bearer test"}
        result = check_existing_task(headers, "db_123", "msg_abc")

        # Should return None on error, not raise
        assert result is None


class TestBuildPageContentBlocks:
    """Tests for building Notion page content blocks from Claude analysis."""

    def test_creates_toggle_for_plain_text(self):
        """Original email should be in a collapsed toggle block."""
        blocks = build_page_content_blocks("This is test content", None)
        assert len(blocks) > 0
        # Should have toggle block for original email
        block_types = [b["type"] for b in blocks]
        assert "toggle" in block_types

    def test_chunks_long_content_in_toggle(self):
        """Content longer than MAX_CODE_BLOCK_LENGTH should be chunked in toggle."""
        long_content = "A" * 5000
        blocks = build_page_content_blocks(long_content, None)
        # Find the toggle block
        toggle_blocks = [b for b in blocks if b["type"] == "toggle"]
        assert len(toggle_blocks) == 1
        # Check children (code blocks) in toggle
        toggle_children = toggle_blocks[0]["toggle"]["children"]
        assert len(toggle_children) >= 3  # 5000 / 2000 = 3 blocks

    def test_creates_sections_from_analysis(self):
        """Analysis should create structured sections with callout and action items."""
        analysis = {
            "summary": "This is a test summary",
            "action_items": ["Task 1", "Task 2"],
            "key_dates": [{"date": "2024-01-15", "context": "Deadline"}],
            "important_links": [{"url": "https://example.com", "description": "Example"}],
            "key_contacts": [{"name": "John", "email": "john@test.com", "role": "Owner"}],
            "urgency": "high",
            "category": "request"
        }
        blocks = build_page_content_blocks("Original content", analysis)
        block_types = [b["type"] for b in blocks]

        # Should have callout (summary), headings, to_do items, bullets, divider, and toggle
        assert "callout" in block_types
        assert "to_do" in block_types
        assert "heading_2" in block_types
        assert "bulleted_list_item" in block_types
        assert "divider" in block_types
        assert "toggle" in block_types

    def test_urgency_affects_callout_emoji(self):
        """High urgency should show red emoji, low should show green."""
        high_analysis = {"summary": "Urgent!", "urgency": "high", "action_items": [], "key_dates": [], "important_links": [], "key_contacts": [], "category": "request"}  # noqa: E501
        low_analysis = {"summary": "Not urgent", "urgency": "low", "action_items": [], "key_dates": [], "important_links": [], "key_contacts": [], "category": "info"}  # noqa: E501

        high_blocks = build_page_content_blocks("", high_analysis)
        low_blocks = build_page_content_blocks("", low_analysis)

        high_callout = [b for b in high_blocks if b["type"] == "callout"][0]
        low_callout = [b for b in low_blocks if b["type"] == "callout"][0]

        assert high_callout["callout"]["icon"]["emoji"] == "🔴"
        assert low_callout["callout"]["icon"]["emoji"] == "🟢"


class TestHandler:
    """Tests for the main handler function."""

    def test_raises_exception_without_auth(self, mock_pd):
        with pytest.raises(Exception) as exc_info:
            handler(mock_pd)
        assert "Notion account not connected" in str(exc_info.value)

    @patch.dict(os.environ, {}, clear=True)
    def test_raises_exception_without_database_id(self, mock_pd, notion_auth):
        mock_pd.inputs = notion_auth
        with pytest.raises(Exception) as exc_info:
            handler(mock_pd)
        assert "NOTION_DATABASE_ID" in str(exc_info.value)

    @patch.dict(os.environ, {"NOTION_DATABASE_ID": "test_db_123"})
    def test_returns_successful_mappings_on_empty_input(self, mock_pd, notion_auth):
        """Verify successful_mappings is always returned (bug fix)."""
        mock_pd.inputs = notion_auth
        mock_pd.steps = {"fetch_gmail_emails": {"$return_value": []}}

        result = handler(mock_pd)

        assert "successful_mappings" in result
        assert result["successful_mappings"] == []

    @patch.dict(os.environ, {"NOTION_DATABASE_ID": "test_db_123"})
    def test_returns_successful_mappings_on_error(self, mock_pd, notion_auth):
        """Verify successful_mappings is returned even on error (bug fix)."""
        mock_pd.inputs = notion_auth
        mock_pd.steps = {"fetch_gmail_emails": {"$return_value": None}}

        result = handler(mock_pd)

        # Even on error, should have successful_mappings key
        assert "successful_mappings" in result

    @patch.dict(os.environ, {"NOTION_DATABASE_ID": "test_db_123"})
    @patch('steps.create_notion_task.check_existing_task')
    @patch('steps.create_notion_task.requests.post')
    @patch('steps.create_notion_task.requests.patch')
    def test_skips_duplicate_emails(self, mock_patch, mock_post, mock_check, mock_pd, notion_auth, sample_email):
        """Verify duplicate detection works (bug fix)."""
        mock_pd.inputs = notion_auth
        mock_pd.steps = {"fetch_gmail_emails": {"$return_value": [sample_email]}}

        # Simulate existing task found
        mock_check.return_value = {"id": "existing_page_id"}

        result = handler(mock_pd)

        # Should mark as skipped, not create new
        assert len(result["successful_mappings"]) == 1
        assert result["successful_mappings"][0]["skipped"] is True
        assert result["skipped_duplicates"] == 1
        # Should NOT have called post to create page
        mock_post.assert_not_called()

    @patch.dict(os.environ, {"NOTION_DATABASE_ID": "test_db_123"})
    @patch('steps.create_notion_task.check_existing_task')
    @patch('steps.create_notion_task.requests.post')
    @patch('steps.create_notion_task.requests.patch')
    @patch('steps.create_notion_task.time.sleep')
    def test_creates_new_task_when_no_duplicate(self, mock_sleep, mock_patch, mock_post, mock_check, mock_pd, notion_auth, sample_email):  # noqa: E501
        """Verify new task creation when no duplicate exists."""
        mock_pd.inputs = notion_auth
        mock_pd.steps = {"fetch_gmail_emails": {"$return_value": [sample_email]}}

        # No existing task
        mock_check.return_value = None

        # Mock successful page creation
        mock_post_response = MagicMock()
        mock_post_response.json.return_value = {"id": "new_page_id"}
        mock_post.return_value = mock_post_response

        # Mock successful block append
        mock_patch_response = MagicMock()
        mock_patch.return_value = mock_patch_response

        result = handler(mock_pd)

        assert len(result["successful_mappings"]) == 1
        assert result["successful_mappings"][0]["notion_page_id"] == "new_page_id"
        assert result["successful_mappings"][0].get("skipped") is None
