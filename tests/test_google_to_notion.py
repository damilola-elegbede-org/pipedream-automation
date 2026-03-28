"""
Tests for google_to_notion.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.google_to_notion import handler, safe_get, extract_notion_page_id, format_notion_date


class TestSafeGet:
    """Tests for the safe_get helper function."""

    def test_gets_nested_dict_value(self):
        data = {"a": {"b": {"c": "value"}}}
        assert safe_get(data, ["a", "b", "c"]) == "value"

    def test_gets_list_index(self):
        data = {"items": ["first", "second", "third"]}
        assert safe_get(data, ["items", 1]) == "second"

    def test_returns_default_for_missing_key(self):
        data = {"a": 1}
        assert safe_get(data, ["b"], default="default") == "default"

    def test_returns_default_for_none_value(self):
        data = {"a": None}
        assert safe_get(data, ["a"], default="default") == "default"

    def test_handles_single_key(self):
        data = {"key": "value"}
        assert safe_get(data, "key") == "value"


class TestExtractNotionPageId:
    """Tests for the extract_notion_page_id function."""

    def test_extracts_32_char_hex_id_from_notes(self):
        notes = "Notion Task: Test Task\nLink: https://www.notion.so/Page-Title-abc123def456789012345678901234ab"
        result = extract_notion_page_id(notes)
        assert result == "abc123def456789012345678901234ab"

    def test_handles_query_params(self):
        notes = "Link: https://www.notion.so/Page-Title-abc123def456789012345678901234ab?pvs=4"
        result = extract_notion_page_id(notes)
        assert result == "abc123def456789012345678901234ab"

    def test_returns_none_for_no_notion_url(self):
        notes = "Just some regular task notes without a URL"
        result = extract_notion_page_id(notes)
        assert result is None

    def test_returns_none_for_empty(self):
        assert extract_notion_page_id(None) is None
        assert extract_notion_page_id("") is None


class TestFormatNotionDate:
    """Tests for the format_notion_date function."""

    def test_extracts_date_from_rfc3339(self):
        result = format_notion_date("2024-01-20T00:00:00.000Z")
        assert result == "2024-01-20"

    def test_handles_date_only(self):
        result = format_notion_date("2024-01-20")
        assert result == "2024-01-20"

    def test_handles_none(self):
        assert format_notion_date(None) is None

    def test_handles_empty(self):
        assert format_notion_date("") is None


class TestHandler:
    """Tests for the main handler function."""

    def test_exits_for_non_notion_task(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "title": "Regular Task",
                    "notes": "Just a regular task, no Notion URL"
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "does not have a Notion URL" in mock_pd.flow.exit_message

    def test_exits_for_task_without_notes(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "title": "Task Without Notes"
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True

    def test_processes_notion_linked_task(self, mock_pd, sample_gtask_trigger):
        mock_pd.steps = sample_gtask_trigger

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert "NotionUpdate" in result
        assert result["NotionUpdate"]["PageId"] is not None
        assert len(result["NotionUpdate"]["PageId"]) == 32

    def test_maps_completed_status(self, mock_pd, sample_gtask_trigger_completed):
        mock_pd.steps = sample_gtask_trigger_completed

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["NotionUpdate"]["ListValue"] == "Completed"

    def test_maps_incomplete_status(self, mock_pd, sample_gtask_trigger):
        mock_pd.steps = sample_gtask_trigger

        result = handler(mock_pd)

        assert result["NotionUpdate"]["ListValue"] == "Next Actions"

    def test_extracts_due_date(self, mock_pd, sample_gtask_trigger):
        mock_pd.steps = sample_gtask_trigger

        result = handler(mock_pd)

        assert result["NotionUpdate"]["DueDate"]["start"] == "2024-01-20"

    def test_exits_when_id_extraction_fails(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "title": "Task with Bad URL",
                    "notes": "Link: https://www.notion.so/no-valid-id-here"
                }
            }
        }

        handler(mock_pd)

        assert mock_pd.flow.exit_called is True
        assert "Could not reliably extract" in mock_pd.flow.exit_message

