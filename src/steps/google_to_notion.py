"""
Google Task to Notion Sync

Processes Google Tasks triggers, validates if they originated from Notion
(by checking the notes field for a Notion URL), and extracts the Notion
Page ID for syncing updates back to Notion. Also syncs completion status.

Usage: Copy-paste into a Pipedream Python step
"""
import logging
import re

# Configure logging for Pipedream
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Regex pattern for Notion Page ID (32 hex characters)
# Matches the ID at the end of a Notion URL, which can be:
# - After the last hyphen: https://www.notion.so/Page-Title-abc123def456...
# - With query params: https://www.notion.so/Page-abc123...?pvs=4
NOTION_PAGE_ID_PATTERN = re.compile(r'([a-f0-9]{32})(?:\?|$)', re.IGNORECASE)


def safe_get(data, keys, default=None):
    """
    Safely accesses nested dictionary keys or list indices.

    Args:
        data: The dictionary or list to access.
        keys: A list of keys/indices representing the path.
        default: The value to return if the path is not found or an error occurs.

    Returns:
        The value at the nested path or the default value.
    """
    current = data
    if not isinstance(keys, list):
        keys = [keys]

    for key in keys:
        try:
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, list):
                if isinstance(key, int) and 0 <= key < len(current):
                    current = current[key]
                else:
                    if isinstance(key, int):
                        logger.warning(f"Invalid list index '{key}' for list: {current}")
                    return default
            else:
                logger.warning(f"Cannot access key '{key}' in non-dict/list item: {current}")
                return default

            if current is None:
                return default

        except (TypeError, IndexError, AttributeError) as e:
            logger.warning(f"Error accessing key '{key}': {e}")
            return default
    return current


def extract_notion_page_id(text):
    """
    Extracts the Notion Page ID from text containing a Notion URL.

    Notion Page IDs are 32 hexadecimal characters. They appear at the end
    of the URL, typically after the last hyphen in the page title slug.

    Args:
        text: Text that may contain a Notion URL (e.g., task notes).

    Returns:
        The 32-character page ID if found, None otherwise.
    """
    if not text:
        return None

    # Try regex pattern first (most reliable)
    match = NOTION_PAGE_ID_PATTERN.search(text)
    if match:
        return match.group(1)

    # Fallback: try extracting after last hyphen if there's a notion.so URL
    try:
        if "notion.so/" in text:
            # Find the URL portion
            url_match = re.search(r'https?://[^\s]+notion\.so/[^\s]+', text)
            if url_match:
                url = url_match.group(0)
                # Remove query params
                clean_url = url.split('?')[0]
                parts = clean_url.rsplit('-', 1)
                if len(parts) > 1 and parts[-1]:
                    potential_id = parts[-1]
                    # Validate it looks like a hex ID (at least 20 chars)
                    if len(potential_id) >= 20 and all(c in '0123456789abcdefABCDEF' for c in potential_id):
                        return potential_id
    except Exception:
        logger.debug("Fallback extraction failed for Notion URL", exc_info=True)

    return None


def validate_notion_page_id(page_id):
    """
    Validate that a Notion Page ID is exactly 32 hex characters.

    Handles both formatted (with hyphens) and unformatted IDs.

    Args:
        page_id: The extracted page ID string.

    Returns:
        The cleaned 32-character page ID if valid, None otherwise.
    """
    if not page_id:
        return None

    # Remove any hyphens (some IDs may be formatted with dashes)
    cleaned = page_id.lower().replace('-', '')

    # Validate: exactly 32 hexadecimal characters
    if len(cleaned) == 32 and all(c in '0123456789abcdef' for c in cleaned):
        return cleaned

    logger.warning(f"Invalid Notion Page ID format: '{page_id}' (cleaned: '{cleaned}', length: {len(cleaned)})")
    return None


def format_notion_date(due_date):
    """
    Format Google Tasks due date for Notion.

    Google Tasks due dates are RFC 3339 format like "2024-01-20T00:00:00.000Z"
    but only the date portion is meaningful.

    Args:
        due_date: The due date string from Google Tasks.

    Returns:
        Date string in YYYY-MM-DD format for Notion, or None.
    """
    if not due_date:
        return None

    # Extract just the date portion (before 'T')
    return due_date.split('T')[0]


def handler(pd: "pipedream"):  # noqa: F821
    """
    Processes Google Tasks triggers, checks if they originated from Notion,
    and extracts relevant details including the Notion Page ID from the notes.
    Also syncs completion status.
    """
    task_data = safe_get(pd.steps, ["trigger", "event"], default={})

    # --- 1. Validate if the task is Notion-originated ---
    notes = safe_get(task_data, ["notes"])
    task_title = safe_get(task_data, ["title"], default="Untitled Task")

    # Check if notes contain a Notion URL
    if not notes or "notion.so/" not in notes:
        exit_message = f"Task '{task_title}' does not have a Notion URL in notes. Skipping."
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    logger.info(f"Processing Notion-linked task: '{task_title}'")

    # --- 2. Extract and Validate Notion Page ID from Notes ---
    raw_page_id = extract_notion_page_id(notes)
    page_id = validate_notion_page_id(raw_page_id)

    if not page_id:
        exit_message = f"Could not reliably extract/validate Notion Page ID from notes for task '{task_title}'. Raw extraction: '{raw_page_id}'. Skipping."  # noqa: E501
        logger.warning(exit_message)
        pd.flow.exit(exit_message)
        return

    logger.info(f"Extracted and validated Notion Page ID: {page_id}")

    # --- 3. Extract Task Status (for completion sync) ---
    task_status = safe_get(task_data, ["status"])  # "completed" or "needsAction"
    logger.info(f"Task status: {task_status}")

    # Map Google Tasks status to Notion List values
    if task_status == "completed":
        list_value = "Completed"
    else:
        list_value = "Next Actions"

    logger.info(f"Mapped to Notion List value: {list_value}")

    # --- 4. Extract Due Date ---
    due_date = safe_get(task_data, ["due"])
    notion_due_date = format_notion_date(due_date)
    logger.info(f"Due date: {notion_due_date}")

    # --- 5. Prepare Return Value ---
    ret_val = {
        "NotionUpdate": {
            "PageId": page_id,
            "ListValue": list_value,  # For Notion "List" field
            "DueDate": {
                "start": notion_due_date,
                "end": None
            } if notion_due_date else None
        }
    }

    logger.info(f"Returning: {ret_val}")

    # --- 6. Return data for use in future steps ---
    return ret_val
