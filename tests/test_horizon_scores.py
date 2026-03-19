"""
Tests for update_horizon_scores.py Pipedream step.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import json

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.update_horizon_scores import (
    handler,
    extract_text_from_rich_text,
    parse_blocks_to_text,
    extract_task_info,
    retry_with_backoff,
    fetch_page_blocks,
    call_claude,
    score_tasks_batch,
    markdown_to_notion_blocks,
    get_score_color,
    create_table_block,
    create_callout_block,
    HorizonScoringError,
)


class TestExtractTextFromRichText:
    """Tests for the extract_text_from_rich_text helper function."""

    def test_extracts_plain_text(self):
        rich_text = [
            {"plain_text": "Hello "},
            {"plain_text": "World"}
        ]
        assert extract_text_from_rich_text(rich_text) == "Hello World"

    def test_handles_empty_array(self):
        assert extract_text_from_rich_text([]) == ""

    def test_handles_none(self):
        assert extract_text_from_rich_text(None) == ""

    def test_handles_missing_plain_text(self):
        rich_text = [{"type": "text"}]  # No plain_text key
        assert extract_text_from_rich_text(rich_text) == ""


class TestParseBlocksToText:
    """Tests for the parse_blocks_to_text function."""

    def test_parses_heading_1(self):
        blocks = [{"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}}]
        result = parse_blocks_to_text(blocks)
        assert "# Title" in result

    def test_parses_heading_2(self):
        blocks = [{"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Subtitle"}]}}]
        result = parse_blocks_to_text(blocks)
        assert "## Subtitle" in result

    def test_parses_paragraph(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Some text"}]}}]
        result = parse_blocks_to_text(blocks)
        assert "Some text" in result

    def test_parses_bulleted_list(self):
        blocks = [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "Item 1"}]}}]
        result = parse_blocks_to_text(blocks)
        assert "• Item 1" in result

    def test_parses_to_do(self):
        blocks = [{"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Task"}], "checked": True}}]
        result = parse_blocks_to_text(blocks)
        assert "[x] Task" in result

    def test_parses_unchecked_to_do(self):
        blocks = [{"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Task"}], "checked": False}}]
        result = parse_blocks_to_text(blocks)
        assert "[ ] Task" in result

    def test_parses_quote(self):
        blocks = [{"type": "quote", "quote": {"rich_text": [{"plain_text": "A quote"}]}}]
        result = parse_blocks_to_text(blocks)
        assert "> A quote" in result

    def test_parses_divider(self):
        blocks = [{"type": "divider", "divider": {}}]
        result = parse_blocks_to_text(blocks)
        assert "---" in result

    def test_handles_empty_blocks(self):
        result = parse_blocks_to_text([])
        assert result == ""


class TestExtractTaskInfo:
    """Tests for the extract_task_info function."""

    def test_extracts_title(self):
        task = {
            "id": "task_123",
            "properties": {
                "Task name": {
                    "type": "title",
                    "title": [{"plain_text": "Test Task"}]
                }
            }
        }
        info = extract_task_info(task)
        assert info["id"] == "task_123"
        assert info["title"] == "Test Task"

    def test_extracts_list_status(self):
        task = {
            "id": "task_123",
            "properties": {
                "List": {
                    "type": "status",
                    "status": {"name": "Next Actions"}
                }
            }
        }
        info = extract_task_info(task)
        assert info["list"] == "Next Actions"

    def test_extracts_priority_select(self):
        task = {
            "id": "task_123",
            "properties": {
                "Priority": {
                    "type": "select",
                    "select": {"name": "High"}
                }
            }
        }
        info = extract_task_info(task)
        assert info["priority"] == "High"

    def test_extracts_due_date(self):
        task = {
            "id": "task_123",
            "properties": {
                "Due": {
                    "type": "date",
                    "date": {"start": "2024-01-20"}
                }
            }
        }
        info = extract_task_info(task)
        assert info["due_date"] == "2024-01-20"

    def test_handles_missing_properties(self):
        task = {"id": "task_123", "properties": {}}
        info = extract_task_info(task)
        assert info["id"] == "task_123"
        assert info["title"] == ""
        assert info["list"] == ""


class TestRetryWithBackoff:
    """Tests for the retry_with_backoff function."""

    def test_succeeds_on_first_try(self):
        mock_func = MagicMock()
        mock_response = MagicMock()
        mock_func.return_value = mock_response

        result = retry_with_backoff(mock_func)

        assert result == mock_response
        mock_func.assert_called_once()

    @patch('steps.update_horizon_scores.time.sleep')
    def test_retries_on_429(self, mock_sleep):
        import requests
        mock_func = MagicMock()

        # First call raises 429, second succeeds
        error_response = MagicMock()
        error_response.status_code = 429
        error_response.headers = {}
        error = requests.HTTPError()
        error.response = error_response

        success_response = MagicMock()
        mock_func.side_effect = [error, success_response]

        result = retry_with_backoff(mock_func, max_retries=3)

        assert result == success_response
        assert mock_func.call_count == 2

    @patch('steps.update_horizon_scores.time.sleep')
    def test_respects_retry_after_header(self, mock_sleep):
        import requests
        mock_func = MagicMock()

        error_response = MagicMock()
        error_response.status_code = 429
        error_response.headers = {'Retry-After': '5'}
        error = requests.HTTPError()
        error.response = error_response

        success_response = MagicMock()
        mock_func.side_effect = [error, success_response]

        retry_with_backoff(mock_func)

        # Should wait 5 seconds as specified
        mock_sleep.assert_called_once_with(5.0)


class TestHandler:
    """Tests for the main handler function."""

    def test_raises_exception_without_notion_token(self, mock_pd):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(Exception) as exc_info:
                handler(mock_pd)
            assert "NOTION_API_TOKEN" in str(exc_info.value)

    def test_raises_exception_without_database_id(self, mock_pd):
        with patch.dict(os.environ, {"NOTION_API_TOKEN": "test_token"}, clear=True):
            with pytest.raises(Exception) as exc_info:
                handler(mock_pd)
            assert "NOTION_DATABASE_ID" in str(exc_info.value)

    def test_raises_exception_without_horizons_page_id(self, mock_pd):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db"
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc_info:
                handler(mock_pd)
            assert "NOTION_HORIZONS_PAGE_ID" in str(exc_info.value)

    def test_raises_exception_without_anthropic_key(self, mock_pd):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db",
            "NOTION_HORIZONS_PAGE_ID": "test_page"
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(Exception) as exc_info:
                handler(mock_pd)
            assert "ANTHROPIC_API_KEY" in str(exc_info.value)


class TestCallClaude:
    """Tests for the call_claude function."""

    @patch('steps.update_horizon_scores.requests.post')
    def test_returns_response_text(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": "This is the response"}]
        }
        mock_post.return_value = mock_response

        result = call_claude("Test prompt", "test_key")

        assert result == "This is the response"

    @patch('steps.update_horizon_scores.requests.post')
    def test_uses_correct_headers(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": "ok"}]}
        mock_post.return_value = mock_response

        call_claude("Test", "my_api_key")

        call_args = mock_post.call_args
        headers = call_args[1]["headers"]
        assert headers["x-api-key"] == "my_api_key"
        assert headers["anthropic-version"] == "2023-06-01"


class TestScoreTasksBatch:
    """Tests for the score_tasks_batch function."""

    @patch('steps.update_horizon_scores.call_claude')
    def test_parses_json_response(self, mock_claude):
        mock_claude.return_value = '''[
            {"score": 85, "reasoning": "Good alignment"},
            {"score": 45, "reasoning": "Moderate alignment"}
        ]'''

        tasks = [
            {"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},
            {"id": "task_2", "title": "Task 2", "list": "Someday/Maybe", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert len(result) == 2
        assert result[0]["task_id"] == "task_1"
        assert result[0]["score"] == 85
        assert result[1]["task_id"] == "task_2"
        assert result[1]["score"] == 45

    @patch('steps.update_horizon_scores.call_claude')
    def test_handles_json_with_surrounding_text(self, mock_claude):
        # Claude sometimes adds explanatory text around JSON
        mock_claude.return_value = '''Here are the scores:
        [{"score": 75, "reasoning": "Aligned"}]
        That's the result.'''

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert len(result) == 1
        assert result[0]["task_id"] == "task_1"
        assert result[0]["score"] == 75

    @patch('steps.update_horizon_scores.call_claude')
    def test_injects_task_ids_positionally(self, mock_claude):
        """Task IDs are injected by position, not from Claude's response."""
        mock_claude.return_value = '''[
            {"task_id": "wrong_id", "score": 90, "reasoning": "Great"},
            {"score": 60, "reasoning": "OK"}
        ]'''

        tasks = [
            {"id": "real_id_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},
            {"id": "real_id_2", "title": "Task 2", "list": "Waiting For", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        # Even if Claude returns a wrong task_id, positional injection overrides it
        assert result[0]["task_id"] == "real_id_1"
        assert result[1]["task_id"] == "real_id_2"

    @patch('steps.update_horizon_scores.call_claude')
    def test_truncates_on_score_count_mismatch(self, mock_claude):
        """Extra scores from Claude are truncated to match task count."""
        mock_claude.return_value = '''[
            {"score": 80, "reasoning": "Good"},
            {"score": 50, "reasoning": "OK"},
            {"score": 30, "reasoning": "Extra"}
        ]'''

        tasks = [
            {"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},
            {"id": "task_2", "title": "Task 2", "list": "Waiting For", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert len(result) == 2
        assert result[0]["task_id"] == "task_1"
        assert result[1]["task_id"] == "task_2"

    @patch('steps.update_horizon_scores.call_claude')
    def test_raises_on_invalid_json(self, mock_claude):
        """Test that invalid JSON raises HorizonScoringError (fail loudly)."""
        mock_claude.return_value = "This is not valid JSON"

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]

        with pytest.raises(HorizonScoringError, match="No JSON array found"):
            score_tasks_batch(tasks, "test rubric", "test_key")


class TestIntegration:
    """Integration-style tests for the full workflow."""

    @patch('steps.update_horizon_scores.update_horizon_score')
    @patch('steps.update_horizon_scores.score_tasks_batch')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    def test_full_workflow_success(
        self, mock_parse, mock_fetch, mock_rubric, mock_query,
        mock_score, mock_update, mock_pd
    ):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db",
            "NOTION_HORIZONS_PAGE_ID": "test_page",
            "ANTHROPIC_API_KEY": "test_key"
        }

        with patch.dict(os.environ, env, clear=True):
            # Mock the workflow
            mock_fetch.return_value = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
            mock_parse.return_value = "Purpose: Be awesome"
            mock_rubric.return_value = "Score based on alignment"
            mock_query.return_value = [
                {"id": "task_1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "Task 1"}]}}}
            ]
            mock_score.return_value = [
                {"task_id": "task_1", "score": 80, "reasoning": "Good"}
            ]
            mock_update.return_value = True

            result = handler(mock_pd)

            assert result["status"] == "Completed"
            assert result["tasks_scored"] == 1
            assert len(result["successful_updates"]) == 1

    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    def test_returns_no_tasks_message(
        self, mock_parse, mock_fetch, mock_rubric, mock_query, mock_pd
    ):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db",
            "NOTION_HORIZONS_PAGE_ID": "test_page",
            "ANTHROPIC_API_KEY": "test_key"
        }

        with patch.dict(os.environ, env, clear=True):
            mock_fetch.return_value = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
            mock_parse.return_value = "Purpose: Be awesome"
            mock_rubric.return_value = "Score rubric"
            mock_query.return_value = []  # No tasks

            result = handler(mock_pd)

            assert result["status"] == "Completed"
            assert result["tasks_scored"] == 0
            assert "No tasks found" in result.get("message", "")


class TestGetScoreColor:
    """Tests for the get_score_color function."""

    def test_high_leverage_green(self):
        assert get_score_color("90-100") == "green"
        assert get_score_color("Score: 90+") == "green"

    def test_goal_aligned_blue(self):
        assert get_score_color("75-89") == "blue"

    def test_area_support_default(self):
        assert get_score_color("50-74") == "default"

    def test_values_aligned_orange(self):
        assert get_score_color("30-49") == "orange"

    def test_maintenance_gray(self):
        assert get_score_color("10-29") == "gray"

    def test_misaligned_red(self):
        assert get_score_color("0-9") == "red"

    def test_no_score_default(self):
        assert get_score_color("Some random text") == "default"


class TestCreateTableBlock:
    """Tests for the create_table_block function."""

    def test_creates_table_with_header(self):
        lines = [
            "Header1 | Header2 | Header3",
            "Value1 | Value2 | Value3"
        ]
        result = create_table_block(lines)

        assert result["type"] == "table"
        assert result["table"]["table_width"] == 3
        assert result["table"]["has_column_header"] is True
        assert len(result["table"]["children"]) == 2

    def test_header_row_is_bold(self):
        lines = ["Header | Value"]
        result = create_table_block(lines)

        header_row = result["table"]["children"][0]
        first_cell = header_row["table_row"]["cells"][0][0]
        assert first_cell["annotations"]["bold"] is True

    def test_applies_score_colors(self):
        lines = [
            "Score | Meaning",
            "90-100 | High leverage"
        ]
        result = create_table_block(lines)

        data_row = result["table"]["children"][1]
        score_cell = data_row["table_row"]["cells"][0][0]
        assert score_cell["annotations"]["color"] == "green"

    def test_returns_none_for_empty(self):
        assert create_table_block([]) is None

    def test_pads_short_rows(self):
        lines = [
            "A | B | C",
            "X"  # Only one cell
        ]
        result = create_table_block(lines)

        # Should still have 3 cells in second row
        data_row = result["table"]["children"][1]
        assert len(data_row["table_row"]["cells"]) == 3


class TestCreateCalloutBlock:
    """Tests for the create_callout_block function."""

    def test_creates_callout_with_emoji(self):
        result = create_callout_block("Test message", "💡")

        assert result["type"] == "callout"
        assert result["callout"]["icon"]["emoji"] == "💡"
        assert result["callout"]["rich_text"][0]["text"]["content"] == "Test message"

    def test_yellow_background_for_lightbulb(self):
        result = create_callout_block("Tip", "💡")
        assert result["callout"]["color"] == "yellow_background"

    def test_orange_background_for_warning(self):
        result = create_callout_block("Warning", "⚠️")
        assert result["callout"]["color"] == "orange_background"

    def test_blue_background_for_clipboard(self):
        result = create_callout_block("Info", "📋")
        assert result["callout"]["color"] == "blue_background"

    def test_gray_background_for_unknown_emoji(self):
        result = create_callout_block("Text", "🔮")
        assert result["callout"]["color"] == "gray_background"


class TestMarkdownToNotionBlocks:
    """Tests for the enhanced markdown_to_notion_blocks function."""

    def test_parses_divider(self):
        result = markdown_to_notion_blocks("---")
        assert len(result) == 1
        assert result[0]["type"] == "divider"

    def test_parses_heading_with_emoji(self):
        result = markdown_to_notion_blocks("# 🎯 My Title")
        assert result[0]["type"] == "heading_1"
        assert result[0]["heading_1"]["rich_text"][0]["text"]["content"] == "🎯 My Title"

    def test_parses_table_block(self):
        markdown = """[TABLE]
Score | Meaning
90-100 | High
[/TABLE]"""
        result = markdown_to_notion_blocks(markdown)

        assert len(result) == 1
        assert result[0]["type"] == "table"
        assert result[0]["table"]["table_width"] == 2

    def test_parses_callout_block(self):
        markdown = "[CALLOUT:💡] This is a tip [/CALLOUT]"
        result = markdown_to_notion_blocks(markdown)

        assert len(result) == 1
        assert result[0]["type"] == "callout"
        assert result[0]["callout"]["icon"]["emoji"] == "💡"
        assert "This is a tip" in result[0]["callout"]["rich_text"][0]["text"]["content"]

    def test_parses_multiline_callout(self):
        markdown = """[CALLOUT:📋] This is line one
This is line two
[/CALLOUT]"""
        result = markdown_to_notion_blocks(markdown)

        assert len(result) == 1
        assert result[0]["type"] == "callout"
        assert "line one" in result[0]["callout"]["rich_text"][0]["text"]["content"]
        assert "line two" in result[0]["callout"]["rich_text"][0]["text"]["content"]

    def test_parses_bullet_list(self):
        result = markdown_to_notion_blocks("- Item one\n- Item two")
        assert len(result) == 2
        assert result[0]["type"] == "bulleted_list_item"
        assert result[1]["type"] == "bulleted_list_item"

    def test_parses_numbered_list(self):
        result = markdown_to_notion_blocks("1. First\n2. Second")
        assert len(result) == 2
        assert result[0]["type"] == "numbered_list_item"

    def test_parses_bold_text(self):
        result = markdown_to_notion_blocks("**Bold Header**")
        assert result[0]["type"] == "paragraph"
        assert result[0]["paragraph"]["rich_text"][0]["annotations"]["bold"] is True
        # Should not include ** markers
        assert "**" not in result[0]["paragraph"]["rich_text"][0]["text"]["content"]

    def test_parses_complex_document(self):
        markdown = """# 🎯 Horizon Score Rubric

[CALLOUT:📋] Overview text [/CALLOUT]

---

## 📊 Score Ranges

[TABLE]
Score | Meaning
90-100 | High
0-9 | Low
[/TABLE]

- Bullet point
"""
        result = markdown_to_notion_blocks(markdown)

        # Should have: heading, callout, divider, heading, table, bullet
        types = [b["type"] for b in result]
        assert "heading_1" in types
        assert "callout" in types
        assert "divider" in types
        assert "heading_2" in types
        assert "table" in types
        assert "bulleted_list_item" in types
