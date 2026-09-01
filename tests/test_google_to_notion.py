"""
Tests for google_to_notion.py Pipedream step.
"""
import pytest  # noqa: F401
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from steps.google_to_notion import handler, safe_get, extract_notion_page_id, format_notion_date, is_notion_linked


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



class TestNotionDomainCutover:
    """ENG-2091: the reverse sync was dead from 2026-05-10 to 2026-09-01.

    The guard tested for the literal string "notion.so/". Notion changed the
    `url` property it returns from https://www.notion.so/... to
    https://app.notion.com/p/..., and notion_task_to_google.py writes that
    value verbatim into the task notes. Every task created after the cutover
    failed the guard and the workflow exited at step 1.

    Measured on D's Default List: 69 tasks with notion.so notes (due
    2022-09-17 -> 2026-05-10) and 58 with notion.com notes (2026-05-10 ->
    2026-09-27), of which 39 were open and invisible to the sync.

    WHY IT SURVIVED 16 WEEKS: every fixture in this file hardcoded
    www.notion.so, so the suite passed green over a guard that rejected 100%
    of current production input. These tests cover BOTH domains for exactly
    that reason — a fix asserted only against the old form can regress
    identically.
    """

    # The real notes body of Google task bWY2RkhzMHZla1pnTkVjVA, completed in
    # Google 2026-08-18 and still reading In Progress in Notion 13 days later.
    APP_NOTION_NOTES = (
        "Notion Task: Notify Heather and Toby that Fri Aug 21 SCF lessons are cancelled\n"
        "Link: https://app.notion.com/p/Notify-Heather-and-Toby-"
        "3be48b0cacbf8155a353cf5575eaf3c1"
    )
    LEGACY_NOTES = (
        "Notion Task: Legacy task\n"
        "Link: https://www.notion.so/Legacy-Page-3be48b0cacbf8155a353cf5575eaf3c1"
    )

    def _trigger(self, notes, status="needsAction"):
        return {"trigger": {"event": {
            "title": "T", "notes": notes, "status": status, "due": "2026-08-21T00:00:00.000Z",
        }}}

    def test_app_notion_com_is_not_skipped(self, mock_pd):
        """The regression itself: this exited at step 1 for 16 weeks."""
        mock_pd.steps = self._trigger(self.APP_NOTION_NOTES)
        result = handler(mock_pd)
        assert mock_pd.flow.exit_called is False, (
            "an app.notion.com task must not be skipped as 'no Notion URL'"
        )
        assert result["NotionUpdate"]["PageId"] == "3be48b0cacbf8155a353cf5575eaf3c1"

    def test_legacy_notion_so_still_works(self, mock_pd):
        """69 pre-cutover tasks still carry the old domain."""
        mock_pd.steps = self._trigger(self.LEGACY_NOTES)
        result = handler(mock_pd)
        assert mock_pd.flow.exit_called is False
        assert result["NotionUpdate"]["PageId"] == "3be48b0cacbf8155a353cf5575eaf3c1"

    def test_a_task_with_no_notion_link_is_still_skipped(self, mock_pd):
        """The guard must still do its job — this is not a blanket pass."""
        mock_pd.steps = self._trigger("Just a plain task, no link at all")
        handler(mock_pd)
        assert mock_pd.flow.exit_called is True
        assert "does not have a Notion URL" in mock_pd.flow.exit_message

    def test_completing_sets_status_as_well_as_list(self, mock_pd):
        """ENG-2091 second defect: List moved, Status did not, leaving
        half-closed tasks. 7 were fixed by hand."""
        mock_pd.steps = self._trigger(self.APP_NOTION_NOTES, status="completed")
        result = handler(mock_pd)
        assert result["NotionUpdate"]["ListValue"] == "Completed"
        assert result["NotionUpdate"]["StatusValue"] == "Done"

    def test_incomplete_leaves_status_untouched(self, mock_pd):
        """Google has two states, Notion has four. Mapping needsAction onto one
        of them would overwrite a deliberate Not Started or Archived."""
        mock_pd.steps = self._trigger(self.APP_NOTION_NOTES, status="needsAction")
        result = handler(mock_pd)
        assert result["NotionUpdate"]["ListValue"] == "Next Actions"
        assert result["NotionUpdate"]["StatusValue"] is None

    def test_extractor_resolves_both_domains(self):
        """The extractor was always domain-agnostic; only the guard was not."""
        for notes in (self.APP_NOTION_NOTES, self.LEGACY_NOTES):
            assert extract_notion_page_id(notes) == "3be48b0cacbf8155a353cf5575eaf3c1"


class TestNotionHostSanitization:
    """CodeQL py/incomplete-url-substring-sanitization (high) on PR #40.

    The first version of the ENG-2091 fix tested `"notion.com/" in text` and,
    in the fallback, matched `https?://[^\\s]*notion\\.(?:so|com)/`. Both accept
    a URL that merely CONTAINS the string — an attacker-controlled host with
    "notion.com/" in its path or query passes. Same defect class as ENG-1712.

    The guard decides whether a Google task is Notion-originated, and its
    output feeds a page id straight into a Notion write. A task whose notes an
    attacker can influence should not be able to spoof that.
    """

    @pytest.mark.parametrize("url", [
        "https://www.notion.so/Page-3be48b0cacbf8155a353cf5575eaf3c1",
        "https://app.notion.com/p/Page-3be48b0cacbf8155a353cf5575eaf3c1",
        "https://notion.so/3be48b0cacbf8155a353cf5575eaf3c1",
        "https://NOTION.COM/p/3be48b0cacbf8155a353cf5575eaf3c1",
    ])
    def test_genuine_notion_hosts_accepted(self, url):
        assert is_notion_linked(f"Link: {url}") is True

    @pytest.mark.parametrize("url", [
        # the string appears, the HOST is not Notion
        "https://evil.example/notion.com/3be48b0cacbf8155a353cf5575eaf3c1",
        "https://evil.example/?redirect=https://notion.so/abc",
        # suffix-only checks would accept these
        "https://notion.so.evil.example/p/3be48b0cacbf8155a353cf5575eaf3c1",
        "https://evil-notion.com/p/3be48b0cacbf8155a353cf5575eaf3c1",
        "https://fakenotion.so/p/3be48b0cacbf8155a353cf5575eaf3c1",
    ])
    def test_lookalike_hosts_rejected(self, url):
        assert is_notion_linked(f"Link: {url}") is False, (
            f"{url} is not a Notion URL — its host is not a Notion domain"
        )

    def test_a_lookalike_does_not_reach_the_notion_write(self, mock_pd):
        """End to end: a spoofed host must exit, not return a PageId."""
        mock_pd.steps = {"trigger": {"event": {
            "title": "Spoofed",
            "notes": "Link: https://evil.example/notion.so/3be48b0cacbf8155a353cf5575eaf3c1",
        }}}
        handler(mock_pd)
        assert mock_pd.flow.exit_called is True
