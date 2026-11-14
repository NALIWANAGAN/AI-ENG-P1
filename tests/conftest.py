from unittest.mock import MagicMock
import pytest
from client.postgres_client import PostgresClient
import client.openai_client as openai_module
from dotenv import load_dotenv
import os
load_dotenv()

@pytest.fixture
def mock_pg_client(monkeypatch):
    """Unit test fixture: returns a fresh PostgresClient backed by a mock connection."""
    PostgresClient._instance = None
    PostgresClient._conn = None

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.broken = False

    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    monkeypatch.setenv("POSTGRES_URL", "POSTGRES_TEST_URL")
    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: mock_conn)

    client = PostgresClient()
    yield client, mock_conn, mock_cursor

    PostgresClient._instance = None
    PostgresClient._conn = None


@pytest.fixture
def live_pg_client(monkeypatch):
    """Integration test fixture: connects to real Postgres and rolls back all changes."""
    PostgresClient._instance = None
    PostgresClient._conn = None

    client = PostgresClient()
    client.dbc = os.getenv("POSTGRES_TEST_URL")
    conn = client.get_connection()
    conn.autocommit = False

    # Prevent any code under test from committing permanently mid-test
    monkeypatch.setattr(conn, "commit", lambda: None)

    yield client

    conn.rollback()
    conn.close()
    PostgresClient._instance = None
    PostgresClient._conn = None
    


@pytest.fixture
def mock_openai_client(monkeypatch):
    """Unit test fixture: replaces the module-level OpenAI client with a MagicMock."""
    mock_client = MagicMock()

    # Default text completion response
    mock_message = MagicMock()
    mock_message.content = "SELECT * FROM vendors;"
    mock_message.tool_calls = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client.chat.completions.create.return_value = mock_response

    monkeypatch.setattr(openai_module, "client", mock_client)
    return mock_client
