"""
Tests for notion_task_to_google.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.notion_task_to_google import handler, safe_get, format_due_date


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

    def test_handles_empty_string(self):
        assert format_due_date("") is None


class TestSafeGet:
    """Tests for the safe_get helper function."""

    def test_gets_nested_dict_value(self):
        data = {"a": {"b": {"c": "value"}}}
        assert safe_get(data, ["a", "b", "c"]) == "value"

    def test_gets_list_index(self):
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        assert safe_get(data, ["items", 0, "name"]) == "first"

    def test_returns_default_for_missing_key(self):
        data = {"a": 1}
        assert safe_get(data, ["b", "c"], default="default") == "default"

    def test_handles_empty_list(self):
        data = {"items": []}
        assert safe_get(data, ["items", 0], default="default") == "default"


class TestHandler:
    """Tests for the main handler function."""

    def test_exits_when_due_date_missing(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "properties": {
                        "Task name": {"title": [{"plain_text": "Test Task"}]},
                        "Due Date": {"date": None},
                        "Google Task ID": {"rich_text": []}
                    },
                    "url": "https://www.notion.so/test"
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "Due Date is missing" in mock_pd.flow.exit_message

    def test_exits_when_task_already_exists(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "properties": {
                        "Task name": {"title": [{"plain_text": "Test Task"}]},
                        "Due Date": {"date": {"start": "2024-01-20", "end": None}},
                        "Google Task ID": {"rich_text": [{"plain_text": "existing_task_id"}]}
                    },
                    "url": "https://www.notion.so/test"
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "Google Task ID exists" in mock_pd.flow.exit_message

    def test_processes_valid_task(self, mock_pd, sample_notion_task_trigger_gtask):
        mock_pd.steps = sample_notion_task_trigger_gtask

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["GTask"]["Title"] == "Test Task"
        assert result["GTask"]["Due"] == "2024-01-20T00:00:00.000Z"
        assert "Notes" in result["GTask"]

    def test_includes_notion_url_in_notes(self, mock_pd, sample_notion_task_trigger_gtask):
        mock_pd.steps = sample_notion_task_trigger_gtask

        result = handler(mock_pd)

        assert "notion.so" in result["GTask"]["Notes"]

    def test_includes_notion_id_and_url(self, mock_pd, sample_notion_task_trigger_gtask):
        mock_pd.steps = sample_notion_task_trigger_gtask

        result = handler(mock_pd)

        assert "NotionId" in result["GTask"]
        assert "NotionUrl" in result["GTask"]
