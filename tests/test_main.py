import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client

from client.postgres_client import PostgresClient
from extractor.extract_invoice import ExtractedDocument, FileMetadata
from main import (
    MAX_ERROR_RETRIES,
    SYSTEM_PROMPT,
    _to_openai_tools,
    mcp,
    process_document,
)


class _FakeCursor:
    def __init__(self, *, description=None, fetchall_results=None, fetchmany_result=None, rowcount=0):
        self.description = description
        self._fetchall_results = list(fetchall_results or [])
        self._fetchmany_result = list(fetchmany_result or [])
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._fetchall_results.pop(0)

    def fetchmany(self, size):
        return self._fetchmany_result


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    closed = False
    broken = False

    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def transaction(self):
        return _FakeTransaction()


@pytest.fixture
def fake_pg_env(monkeypatch):
    PostgresClient._instance = None
    PostgresClient._conn = None
    monkeypatch.setenv("POSTGRES_URL", "POSTGRES_TEST_URL")

    def _use_cursor(cursor: _FakeCursor):
        fake_conn = _FakeConn(cursor)
        monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: fake_conn)

    yield _use_cursor

    PostgresClient._instance = None
    PostgresClient._conn = None


def _sample_doc() -> ExtractedDocument:
    now = datetime.now(timezone.utc)
    return ExtractedDocument(
        raw_text="Invoice #INV-100\nTotal: $50.00",
        metadata=FileMetadata(
            file_name="inv-100.pdf",
            file_path="/tmp/inv-100.pdf",
            created_at_utc=now,
            modified_at_utc=now,
            page_count=1,
            is_encrypted=False,
            title="INV-100",
        ),
    )


def _tool_call(call_id: str, name: str, args: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _llm_response(content: str = "", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.mark.anyio
async def test_process_document_happy_path_appends_turns_without_reinjection(fake_pg_env):
    fake_pg_env(
        _FakeCursor(
            fetchall_results=[
                [{"column_name": "id", "data_type": "uuid"}],
                [{"constraint_type": "primary key"}],
            ],
            description=None,
            rowcount=1,
        )
    )

    llm_client = MagicMock()
    llm_client.chat.completions.create.side_effect = [
        _llm_response(tool_calls=[_tool_call("c1", "inspect_table_schema", {"table_names": ["vendors"]})]),
        _llm_response(tool_calls=[_tool_call("c2", "execute_model_query", {"query": "INSERT INTO vendors (name) VALUES ('Acme');"})]),
        _llm_response(content="Stored vendor Acme."),
    ]

    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        ok = await process_document(
            doc=_sample_doc(),
            mcp_client=mcp_client,
            llm_client=llm_client,
            openai_tools=openai_tools,
            model_name="gpt-oss:20b",
        )

    assert ok.success is True
    assert ok.file_name == "inv-100.pdf"
    assert ok.message == "Stored vendor Acme."
    assert llm_client.chat.completions.create.call_count == 3

    # Verify static system prompt & initial user prompt appear once at indices 0 and 1
    final_messages = llm_client.chat.completions.create.call_args.kwargs["messages"]
    assert final_messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert final_messages[1]["role"] == "user"
    assert sum(1 for m in final_messages if m["role"] == "system") == 1
    assert sum(1 for m in final_messages if m["role"] == "user") == 1


@pytest.mark.anyio
async def test_process_document_retries_on_retryable_error_and_succeeds(fake_pg_env):
    fake_pg_env(_FakeCursor(description=None, rowcount=1))

    llm_client = MagicMock()
    llm_client.chat.completions.create.side_effect = [
        # Turn 1: rejected by SQLGlot (DROP is retryable=True)
        _llm_response(tool_calls=[_tool_call("c1", "execute_model_query", {"query": "DROP TABLE vendors;"})]),
        # Turn 2: corrected SQL succeeds
        _llm_response(tool_calls=[_tool_call("c2", "execute_model_query", {"query": "INSERT INTO vendors (name) VALUES ('Acme');"})]),
        # Turn 3: done
        _llm_response(content="Recovered and inserted."),
    ]

    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        ok = await process_document(
            doc=_sample_doc(),
            mcp_client=mcp_client,
            llm_client=llm_client,
            openai_tools=openai_tools,
            model_name="gpt-oss:20b",
        )

    assert ok.success is True
    assert ok.message == "Recovered and inserted."
    final_messages = llm_client.chat.completions.create.call_args.kwargs["messages"]
    first_tool_msg = json.loads(final_messages[3]["content"])
    assert first_tool_msg["success"] is False
    assert first_tool_msg["retryable"] is True
    assert first_tool_msg["retry_count"] == f"1 of {MAX_ERROR_RETRIES}"


@pytest.mark.anyio
async def test_process_document_stops_after_max_error_retries(fake_pg_env):
    fake_pg_env(_FakeCursor())

    llm_client = MagicMock()
    llm_client.chat.completions.create.side_effect = [
        _llm_response(tool_calls=[_tool_call("c1", "execute_model_query", {"query": "DROP TABLE vendors;"})]),
        _llm_response(tool_calls=[_tool_call("c2", "execute_model_query", {"query": "DELETE FROM vendors;"})]),
        _llm_response(tool_calls=[_tool_call("c3", "execute_model_query", {"query": "TRUNCATE vendors;"})]),
    ]

    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        ok = await process_document(
            doc=_sample_doc(),
            mcp_client=mcp_client,
            llm_client=llm_client,
            openai_tools=openai_tools,
            model_name="gpt-oss:20b",
        )

    assert ok.success is False
    assert "Only INSERT, UPDATE and SELECT" in (ok.message or "")
    assert llm_client.chat.completions.create.call_count == MAX_ERROR_RETRIES


@pytest.mark.anyio
async def test_process_document_aborts_immediately_on_non_retryable_error(fake_pg_env, monkeypatch):
    from services.query_service import ModelQueryResponse

    fake_pg_env(_FakeCursor())
    monkeypatch.setattr(
        "services.query_service.QueryService.execute_model_query",
        lambda self, query: ModelQueryResponse(
            success=False,
            retryable=False,
            sqlstate="23505",
            notice="Duplicate invoice",
            db_response=None,
        ),
    )

    llm_client = MagicMock()
    llm_client.chat.completions.create.side_effect = [
        _llm_response(tool_calls=[_tool_call("c1", "execute_model_query", {"query": "INSERT INTO vendors (name) VALUES ('Acme');"})]),
    ]

    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        ok = await process_document(
            doc=_sample_doc(),
            mcp_client=mcp_client,
            llm_client=llm_client,
            openai_tools=openai_tools,
            model_name="gpt-oss:20b",
        )

    assert ok.success is False
    assert ok.message == "Duplicate invoice"
    assert llm_client.chat.completions.create.call_count == 1


@pytest.mark.anyio
async def test_process_document_strips_harmony_tokens_and_catches_tool_error(fake_pg_env):
    fake_pg_env(_FakeCursor(description=None, rowcount=1))

    llm_client = MagicMock()
    llm_client.chat.completions.create.side_effect = [
        # Turn 1: Hallucinated unknown tool -> caught as ToolError and fed back to LLM
        _llm_response(tool_calls=[_tool_call("c1", "nonexistent_tool", {})]),
        # Turn 2: Leaked `<|channel|>` token on valid tool -> stripped and executed
        _llm_response(
            tool_calls=[
                _tool_call(
                    "c2",
                    "execute_model_query<|channel|>",
                    {"query": "INSERT INTO vendors (name) VALUES ('Acme');"},
                )
            ]
        ),
        # Turn 3: Done
        _llm_response(content="Completed."),
    ]

    async with Client(mcp) as mcp_client:
        openai_tools = _to_openai_tools(await mcp_client.list_tools())
        ok = await process_document(
            doc=_sample_doc(),
            mcp_client=mcp_client,
            llm_client=llm_client,
            openai_tools=openai_tools,
            model_name="gpt-oss:20b",
        )

    assert ok.success is True
    assert ok.file_name == "inv-100.pdf"
    assert ok.message == "Completed."
    assert llm_client.chat.completions.create.call_count == 3
    final_messages = llm_client.chat.completions.create.call_args.kwargs["messages"]
    first_tool_msg = json.loads(final_messages[3]["content"])
    assert first_tool_msg["success"] is False
    assert "Unknown tool" in first_tool_msg["notice"]


def test_extract_documents_skips_scanned_pdfs_with_no_text_layer(monkeypatch):
    from pathlib import Path
    from extractor.extract_invoice import ExtractInvoice, NoTextLayerError
    from main import _extract_documents

    valid_doc = _sample_doc()

    def _fake_extract(self, file_path: str):
        if "inv3.pdf" in file_path:
            raise NoTextLayerError("inv3.pdf: no extractable text (scanned PDF?)")
        return valid_doc

    monkeypatch.setattr(ExtractInvoice, "extract_invoice", _fake_extract)

    docs = _extract_documents([Path("inv3.pdf"), Path("valid.pdf")])
    assert docs == [valid_doc]
