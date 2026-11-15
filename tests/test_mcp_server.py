import json

import pytest
from fastmcp import Client

from client.postgres_client import PostgresClient
from main import mcp


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


@pytest.mark.anyio
async def test_mcp_server_exposes_mounted_tools_without_di_params():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    assert {"inspect_table_schema", "execute_model_query"}.issubset(by_name.keys())

    # DI parameters (`service`) must be hidden from the LLM input schema
    assert list(by_name["inspect_table_schema"].input_schema["properties"].keys()) == ["table_names"]
    assert list(by_name["execute_model_query"].input_schema["properties"].keys()) == ["query"]


@pytest.mark.anyio
async def test_mcp_server_inspect_table_schema_resolves_di(fake_pg_env):
    fake_pg_env(
        _FakeCursor(
            fetchall_results=[
                [{"column_name": "id", "data_type": "uuid"}],
                [{"constraint_type": "primary key"}],
            ]
        )
    )

    async with Client(mcp) as client:
        result = await client.call_tool("inspect_table_schema", {"table_names": ["vendors"]})

    assert result.is_error is False
    assert result.structured_content["success"] is True
    assert "vendors" in result.structured_content["table_information"]


@pytest.mark.anyio
async def test_mcp_server_execute_model_query_resolves_di(fake_pg_env):
    fake_pg_env(
        _FakeCursor(
            description=[("id",)],
            fetchmany_result=[{"id": 1}],
        )
    )

    async with Client(mcp) as client:
        result = await client.call_tool("execute_model_query", {"query": "SELECT id FROM vendors;"})

    assert result.is_error is False
    assert result.structured_content["success"] is True
    assert json.loads(result.structured_content["db_response"]) == [{"id": 1}]
