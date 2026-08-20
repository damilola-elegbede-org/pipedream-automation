"""
Tests for update_horizon_scores.py Pipedream step.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import json  # noqa: F401

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.update_horizon_scores import (  # noqa: F401
    handler,
    extract_text_from_rich_text,
    parse_blocks_to_text,
    extract_task_info,
    retry_with_backoff,
    fetch_page_blocks,
    fetch_page_metadata,
    query_tasks_incremental,
    query_tasks_unscored,
    query_tasks,
    resolve_project_relation,
    call_claude,
    score_tasks_batch,
    compute_gate_then_rank_score,
    SCORING_FORMULA_VERSION,
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


class TestComputeGateThenRankScore:
    """Tests for the deterministic gate-then-rank scoring formula."""

    def test_action_class_adds_boosts(self):
        assert compute_gate_then_rank_score("action", 70, True, True) == 90
        assert compute_gate_then_rank_score("action", 70, False, False) == 70

    def test_action_class_caps_at_100(self):
        assert compute_gate_then_rank_score("action", 95, True, True) == 100

    def test_reading_class_hard_caps_at_25(self):
        assert compute_gate_then_rank_score("reading", 90, True, True) == 25
        assert compute_gate_then_rank_score("reading", 10, False, False) == 10

    def test_waiting_class_always_zero(self):
        assert compute_gate_then_rank_score("waiting", 100, True, True) == 0

    def test_unknown_class_raises(self):
        with pytest.raises(HorizonScoringError, match="Unknown class value"):
            compute_gate_then_rank_score("bogus", 50, False, False)


class TestScoreTasksBatch:
    """Tests for the score_tasks_batch function."""

    @patch('steps.update_horizon_scores.call_claude')
    def test_parses_json_response(self, mock_claude):
        mock_claude.return_value = '''[
            {"class": "action", "align": 85, "unblocks": false, "expiring": false},
            {"class": "action", "align": 45, "unblocks": false, "expiring": false}
        ]'''

        tasks = [
            {"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
            {"id": "task_2", "title": "Task 2", "list": "Someday/Maybe", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}  # noqa: E501
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
        [{"class": "action", "align": 75, "unblocks": false, "expiring": false}]
        That's the result.'''

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]  # noqa: E501

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert len(result) == 1
        assert result[0]["task_id"] == "task_1"
        assert result[0]["score"] == 75

    @patch('steps.update_horizon_scores.call_claude')
    def test_injects_task_ids_positionally(self, mock_claude):
        """Task IDs are injected by position, not from Claude's response."""
        mock_claude.return_value = '''[
            {"task_id": "wrong_id", "class": "action", "align": 90, "unblocks": false, "expiring": false},
            {"class": "action", "align": 60, "unblocks": false, "expiring": false}
        ]'''

        tasks = [
            {"id": "real_id_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
            {"id": "real_id_2", "title": "Task 2", "list": "Waiting For", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}  # noqa: E501
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        # Even if Claude returns a wrong task_id, positional injection overrides it
        assert result[0]["task_id"] == "real_id_1"
        assert result[1]["task_id"] == "real_id_2"

    @patch('steps.update_horizon_scores.call_claude')
    def test_truncates_on_score_count_mismatch(self, mock_claude):
        """Extra scores from Claude are truncated to match task count."""
        mock_claude.return_value = '''[
            {"class": "action", "align": 80, "unblocks": false, "expiring": false},
            {"class": "action", "align": 50, "unblocks": false, "expiring": false},
            {"class": "action", "align": 30, "unblocks": false, "expiring": false}
        ]'''

        tasks = [
            {"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
            {"id": "task_2", "title": "Task 2", "list": "Waiting For", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}  # noqa: E501
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert len(result) == 2
        assert result[0]["task_id"] == "task_1"
        assert result[1]["task_id"] == "task_2"

    @patch('steps.update_horizon_scores.call_claude')
    def test_raises_on_invalid_json(self, mock_claude):
        """Test that invalid JSON raises HorizonScoringError (fail loudly)."""
        mock_claude.return_value = "This is not valid JSON"

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]  # noqa: E501

        with pytest.raises(HorizonScoringError, match="No JSON array found"):
            score_tasks_batch(tasks, "test rubric", "test_key")

    @patch('steps.update_horizon_scores.call_claude')
    def test_raises_on_malformed_class(self, mock_claude):
        """A missing/invalid class or align field fails loudly, not silently."""
        mock_claude.return_value = '''[
            {"class": "bogus", "align": 50, "unblocks": false, "expiring": false}
        ]'''

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]  # noqa: E501

        with pytest.raises(HorizonScoringError, match="Malformed scoring entry"):
            score_tasks_batch(tasks, "test rubric", "test_key")

    @patch('steps.update_horizon_scores.call_claude')
    def test_raises_on_missing_boolean_field(self, mock_claude):
        """A missing unblocks/expiring field fails loudly instead of defaulting to False."""
        mock_claude.return_value = '''[
            {"class": "action", "align": 50}
        ]'''

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]  # noqa: E501

        with pytest.raises(HorizonScoringError, match="Malformed scoring entry"):
            score_tasks_batch(tasks, "test rubric", "test_key")

    @patch('steps.update_horizon_scores.call_claude')
    def test_raises_on_stringly_typed_boolean(self, mock_claude):
        """The string "false" is truthy in Python — must be rejected, not coerced to True."""
        mock_claude.return_value = '''[
            {"class": "action", "align": 50, "unblocks": "false", "expiring": false}
        ]'''

        tasks = [{"id": "task_1", "title": "Task 1", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""}]  # noqa: E501

        with pytest.raises(HorizonScoringError, match="Malformed scoring entry"):
            score_tasks_batch(tasks, "test rubric", "test_key")

    @patch('steps.update_horizon_scores.call_claude')
    def test_gate_then_rank_applied_end_to_end(self, mock_claude):
        """Reading-class entries are hard-capped even with a high align score."""
        mock_claude.return_value = '''[
            {"class": "action", "align": 70, "unblocks": true, "expiring": true},
            {"class": "reading", "align": 96, "unblocks": false, "expiring": false},
            {"class": "waiting", "align": 80, "unblocks": true, "expiring": true}
        ]'''

        tasks = [
            {"id": "t1", "title": "Action", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
            {"id": "t2", "title": "Reading", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
            {"id": "t3", "title": "Waiting", "list": "Next Actions", "project": "", "area": "", "priority": "", "due_date": "", "notes": ""},  # noqa: E501
        ]

        result = score_tasks_batch(tasks, "test rubric", "test_key")

        assert result[0]["score"] == 90  # 70 + 10 + 10
        assert result[1]["score"] == 25  # hard-capped despite align=96
        assert result[2]["score"] == 0   # waiting always scores 0


class TestIntegration:
    """Integration-style tests for the full workflow."""

    @patch('steps.update_horizon_scores.update_horizon_score')
    @patch('steps.update_horizon_scores.score_tasks_batch')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_full_workflow_success(
        self, mock_meta, mock_parse, mock_fetch, mock_rubric, mock_query,
        mock_score, mock_update, mock_pd
    ):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db",
            "NOTION_HORIZONS_PAGE_ID": "test_page",
            "ANTHROPIC_API_KEY": "test_key"
        }

        with patch.dict(os.environ, env, clear=True):
            # First run — no state yet → full scan
            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}
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
            assert result["rubric_source"] == "regenerated"
            assert result["scan_type"] == "full"

    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_returns_no_tasks_message(
        self, mock_meta, mock_parse, mock_fetch, mock_rubric, mock_query, mock_pd
    ):
        env = {
            "NOTION_API_TOKEN": "test_token",
            "NOTION_DATABASE_ID": "test_db",
            "NOTION_HORIZONS_PAGE_ID": "test_page",
            "ANTHROPIC_API_KEY": "test_key"
        }

        with patch.dict(os.environ, env, clear=True):
            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}
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


class TestFetchPageMetadata:
    """Tests for the fetch_page_metadata helper."""

    @patch('steps.update_horizon_scores.retry_with_backoff')
    def test_returns_page_dict(self, mock_retry):
        page_data = {
            "id": "page_123",
            "last_edited_time": "2024-06-01T12:00:00.000Z",
            "object": "page",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = page_data
        mock_retry.return_value = mock_response

        result = fetch_page_metadata("page_123", {"Authorization": "Bearer tok"})

        assert result == page_data
        assert result["last_edited_time"] == "2024-06-01T12:00:00.000Z"


class TestRubricCaching:
    """Tests for rubric cache hit / miss via handler."""

    ENV = {
        "NOTION_API_TOKEN": "test_token",
        "NOTION_DATABASE_ID": "test_db",
        "NOTION_HORIZONS_PAGE_ID": "test_page",
        "ANTHROPIC_API_KEY": "test_key",
    }

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_rubric_cache_hit_skips_block_fetch_and_claude(
        self, mock_meta, mock_query, mock_score_all, mock_update_all, mock_pd
    ):
        """When horizons haven't changed and cache exists, skip block fetch + rubric generation."""
        with patch.dict(os.environ, self.ENV, clear=True):
            # Pre-populate state as if a prior run succeeded
            mock_pd.state["horizons_last_edited_at"] = "2024-06-01T12:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Cached rubric text"
            mock_pd.state["last_run_at"] = None  # force full scan

            mock_meta.return_value = {"last_edited_time": "2024-06-01T12:00:00.000Z"}
            mock_query.return_value = [
                {"id": "t1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "T1"}]}}}
            ]
            mock_score_all.return_value = [{"task_id": "t1", "score": 70, "reasoning": "ok"}]
            mock_update_all.return_value = ([{"task_id": "t1", "score": 70, "reasoning": "ok"}], [])

            with patch('steps.update_horizon_scores.fetch_page_blocks') as mock_blocks, \
                 patch('steps.update_horizon_scores.generate_rubric') as mock_gen:
                result = handler(mock_pd)

                mock_blocks.assert_not_called()
                mock_gen.assert_not_called()

            assert result["rubric_source"] == "cache"

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_rubric_regenerated_when_horizons_changed(
        self, mock_meta, mock_parse, mock_blocks, mock_gen,
        mock_query, mock_score_all, mock_update_all, mock_pd
    ):
        """When horizons page has a new last_edited_time, regenerate the rubric."""
        with patch.dict(os.environ, self.ENV, clear=True):
            mock_pd.state["horizons_last_edited_at"] = "2024-06-01T12:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Old rubric"
            mock_pd.state["last_run_at"] = None  # force full scan

            # Horizons page was edited
            mock_meta.return_value = {"last_edited_time": "2024-06-15T08:00:00.000Z"}
            mock_blocks.return_value = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
            mock_parse.return_value = "New purpose statement"
            mock_gen.return_value = "New rubric text"
            mock_query.return_value = [
                {"id": "t1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "T1"}]}}}
            ]
            mock_score_all.return_value = [{"task_id": "t1", "score": 90, "reasoning": "great"}]
            mock_update_all.return_value = ([{"task_id": "t1", "score": 90, "reasoning": "great"}], [])

            result = handler(mock_pd)

            mock_gen.assert_called_once()
            assert result["rubric_source"] == "regenerated"
            assert mock_pd.state["rubric_cache"] == "New rubric text"
            assert mock_pd.state["horizons_last_edited_at"] == "2024-06-15T08:00:00.000Z"


class TestIncrementalQueries:
    """Tests for incremental task query, deduplication, and full scan trigger."""

    ENV = {
        "NOTION_API_TOKEN": "test_token",
        "NOTION_DATABASE_ID": "test_db",
        "NOTION_HORIZONS_PAGE_ID": "test_page",
        "ANTHROPIC_API_KEY": "test_key",
    }

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks_unscored')
    @patch('steps.update_horizon_scores.query_tasks_incremental')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_deduplication_task_in_both_queries_scored_once(
        self, mock_meta, mock_delta, mock_backlog,
        mock_score_all, mock_update_all, mock_pd
    ):
        """A task appearing in both delta and backlog queries is scored only once."""
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        with patch.dict(os.environ, self.ENV, clear=True):
            mock_pd.state["horizons_last_edited_at"] = "2024-01-01T00:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Cached rubric"
            mock_pd.state["last_run_at"] = recent
            mock_pd.state["last_full_scan_at"] = recent
            mock_pd.state["scoring_formula_version"] = SCORING_FORMULA_VERSION

            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}

            shared_task = {"id": "dup_task", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "Dup"}]}}}  # noqa: E501
            unique_delta = {"id": "delta_only", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "Delta"}]}}}  # noqa: E501
            unique_backlog = {"id": "backlog_only", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "Backlog"}]}}}  # noqa: E501

            mock_delta.return_value = [shared_task, unique_delta]
            mock_backlog.return_value = [shared_task, unique_backlog]

            mock_score_all.return_value = [
                {"task_id": "dup_task", "score": 50, "reasoning": "ok"},
                {"task_id": "delta_only", "score": 60, "reasoning": "ok"},
                {"task_id": "backlog_only", "score": 40, "reasoning": "ok"},
            ]
            mock_update_all.return_value = (
                [
                    {"task_id": "dup_task", "score": 50, "reasoning": "ok"},
                    {"task_id": "delta_only", "score": 60, "reasoning": "ok"},
                    {"task_id": "backlog_only", "score": 40, "reasoning": "ok"},
                ],
                [],
            )

            result = handler(mock_pd)

            assert result["scan_type"] == "incremental"
            assert result["delta_count"] == 2
            assert result["backlog_count"] == 2
            # 3 unique tasks after dedup (dup_task counted once)
            assert result["tasks_scored"] == 3

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_full_scan_when_last_full_scan_over_30_days(
        self, mock_meta, mock_query, mock_score_all, mock_update_all, mock_pd
    ):
        """Full scan triggered when last_full_scan_at is >30 days ago."""
        with patch.dict(os.environ, self.ENV, clear=True):
            mock_pd.state["horizons_last_edited_at"] = "2024-01-01T00:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Cached rubric"
            mock_pd.state["last_run_at"] = "2024-06-01T00:00:00.000Z"
            # Last full scan was 60 days ago
            mock_pd.state["last_full_scan_at"] = "2024-01-01T00:00:00.000Z"

            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}
            mock_query.return_value = [
                {"id": "t1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "T1"}]}}}
            ]
            mock_score_all.return_value = [{"task_id": "t1", "score": 70, "reasoning": "ok"}]
            mock_update_all.return_value = ([{"task_id": "t1", "score": 70, "reasoning": "ok"}], [])

            result = handler(mock_pd)

            assert result["scan_type"] == "full"
            mock_query.assert_called_once()
            assert mock_pd.state["last_full_scan_at"] is not None

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_full_scan_forced_when_scoring_formula_version_changed(
        self, mock_meta, mock_query, mock_score_all, mock_update_all, mock_pd
    ):
        """A scoring-formula version bump forces a full rescan even with a fresh
        last_full_scan_at — otherwise old- and new-formula scores would coexist
        in the same database for up to 30 days (Codex pre-PR finding, ENG-1756)."""
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        with patch.dict(os.environ, self.ENV, clear=True):
            mock_pd.state["horizons_last_edited_at"] = "2024-01-01T00:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Cached rubric"
            mock_pd.state["last_run_at"] = recent
            mock_pd.state["last_full_scan_at"] = recent  # fresh — would normally stay incremental
            mock_pd.state["scoring_formula_version"] = 1  # stale — deployed formula is v2

            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}
            mock_query.return_value = [
                {"id": "t1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "T1"}]}}}
            ]
            mock_score_all.return_value = [{"task_id": "t1", "score": 70, "reasoning": "ok"}]
            mock_update_all.return_value = ([{"task_id": "t1", "score": 70, "reasoning": "ok"}], [])

            result = handler(mock_pd)

            assert result["scan_type"] == "full"
            mock_query.assert_called_once()
            assert mock_pd.state["scoring_formula_version"] == SCORING_FORMULA_VERSION

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks_unscored')
    @patch('steps.update_horizon_scores.query_tasks_incremental')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_incremental_stays_incremental_when_scoring_version_matches(
        self, mock_meta, mock_delta, mock_backlog, mock_score_all, mock_update_all, mock_pd
    ):
        """Matching scoring_formula_version takes the normal incremental path."""
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        with patch.dict(os.environ, self.ENV, clear=True):
            mock_pd.state["horizons_last_edited_at"] = "2024-01-01T00:00:00.000Z"
            mock_pd.state["rubric_cache"] = "Cached rubric"
            mock_pd.state["last_run_at"] = recent
            mock_pd.state["last_full_scan_at"] = recent
            mock_pd.state["scoring_formula_version"] = SCORING_FORMULA_VERSION

            mock_meta.return_value = {"last_edited_time": "2024-01-01T00:00:00.000Z"}
            mock_delta.return_value = []
            mock_backlog.return_value = []

            result = handler(mock_pd)

            assert result["scan_type"] == "incremental"

    @patch('steps.update_horizon_scores.update_scores_parallel')
    @patch('steps.update_horizon_scores.score_all_batches_parallel')
    @patch('steps.update_horizon_scores.query_tasks')
    @patch('steps.update_horizon_scores.generate_rubric')
    @patch('steps.update_horizon_scores.fetch_page_blocks')
    @patch('steps.update_horizon_scores.parse_blocks_to_text')
    @patch('steps.update_horizon_scores.fetch_page_metadata')
    def test_first_run_no_state_behaves_as_full_scan(
        self, mock_meta, mock_parse, mock_blocks, mock_gen,
        mock_query, mock_score_all, mock_update_all, mock_pd
    ):
        """First run (empty state) should do full scan and regenerate rubric."""
        with patch.dict(os.environ, self.ENV, clear=True):
            # pd.state is empty (first run)
            mock_meta.return_value = {"last_edited_time": "2024-06-01T12:00:00.000Z"}
            mock_blocks.return_value = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
            mock_parse.return_value = "Purpose: Be awesome"
            mock_gen.return_value = "Fresh rubric"
            mock_query.return_value = [
                {"id": "t1", "properties": {"Task name": {"type": "title", "title": [{"plain_text": "T1"}]}}}
            ]
            mock_score_all.return_value = [{"task_id": "t1", "score": 80, "reasoning": "good"}]
            mock_update_all.return_value = ([{"task_id": "t1", "score": 80, "reasoning": "good"}], [])

            result = handler(mock_pd)

            assert result["scan_type"] == "full"
            assert result["rubric_source"] == "regenerated"
            mock_gen.assert_called_once()
            mock_query.assert_called_once()
            # State should be populated after run
            assert mock_pd.state["last_run_at"] is not None
            assert mock_pd.state["rubric_cache"] == "Fresh rubric"
            assert mock_pd.state["last_full_scan_at"] is not None

    def test_query_tasks_unscored_filter_shape(self):
        """query_tasks_unscored must include Horizon Score is_empty in the filter."""
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_response.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_response

        query_tasks_unscored("db_123", {"Authorization": "Bearer tok"}, session=mock_session)

        call_args = mock_session.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args.kwargs["json"]
        and_conditions = payload["filter"]["and"]

        # Find the Horizon Score filter
        hs_filters = [f for f in and_conditions if f.get("property") == "Horizon Score"]
        assert len(hs_filters) == 1
        assert hs_filters[0] == {"property": "Horizon Score", "number": {"is_empty": True}}


class TestDueDateFilterProperty:
    """ENG-1933: the live Notion property is "Due Date", not "Due" — a filter
    keyed on "Due" 400s against the real database. Guards all three query
    functions plus query_tasks' Python-side fallback filter regressing back
    to the wrong key."""

    def _filter_payload_from_mock(self, mock_session):
        call_args = mock_session.post.call_args
        return call_args[1]["json"] if "json" in call_args[1] else call_args.kwargs["json"]

    def test_query_tasks_unscored_uses_due_date_property(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_session.post.return_value = mock_response

        query_tasks_unscored("db_123", {"Authorization": "Bearer tok"}, session=mock_session)

        payload = self._filter_payload_from_mock(mock_session)
        and_conditions = payload["filter"]["and"]
        due_filters = [f for f in and_conditions if f.get("property") in ("Due", "Due Date")]
        assert len(due_filters) == 1
        assert due_filters[0] == {"property": "Due Date", "date": {"is_empty": True}}

    def test_query_tasks_incremental_uses_due_date_property(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_session.post.return_value = mock_response

        query_tasks_incremental(
            "db_123", {"Authorization": "Bearer tok"}, "2024-01-01T00:00:00.000Z", session=mock_session
        )

        payload = self._filter_payload_from_mock(mock_session)
        and_conditions = payload["filter"]["and"]
        due_filters = [f for f in and_conditions if f.get("property") in ("Due", "Due Date")]
        assert len(due_filters) == 1
        assert due_filters[0] == {"property": "Due Date", "date": {"is_empty": True}}

    def test_query_tasks_full_scan_uses_due_date_property(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_session.post.return_value = mock_response

        query_tasks("db_123", {"Authorization": "Bearer tok"}, session=mock_session)

        payload = self._filter_payload_from_mock(mock_session)
        and_conditions = payload["filter"]["and"]
        due_filters = [f for f in and_conditions if f.get("property") in ("Due", "Due Date")]
        assert len(due_filters) == 1
        assert due_filters[0] == {"property": "Due Date", "date": {"is_empty": True}}

    def test_query_tasks_fallback_filters_on_due_date_key(self):
        """The compound-filter-failed fallback re-filters in Python — it must
        read the same "Due Date" key the live API returns, not "Due" (which
        is always absent, so the old code silently kept every task)."""
        mock_session = MagicMock()
        compound_response = MagicMock()
        compound_response.post_effect = None
        fallback_response = MagicMock()
        fallback_response.json.return_value = {
            "results": [
                {"id": "has_due", "properties": {"Due Date": {"date": {"start": "2025-01-01"}}}},
                {"id": "no_due", "properties": {"Due Date": {"date": None}}},
            ],
            "has_more": False,
        }
        mock_session.post.side_effect = [Exception("compound filter unsupported"), fallback_response]

        tasks = query_tasks("db_123", {"Authorization": "Bearer tok"}, session=mock_session)

        ids = [t["id"] for t in tasks]
        assert ids == ["no_due"]


class TestResolveProjectRelation:
    """ENG-1933 AC4: a Project relation must resolve to the target page's
    name + Status, not just a bare "[Related to N project(s)]" count."""

    def test_resolves_name_and_status_type(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "A Lo Cubano Boulder Fest"}]},
                "Status": {"type": "status", "status": {"name": "In Progress"}},
            }
        }
        mock_session.get.return_value = mock_response

        name, status = resolve_project_relation("proj_1", {"Authorization": "Bearer tok"}, session=mock_session)
        assert name == "A Lo Cubano Boulder Fest"
        assert status == "In Progress"

    def test_resolves_status_as_select_type(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Old Project"}]},
                "Status": {"type": "select", "select": {"name": "Canceled"}},
            }
        }
        mock_session.get.return_value = mock_response

        name, status = resolve_project_relation("proj_2", {"Authorization": "Bearer tok"}, session=mock_session)
        assert name == "Old Project"
        assert status == "Canceled"

    def test_caches_repeat_lookups(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Cached Project"}]}}
        }
        mock_session.get.return_value = mock_response
        cache = {}

        resolve_project_relation("proj_3", {"Authorization": "Bearer tok"}, session=mock_session, cache=cache)
        resolve_project_relation("proj_3", {"Authorization": "Bearer tok"}, session=mock_session, cache=cache)

        assert mock_session.get.call_count == 1

    def test_fails_open_on_error(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = Exception("network error")

        name, status = resolve_project_relation("proj_missing", {"Authorization": "Bearer tok"}, session=mock_session)
        assert name == ""
        assert status == ""


class TestExtractTaskInfoProjectResolution:
    """ENG-1933 AC4: extract_task_info resolves a Project relation via
    resolve_project_relation when headers are supplied, and falls back to
    the prior placeholder when they are not (unit-test isolation path)."""

    def test_resolves_project_relation_when_headers_supplied(self):
        task = {
            "id": "task_1",
            "properties": {
                "Project": {"type": "relation", "relation": [{"id": "proj_1"}]},
            },
        }
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Flagship Project"}]},
                "Status": {"type": "status", "status": {"name": "In Progress"}},
            }
        }
        mock_session.get.return_value = mock_response

        info = extract_task_info(task, {"Authorization": "Bearer tok"}, mock_session, {})
        assert info["project"] == "Flagship Project (In Progress)"

    def test_falls_back_to_placeholder_without_headers(self):
        task = {
            "id": "task_1",
            "properties": {
                "Project": {"type": "relation", "relation": [{"id": "proj_1"}, {"id": "proj_2"}]},
            },
        }
        info = extract_task_info(task)
        assert info["project"] == "[Related to 2 project(s)]"

    def test_falls_back_to_placeholder_when_resolution_yields_no_name(self):
        task = {
            "id": "task_1",
            "properties": {
                "Project": {"type": "relation", "relation": [{"id": "proj_1"}]},
            },
        }
        mock_session = MagicMock()
        mock_session.get.side_effect = Exception("boom")

        info = extract_task_info(task, {"Authorization": "Bearer tok"}, mock_session, {})
        assert info["project"] == "[Related to 1 project(s)]"
