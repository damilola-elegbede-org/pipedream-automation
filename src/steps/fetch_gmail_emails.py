"""
Gmail Email Fetcher

Fetches emails from Gmail based on label filters. Retrieves full email content
including plain text and HTML body for processing by downstream steps.

Usage: Copy-paste into a Pipedream Python step
Required: Connect Gmail account with 'gmail.readonly' scope
"""
import requests
import base64
import random
import time

# --- Configuration ---
DEFAULT_MAX_RESULTS = 50  # Limit to prevent timeouts and rate limits


def get_header_value(headers, name):
    """Gets the value of a specific header from the list of headers."""
    name_lower = name.lower()
    for header in headers:
        if header.get('name', '').lower() == name_lower:
            return header.get('value', '')
    return ''


def get_body_parts(payload):
    """
    Recursively extracts text/plain and text/html parts from the message payload.
    """
    plain_text_body = None
    html_body = None

    if not payload:
        return plain_text_body, html_body

    mime_type = payload.get('mimeType', '')
    body_data = payload.get('body', {}).get('data')

    if mime_type == 'text/plain' and body_data:
        try:
            decoded_data = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')
            plain_text_body = decoded_data
        except Exception as e:
            print(f"  Error decoding text/plain part: {e}")
            plain_text_body = "Error decoding content"

    elif mime_type == 'text/html' and body_data:
        try:
            decoded_data = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')
            html_body = decoded_data
        except Exception as e:
            print(f"  Error decoding text/html part: {e}")
            html_body = "Error decoding content"

    # If the current part is multipart, recurse into its parts
    if payload.get('parts'):
        for part in payload['parts']:
            pt_part, html_part = get_body_parts(part)
            if pt_part and not plain_text_body:
                plain_text_body = pt_part
            if html_part and not html_body:
                html_body = html_part

    return plain_text_body, html_body


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


def deduplicate_by_thread(email_list):
    """
    Keep only the most recent email from each thread.

    When multiple emails belong to the same Gmail thread (conversation),
    this function filters to keep only the most recent one, which typically
    contains the full conversation context (quoted replies).

    Args:
        email_list: List of email dicts with 'thread_id' and 'date' fields

    Returns:
        List of emails with one email per unique thread (the most recent)
    """
    from email.utils import parsedate_to_datetime

    if not email_list:
        return []

    threads = {}
    for email in email_list:
        thread_id = email.get("thread_id") or email.get("message_id")

        # Parse email date for comparison
        email_date = None
        date_str = email.get("date", "")
        if date_str:
            try:
                email_date = parsedate_to_datetime(date_str)
            except (ValueError, TypeError):
                pass

        # First email in thread or compare dates to keep most recent
        if thread_id not in threads:
            threads[thread_id] = {"email": email, "date": email_date}
        elif email_date:
            existing = threads[thread_id]
            if existing["date"] is None or email_date > existing["date"]:
                threads[thread_id] = {"email": email, "date": email_date}

    return [entry["email"] for entry in threads.values()]


def handler(pd: "pipedream"):  # noqa: F821
    # --- 1. Authentication ---
    try:
        token = f'{pd.inputs["gmail"]["$auth"]["oauth_access_token"]}'
    except KeyError:
        raise Exception("Gmail account not connected or input name is not 'gmail'. Please connect a Gmail account to this step.")  # noqa: E501

    authorization = f'Bearer {token}'
    common_headers = {"Authorization": authorization}

    # --- 2. Define Labels and Limits (Using Pipedream Inputs or Defaults) ---
    required_label = pd.inputs.get("required_label", "notion")
    excluded_label = pd.inputs.get("excluded_label", "notiontaskcreated")
    max_results = pd.inputs.get("max_results", DEFAULT_MAX_RESULTS)

    # Ensure max_results is an integer
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        print(f"Warning: Invalid max_results value '{max_results}', using default {DEFAULT_MAX_RESULTS}")
        max_results = DEFAULT_MAX_RESULTS

    # --- 3. Construct Gmail API Search Query ---
    query = f'label:{required_label} -label:{excluded_label}'
    print(f"Using Gmail search query: {query}")
    print(f"Maximum results limit: {max_results}")

    # --- 4. Find Matching Message IDs (using users.messages.list) ---
    all_message_ids = []
    page_token = None
    list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

    while len(all_message_ids) < max_results:
        params = {'q': query, 'maxResults': min(100, max_results - len(all_message_ids))}
        if page_token:
            params['pageToken'] = page_token

        print(f"Requesting message list page... (Page token: {page_token}, Current count: {len(all_message_ids)})")
        try:
            r_list = retry_with_backoff(
                lambda p=params: requests.get(list_url, headers=common_headers, params=p, timeout=30)
            )
        except requests.exceptions.RequestException as e:
            print(f"Error during Gmail API list request: {e}")
            raise e

        response_data = r_list.json()
        messages = response_data.get('messages', [])

        if messages:
            # Only add up to max_results
            remaining = max_results - len(all_message_ids)
            all_message_ids.extend([msg['id'] for msg in messages[:remaining]])

        page_token = response_data.get('nextPageToken')
        if not page_token or len(all_message_ids) >= max_results:
            break

    print(f"Found {len(all_message_ids)} total matching message IDs (limited to {max_results}).")

    # --- 5. Fetch Details for Each Message ID (using users.messages.get) ---
    email_details_list = []
    failed_message_ids = []
    get_url_base = "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    get_params = {'format': 'full'}

    print(f"Fetching details for {len(all_message_ids)} messages...")
    for msg_id in all_message_ids:
        get_url = f"{get_url_base}{msg_id}"
        print(f"  Fetching full details for message ID: {msg_id}")
        try:
            r_get = retry_with_backoff(
                lambda url=get_url: requests.get(url, headers=common_headers, params=get_params, timeout=30)
            )

            message_data = r_get.json()
            payload = message_data.get('payload', {})
            payload_headers = payload.get('headers', [])

            # Extract header values
            subject = get_header_value(payload_headers, 'Subject')
            sender = get_header_value(payload_headers, 'From')
            receiver = get_header_value(payload_headers, 'To')
            date_sent = get_header_value(payload_headers, 'Date')
            message_id_header = get_header_value(payload_headers, 'Message-ID')

            # Extract email body content
            plain_text_body, html_body = get_body_parts(payload)

            # Construct the clickable Gmail URL
            gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"

            # Extract thread ID for deduplication
            thread_id = message_data.get('threadId', msg_id)

            # Append structured data with new fields
            email_details_list.append({
                "url": gmail_url,
                "subject": subject,
                "sender": sender,
                "receiver": receiver,
                "date": date_sent,
                "message_id_header": message_id_header,
                "message_id": msg_id,
                "thread_id": thread_id,
                "plain_text_body": plain_text_body if plain_text_body else "",
                "html_body": html_body if html_body else ""
            })

        except requests.exceptions.RequestException as e:
            print(f"  ERROR fetching details for message ID {msg_id}: {e}")
            failed_message_ids.append({"message_id": msg_id, "error": str(e)})
            continue
        except Exception as e:
            print(f"  An unexpected error occurred fetching details for {msg_id}: {e}")
            failed_message_ids.append({"message_id": msg_id, "error": str(e)})
            continue

    # --- 6. Deduplicate by Thread ---
    # Keep only the most recent email per thread to avoid duplicate Notion tasks
    original_count = len(email_details_list)
    email_details_list = deduplicate_by_thread(email_details_list)
    deduped_count = len(email_details_list)

    # --- 7. Return Results ---
    print("\n--- Fetch Complete ---")
    print(f"Successfully fetched details for {original_count} messages.")
    if original_count != deduped_count:
        print(f"After thread deduplication: {deduped_count} unique threads (removed {original_count - deduped_count} duplicates)")  # noqa: E501
    if failed_message_ids:
        print(f"Failed to fetch {len(failed_message_ids)} messages: {[f['message_id'] for f in failed_message_ids]}")

    # Return list directly for backwards compatibility, but failures are logged
    # If we need to track failures in downstream steps, we could change the return format
    return email_details_list
