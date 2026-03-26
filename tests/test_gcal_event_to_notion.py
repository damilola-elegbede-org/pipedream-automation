"""
Tests for gcal_event_to_notion.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.gcal_event_to_notion import handler, safe_get, extract_notion_page_id


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
    """Tests for the extract_notion_page_id function (bug fix for fragile ID extraction)."""

    def test_extracts_32_char_hex_id(self):
        url = "https://www.notion.so/Page-Title-abc123def456789012345678901234ab"
        result = extract_notion_page_id(url)
        assert result == "abc123def456789012345678901234ab"

    def test_handles_query_params(self):
        """Bug fix: should handle URLs with query parameters."""
        url = "https://www.notion.so/Page-Title-abc123def456789012345678901234ab?pvs=4"
        result = extract_notion_page_id(url)
        assert result == "abc123def456789012345678901234ab"

    def test_handles_multiple_query_params(self):
        url = "https://www.notion.so/Page-abc123def456789012345678901234ab?pvs=4&foo=bar"
        result = extract_notion_page_id(url)
        assert result == "abc123def456789012345678901234ab"

    def test_returns_none_for_invalid_url(self):
        url = "https://example.com/not-a-notion-url"
        result = extract_notion_page_id(url)
        assert result is None

    def test_returns_none_for_empty(self):
        assert extract_notion_page_id(None) is None
        assert extract_notion_page_id("") is None


class TestHandler:
    """Tests for the main handler function."""

    def test_exits_for_non_notion_event(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "summary": "Regular Event",
                    "location": "123 Main St"
                }
            }
        }

        result = handler(mock_pd)  # noqa: F841

        assert mock_pd.flow.exit_called is True
        assert "does not have a Notion URL" in mock_pd.flow.exit_message

    def test_exits_for_event_without_location(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "summary": "Event Without Location"
                }
            }
        }

        result = handler(mock_pd)  # noqa: F841

        assert mock_pd.flow.exit_called is True

    def test_processes_notion_linked_event(self, mock_pd, sample_gcal_event_trigger):
        mock_pd.steps = sample_gcal_event_trigger

        result = handler(mock_pd)

        assert mock_pd.flow.exit_called is False
        assert result["Subject"] == "Meeting from Notion"
        assert result["Id"] is not None
        assert len(result["Id"]) == 32  # Valid Notion page ID

    def test_extracts_datetime_from_event(self, mock_pd, sample_gcal_event_trigger):
        mock_pd.steps = sample_gcal_event_trigger

        result = handler(mock_pd)

        assert result["Start"] == "2024-01-20T10:00:00-05:00"
        assert result["End"] == "2024-01-20T11:00:00-05:00"

    def test_handles_all_day_event(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "summary": "All Day Event",
                    "location": "https://www.notion.so/Test-abc123def456789012345678901234ab",
                    "start": {"date": "2024-01-20"},
                    "end": {"date": "2024-01-21"}
                }
            }
        }

        result = handler(mock_pd)

        assert result["Start"] == "2024-01-20"
        assert result["End"] == "2024-01-21"

    def test_exits_when_id_extraction_fails(self, mock_pd):
        mock_pd.steps = {
            "trigger": {
                "event": {
                    "summary": "Event with Bad URL",
                    "location": "https://www.notion.so/no-valid-id-here"
                }
            }
        }

        result = handler(mock_pd)  # noqa: F841

        assert mock_pd.flow.exit_called is True
        assert "Could not reliably extract" in mock_pd.flow.exit_message
