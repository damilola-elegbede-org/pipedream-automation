"""
Label Gmail Processed Emails

Adds a label to Gmail messages that have been successfully processed and
converted to Notion tasks. This prevents re-processing the same emails.

Usage: Copy-paste into a Pipedream Python step
Required: Connect Gmail account with 'gmail.modify' and 'gmail.readonly' scopes
"""
import requests
import time
import random
import json
import re

# --- Configuration ---
PREVIOUS_STEP_NAME = "create_notion_task"
LABEL_NAME_TO_ADD = "notiontaskcreated"
GMAIL_MODIFY_URL_BASE = "https://www.googleapis.com/gmail/v1/users/me/messages/"
GMAIL_LABELS_URL = "https://www.googleapis.com/gmail/v1/users/me/labels"
GMAIL_BATCH_URL = "https://www.googleapis.com/batch/gmail/v1"
BATCH_SIZE = 100  # Gmail batch API maximum


def retry_with_backoff(request_func, max_retries=5):
    """
    Execute request with exponential backoff for rate limits.

    Handles HTTP 429 (Too Many Requests) and 503 (Service Unavailable) errors
    by waiting and retrying with exponential backoff. Respects Retry-After header.
    """
    for attempt in range(max_retries):
        try:
            response = request_func()
            response.raise_for_status()
            return response
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


def get_label_id(service_headers, label_name):
    """Fetches the ID of a Gmail label by its name."""
    print(f"Attempting to find Label ID for: '{label_name}'")
    try:
        response = retry_with_backoff(
            lambda: requests.get(GMAIL_LABELS_URL, headers=service_headers, timeout=30)
        )
        labels_data = response.json()
        labels = labels_data.get('labels', [])
        for label in labels:
            if label.get('name', '').lower() == label_name.lower():
                label_id = label.get('id')
                print(f"Found Label ID: {label_id}")
                return label_id
        print(f"Error: Label '{label_name}' not found in user's labels.")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching labels: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred fetching label ID: {e}")
        return None


def get_cached_label_id(pd, service_headers, label_name):
    """Get label ID from Pipedream Data Store cache or fetch and cache."""
    cache_key = f"label_id_{label_name}"

    # Try to get from cache
    try:
        cached_id = pd.data_store.get(cache_key)
        if cached_id:
            print(f"Using cached Label ID for '{label_name}': {cached_id}")
            return cached_id
    except Exception as e:
        print(f"Warning: Could not access data store cache: {e}")

    # Fetch from API
    label_id = get_label_id(service_headers, label_name)

    # Cache if found
    if label_id:
        try:
            pd.data_store[cache_key] = label_id
            print(f"Cached Label ID for '{label_name}': {label_id}")
        except Exception as e:
            print(f"Warning: Could not cache label ID: {e}")

    return label_id


def batch_label_messages(service_headers, message_ids, label_id):
    """
    Apply label to multiple messages using Gmail batch API.

    Returns tuple of (successful_ids, errors).
    Gmail batch API supports up to 100 requests per batch.
    """
    successfully_labeled = []
    errors = []

    # Process in batches of BATCH_SIZE
    for batch_start in range(0, len(message_ids), BATCH_SIZE):
        batch_ids = message_ids[batch_start:batch_start + BATCH_SIZE]
        batch_num = (batch_start // BATCH_SIZE) + 1
        total_batches = (len(message_ids) + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"Processing batch {batch_num}/{total_batches} ({len(batch_ids)} messages)...")

        # Build multipart batch request body
        boundary = "batch_boundary_gtd_automation"
        batch_body_parts = []

        for idx, msg_id in enumerate(batch_ids):
            modify_body = json.dumps({"addLabelIds": [label_id]})
            part = f"""--{boundary}
Content-Type: application/http
Content-ID: <item{idx}>

POST /gmail/v1/users/me/messages/{msg_id}/modify HTTP/1.1
Content-Type: application/json

{modify_body}
"""
            batch_body_parts.append(part)

        batch_body = "\n".join(batch_body_parts) + f"\n--{boundary}--"

        batch_headers = {
            "Authorization": service_headers["Authorization"],
            "Content-Type": f"multipart/mixed; boundary={boundary}"
        }

        try:
            response = retry_with_backoff(
                lambda body=batch_body, hdrs=batch_headers: requests.post(
                    GMAIL_BATCH_URL,
                    headers=hdrs,
                    data=body,
                    timeout=60  # Batch operations may take longer
                )
            )

            # Parse batch response to identify individual successes/failures
            response_text = response.text

            # Parse multipart response properly
            # Gmail batch responses are multipart with Content-ID headers
            parts = response_text.split(f'--{boundary}')
            parsed_count = 0

            for part in parts:
                # Extract Content-ID and HTTP status from each part
                content_id_match = re.search(r'Content-ID:\s*<response-item(\d+)>', part)
                status_match = re.search(r'HTTP/1\.1\s+(\d+)', part)

                if content_id_match and status_match:
                    idx = int(content_id_match.group(1))
                    status = int(status_match.group(1))
                    if idx < len(batch_ids):
                        msg_id = batch_ids[idx]
                        if status == 200:
                            successfully_labeled.append(msg_id)
                            print(f"  Labeled message: {msg_id}")
                        else:
                            errors.append({
                                "gmail_message_id": msg_id,
                                "error": f"HTTP {status}"
                            })
                        parsed_count += 1

            # Fallback: if we couldn't parse individual responses but batch succeeded
            if parsed_count == 0 and response.status_code == 200:
                successfully_labeled.extend(batch_ids)
                print(f"  Batch completed successfully for {len(batch_ids)} messages")

        except requests.exceptions.HTTPError as http_err:
            status_code = http_err.response.status_code if http_err.response else "N/A"
            error_message = str(http_err)
            print(f"  Batch request failed: {status_code} - {error_message}")

            # Fall back to individual requests for this batch
            print(f"  Falling back to individual requests for batch {batch_num}...")
            for msg_id in batch_ids:
                try:
                    modify_url = f"{GMAIL_MODIFY_URL_BASE}{msg_id}/modify"
                    response = retry_with_backoff(
                        lambda url=modify_url: requests.post(
                            url,
                            headers=service_headers,
                            json={"addLabelIds": [label_id]},
                            timeout=30
                        )
                    )
                    successfully_labeled.append(msg_id)
                    print(f"    Labeled message: {msg_id}")
                except Exception as e:
                    errors.append({
                        "gmail_message_id": msg_id,
                        "error": str(e)
                    })
                    print(f"    Failed to label message {msg_id}: {e}")
                time.sleep(0.1)  # Small delay between fallback requests

        except Exception as e:
            print(f"  Unexpected error in batch {batch_num}: {e}")
            for msg_id in batch_ids:
                errors.append({
                    "gmail_message_id": msg_id,
                    "error": f"Batch failed: {e}"
                })

        # Small delay between batches
        if batch_start + BATCH_SIZE < len(message_ids):
            time.sleep(0.5)

    return successfully_labeled, errors


def handler(pd: "pipedream"):  # noqa: F821
    # --- 1. Get Gmail OAuth Token ---
    try:
        token = pd.inputs["gmail"]["$auth"]["oauth_access_token"]
    except KeyError:
        raise Exception("Gmail account not connected or input name is not 'gmail'. Please connect a Gmail account with 'gmail.modify' and 'gmail.readonly' scopes.")  # noqa: E501

    common_headers = {"Authorization": f"Bearer {token}"}

    # --- 2. Get Label ID (with caching) ---
    target_label_id = get_cached_label_id(pd, common_headers, LABEL_NAME_TO_ADD)
    if not target_label_id:
        return {"error": f"Could not find Label ID for '{LABEL_NAME_TO_ADD}'. Please ensure the label exists in Gmail."}

    # --- 3. Get Data from Previous Step (Notion Step) ---
    try:
        previous_step_output = pd.steps[PREVIOUS_STEP_NAME]["$return_value"]
    except KeyError:
        print(f"Error: Could not find return value from step '{PREVIOUS_STEP_NAME}'. Ensure the step name is correct and it exported data.")  # noqa: E501
        return {"error": f"Could not find data from step {PREVIOUS_STEP_NAME}"}
    except Exception as e:
        print(f"An unexpected error occurred accessing previous step data: {e}")
        return {"error": "Failed to access previous step data."}

    # Check if the previous step output the expected structure
    if not isinstance(previous_step_output, dict) or "successful_mappings" not in previous_step_output:
        print(f"Error: Expected a dictionary with 'successful_mappings' key from step '{PREVIOUS_STEP_NAME}', but received: {type(previous_step_output)}")  # noqa: E501
        return {"error": "Invalid data format from previous step."}

    mappings_to_process = previous_step_output["successful_mappings"]

    if not mappings_to_process:
        print("No successful mappings received from the previous step. Nothing to label.")
        return {"status": "No data received", "labeled_messages": 0}

    if not isinstance(mappings_to_process, list):
        print(f"Error: Expected 'successful_mappings' to be a list, but received type {type(mappings_to_process)}.")
        return {"error": "Invalid data format for successful_mappings."}

    # Extract message IDs using 'gmail_message_id' key
    message_ids_to_label = []
    for item in mappings_to_process:
        if isinstance(item, dict) and "gmail_message_id" in item:
            message_ids_to_label.append(item["gmail_message_id"])
        else:
            print(f"Warning: Skipping item in 'successful_mappings' list as it's not a dictionary or missing 'gmail_message_id': {item}")  # noqa: E501

    if not message_ids_to_label:
        print("No valid Gmail message IDs found in the 'successful_mappings' data.")
        return {"status": "No valid message IDs", "labeled_messages": 0}

    # --- 4. Apply Labels Using Batch API ---
    print(f"Starting to add label '{LABEL_NAME_TO_ADD}' (ID: {target_label_id}) to {len(message_ids_to_label)} message(s)...")  # noqa: E501
    print(f"Using batch API for efficiency (batch size: {BATCH_SIZE})...")

    successfully_labeled_ids, errors = batch_label_messages(
        common_headers,
        message_ids_to_label,
        target_label_id
    )

    # --- 6. Return Summary ---
    print("\n--- Labeling Complete ---")
    print(f"Successfully labeled messages: {len(successfully_labeled_ids)}")
    print(f"Errors encountered: {len(errors)}")

    return {
        "status": "Completed",
        "total_processed": len(message_ids_to_label),
        "successfully_labeled_ids": successfully_labeled_ids,
        "errors": errors
    }
