import importlib
from unittest.mock import MagicMock
import httpx
from openai import APIConnectionError, APIStatusError
import pytest
import client.openai_client as openai_module
from dotenv import load_dotenv
import os

load_dotenv()

# ------------------------------------------------------------------ unit ---
def test_client_initializes_from_env_vars(monkeypatch):
    captured_kwargs = {}

    def fake_openai(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    monkeypatch.setenv("OLLAMA_PROVIDER_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "test-ollama-key")
    monkeypatch.setattr("openai.OpenAI", fake_openai)

    # Reload module so top-level `client = OpenAI(...)` executes with patched env/class
    importlib.reload(openai_module)

    assert str(captured_kwargs["base_url"]).rstrip("/") == "http://localhost:11434/v1"
    assert captured_kwargs["api_key"] == "test-ollama-key"


def test_chat_completion_returns_text(mock_openai_client):
    response = openai_module.client.chat.completions.create(
        model="gpt-oss:20b",
        messages=[{"role": "user", "content": "List all vendors"}],
    )

    assert response.choices[0].message.content == "SELECT * FROM vendors;"
    mock_openai_client.chat.completions.create.assert_called_once_with(
        model="gpt-oss:20b",
        messages=[{"role": "user", "content": "List all vendors"}],
    )


def test_chat_completion_handles_tool_call_response(mock_openai_client):
    # Simulate the LLM requesting your MCP schema inspection tool
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_123"
    mock_tool_call.function.name = "inspect_table_schema"
    mock_tool_call.function.arguments = '{"table_names": ["vendors"]}'

    mock_openai_client.chat.completions.create.return_value.choices[0].message.content = None
    mock_openai_client.chat.completions.create.return_value.choices[0].message.tool_calls = [
        mock_tool_call
    ]

    response = openai_module.client.chat.completions.create(
        model="gpt-oss:20b",
        messages=[{"role": "user", "content": "Inspect vendors"}],
    )

    msg = response.choices[0].message
    assert msg.content is None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == "inspect_table_schema"


def test_raises_api_status_error_on_quota_limit(mock_openai_client):
    fake_request = httpx.Request("POST", "https://ollama.com/v1/chat/completions")
    fake_response = httpx.Response(status_code=402, request=fake_request)

    mock_openai_client.chat.completions.create.side_effect = APIStatusError(
        message="Error code: 402 - Add usage credits",
        response=fake_response,
        body={"error": "quota_exceeded"},
    )

    with pytest.raises(APIStatusError) as exc_info:
        openai_module.client.chat.completions.create(
            model="gpt-oss:20b",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert exc_info.value.status_code == 402


def test_raises_connection_error_when_ollama_is_down(mock_openai_client):
    fake_request = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    mock_openai_client.chat.completions.create.side_effect = APIConnectionError(
        request=fake_request
    )

    with pytest.raises(APIConnectionError):
        openai_module.client.chat.completions.create(
            model="gpt-oss:20b",
            messages=[{"role": "user", "content": "Hello"}],
        )


# ----------------------------------------------------------- integration ---
@pytest.mark.integration
def test_live_ollama_client_responds():
    """Requires a valid OLLAMA_PROVIDER_URL, OLLAMA_API_KEY, and OLLAMA_MODEL_NAME in .env."""
    importlib.reload(openai_module)
    response = openai_module.client.chat.completions.create(
        model=os.getenv("OLLAMA_MODEL_NAME"),
        messages=[{"role": "user", "content": "Reply with only the word OK."}],
        max_tokens=200,
        extra_body={"reasoning": {"effort": "low"}},
    )

    assert response.choices[0].message.content is not None
    assert len(response.choices[0].message.content.strip()) > 0
    