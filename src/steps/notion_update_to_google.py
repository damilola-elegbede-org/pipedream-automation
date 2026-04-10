"""
Notion Update to Google Task Update

Processes Notion page update events and prepares data for updating the
corresponding Google Task. Only processes tasks that already have a
Google Task ID. Also syncs completion status (Notion "List" field).

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
    Processes Notion page update data from a Pipedream trigger for updating
    a Google Task, ensuring safe access to data.
    """
    # --- 1. Safely extract data using the helper function ---
    # Base path adjustments: using ["page"] where appropriate for update triggers
    trigger_event_page = safe_get(pd.steps, ["trigger", "event", "page"], default={})
    properties = safe_get(trigger_event_page, ["properties"], default={})

    # Debug: Log available property names to help troubleshoot
    logger.info(f"Available properties: {list(properties.keys()) if isinstance(properties, dict) else 'N/A'}")

    # Task Name
    task_name = safe_get(properties, ["Task name", "title", 0, "plain_text"], default="Untitled Task")

    # Due Date information
    due_date_start = safe_get(properties, ["Due Date", "date", "start"])

    # Google Task ID - Crucial for update
    google_task_id_prop = safe_get(properties, ["Google Task ID"], default={})
    logger.info(f"Google Task ID property: {google_task_id_prop}")
    task_id = safe_get(google_task_id_prop, ["rich_text", 0, "plain_text"])
    logger.info(f"Extracted task_id: '{task_id}'")

    # List field for completion status (Notion "status" type, not "select")
    list_value = safe_get(properties, ["List", "status", "name"])
    is_completed = list_value == "Completed"
    logger.info(f"List value: '{list_value}', is_completed: {is_completed}")

    # Notion Page URL
    notion_url = safe_get(trigger_event_page, ["url"])

    # --- 2. Check prerequisites for an update ---

    # Exit if Due Date is missing (required for tasks)
    if due_date_start is None:
        exit_message = f"Due Date is missing -- Cannot update task: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # Exit if Google Task ID is missing (required to know *which* task to update)
    if not task_id:
        exit_message = f"Google Task ID is missing -- Cannot update, should be a create task for: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # --- 3. Prepare data for task update ---
    logger.info(f"Preparing to update task '{task_id}' for: '{task_name}'")

    # Format due date for Google Tasks API (RFC 3339, date-only)
    due_date = format_due_date(due_date_start)

    # Build notes with Notion URL for reverse sync identification
    notes = f"Notion Task: {task_name}\nLink: {notion_url or 'N/A'}"

    # Log extracted details
    logger.info(f"Task ID: {task_id}")
    logger.info(f"Title: {task_name}")
    logger.info(f"Due: {due_date}")
    logger.info(f"Completed: {is_completed}")
    logger.info(f"Notion URL: {notion_url}")

    # Structure the return object for the next step (Google Tasks - Update Task)
    ret_obj = {
        "GTask": {
            "TaskId": task_id,
            "Title": task_name,
            "Notes": notes,  # Contains Notion URL for reverse sync
            "Due": due_date,
            "Completed": is_completed  # Maps to Google Task status
        }
    }

    # --- 4. Return data for use in subsequent steps ---
    return ret_obj
