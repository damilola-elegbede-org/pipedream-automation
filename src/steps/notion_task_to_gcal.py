"""
Notion Task to Google Calendar Event Creator

Processes Notion task data from a Pipedream trigger and prepares it for
Google Calendar event creation. Skips tasks without due dates or those
that already have a Google Event ID (should be handled by update flow).

Usage: Copy-paste into a Pipedream Python step
"""
import logging

# Configure logging for Pipedream
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
TIMEZONE = "America/Denver"  # Mountain Time (handles MST/MDT automatically)


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

        except (TypeError, IndexError) as e:
            logger.warning(f"Error accessing key '{key}': {e}")
            return default
    return current


def is_datetime(date_str):
    """Check if string is a dateTime (contains 'T') vs date-only."""
    return bool(date_str and 'T' in date_str)


def generate_event_id(notion_page_id):
    """
    Generate an idempotent Google Calendar event ID from Notion Page ID.

    Google Calendar event IDs must:
    - Be 5-1024 characters
    - Contain only lowercase letters a-v and digits 0-9
    - Be unique per calendar

    We use the Notion Page ID (32 hex chars) and prefix with 'notion' to:
    - Ensure uniqueness (Notion IDs are unique)
    - Enable idempotent operations (same task = same event ID)
    - Prevent duplicates on workflow retries

    Args:
        notion_page_id: The Notion page ID (32 hex characters)

    Returns:
        A valid Google Calendar event ID, or None if page ID is invalid.
    """
    if not notion_page_id:
        return None

    # Clean the ID: remove hyphens, lowercase
    clean_id = notion_page_id.replace('-', '').lower()

    # Validate: should be 32 hex characters
    if len(clean_id) != 32:
        logger.warning(f"Notion page ID has unexpected length: {len(clean_id)}")
        return None

    # Google Calendar allows a-v (lowercase) and 0-9
    # Hex uses a-f and 0-9, so we're within bounds
    # Prefix with 'notion' to namespace our events
    event_id = f"notion{clean_id}"

    return event_id


def normalize_dates(start, end):
    """
    Ensure start and end are in the same format for Google Calendar.

    Google Calendar requires both start and end to use the same format:
    - Either both are date (all-day): "2025-12-22"
    - Or both are dateTime (timed): "2025-12-22T10:00:00"

    Args:
        start: The start date/datetime string from Notion
        end: The end date/datetime string from Notion (can be None)

    Returns:
        Tuple of (normalized_start, normalized_end) in consistent format
    """
    if end is None:
        return start, start

    start_is_datetime = is_datetime(start)
    end_is_datetime = is_datetime(end)

    if start_is_datetime == end_is_datetime:
        # Already consistent
        return start, end

    if start_is_datetime and not end_is_datetime:
        # Start is dateTime, end is date-only
        # Convert end to dateTime at end of day
        logger.info("Normalizing dates: start is dateTime, end is date-only")
        return start, f"{end}T23:59:59"
    else:
        # Start is date-only, end is dateTime
        # Convert start to dateTime at start of day
        logger.info("Normalizing dates: start is date-only, end is dateTime")
        return f"{start}T00:00:00", end


def handler(pd: "pipedream"):  # noqa: F821
    """
    Processes Notion task data from a Pipedream trigger, ensuring safe access
    to potentially missing data paths and handling create/update logic.
    """
    # --- 1. Safely extract data using the helper function ---
    trigger_event = safe_get(pd.steps, ["trigger", "event"], default={})
    properties = safe_get(trigger_event, ["properties"], default={})

    # Due Date information
    due_date_obj = safe_get(properties, ["Due Date", "date"])
    due_date_start = safe_get(due_date_obj, ["start"])
    due_date_end = safe_get(due_date_obj, ["end"])

    # Task Name information
    task_name_list = safe_get(properties, ["Task name", "title"], default=[])
    task_name = ""
    if task_name_list:
        task_name = safe_get(task_name_list, [0, "plain_text"], default="Untitled Task")
    else:
        task_name = "Untitled Task"

    # Google Event ID information
    google_event_id_list = safe_get(properties, ["Google Event ID", "rich_text"], default=[])

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

    # Exit if it looks like an existing event (should be handled by an update flow)
    if google_event_id_list:
        exit_message = f"Google Event ID exists -- Should be an update, skipping creation for: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # --- 3. Prepare data for event creation (if checks above passed) ---
    logger.info(f"Preparing to create event for task: '{task_name}'")

    # Normalize dates to ensure consistent format for Google Calendar
    final_start_date, final_end_date = normalize_dates(due_date_start, due_date_end)

    # Generate idempotency key for duplicate prevention
    idempotency_key = generate_event_id(notion_id)

    # Validate idempotency key was generated successfully
    if not idempotency_key:
        exit_message = f"Invalid or missing Notion Page ID -- Cannot generate event ID for task: '{task_name}'"
        logger.warning(exit_message)
        pd.flow.exit(exit_message)
        return

    # Log extracted details
    logger.info(f"Subject: {task_name}")
    logger.info(f"Start: {final_start_date}")
    logger.info(f"End: {final_end_date}")
    logger.info(f"TimeZone: {TIMEZONE}")
    logger.info(f"Notion ID: {notion_id}")
    logger.info(f"Notion URL: {notion_url}")
    logger.info(f"Idempotency Key (Event ID): {idempotency_key}")

    # Structure the return object for the next step (e.g., Google Calendar create event)
    ret_obj = {
        "GCal": {
            "Subject": task_name,
            "Start": final_start_date,
            "End": final_end_date,
            "TimeZone": TIMEZONE,
            "Update": False,
            "NotionId": notion_id,
            "Url": notion_url,
            "EventId": idempotency_key,  # Used for idempotent event creation
            "Description": f"Notion Task: {task_name}\nLink: {notion_url or 'N/A'}"
        }
    }

    # --- 4. Return data for use in subsequent steps ---
    return ret_obj
