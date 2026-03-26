"""
Google Calendar Event to Notion Sync

Processes Google Calendar event triggers, validates if they originated from
Notion (by checking the location field for a Notion URL), and extracts the
Notion Page ID for syncing updates back to Notion.

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


def extract_notion_page_id(url):
    """
    Extracts the Notion Page ID from a URL using regex.

    Notion Page IDs are 32 hexadecimal characters. They appear at the end
    of the URL, typically after the last hyphen in the page title slug.

    Args:
        url: The Notion URL string.

    Returns:
        The 32-character page ID if found, None otherwise.
    """
    if not url:
        return None

    # Try regex pattern first (most reliable)
    match = NOTION_PAGE_ID_PATTERN.search(url)
    if match:
        return match.group(1)

    # Fallback: try extracting after last hyphen, stripping query params
    try:
        # Remove query params first
        clean_url = url.split('?')[0]
        parts = clean_url.rsplit('-', 1)
        if len(parts) > 1 and parts[-1]:
            potential_id = parts[-1]
            # Validate it looks like a hex ID (at least 20 chars to be safe)
            if len(potential_id) >= 20 and all(c in '0123456789abcdefABCDEF' for c in potential_id):
                return potential_id
    except Exception:
        pass

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


def handler(pd: "pipedream"):  # noqa: F821
    """
    Processes Google Calendar event triggers, checks if they originated from Notion,
    and extracts relevant details including the Notion Page ID from the location URL.
    """
    event_data = safe_get(pd.steps, ["trigger", "event"], default={})

    # --- 1. Validate if the event is Notion-related ---
    location = safe_get(event_data, ["location"])
    event_summary = safe_get(event_data, ["summary"], default="Untitled Event")

    if not location or "https://www.notion.so/" not in location:
        exit_message = f"Event '{event_summary}' does not have a Notion URL in location. Skipping."
        logger.info(exit_message)
        pd.flow.exit(exit_message)
        return

    logger.info(f"Processing Notion-linked event: '{event_summary}'")

    # --- 2. Extract and Validate Notion Page ID from Location URL ---
    raw_page_id = extract_notion_page_id(location)
    page_id = validate_notion_page_id(raw_page_id)

    if not page_id:
        exit_message = f"Could not reliably extract/validate Notion Page ID from location: '{location}' for event '{event_summary}'. Raw extraction: '{raw_page_id}'. Skipping."  # noqa: E501
        logger.warning(exit_message)
        pd.flow.exit(exit_message)
        return

    logger.info(f"Extracted and validated Notion Page ID: {page_id}")

    # --- 3. Extract Start and End Dates/Times ---
    start_obj = safe_get(event_data, ["start"], default={})
    end_obj = safe_get(event_data, ["end"], default={})

    start_time = safe_get(start_obj, ["dateTime"])
    end_time = safe_get(end_obj, ["dateTime"])

    # If dateTime is not present, check for 'date' (all-day event)
    if start_time is None:
        start_time = safe_get(start_obj, ["date"])
    if end_time is None:
        end_time = safe_get(end_obj, ["date"])

    # Fallback: If both dateTime and date are somehow missing
    if start_time is None:
        logger.warning(f"Could not find 'dateTime' or 'date' in start object: {start_obj}. Using raw object string as fallback.")  # noqa: E501
        start_time = str(start_obj)
    if end_time is None:
        logger.warning(f"Could not find 'dateTime' or 'date' in end object: {end_obj}. Using start_time as fallback.")
        end_time = start_time

    logger.info(f"Start: {start_time}")
    logger.info(f"End: {end_time}")

    # --- 4. Prepare Return Value ---
    ret_val = {
        "Subject": event_summary,
        "Start": start_time,
        "End": end_time,
        "Id": page_id
    }

    # --- 5. Return data for use in future steps ---
    return ret_val
