"""
Update Notion Horizon Scores

Scores tasks (0-100) based on alignment with user's "Horizons of Focus"
(Purpose, Values, Vision, Goals, Areas of Focus) using Claude AI.

Usage: Copy-paste into a Pipedream Python step
Required Environment Variables:
  - NOTION_API_TOKEN: Notion internal integration token
  - NOTION_DATABASE_ID: Task database ID
  - NOTION_HORIZONS_PAGE_ID: Horizons of Focus page ID
  - ANTHROPIC_API_KEY: Claude API key
"""
import os
import requests
import time
import json
import random
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# --- Custom Exceptions ---
class HorizonScoringError(Exception):
    """Raised when horizon scoring fails critically.

    This exception is used to FAIL the Pipedream job loudly instead of
    silently continuing with partial or no results.
    """


# --- Configuration ---
NOTION_API_VERSION = "2022-06-28"
CLAUDE_MODEL = "claude-opus-4-5-20251101"
BATCH_SIZE = 40  # Increased for fewer batches (was 25)
LIST_VALUES = ["Next Actions", "Waiting For", "Someday/Maybe"]

# Parallelization settings - tuned for speed within rate limits
SCORING_WORKERS = 6   # Parallel Claude API calls (10 caused timeouts)
UPDATE_WORKERS = 10   # Parallel Notion updates (was 8)
FETCH_WORKERS = 3     # Parallel initial data fetches
BLOCK_DELETE_WORKERS = 5  # Parallel block deletions

# --- API Endpoints ---
NOTION_API_BASE = "https://api.notion.com/v1"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def retry_with_backoff(request_func, max_retries=5):
    """
    Execute request with exponential backoff for rate limits and timeouts.

    Handles:
    - HTTP 429 (Too Many Requests) and 503 (Service Unavailable) errors
    - Connection timeouts and read timeouts
    Retries with exponential backoff. Respects Retry-After header.
    """
    for attempt in range(max_retries):
        try:
            response = request_func()
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as e:
            # Retry on timeouts and connection errors
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"Timeout/connection error: {e}. Waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (429, 503) and attempt < max_retries - 1:
                retry_after = e.response.headers.get('Retry-After')
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = (2 ** attempt) + random.uniform(0, 1)
                else:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"Rate limited. Waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"Max retries ({max_retries}) exceeded")


def fetch_page_blocks(page_id, headers, session=None):
    """
    Recursively fetch all blocks from a Notion page.

    Args:
        page_id: Notion page ID
        headers: API headers
        session: Optional requests.Session for connection pooling

    Returns a list of block objects with their content.
    """
    http = session or requests
    all_blocks = []
    base_url = f"{NOTION_API_BASE}/blocks/{page_id}/children"
    start_cursor = None

    while True:
        params = {"page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor

        response = retry_with_backoff(
            lambda u=base_url, p=params: http.get(u, headers=headers, params=p, timeout=60)
        )
        data = response.json()
        blocks = data.get("results", [])
        all_blocks.extend(blocks)

        # Recursively fetch children for blocks that have them
        for block in blocks:
            if block.get("has_children"):
                block_id = block.get("id")
                child_blocks = fetch_page_blocks(block_id, headers, session)
                all_blocks.extend(child_blocks)

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return all_blocks


def fetch_page_metadata(page_id, headers, session=None):
    """
    Fetch page metadata (not blocks) via GET /v1/pages/{page_id}.

    Returns the raw response JSON including last_edited_time.
    """
    http = session or requests
    url = f"{NOTION_API_BASE}/pages/{page_id}"

    response = retry_with_backoff(
        lambda: http.get(url, headers=headers, timeout=60)
    )
    return response.json()


def query_tasks_incremental(database_id, headers, since_ts, session=None):
    """
    Query tasks edited or created since `since_ts` (ISO timestamp).

    Combines the existing List + empty Due filter with a last_edited_time > since_ts filter.

    Args:
        database_id: Notion database ID
        headers: API headers
        since_ts: ISO 8601 timestamp string — only return tasks edited after this time
        session: Optional requests.Session for connection pooling

    Returns a list of task objects.
    """
    http = session or requests
    all_tasks = []
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    start_cursor = None

    or_conditions = [
        {"property": "List", "status": {"equals": v}} for v in LIST_VALUES
    ]

    filter_payload = {
        "filter": {
            "and": [
                {"or": or_conditions},
                {"property": "Due", "date": {"is_empty": True}},
                {"timestamp": "last_edited_time", "last_edited_time": {"after": since_ts}},
            ]
        },
        "page_size": 100,
    }

    while True:
        if start_cursor:
            filter_payload["start_cursor"] = start_cursor

        response = retry_with_backoff(
            lambda fp=filter_payload: http.post(url, headers=headers, json=fp, timeout=60)
        )
        data = response.json()
        tasks = data.get("results", [])
        all_tasks.extend(tasks)

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        time.sleep(0.1)

    return all_tasks


def query_tasks_unscored(database_id, headers, session=None):
    """
    Query tasks that have no Horizon Score (backlog of unscored tasks).

    Combines List + empty Due filter with Horizon Score is_empty.

    Args:
        database_id: Notion database ID
        headers: API headers
        session: Optional requests.Session for connection pooling

    Returns a list of task objects.
    """
    http = session or requests
    all_tasks = []
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    start_cursor = None

    or_conditions = [
        {"property": "List", "status": {"equals": v}} for v in LIST_VALUES
    ]

    filter_payload = {
        "filter": {
            "and": [
                {"or": or_conditions},
                {"property": "Due", "date": {"is_empty": True}},
                {"property": "Horizon Score", "number": {"is_empty": True}},
            ]
        },
        "page_size": 100,
    }

    while True:
        if start_cursor:
            filter_payload["start_cursor"] = start_cursor

        response = retry_with_backoff(
            lambda fp=filter_payload: http.post(url, headers=headers, json=fp, timeout=60)
        )
        data = response.json()
        tasks = data.get("results", [])
        all_tasks.extend(tasks)

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        time.sleep(0.1)

    return all_tasks


def find_inline_databases(blocks):
    """
    Find child_database blocks in a list of blocks and return their IDs.

    Args:
        blocks: List of Notion block objects (from fetch_page_blocks)

    Returns:
        List of dicts with 'id' and 'title' for each inline database
    """
    databases = []
    for block in blocks:
        if block.get("type") == "child_database":
            db_info = block.get("child_database", {})
            databases.append({
                "id": block.get("id"),
                "title": db_info.get("title", "Untitled")
            })
    return databases


def fetch_in_progress_goals(database_id, headers, session=None):
    """
    Query goals database for In Progress items with Focus Areas and descriptions.

    Args:
        database_id: The Notion database ID
        headers: Notion API headers
        session: Optional requests.Session for connection pooling

    Returns:
        List of goal dicts with name, description, focus_areas, and focus_count,
        sorted by focus_count descending (higher leverage first)
    """
    http = session or requests
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"

    # Start with Status filter, fallback inline if it fails (no redundant test call)
    base_payload = {
        "filter": {
            "property": "Status",
            "status": {"equals": "In Progress"}
        }
    }
    use_filter = True

    goals = []
    start_cursor = None

    while True:
        payload = dict(base_payload) if use_filter else {}
        if start_cursor:
            payload["start_cursor"] = start_cursor

        try:
            response = retry_with_backoff(
                lambda p=payload: http.post(url, headers=headers, json=p, timeout=60)
            )
            data = response.json()
        except Exception as e:
            # Filter failed - retry with no filter (inline fallback, no redundant test)
            if use_filter:
                print(f"    Status filter failed ({e}), fetching all goals...")
                use_filter = False
                payload = {}
                if start_cursor:
                    payload["start_cursor"] = start_cursor
                response = retry_with_backoff(
                    lambda p=payload: http.post(url, headers=headers, json=p, timeout=60)
                )
                data = response.json()
            else:
                raise

        for page in data.get("results", []):
            props = page.get("properties", {})

            # Extract goal title
            title_prop = props.get("Name", {})
            title = ""
            if title_prop.get("type") == "title":
                title = extract_text_from_rich_text(title_prop.get("title", []))

            # Extract goal description (rich_text property)
            description = ""
            desc_prop = props.get("Description", {})
            if desc_prop.get("type") == "rich_text":
                description = extract_text_from_rich_text(desc_prop.get("rich_text", []))
                # Truncate long descriptions to keep prompts manageable
                if len(description) > 500:
                    description = description[:500] + "..."

            # Extract Focus Areas (multi-select)
            focus_areas = []
            focus_prop = props.get("Focus Area", {})
            if focus_prop.get("type") == "multi_select":
                focus_areas = [opt.get("name") for opt in focus_prop.get("multi_select", [])]

            if title:  # Only include goals with titles
                goals.append({
                    "name": title,
                    "description": description,
                    "focus_areas": focus_areas,
                    "focus_count": len(focus_areas)
                })

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    # Sort by focus_count descending (goals touching more areas = higher priority)
    goals.sort(key=lambda g: g["focus_count"], reverse=True)
    return goals


def extract_text_from_rich_text(rich_text_array):
    """Extract plain text from Notion rich_text array."""
    if not rich_text_array:
        return ""
    return "".join(item.get("plain_text", "") for item in rich_text_array)


def fetch_core_values(database_id, headers, session=None):
    """
    Query Core Values database and extract values.

    Args:
        database_id: The Notion database ID for Core Values
        headers: Notion API headers
        session: Optional requests.Session for connection pooling

    Returns:
        List of core value names
    """
    http = session or requests
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"

    response = retry_with_backoff(
        lambda: http.post(url, headers=headers, json={}, timeout=60)
    )

    values = []
    for page in response.json().get("results", []):
        props = page.get("properties", {})
        # Extract value name (title property)
        name_prop = props.get("Name", {})
        name = ""
        if name_prop.get("type") == "title":
            name = extract_text_from_rich_text(name_prop.get("title", []))
        if name:
            values.append(name)

    return values


def parse_blocks_to_text(blocks):
    """
    Convert Notion blocks to readable text format.

    Preserves structure with headings and content.
    """
    text_parts = []

    for block in blocks:
        block_type = block.get("type")
        block_data = block.get(block_type, {})

        if block_type in ("heading_1", "heading_2", "heading_3"):
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            prefix = "#" * int(block_type[-1])
            text_parts.append(f"\n{prefix} {text}\n")

        elif block_type == "paragraph":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            if text.strip():
                text_parts.append(text)

        elif block_type == "bulleted_list_item":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            text_parts.append(f"• {text}")

        elif block_type == "numbered_list_item":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            text_parts.append(f"- {text}")

        elif block_type == "to_do":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            checked = block_data.get("checked", False)
            checkbox = "[x]" if checked else "[ ]"
            text_parts.append(f"{checkbox} {text}")

        elif block_type == "toggle":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            text_parts.append(f"▸ {text}")

        elif block_type == "quote":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            text_parts.append(f"> {text}")

        elif block_type == "callout":
            text = extract_text_from_rich_text(block_data.get("rich_text", []))
            icon = block_data.get("icon", {}).get("emoji", "")
            text_parts.append(f"{icon} {text}")

        elif block_type == "divider":
            text_parts.append("\n---\n")

    return "\n".join(text_parts)


def get_score_color(score_text):
    """
    Return Notion color based on score range.

    Args:
        score_text: Text containing a score range like "90-100" or "0-9"

    Returns:
        Notion color string
    """
    if "90-100" in score_text or "90+" in score_text:
        return "green"
    elif "75-89" in score_text:
        return "blue"
    elif "50-74" in score_text:
        return "default"
    elif "30-49" in score_text:
        return "orange"
    elif "10-29" in score_text:
        return "gray"
    elif "0-9" in score_text or "0-29" in score_text:
        return "red"
    return "default"


def create_table_block(lines):
    """
    Create a Notion table block from pipe-separated lines.

    Args:
        lines: List of lines with | separators (first line is header)

    Returns:
        Notion table block dict
    """
    if not lines:
        return None

    # Parse rows
    rows = []
    for line in lines:
        # Split by | and clean up cells
        cells = [cell.strip() for cell in line.split('|') if cell.strip()]
        if cells:
            rows.append(cells)

    if not rows:
        return None

    # Determine table width from first row
    table_width = len(rows[0])

    # Create table rows
    table_rows = []
    for i, row in enumerate(rows):
        # Pad row if needed
        while len(row) < table_width:
            row.append("")

        # Create cells with formatting
        cells = []
        for j, cell in enumerate(row[:table_width]):
            # First row is header (bold)
            if i == 0:
                cells.append([{
                    "type": "text",
                    "text": {"content": cell},
                    "annotations": {"bold": True}
                }])
            else:
                # Apply color to score column (first column in score tables)
                color = get_score_color(cell) if j == 0 else "default"
                cell_content = [{
                    "type": "text",
                    "text": {"content": cell}
                }]
                if color != "default":
                    cell_content[0]["annotations"] = {"color": color}
                cells.append(cell_content)

        table_rows.append({
            "type": "table_row",
            "table_row": {"cells": cells}
        })

    return {
        "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows
        }
    }


def create_callout_block(text, emoji="💡"):
    """
    Create a Notion callout block with emoji icon.

    Args:
        text: Callout content text
        emoji: Emoji to use as icon

    Returns:
        Notion callout block dict
    """
    # Choose background color based on emoji
    color_map = {
        "💡": "yellow_background",
        "📋": "blue_background",
        "⚠️": "orange_background",
        "✅": "green_background",
        "❌": "red_background",
        "📌": "purple_background",
        "🎯": "blue_background",
    }
    color = color_map.get(emoji, "gray_background")

    return {
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color
        }
    }


def markdown_to_notion_blocks(markdown_text):
    """
    Convert markdown text to Notion block objects.

    Handles:
    - Headings (# ## ###) with emoji support
    - Bullet lists (- or *)
    - Numbered lists (1. 2. etc)
    - Dividers (---)
    - Tables ([TABLE]...[/TABLE] with | separators)
    - Callouts ([CALLOUT:emoji]...[/CALLOUT])
    - Bold text (**text**)
    - Regular paragraphs
    """
    blocks = []
    lines = markdown_text.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            i += 1
            continue

        # Divider
        if stripped == '---' or stripped == '***' or stripped == '___':
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue

        # Table block: [TABLE] ... [/TABLE]
        if stripped == '[TABLE]':
            table_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != '[/TABLE]':
                table_line = lines[i].strip()
                if table_line and '|' in table_line:
                    table_lines.append(table_line)
                i += 1
            i += 1  # Skip [/TABLE]
            table_block = create_table_block(table_lines)
            if table_block:
                blocks.append(table_block)
            continue

        # Callout block: [CALLOUT:emoji] text [/CALLOUT]
        if stripped.startswith('[CALLOUT:'):
            # Extract emoji
            emoji_end = stripped.find(']')
            if emoji_end > 9:
                emoji = stripped[9:emoji_end]
                # Get text - may span multiple lines until [/CALLOUT]
                text_start = emoji_end + 1
                callout_text = stripped[text_start:].strip()

                # Check if [/CALLOUT] is on same line
                if '[/CALLOUT]' in callout_text:
                    callout_text = callout_text.replace('[/CALLOUT]', '').strip()
                else:
                    # Collect multi-line callout
                    i += 1
                    while i < len(lines) and '[/CALLOUT]' not in lines[i]:
                        callout_text += ' ' + lines[i].strip()
                        i += 1
                    if i < len(lines) and '[/CALLOUT]' in lines[i]:
                        callout_text += ' ' + lines[i].replace('[/CALLOUT]', '').strip()

                callout_text = callout_text.strip()
                if callout_text:
                    blocks.append(create_callout_block(callout_text, emoji))
            i += 1
            continue

        # Headings (with emoji support)
        if stripped.startswith('### '):
            blocks.append({
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": stripped[4:]}}]}
            })
        elif stripped.startswith('## '):
            blocks.append({
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": stripped[3:]}}]}
            })
        elif stripped.startswith('# '):
            blocks.append({
                "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        # Bullet lists
        elif stripped.startswith('- ') or stripped.startswith('* '):
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        # Numbered lists (1. 2. etc)
        elif len(stripped) > 2 and stripped[0].isdigit() and '. ' in stripped[:4]:
            content = stripped.split('. ', 1)[1] if '. ' in stripped else stripped
            blocks.append({
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": content}}]}
            })
        # Bold headers without # (e.g., **Score Range:**)
        elif stripped.startswith('**') and stripped.endswith('**'):
            # Remove ** markers
            bold_text = stripped[2:-2]
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": bold_text}, "annotations": {"bold": True}}]}  # noqa: E501
            })
        # Regular paragraphs
        else:
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped}}]}
            })

        i += 1

    return blocks


def save_rubric_to_notion(rubric_text, page_id, headers, session=None):
    """
    Clear existing blocks and save new rubric to a Notion page.

    Args:
        rubric_text: The rubric markdown text
        page_id: The Notion page ID to update
        headers: Notion API headers
        session: Optional requests.Session for connection pooling

    Returns:
        True if successful
    """
    http = session or requests

    # 1. Fetch existing blocks to delete
    existing_blocks = fetch_page_blocks(page_id, headers, session)
    print(f"    Clearing {len(existing_blocks)} existing blocks...")

    # 2. Delete existing blocks IN PARALLEL for speed
    def delete_block(block_id: str) -> bool:
        """Delete a single block."""
        url = f"{NOTION_API_BASE}/blocks/{block_id}"
        try:
            retry_with_backoff(
                lambda url=url: http.delete(url, headers=headers, timeout=60)
            )
            return True
        except Exception as e:
            print(f"    Warning: Failed to delete block {block_id}: {e}")
            return False

    block_ids = [b.get("id") for b in existing_blocks if b.get("id")]
    if block_ids:
        with ThreadPoolExecutor(max_workers=BLOCK_DELETE_WORKERS) as executor:
            list(executor.map(delete_block, block_ids))

    # 3. Convert rubric to Notion blocks
    new_blocks = markdown_to_notion_blocks(rubric_text)
    print(f"    Writing {len(new_blocks)} new blocks...")

    # 4. Append new blocks in batches (Notion limit: 100 blocks per request)
    url = f"{NOTION_API_BASE}/blocks/{page_id}/children"
    for i in range(0, len(new_blocks), 100):
        batch = new_blocks[i:i + 100]
        retry_with_backoff(
            lambda batch=batch: http.patch(url, headers=headers, json={"children": batch}, timeout=60)
        )
        time.sleep(0.1)  # Reduced from 0.3s - retry_with_backoff handles rate limits

    return True


def call_claude(prompt, anthropic_key, max_tokens=4096, session=None):
    """
    Call Claude API with the given prompt.

    Args:
        prompt: The prompt to send to Claude
        anthropic_key: Anthropic API key
        max_tokens: Maximum tokens in response
        session: Optional requests.Session for connection pooling

    Returns the response text or raises an exception.
    """
    http = session or requests
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    response = retry_with_backoff(
        lambda: http.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=120)
    )

    data = response.json()
    content = data.get("content", [])
    if content and len(content) > 0:
        return content[0].get("text", "")
    raise Exception(f"Unexpected Claude response format: {data}")


def generate_rubric(horizons_content, anthropic_key, session=None):
    """
    Generate a scoring rubric based on the Horizons of Focus content.

    Uses GTD (Getting Things Done) framework for prioritization.
    Returns the rubric as a formatted string with special markers for Notion blocks.
    """
    prompt = f"""You are helping implement a David Allen GTD (Getting Things Done) prioritization system.

## Context
In GTD, "Horizons of Focus" represent different altitudes of perspective:
- Horizon 5: Purpose/Principles - Life purpose and core values
- Horizon 4: Vision - Long-term vision of ideal life/work
- Horizon 3: Goals - 1-2 year objectives
- Horizon 2: Areas of Focus - Key areas of responsibility (roles, accountabilities)
- Horizon 1: Projects - Multi-step outcomes
- Ground: Next Actions - Individual tasks

## Your Task
Create a scoring rubric (0-100) that evaluates how well a task aligns with the person's stated identity and priorities from their Horizons of Focus document below.  # noqa: E501

The "Horizon Score" serves as a **priority indicator** - helping the user decide which flexible tasks (no due date) to tackle next based on who they have said they want to be.  # noqa: E501

## Document Structure
The Horizons document includes:
- Purpose and Core Values
- Vision
- Areas of Focus: Spirituality, Personal Development, Health, Romance, Family, Business & Career, Finances, Fun & Recreation, Social, Humanitarian  # noqa: E501
- In Progress Goals (each tagged with relevant Focus Areas)

## Scoring Guidelines
Goals spanning MULTIPLE Focus Areas indicate higher strategic leverage.

---
HORIZONS OF FOCUS:
{horizons_content}
---

## OUTPUT FORMAT (CRITICAL - Follow exactly)

You MUST format your response using these special markers for beautiful Notion formatting:

1. Use emojis in headers: # 🎯 Title
2. Use [CALLOUT:emoji] text [/CALLOUT] for important callout boxes
3. Use [TABLE] and [/TABLE] for tables with | separators
4. Use --- for section dividers

Here is the EXACT structure to follow:

# 🎯 Horizon Score Rubric

[CALLOUT:📋] This rubric evaluates how well your tasks align with your stated Horizons of Focus - your purpose, values, vision, and goals. Use it to prioritize flexible tasks based on who you want to be. [/CALLOUT]  # noqa: E501

---

## 📊 Score Ranges

[TABLE]
Score | Meaning | Criteria
90-100 | 🔥 High Leverage | Directly advances a goal spanning 3+ Focus Areas - "This IS who I want to be"
75-89 | ✅ Goal-Aligned | Directly advances a focused goal (1-2 Focus Areas) - "This supports my goals"
50-74 | 📁 Area Support | Supports an Area of Focus with active goals - "This maintains important areas"
30-49 | 💭 Values-Aligned | Aligns with values but no direct goal connection - "This feels right but isn't urgent"
10-29 | 🔧 Maintenance | Necessary but not identity-aligned - "Needs doing but doesn't define me"
0-9 | ⚠️ Misaligned | Distraction from priorities - "This pulls me away from who I want to be"
[/TABLE]

---

## 🎯 Your High-Leverage Goals (90-100 points)

[CALLOUT:💡] Tasks that directly advance these goals score 90-100. These goals span multiple Focus Areas, meaning progress here creates compounding benefits. [/CALLOUT]  # noqa: E501

(List their actual goals that span 3+ focus areas with descriptions)

---

## ✅ Your Focused Goals (75-89 points)

(List their goals that span 1-2 focus areas)

---

## 📁 Your Active Focus Areas (50-74 points)

[TABLE]
Focus Area | Active Goals | Examples
(area) | (count) | (example tasks)
[/TABLE]

---

## 💭 Values & Purpose Alignment (30-49 points)

(Reference their actual core values and purpose statement)

---

## 🔧 Maintenance Tasks (10-29 points)

(General life maintenance that doesn't advance goals)

---

## ⚠️ Potential Distractions (0-9 points)

[CALLOUT:⚠️] Be cautious of tasks that don't align with any stated goal, value, or focus area. These may be distractions pulling you away from your priorities. [/CALLOUT]  # noqa: E501

Create this rubric based on THIS person's specific horizons. Be concrete - use their actual goals, values, and areas."""

    print("Generating scoring rubric from Horizons of Focus...")
    rubric = call_claude(prompt, anthropic_key, session=session)
    print(f"Rubric generated ({len(rubric)} characters)")
    return rubric


def query_tasks(database_id, headers, session=None):
    """
    Query all tasks with List property in target values and no due date.

    Args:
        database_id: Notion database ID
        headers: API headers
        session: Optional requests.Session for connection pooling

    Returns a list of task objects with their properties.
    """
    http = session or requests
    all_tasks = []
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    start_cursor = None

    # Build explicit filter structure for List property AND empty Due date
    # Only score tasks without due dates (flexible tasks needing prioritization)
    or_conditions = [
        {"property": "List", "status": {"equals": "Next Actions"}},
        {"property": "List", "status": {"equals": "Waiting For"}},
        {"property": "List", "status": {"equals": "Someday/Maybe"}}
    ]

    filter_payload = {
        "filter": {
            "and": [
                {"or": or_conditions},
                {"property": "Due", "date": {"is_empty": True}}
            ]
        },
        "page_size": 100
    }

    # Debug: print the filter being sent
    print(f"  Filter: {json.dumps(filter_payload['filter'])}")

    use_fallback = False

    while True:
        if start_cursor:
            filter_payload["start_cursor"] = start_cursor

        try:
            response = retry_with_backoff(
                lambda fp=filter_payload: http.post(url, headers=headers, json=fp, timeout=60)
            )
            data = response.json()
        except Exception as e:
            if not use_fallback:
                print(f"  Compound filter failed ({e}), trying simpler filter...")
                # Fallback: just filter by List, then filter Due in Python
                filter_payload = {
                    "filter": {"or": or_conditions},
                    "page_size": 100
                }
                use_fallback = True
                continue
            else:
                raise

        tasks = data.get("results", [])

        # If using fallback, filter out tasks with due dates in Python
        if use_fallback:
            original_count = len(tasks)
            tasks = [t for t in tasks
                     if not t.get("properties", {}).get("Due", {}).get("date")]
            print(f"  Fetched {original_count} tasks, {len(tasks)} without due dates (total: {len(all_tasks) + len(tasks)})")  # noqa: E501
        else:
            print(f"  Fetched {len(tasks)} tasks (total: {len(all_tasks) + len(tasks)})")

        all_tasks.extend(tasks)

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        if start_cursor:
            filter_payload["start_cursor"] = start_cursor
        # Small delay between pagination requests - reduced from 0.3s
        time.sleep(0.1)

    return all_tasks


def extract_task_info(task):
    """
    Extract relevant information from a task for scoring.

    Returns a dict with task details.
    """
    properties = task.get("properties", {})
    task_info = {
        "id": task.get("id"),
        "title": "",
        "list": "",
        "project": "",
        "area": "",
        "priority": "",
        "due_date": "",
        "notes": "",
    }

    # Extract title
    title_prop = properties.get("Task name", properties.get("Name", {}))
    if title_prop.get("type") == "title":
        title_array = title_prop.get("title", [])
        task_info["title"] = extract_text_from_rich_text(title_array)

    # Extract List (status type)
    list_prop = properties.get("List", {})
    if list_prop.get("type") == "status":
        status = list_prop.get("status")
        if status:
            task_info["list"] = status.get("name", "")

    # Extract Project (relation or select)
    project_prop = properties.get("Project", properties.get("Projects", {}))
    if project_prop.get("type") == "relation":
        relations = project_prop.get("relation", [])
        if relations:
            task_info["project"] = f"[Related to {len(relations)} project(s)]"
    elif project_prop.get("type") == "select":
        select = project_prop.get("select")
        if select:
            task_info["project"] = select.get("name", "")

    # Extract Area (select or relation)
    area_prop = properties.get("Area", properties.get("Areas", {}))
    if area_prop.get("type") == "select":
        select = area_prop.get("select")
        if select:
            task_info["area"] = select.get("name", "")
    elif area_prop.get("type") == "relation":
        relations = area_prop.get("relation", [])
        if relations:
            task_info["area"] = f"[Related to {len(relations)} area(s)]"

    # Extract Priority (select)
    priority_prop = properties.get("Priority", {})
    if priority_prop.get("type") == "select":
        select = priority_prop.get("select")
        if select:
            task_info["priority"] = select.get("name", "")

    # Extract Due Date
    due_prop = properties.get("Due", properties.get("Due Date", {}))
    if due_prop.get("type") == "date":
        date_obj = due_prop.get("date")
        if date_obj:
            task_info["due_date"] = date_obj.get("start", "")

    # Extract Notes/Description (rich_text)
    notes_prop = properties.get("Notes", properties.get("Description", {}))
    if notes_prop.get("type") == "rich_text":
        notes_array = notes_prop.get("rich_text", [])
        task_info["notes"] = extract_text_from_rich_text(notes_array)[:500]  # Limit length

    return task_info


def score_tasks_batch(tasks, rubric, anthropic_key, session=None):
    """
    Score a batch of tasks using Claude.

    Args:
        tasks: List of task info dicts
        rubric: Scoring rubric string
        anthropic_key: Anthropic API key
        session: Optional requests.Session for connection pooling

    Returns a list of {task_id, score, reasoning} dicts.
    """
    # Format tasks for the prompt
    tasks_text = ""
    for i, task in enumerate(tasks, 1):
        tasks_text += f"""
Task {i}:
- Title: {task['title']}
- List: {task['list']}
- Project: {task['project'] or 'None'}
- Area: {task['area'] or 'None'}
- Priority: {task['priority'] or 'None'}
- Due Date: {task['due_date'] or 'None'}
- Notes: {task['notes'] or 'None'}
"""

    prompt = f"""You are scoring tasks based on how well they align with a person's Horizons of Focus.

SCORING RUBRIC:
{rubric}

TASKS TO SCORE:
{tasks_text}

For each task, provide a score from 0-100 based on alignment with the Horizons of Focus.
- 90-100: Directly advances a stated goal or is critical to purpose
- 70-89: Strongly supports an area of focus or contributes to vision
- 50-69: Moderately aligned with values or supports goals indirectly
- 30-49: Neutral maintenance task or loosely connected
- 0-29: Misaligned, distraction, or contrary to stated priorities

Return exactly {len(tasks)} scores, one per task, in the same order as listed above.

Return your response as a JSON array with this exact format:
[
  {{"score": 85, "reasoning": "Brief explanation"}},
  ...
]

IMPORTANT: Return ONLY the JSON array, no other text."""

    response_text = call_claude(prompt, anthropic_key, session=session)

    # Parse JSON response - FAIL LOUDLY on parse errors
    try:
        # Find JSON array in response
        start_idx = response_text.find('[')
        end_idx = response_text.rfind(']') + 1
        if start_idx == -1 or end_idx == 0:
            raise HorizonScoringError(
                f"No JSON array found in Claude response. "
                f"Response was: {response_text[:500]}..."
            )
        json_str = response_text[start_idx:end_idx]
        scores = json.loads(json_str)

        # Validate score count matches task count
        if len(scores) != len(tasks):
            print(
                f"Warning: Score count mismatch: got {len(scores)} scores "
                f"for {len(tasks)} tasks. Truncating to min."
            )
            scores = scores[:len(tasks)]

        # Inject known-good task IDs by position (never trust Claude with IDs)
        for i, score_entry in enumerate(scores):
            score_entry["task_id"] = tasks[i]["id"]

        return scores
    except json.JSONDecodeError as e:
        raise HorizonScoringError(
            f"Failed to parse Claude response as JSON: {e}. "
            f"Response was: {response_text[:500]}..."
        ) from e


def update_horizon_score(task_id, score, headers, session=None):
    """
    Update a task's Horizon Score property in Notion.

    Args:
        task_id: Notion page ID
        score: Score value (0-100)
        headers: API headers
        session: Optional requests.Session for connection pooling

    Returns True on success, False on failure.
    """
    http = session or requests
    url = f"{NOTION_API_BASE}/pages/{task_id}"
    payload = {
        "properties": {
            "Horizon Score": {"number": score}
        }
    }

    try:
        retry_with_backoff(
            lambda: http.patch(url, headers=headers, json=payload, timeout=60)
        )
        return True
    except Exception as e:
        print(f"  Error updating task {task_id}: {e}")
        return False


def score_all_batches_parallel(task_batches, rubric, anthropic_key, session=None):
    """
    Score multiple batches of tasks in parallel using ThreadPoolExecutor.

    Args:
        task_batches: List of task info lists (each batch is a list of task dicts)
        rubric: The scoring rubric string
        anthropic_key: Anthropic API key
        session: Optional requests.Session for connection pooling

    Returns:
        List of score dicts with task_id, score, and reasoning

    Raises:
        HorizonScoringError: If ANY batch fails to score (fail loudly)
    """
    all_scores = []
    failed_batches = []
    total_batches = len(task_batches)

    print(f"  Scoring {total_batches} batches with {SCORING_WORKERS} parallel workers...")

    with ThreadPoolExecutor(max_workers=SCORING_WORKERS) as executor:
        # Submit all batches for parallel execution
        future_to_batch = {
            executor.submit(score_tasks_batch, batch, rubric, anthropic_key, session): i
            for i, batch in enumerate(task_batches)
        }

        # Collect results as they complete
        for future in as_completed(future_to_batch):
            batch_num = future_to_batch[future]
            try:
                scores = future.result()
                all_scores.extend(scores)
                print(f"  Batch {batch_num + 1}/{total_batches} complete ({len(scores)} scores)")
            except Exception as e:
                print(f"  Batch {batch_num + 1}/{total_batches} failed: {e}")
                failed_batches.append((batch_num + 1, str(e)))

    # FAIL LOUDLY if ANY batch failed
    if failed_batches:
        failed_info = ", ".join([f"Batch {num}: {err}" for num, err in failed_batches])
        raise HorizonScoringError(
            f"{len(failed_batches)}/{total_batches} batches failed to score. "
            f"Failures: {failed_info}"
        )

    return all_scores


def update_scores_parallel(scores, headers, session=None):
    """
    Update Notion pages with scores in parallel using ThreadPoolExecutor.

    Args:
        scores: List of score dicts with task_id, score, reasoning
        headers: Notion API headers
        session: Optional requests.Session for connection pooling

    Returns:
        Tuple of (successful_updates, errors)
    """
    successful = []
    errors = []
    total = len(scores)

    def update_single(score_data):
        """Update a single task and return result."""
        task_id = score_data.get("task_id")
        raw_score = score_data.get("score")
        reasoning = score_data.get("reasoning", "")

        if not task_id or raw_score is None:
            return None, None, False, "Missing task_id or score", score_data

        try:
            score = max(0, min(100, int(raw_score)))
        except (ValueError, TypeError):
            return task_id, None, False, f"Invalid score value: {raw_score}", score_data

        success = update_horizon_score(task_id, score, headers, session)
        return task_id, score, success, reasoning, None

    print(f"  Updating {total} tasks with {UPDATE_WORKERS} parallel workers...")

    with ThreadPoolExecutor(max_workers=UPDATE_WORKERS) as executor:
        futures = [executor.submit(update_single, s) for s in scores]

        completed = 0
        for future in as_completed(futures):
            task_id, score, success, reasoning, error_data = future.result()
            completed += 1

            if error_data:
                errors.append({
                    "task_id": task_id,
                    "error": reasoning,
                    "data": error_data
                })
            elif success:
                successful.append({
                    "task_id": task_id,
                    "score": score,
                    "reasoning": reasoning
                })
                if completed % 25 == 0 or completed == total:
                    print(f"  Progress: {completed}/{total} updates complete")
            else:
                errors.append({
                    "task_id": task_id,
                    "score": score,
                    "error": "Failed to update Notion"
                })

    # FAIL LOUDLY if error rate exceeds 20%
    if total > 0:
        error_rate = len(errors) / total
        if error_rate > 0.20:
            raise HorizonScoringError(
                f"Update failure rate too high: {len(errors)}/{total} ({error_rate:.0%}) failed. "
                f"Threshold is 20%. First few errors: {errors[:3]}"
            )

    return successful, errors


def handler(pd: "pipedream"):  # noqa: F821
    """Main entry point for Pipedream step.

    Supports incremental scoring via pd.state persistence:
    - Caches rubric and only regenerates when Horizons page changes
    - Delta queries for recently-edited tasks + unscored backlog
    - Monthly full scan for drift detection
    """

    # --- 1. Get Credentials from Environment ---
    notion_token = os.environ.get("NOTION_API_TOKEN")
    if not notion_token:
        raise Exception("NOTION_API_TOKEN environment variable not set")

    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not database_id:
        raise Exception("NOTION_DATABASE_ID environment variable not set")

    horizons_page_id = os.environ.get("NOTION_HORIZONS_PAGE_ID")
    if not horizons_page_id:
        raise Exception("NOTION_HORIZONS_PAGE_ID environment variable not set")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise Exception("ANTHROPIC_API_KEY environment variable not set")

    # Optional: page to save the rubric for review
    rubric_page_id = os.environ.get("NOTION_RUBRIC_PAGE_ID")

    # Optional: inline database IDs for Goals and Core Values
    goals_db_id = os.environ.get("NOTION_GOALS_DB_ID")
    core_values_db_id = os.environ.get("NOTION_CORE_VALUES_DB_ID")

    # --- 2. Set up headers and sessions for connection pooling ---
    notion_headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }

    # Create sessions for connection pooling (reuses TCP connections)
    notion_session = requests.Session()
    notion_session.headers.update(notion_headers)
    anthropic_session = requests.Session()

    successful_updates = []
    errors = []
    rubric_source = "regenerated"
    scan_type = "incremental"
    delta_count = 0
    backlog_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        # --- 3. Rubric decision (cheap metadata check) ---
        print("Step 1: Checking if Horizons of Focus have changed...")
        page_meta = fetch_page_metadata(horizons_page_id, notion_headers, notion_session)
        current_edited_at = page_meta.get("last_edited_time")

        cached_edited_at = pd.state.get("horizons_last_edited_at")
        cached_rubric = pd.state.get("rubric_cache")

        if cached_edited_at == current_edited_at and cached_rubric:
            # Horizons unchanged and cache exists — skip block fetch + Claude rubric call
            rubric = cached_rubric
            rubric_source = "cache"
            print("  Rubric: reused from cache")
        else:
            # Horizons changed OR cache missing — full regeneration
            reason = "horizons changed" if cached_edited_at != current_edited_at else "cache missing"
            print(f"  Rubric: regenerating ({reason})")

            # Fetch horizons content, core values, and goals in parallel
            def fetch_horizons():
                blocks = fetch_page_blocks(horizons_page_id, notion_headers, notion_session)
                content = parse_blocks_to_text(blocks)
                return blocks, content

            def fetch_values_safe():
                if not core_values_db_id:
                    return None
                try:
                    return fetch_core_values(core_values_db_id, notion_headers, notion_session)
                except Exception as e:
                    print(f"  Warning: Could not fetch Core Values: {e}")
                    return None

            def fetch_goals_safe():
                if not goals_db_id:
                    return None
                try:
                    return fetch_in_progress_goals(goals_db_id, notion_headers, notion_session)
                except Exception as e:
                    print(f"  Warning: Could not fetch Goals: {e}")
                    return None

            with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
                horizons_future = executor.submit(fetch_horizons)
                values_future = executor.submit(fetch_values_safe)
                goals_future = executor.submit(fetch_goals_safe)

                blocks, horizons_content = horizons_future.result()
                core_values = values_future.result()
                goals = goals_future.result()

            print(f"  Fetched {len(blocks)} blocks, {len(horizons_content)} characters of content")

            if not horizons_content.strip():
                raise HorizonScoringError("Horizons of Focus page is empty or has no readable content")

            # Append Core Values
            if core_values:
                horizons_content += "\n\n## Core Values\n"
                for value in core_values:
                    horizons_content += f"• {value}\n"
                print(f"  Added {len(core_values)} core values")
            elif core_values_db_id:
                print("  No core values found")
            else:
                print("  NOTION_CORE_VALUES_DB_ID not set, skipping Core Values")

            # Append In Progress Goals
            if goals:
                horizons_content += "\n\n## In Progress Goals (ordered by cross-area impact)\n"
                for goal in goals:
                    areas_str = ", ".join(goal["focus_areas"]) if goal["focus_areas"] else "No specific area"
                    desc = goal.get("description", "")
                    if desc:
                        horizons_content += f"• {goal['name']} [Focus Areas: {areas_str}]\n  Description: {desc}\n"
                    else:
                        horizons_content += f"• {goal['name']} [Focus Areas: {areas_str}]\n"
                print(f"  Added {len(goals)} in-progress goals")
            elif goals_db_id:
                print("  No in-progress goals found")
            else:
                print("  NOTION_GOALS_DB_ID not set, skipping Goals")

            # Generate rubric
            print("\n  Generating scoring rubric with Claude...")
            rubric = generate_rubric(horizons_content, anthropic_key, anthropic_session)

            # Save rubric to Notion page (if configured)
            if rubric_page_id:
                print("  Saving rubric to Notion page...")
                try:
                    save_rubric_to_notion(rubric, rubric_page_id, notion_headers, notion_session)
                    print(f"  Rubric saved: https://notion.so/{rubric_page_id.replace('-', '')}")
                except Exception as e:
                    print(f"  Warning: Failed to save rubric to Notion: {e}")

            # Update rubric cache in state
            pd.state["horizons_last_edited_at"] = current_edited_at
            pd.state["rubric_cache"] = rubric

        # --- 4. Task query decision ---
        last_run_at = pd.state.get("last_run_at")
        last_full_scan_at = pd.state.get("last_full_scan_at")

        # Determine if full scan is needed
        needs_full_scan = False
        if last_full_scan_at is None:
            needs_full_scan = True
        else:
            try:
                last_full_dt = datetime.fromisoformat(last_full_scan_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last_full_dt > timedelta(days=30):
                    needs_full_scan = True
            except (ValueError, AttributeError):
                needs_full_scan = True

        if needs_full_scan or last_run_at is None:
            # Full scan path
            scan_type = "full"
            if last_run_at is None:
                print("\nStep 2: First run — performing full scan...")
            else:
                print("\nStep 2: Full scan triggered (monthly drift detection)")
            tasks = query_tasks(database_id, notion_headers, notion_session)
            delta_count = len(tasks)
            backlog_count = 0
            pd.state["last_full_scan_at"] = now_iso
            print(f"  Found {len(tasks)} tasks (full scan)")
        else:
            # Incremental path: delta + unscored backlog
            print(f"\nStep 2: Incremental query (since {last_run_at})...")

            with ThreadPoolExecutor(max_workers=2) as executor:
                delta_future = executor.submit(
                    query_tasks_incremental, database_id, notion_headers, last_run_at, notion_session
                )
                backlog_future = executor.submit(
                    query_tasks_unscored, database_id, notion_headers, notion_session
                )
                delta_tasks = delta_future.result()
                backlog_tasks = backlog_future.result()

            delta_count = len(delta_tasks)
            backlog_count = len(backlog_tasks)

            # Deduplicate by task ID
            seen_ids = set()
            tasks = []
            for task in delta_tasks + backlog_tasks:
                tid = task.get("id")
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    tasks.append(task)

            print(f"  Delta: {delta_count} tasks | Unscored backlog: {backlog_count} tasks | Union after dedup: {len(tasks)} tasks")  # noqa: E501

        if not tasks:
            pd.state["last_run_at"] = now_iso
            return {
                "status": "Completed",
                "message": "No tasks found matching filter criteria",
                "tasks_scored": 0,
                "successful_updates": [],
                "errors": [],
                "rubric_source": rubric_source,
                "scan_type": scan_type,
                "delta_count": delta_count,
                "backlog_count": backlog_count,
            }

        # --- 5. Extract task info for scoring ---
        print("\nStep 3: Extracting task information...")
        task_infos = [extract_task_info(task) for task in tasks]

        # --- 6. Score tasks in parallel batches ---
        print(f"\nStep 4: Scoring tasks in parallel batches of {BATCH_SIZE}...")
        task_batches = [
            task_infos[i:i + BATCH_SIZE]
            for i in range(0, len(task_infos), BATCH_SIZE)
        ]
        all_scores = score_all_batches_parallel(task_batches, rubric, anthropic_key, anthropic_session)
        print(f"  Received {len(all_scores)} scores from Claude")

        # --- 7. Update Notion with scores in parallel ---
        print("\nStep 5: Updating Horizon Scores in Notion (parallel)...")
        successful_updates, errors = update_scores_parallel(all_scores, notion_headers, notion_session)

    except HorizonScoringError:
        # FAIL LOUDLY - re-raise scoring errors to fail the Pipedream job
        raise
    except Exception as e:
        # FAIL LOUDLY - wrap unexpected errors and re-raise
        raise HorizonScoringError(f"Unexpected error during execution: {e}") from e
    finally:
        # Clean up sessions to release TCP connections
        notion_session.close()
        anthropic_session.close()

    # --- 8. Write state and return summary ---
    pd.state["last_run_at"] = now_iso

    status = "Completed" if not errors else "Partial"
    print("\n--- Processing Complete ---")
    print(f"Successfully updated: {len(successful_updates)}")
    print(f"Errors: {len(errors)}")

    return {
        "status": status,
        "tasks_scored": len(successful_updates),
        "successful_updates": successful_updates,
        "errors": errors,
        "rubric_preview": rubric[:500] if 'rubric' in dir() else None,
        "rubric_source": rubric_source,
        "scan_type": scan_type,
        "delta_count": delta_count,
        "backlog_count": backlog_count,
    }
