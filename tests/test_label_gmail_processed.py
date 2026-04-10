"""
Tests for label_gmail_processed.py Pipedream step.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.label_gmail_processed import handler, get_label_id


class TestGetLabelId:
    """Tests for the get_label_id helper function."""

    @patch('steps.label_gmail_processed.requests.get')
    def test_finds_label_by_name(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "labels": [
                {"id": "Label_123", "name": "notiontaskcreated"},
                {"id": "Label_456", "name": "other"}
            ]
        }
        mock_get.return_value = mock_response

        headers = {"Authorization": "Bearer test"}
        result = get_label_id(headers, "notiontaskcreated")

        assert result == "Label_123"

    @patch('steps.label_gmail_processed.requests.get')
    def test_case_insensitive_match(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "labels": [{"id": "Label_123", "name": "NotionTaskCreated"}]
        }
        mock_get.return_value = mock_response

        headers = {"Authorization": "Bearer test"}
        result = get_label_id(headers, "notiontaskcreated")

        assert result == "Label_123"

    @patch('steps.label_gmail_processed.requests.get')
    def test_returns_none_when_not_found(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"labels": []}
        mock_get.return_value = mock_response

        headers = {"Authorization": "Bearer test"}
        result = get_label_id(headers, "nonexistent")

        assert result is None


class TestHandler:
    """Tests for the main handler function."""

    def test_raises_exception_without_auth(self, mock_pd):
        with pytest.raises(Exception) as exc_info:
            handler(mock_pd)
        assert "Gmail account not connected" in str(exc_info.value)

    @patch('steps.label_gmail_processed.get_label_id')
    def test_returns_error_when_label_not_found(self, mock_get_label, mock_pd, gmail_auth):
        mock_pd.inputs = gmail_auth
        mock_pd.steps = {"create_notion_task": {"$return_value": {"successful_mappings": []}}}
        mock_get_label.return_value = None

        result = handler(mock_pd)

        assert "error" in result
        assert "Could not find Label ID" in result["error"]

    @patch('steps.label_gmail_processed.get_label_id')
    def test_handles_empty_mappings(self, mock_get_label, mock_pd, gmail_auth):
        mock_pd.inputs = gmail_auth
        mock_pd.steps = {"create_notion_task": {"$return_value": {"successful_mappings": []}}}
        mock_get_label.return_value = "Label_123"

        result = handler(mock_pd)

        assert result["labeled_messages"] == 0
        assert result["status"] == "No data received"

    @patch('steps.label_gmail_processed.get_label_id')
    def test_handles_missing_successful_mappings_key(self, mock_get_label, mock_pd, gmail_auth):
        """Test behavior when previous step doesn't include successful_mappings."""
        mock_pd.inputs = gmail_auth
        mock_pd.steps = {"create_notion_task": {"$return_value": {"error": "some error"}}}
        mock_get_label.return_value = "Label_123"

        result = handler(mock_pd)

        # Should handle gracefully
        assert "error" in result

    @patch('steps.label_gmail_processed.get_label_id')
    @patch('steps.label_gmail_processed.requests.post')
    @patch('steps.label_gmail_processed.time.sleep')
    def test_labels_messages_successfully(self, mock_sleep, mock_post, mock_get_label, mock_pd, gmail_auth, sample_successful_mappings):  # noqa: E501
        mock_pd.inputs = gmail_auth
        mock_pd.steps = {"create_notion_task": {"$return_value": sample_successful_mappings}}
        mock_get_label.return_value = "Label_123"

        # Mock batch API response with proper attributes
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"id": "msg_abc123"}\n{"id": "msg_def456"}'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = handler(mock_pd)

        assert result["status"] == "Completed"
        assert len(result["successfully_labeled_ids"]) == 2

    @patch('steps.label_gmail_processed.get_label_id')
    @patch('steps.label_gmail_processed.requests.post')
    @patch('steps.label_gmail_processed.time.sleep')
    def test_handles_partial_label_failure(self, mock_sleep, mock_post, mock_get_label, mock_pd, gmail_auth, sample_successful_mappings):  # noqa: E501
        mock_pd.inputs = gmail_auth
        mock_pd.steps = {"create_notion_task": {"$return_value": sample_successful_mappings}}
        mock_get_label.return_value = "Label_123"

        import requests
        # Create a proper HTTPError with response attribute for batch API failure
        mock_error_response = MagicMock()
        mock_error_response.status_code = 500
        mock_error_response.headers = {}
        mock_error_response.json.return_value = {"error": {"message": "Server Error"}}
        http_error = requests.exceptions.HTTPError("API Error")
        http_error.response = mock_error_response

        # Batch API fails, then fallback: first individual succeeds, second fails
        mock_success = MagicMock()
        mock_success.raise_for_status = MagicMock()
        mock_post.side_effect = [http_error, mock_success, http_error]

        result = handler(mock_pd)

        # Should have 1 success and 1 error from fallback individual requests
        assert len(result["successfully_labeled_ids"]) == 1
        assert len(result["errors"]) == 1
