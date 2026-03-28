"""
Tests for notion_task_to_gcal.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.notion_task_to_gcal import handler, safe_get, is_datetime, normalize_dates


class TestIsDatetime:
    """Tests for the is_datetime helper function."""

    def test_detects_datetime_format(self):
        assert is_datetime("2024-01-20T10:00:00") is True
        assert is_datetime("2024-01-20T10:00:00.000+00:00") is True

    def test_detects_date_only_format(self):
        assert is_datetime("2024-01-20") is False

    def test_handles_none(self):
        assert is_datetime(None) is False

    def test_handles_empty_string(self):
        assert is_datetime("") is False


class TestNormalizeDates:
    """Tests for the normalize_dates helper function."""

    def test_both_dates_same_format(self):
        # Both date-only
        start, end = normalize_dates("2024-01-20", "2024-01-21")
        assert start == "2024-01-20"
        assert end == "2024-01-21"

        # Both datetime
        start, end = normalize_dates("2024-01-20T10:00:00", "2024-01-20T14:00:00")
        assert start == "2024-01-20T10:00:00"
        assert end == "2024-01-20T14:00:00"

    def test_end_is_none_returns_start_for_both(self):
        start, end = normalize_dates("2024-01-20", None)
        assert start == "2024-01-20"
        assert end == "2024-01-20"

    def test_start_datetime_end_date_normalizes_end(self):
        start, end = normalize_dates("2024-01-20T10:00:00", "2024-01-21")
        assert start == "2024-01-20T10:00:00"
        assert end == "2024-01-21T23:59:59"

    def test_start_date_end_datetime_normalizes_start(self):
        start, end = normalize_dates("2024-01-20", "2024-01-21T14:00:00")
        assert start == "2024-01-20T00:00:00"
        assert end == "2024-01-21T14:00:00"


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
                        "Google Event ID": {"rich_text": []}
                    },
                    "url": "https://www.notion.so/test"
                }
            }
        }

        result = handler(mock_pd)  # noqa: F841

        assert mock_pd.flow.exit_called is True
        assert "Due Date is missing" in mock_pd.flow.exit_message

    def test_exits_when_event_already_exists(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "properties": {
                        "Task name": {"title": [{"plain_text": "Test Task"}]},
                        "Due Date": {"date": {"start": "2024-01-20", "end": None}},
                        "Google Event ID": {"rich_text": [{"plain_text": "existing_event_id"}]}
                    },
                    "url": "https://www.notion.so/test"
                }
            }
        }

        result = handler(mock_pd)  # noqa: F841

        assert mock_pd.flow.exit_called is True
        assert "Google Event ID exists" in mock_pd.flow.exit_message

    def test_processes_valid_task(self, mock_pd, sample_notion_task_trigger):
        mock_pd.steps = sample_notion_task_trigger

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["GCal"]["Subject"] == "Test Task"
        assert result["GCal"]["Start"] == "2024-01-20"
        assert result["GCal"]["End"] == "2024-01-21"
        assert result["GCal"]["Update"] is False  # Update=False means it's a create

    def test_uses_start_as_end_when_end_missing(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",  # 32-char hex Notion page ID
                    "properties": {
                        "Task name": {"title": [{"plain_text": "Single Day Task"}]},
                        "Due Date": {"date": {"start": "2024-01-20", "end": None}},
                        "Google Event ID": {"rich_text": []}
                    },
                    "url": "https://www.notion.so/single-day"
                }
            }
        }

        result = handler(mock_pd)

        # End should default to start date
        assert result["GCal"]["End"] == result["GCal"]["Start"]

    def test_includes_notion_url_in_result(self, mock_pd, sample_notion_task_trigger):
        mock_pd.steps = sample_notion_task_trigger

        result = handler(mock_pd)

        assert "Url" in result["GCal"]
        assert "notion.so" in result["GCal"]["Url"]

    def test_includes_description(self, mock_pd, sample_notion_task_trigger):
        mock_pd.steps = sample_notion_task_trigger

        result = handler(mock_pd)

        assert "Description" in result["GCal"]
        assert "Notion Task" in result["GCal"]["Description"]
