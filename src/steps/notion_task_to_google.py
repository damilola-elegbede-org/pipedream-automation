"""
Notion Task to Google Task Creator

Processes Notion task data from a Pipedream trigger and prepares it for
Google Tasks creation. Skips tasks without due dates or those that already
have a Google Task ID (should be handled by update flow).

Usage: Copy-paste into a Pipedream Python step
"""
import logging

# Configure logging for Pipedream
logger = logging.getLogger()
logger.setLevel(logging.INFO)


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
    for key in keys:
        try:
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, list):
                if isinstance(key, int) and 0 <= key < len(current):
                    current = current[key]
                else:
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


def format_due_date(date_str):
    """
    Format date for Google Tasks API.

    Google Tasks only uses the date portion - time is discarded.
    Must be RFC 3339 format: "2024-01-20T00:00:00.000Z"

    Args:
        date_str: The date/datetime string from Notion (e.g., "2024-01-20" or "2024-01-20T10:00:00")

    Returns:
        RFC 3339 formatted date string for Google Tasks
    """
    if not date_str:
        return None

    # Strip any time component and format as midnight UTC
    date_only = date_str.split('T')[0]
    return f"{date_only}T00:00:00.000Z"


def handler(pd: "pipedream"):  # noqa: F821
    """
    Processes Notion task data from a Pipedream trigger and prepares it for
    Google Tasks creation.
    """
    # --- 1. Safely extract data using the helper function ---
    trigger_event = safe_get(pd.steps, ["trigger", "event"], default={})
    properties = safe_get(trigger_event, ["properties"], default={})

    # Due Date information
    due_date_obj = safe_get(properties, ["Due Date", "date"])
    due_date_start = safe_get(due_date_obj, ["start"])

    # Task Name information
    task_name = safe_get(properties, ["Task name", "title", 0, "plain_text"], default="Untitled Task")

    # Google Task ID information (check if task already synced)
    google_task_id_list = safe_get(properties, ["Google Task ID", "rich_text"], default=[])

    # Other event details
    notion_id = safe_get(trigger_event, ["id"])
    notion_url = safe_get(trigger_event, ["url"])

    # --- 2. Check conditions and decide action ---

    # Exit if Due Date is missing
    if due_date_start is None:
        exit_message = f"Due Date is missing -- Skipping task: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # Exit if it already has a Google Task ID (should be handled by update flow)
    if google_task_id_list:
        exit_message = f"Google Task ID exists -- Should be an update, skipping creation for: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # --- 3. Prepare data for task creation ---
    logger.info(f"Preparing to create Google Task for: '{task_name}'")

    # Format due date for Google Tasks API (RFC 3339, date-only)
    due_date = format_due_date(due_date_start)

    # Build notes with Notion URL for reverse sync identification
    notes = f"Notion Task: {task_name}\nLink: {notion_url or 'N/A'}"

    # Log extracted details
    logger.info(f"Title: {task_name}")
    logger.info(f"Due: {due_date}")
    logger.info(f"Notion ID: {notion_id}")
    logger.info(f"Notion URL: {notion_url}")

    # Structure the return object for the next step (Google Tasks - Create Task)
    ret_obj = {
        "GTask": {
            "Title": task_name,
            "Notes": notes,  # Contains Notion URL for reverse sync
            "Due": due_date,
            "NotionId": notion_id,
            "NotionUrl": notion_url
        }
    }

    # --- 4. Return data for use in subsequent steps ---
    return ret_obj
