"""
Tests for notion_update_to_google.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.notion_update_to_google import handler, safe_get, format_due_date


class TestFormatDueDate:
    """Tests for the format_due_date helper function."""

    def test_formats_date_only(self):
        result = format_due_date("2024-01-20")
        assert result == "2024-01-20T00:00:00.000Z"

    def test_strips_time_from_datetime(self):
        result = format_due_date("2024-01-20T10:30:00")
        assert result == "2024-01-20T00:00:00.000Z"

    def test_handles_none(self):
        assert format_due_date(None) is None


class TestSafeGet:
    """Tests for the safe_get helper function."""

    def test_gets_nested_dict_value(self):
        data = {"page": {"properties": {"name": "value"}}}
        assert safe_get(data, ["page", "properties", "name"]) == "value"

    def test_returns_default_on_missing(self):
        data = {"a": 1}
        assert safe_get(data, ["b"], default="default") == "default"


class TestHandler:
    """Tests for the main handler function."""

    def test_exits_when_due_date_missing(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "page": {
                        "properties": {
                            "Task name": {"title": [{"plain_text": "Test Task"}]},
                            "Due Date": {"date": None},
                            "Google Task ID": {"rich_text": [{"plain_text": "task_123"}]},
                            "List": {"status": {"name": "Next Action"}}
                        },
                        "url": "https://www.notion.so/test"
                    }
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "Due Date is missing" in mock_pd.flow.exit_message

    def test_exits_when_google_task_id_missing(self, mock_pd):
        """Without task ID, this should be a create, not update."""
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "page": {
                        "properties": {
                            "Task name": {"title": [{"plain_text": "Test Task"}]},
                            "Due Date": {"date": {"start": "2024-01-20", "end": None}},
                            "Google Task ID": {"rich_text": []},  # Empty = no task
                            "List": {"status": {"name": "Next Action"}}
                        },
                        "url": "https://www.notion.so/test"
                    }
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "Google Task ID is missing" in mock_pd.flow.exit_message

    def test_processes_valid_update(self, mock_pd, sample_notion_update_trigger_gtask):
        mock_pd.steps = sample_notion_update_trigger_gtask

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["GTask"]["Title"] == "Updated Task"
        assert result["GTask"]["TaskId"] == "gtask_xyz789"
        assert "Completed" in result["GTask"]

    def test_detects_completed_status(self, mock_pd):
        """Test that List='Completed' sets Completed=True."""
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "page": {
                        "properties": {
                            "Task name": {"title": [{"plain_text": "Completed Task"}]},
                            "Due Date": {"date": {"start": "2024-01-20", "end": None}},
                            "Google Task ID": {"rich_text": [{"plain_text": "task_123"}]},
                            "List": {"status": {"name": "Completed"}}
                        },
                        "url": "https://www.notion.so/test"
                    }
                }
            }
        }

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["GTask"]["Completed"] is True

    def test_detects_incomplete_status(self, mock_pd, sample_notion_update_trigger_gtask):
        mock_pd.steps = sample_notion_update_trigger_gtask

        result = handler(mock_pd)

        # sample_notion_update_trigger_gtask has List="Next Action"
        assert result["GTask"]["Completed"] is False

    def test_includes_notion_url_in_notes(self, mock_pd, sample_notion_update_trigger_gtask):
        mock_pd.steps = sample_notion_update_trigger_gtask

        result = handler(mock_pd)

        assert "Notes" in result["GTask"]
        assert "notion.so" in result["GTask"]["Notes"]

    def test_formats_due_date_correctly(self, mock_pd, sample_notion_update_trigger_gtask):
        mock_pd.steps = sample_notion_update_trigger_gtask

        result = handler(mock_pd)

        # Should be RFC 3339 format
        assert result["GTask"]["Due"].endswith("T00:00:00.000Z")
