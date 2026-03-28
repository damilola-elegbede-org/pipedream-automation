"""
Shared test fixtures for Pipedream step tests.

These fixtures provide mock objects that simulate the Pipedream runtime environment,
allowing unit tests to run without actual API connections.
"""
import pytest
from unittest.mock import MagicMock, PropertyMock  # noqa: F401


class MockFlow:
    """Mock Pipedream flow object for testing early exits."""

    def __init__(self):
        self.exit_called = False
        self.exit_message = None

    def exit(self, message=None):
        self.exit_called = True
        self.exit_message = message


class MockDataStore(dict):
    """Mock Pipedream Data Store for testing caching."""

    def get(self, key, default=None):
        return super().get(key, default)


class MockState(dict):
    """Mock Pipedream pd.state — a persistent dict between workflow executions."""

    def get(self, key, default=None):
        return super().get(key, default)


class MockPipedream:
    """Mock Pipedream context object for testing handlers."""

    def __init__(self):
        self.inputs = {}
        self.steps = {}
        self.flow = MockFlow()
        self.data_store = MockDataStore()
        self.state = MockState()


@pytest.fixture
def mock_pd():
    """Create a mock Pipedream context object."""
    return MockPipedream()


@pytest.fixture
def gmail_auth():
    """Mock Gmail OAuth token structure."""
    return {"gmail": {"$auth": {"oauth_access_token": "test_gmail_token"}}}


@pytest.fixture
def notion_auth():
    """Mock Notion OAuth token structure."""
    return {"notion": {"$auth": {"oauth_access_token": "test_notion_token"}}}


@pytest.fixture
def sample_email():
    """Sample email data structure matching Gmail API output."""
    return {
        "message_id": "msg_abc123",
        "message_id_header": "<test@example.com>",
        "thread_id": "thread_abc123",
        "subject": "Test Email Subject",
        "sender": "John Doe <john@example.com>",
        "receiver": "Jane Doe <jane@example.com>",
        "date": "Mon, 15 Jan 2024 10:30:00 -0500",
        "url": "https://mail.google.com/mail/u/0/#inbox/msg_abc123",
        "plain_text_body": "This is the plain text body of the email.",
        "html_body": "<html><body><p>This is the HTML body of the email.</p></body></html>"
    }


@pytest.fixture
def sample_notion_task_trigger():
    """Sample Notion task trigger data structure."""
    return {
        "trigger": {
            "event": {
                "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # 32-char hex Notion page ID
                "properties": {
                    "Task name": {
                        "title": [{"plain_text": "Test Task"}]
                    },
                    "Due Date": {
                        "date": {
                            "start": "2024-01-20",
                            "end": "2024-01-21"
                        }
                    },
                    "Google Event ID": {
                        "rich_text": []  # Empty means no event yet
                    }
                },
                "url": "https://www.notion.so/Test-Task-abc123def456"
            }
        }
    }


@pytest.fixture
def sample_notion_update_trigger():
    """Sample Notion update trigger with existing Google Event ID."""
    return {
        "trigger": {
            "event": {
                "page": {
                    "properties": {
                        "Task name": {
                            "title": [{"plain_text": "Updated Task"}]
                        },
                        "Due Date": {
                            "date": {
                                "start": "2024-01-22",
                                "end": None
                            }
                        },
                        "Google Event ID": {
                            "rich_text": [{"plain_text": "gcal_event_xyz789"}]
                        }
                    },
                    "url": "https://www.notion.so/Updated-Task-abc123def456"
                }
            }
        }
    }


@pytest.fixture
def sample_gcal_event_trigger():
    """Sample Google Calendar event trigger with Notion URL in location."""
    return {
        "trigger": {
            "event": {
                "summary": "Meeting from Notion",
                "location": "https://www.notion.so/Test-Task-abc123def456789012345678901234ab",
                "start": {
                    "dateTime": "2024-01-20T10:00:00-05:00"
                },
                "end": {
                    "dateTime": "2024-01-20T11:00:00-05:00"
                }
            }
        }
    }


@pytest.fixture
def sample_successful_mappings():
    """Sample successful mappings from create_notion_task step."""
    return {
        "status": "Completed",
        "total_processed": 2,
        "successful_mappings": [
            {
                "gmail_message_id": "msg_abc123",
                "notion_page_id": "notion_page_1",
                "rendered_image_url": None
            },
            {
                "gmail_message_id": "msg_def456",
                "notion_page_id": "notion_page_2",
                "rendered_image_url": "https://hcti.io/image/abc123"
            }
        ],
        "errors": []
    }


# Google Tasks fixtures

@pytest.fixture
def sample_notion_task_trigger_gtask():
    """Sample Notion task trigger for Google Tasks (no existing Task ID)."""
    return {
        "trigger": {
            "event": {
                "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # 32-char hex Notion page ID
                "properties": {
                    "Task name": {
                        "title": [{"plain_text": "Test Task"}]
                    },
                    "Due Date": {
                        "date": {
                            "start": "2024-01-20",
                            "end": None
                        }
                    },
                    "Google Task ID": {
                        "rich_text": []  # Empty means no task yet
                    },
                    "List": {
                        "select": {"name": "Next Action"}
                    }
                },
                "url": "https://www.notion.so/Test-Task-abc123def456"
            }
        }
    }


@pytest.fixture
def sample_notion_update_trigger_gtask():
    """Sample Notion update trigger with existing Google Task ID."""
    return {
        "trigger": {
            "event": {
                "page": {
                    "properties": {
                        "Task name": {
                            "title": [{"plain_text": "Updated Task"}]
                        },
                        "Due Date": {
                            "date": {
                                "start": "2024-01-22",
                                "end": None
                            }
                        },
                        "Google Task ID": {
                            "rich_text": [{"plain_text": "gtask_xyz789"}]
                        },
                        "List": {
                            "status": {"name": "Next Action"}
                        }
                    },
                    "url": "https://www.notion.so/Updated-Task-abc123def456"
                }
            }
        }
    }


@pytest.fixture
def sample_gtask_trigger():
    """Sample Google Task trigger with Notion URL in notes (incomplete task)."""
    return {
        "trigger": {
            "event": {
                "title": "Task from Notion",
                "notes": "Notion Task: Task from Notion\nLink: https://www.notion.so/Test-Task-abc123def456789012345678901234ab",  # noqa: E501
                "due": "2024-01-20T00:00:00.000Z",
                "status": "needsAction"  # Not completed
            }
        }
    }


@pytest.fixture
def sample_gtask_trigger_completed():
    """Sample Google Task trigger with completed status."""
    return {
        "trigger": {
            "event": {
                "title": "Completed Task from Notion",
                "notes": "Notion Task: Completed Task\nLink: https://www.notion.so/Test-Task-abc123def456789012345678901234ab",  # noqa: E501
                "due": "2024-01-20T00:00:00.000Z",
                "status": "completed"
            }
        }
    }
