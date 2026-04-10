"""
Notion Update to Google Calendar Update

Processes Notion page update events and prepares data for updating the
corresponding Google Calendar event. Only processes tasks that already
have a Google Event ID.

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
    Processes Notion page update data from a Pipedream trigger for updating
    a Google Calendar event, ensuring safe access to data.
    """
    # --- 1. Safely extract data using the helper function ---
    # Base path adjustments: using ["page"] where appropriate
    trigger_event_page = safe_get(pd.steps, ["trigger", "event", "page"], default={})
    properties = safe_get(trigger_event_page, ["properties"], default={})

    # Debug: Log available property names to help troubleshoot
    logger.info(f"Available properties: {list(properties.keys()) if isinstance(properties, dict) else 'N/A'}")

    # Task Name
    task_name = safe_get(properties, ["Task name", "title", 0, "plain_text"], default="Untitled Task")

    # Due Date information
    due_date_start = safe_get(properties, ["Due Date", "date", "start"])
    due_date_end = safe_get(properties, ["Due Date", "date", "end"])

    # Google Event ID - Crucial for update
    google_event_id_prop = safe_get(properties, ["Google Event ID"], default={})
    logger.info(f"Google Event ID property: {google_event_id_prop}")
    event_id = safe_get(google_event_id_prop, ["rich_text", 0, "plain_text"])
    logger.info(f"Extracted event_id: '{event_id}'")

    # Notion Page URL
    notion_url = safe_get(trigger_event_page, ["url"])

    # --- 2. Check prerequisites for an update ---

    # Exit if Due Date is missing (required for calendar events)
    if due_date_start is None:
        exit_message = f"Due Date is missing -- Cannot update event for task: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # Exit if Google Event ID is missing (required to know *which* event to update)
    if not event_id:
        exit_message = f"Google Event ID is missing -- Cannot update, should be a create event for task: '{task_name}'"
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    # --- 3. Prepare data for event update (if checks passed) ---
    logger.info(f"Preparing to update event '{event_id}' for task: '{task_name}'")

    # Normalize dates to ensure consistent format for Google Calendar
    final_start_date, final_end_date = normalize_dates(due_date_start, due_date_end)

    # Log extracted details
    logger.info(f"Event ID: {event_id}")
    logger.info(f"Subject: {task_name}")
    logger.info(f"Start: {final_start_date}")
    logger.info(f"End: {final_end_date}")
    logger.info(f"TimeZone: {TIMEZONE}")
    logger.info(f"Notion URL: {notion_url}")

    # Structure the return object for the next step (e.g., Google Calendar update event)
    # CreateIfMissing flag: If the calendar event was deleted (404), create a new one
    # instead of failing. This handles the case where a user deletes an event in
    # Google Calendar but the task still exists in Notion.
    ret_obj = {
        "GCal": {
            "Subject": task_name,
            "Start": final_start_date,
            "End": final_end_date,
            "TimeZone": TIMEZONE,
            "Update": True,
            "EventId": event_id,
            "Url": notion_url,
            "Description": f"Notion Task: {task_name}\nLink: {notion_url or 'N/A'}",
            "CreateIfMissing": True  # Handle 404: recreate event if it was deleted
        }
    }

    # --- 4. Return data for use in subsequent steps ---
    return ret_obj
