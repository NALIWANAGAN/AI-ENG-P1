from decimal import Decimal
import json
from unittest.mock import MagicMock
import uuid
import psycopg
import pytest

# Update the import path below to match the module where execute_model_query lives
from tools.execute_query import (
    MAX_ROWS,
    ModelQueryResponse,
    _describe,
    _is_retryable,
    execute_model_query,
)

ALLOWED_QUERIES = [
    # SELECT basics
    "SELECT * FROM vendors;",
    "SELECT * FROM vendors",
    "select * from vendors;",
    "SeLeCt * FrOm vendors",
    "SELECT 1;",
    "   SELECT 1   ",
    "/* leading comment */ SELECT 1",
    "SELECT 1 -- trailing comment",
    # SELECT, more complex
    "SELECT v.name, i.invoice_number FROM invoices i JOIN vendors v ON v.id = i.vendor_id",
    "SELECT vendor_id, SUM(total) FROM invoices GROUP BY vendor_id HAVING SUM(total) > 100",
    "SELECT * FROM invoices ORDER BY issue_date DESC LIMIT 10 OFFSET 5",
    "SELECT * FROM invoices WHERE vendor_id IN (SELECT id FROM vendors WHERE name = 'Acme')",
    "WITH big AS (SELECT * FROM invoices WHERE total > 1000) SELECT * FROM big",
    "SELECT id, SUM(total) OVER (PARTITION BY vendor_id) FROM invoices",
    "SELECT name FROM vendors UNION SELECT invoice_number FROM invoices",
    "SELECT id FROM vendors INTERSECT SELECT vendor_id FROM invoices",
    # INSERT
    "INSERT INTO vendors (name) VALUES ('xyz');",
    "INSERT INTO vendors (name) VALUES ('xyz')",
    "INSERT INTO vendors (name, tax_id) VALUES ('A', '1'), ('B', '2')",
    "INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total) "
    "VALUES ('d9621261-3d73-4c6a-9631-1504b1db8c96', 'INV-1', '2026-01-01', '2026-01-31', 'USD', 100.00, 10.00, 110.00)",
    "INSERT INTO line_items (invoice_id, description, quantity, unit_price, line_total) "
    "SELECT id, 'Widget', 1.5, 0.33, 0.50 FROM invoices WHERE invoice_number = 'INV-1'",
    "INSERT INTO vendors (name) VALUES ('a') RETURNING id",
    "INSERT INTO vendors (id, name) VALUES ('d9621261-3d73-4c6a-9631-1504b1db8c96', 'a') "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
    # UPDATE with WHERE
    "UPDATE invoices SET total = 110 WHERE id = 'd9621261-3d73-4c6a-9631-1504b1db8c96'",
    "UPDATE invoices SET total = 1 WHERE vendor_id IN (SELECT id FROM vendors WHERE name = 'Acme')",
    # dangerous-looking text that is only DATA
    "INSERT INTO vendors (name) VALUES ('x; DROP TABLE vendors')",
    "SELECT 'DROP TABLE vendors'",
    "SELECT 1 /* ; DROP TABLE vendors */",
    "SELECT 1 -- DROP TABLE vendors",
    # documented policy decisions
    "SELECT 1;;",
    "SELECT pg_sleep(10)",
]

REJECTED_QUERIES = [
    # DDL
    ("DROP TABLE invoices", "Only INSERT, UPDATE and SELECT"),
    ("drop table invoices", "Only INSERT, UPDATE and SELECT"),
    ("DROP TABLE IF EXISTS invoices CASCADE", "Only INSERT, UPDATE and SELECT"),
    ("ALTER TABLE invoices ADD COLUMN x int", "Only INSERT, UPDATE and SELECT"),
    ("CREATE TABLE evil (x int)", "Only INSERT, UPDATE and SELECT"),
    ("CREATE TABLE t AS SELECT * FROM vendors", "Only INSERT, UPDATE and SELECT"),
    ("CREATE INDEX i ON invoices (total)", "Only INSERT, UPDATE and SELECT"),
    ("TRUNCATE invoices", "Only INSERT, UPDATE and SELECT"),
    # DELETE
    ("DELETE FROM invoices", "Only INSERT, UPDATE and SELECT"),
    ("DELETE FROM invoices WHERE id = 1", "Only INSERT, UPDATE and SELECT"),
    ("DELETE FROM invoices WHERE id IN (SELECT id FROM invoices)", "Only INSERT, UPDATE and SELECT"),
    # privileges / session / server commands
    ("GRANT ALL ON invoices TO bob", "Only INSERT, UPDATE and SELECT"),
    ("REVOKE ALL ON vendors FROM bob", "Only INSERT, UPDATE and SELECT"),
    ("COPY vendors TO '/tmp/x'", "Only INSERT, UPDATE and SELECT"),
    ("SET ROLE postgres", "Only INSERT, UPDATE and SELECT"),
    ("BEGIN", "Only INSERT, UPDATE and SELECT"),
    ("VACUUM vendors", "Only INSERT, UPDATE and SELECT"),
    ("CALL some_procedure()", "Only INSERT, UPDATE and SELECT"),
    ("DO $$ BEGIN DROP TABLE vendors; END $$", "Only INSERT, UPDATE and SELECT"),
    ("EXPLAIN ANALYZE DELETE FROM invoices", "Only INSERT, UPDATE and SELECT"),
    # allowed statement types hiding something dangerous
    ("SELECT * INTO new_table FROM vendors", "INTO"),
    ("WITH d AS (DELETE FROM invoices RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE invoices SET total = 1 RETURNING *) SELECT * FROM u", "INSERT or UPDATE"),
    ("WITH i AS (INSERT INTO vendors (name) VALUES ('a') RETURNING *) SELECT * FROM i", "INSERT or UPDATE"),
    # UPDATE without a top-level WHERE
    ("UPDATE invoices SET total = 0", "WHERE"),
    ("UPDATE invoices SET total = (SELECT 1 WHERE true)", "WHERE"),
    ("UPDATE invoices SET total = 0 -- WHERE id = 1", "WHERE"),
    ("UPDATE invoices SET total = 0 /* WHERE id = 1 */", "WHERE"),
    ("UPDATE vendors SET name = 'WHERE'", "WHERE"),
    # more than one statement
    ("INSERT INTO vendors (name) VALUES ('a'); DROP TABLE vendors", "one SQL statement"),
    ("SELECT 1; SELECT 2", "one SQL statement"),
    ("SELECT 1; DELETE FROM invoices", "one SQL statement"),
    ("SELECT 1; /* sneaky */ DROP TABLE vendors;", "one SQL statement"),
    # without statements / not SQL / broken SQL
    ("", "without statements"),
    ("   ", "without statements"),
    ("\n\t", "without statements"),
    (None, "without statements"),
    (";", "no SQL statement"),
    ("-- just a comment", "no SQL statement"),
    ("/* only a comment */", "no SQL statement"),
    ("SELECT 'unterminated", "syntax error"),
    ("SELEC garbage", "Only INSERT, UPDATE and SELECT"),
    ("hello world", "Only INSERT, UPDATE and SELECT"),
]


def _make_pg_error(
    sqlstate: str | None,
    primary: str = "constraint violation",
    constraint: str | None = None,
    detail: str | None = None,
    hint: str | None = None,
) -> psycopg.Error:
    """Builds a psycopg.Error with mocked diag attributes for unit tests."""
    exc = psycopg.Error(primary)
    diag = MagicMock()
    diag.message_primary = primary
    diag.constraint_name = constraint
    diag.message_detail = detail
    diag.message_hint = hint

    type(exc).sqlstate = property(lambda self: sqlstate)
    type(exc).diag = property(lambda self: diag)
    return exc


# ------------------------------------------------------------ helper units ---
@pytest.mark.parametrize(
    "sqlstate, expected",
    [
        (None, False),      # connection/network error -> not model's fault
        ("08006", False),   # connection failure
        ("57014", False),   # statement_timeout (e.g. pg_sleep)
        ("23505", False),   # unique_violation (duplicate invoice) -> skip doc
        ("42501", False),   # insufficient_privilege -> abort & log
        ("22P02", True),    # invalid_text_representation (bad UUID/date) -> retryable
        ("23503", True),    # foreign_key_violation -> retryable
        ("23502", True),    # not_null_violation -> retryable
        ("23514", True),    # check_violation -> retryable
        ("42601", True),    # syntax_error -> retryable
        ("42703", True),    # undefined_column -> retryable
        ("42P01", True),    # undefined_table -> retryable
    ],
)
def test_is_retryable_classification(sqlstate, expected):
    assert _is_retryable(sqlstate) is expected


def test_describe_formats_all_diagnostic_fields():
    exc = _make_pg_error(
        sqlstate="23503",
        primary='insert or update on table "invoices" violates foreign key constraint',
        constraint="invoices_vendor_id_fkey",
        detail="Key (vendor_id)=(00000000-0000-0000-0000-000000000000) is not present in table \"vendors\".",
        hint="Insert the vendor first.",
    )
    msg = _describe(exc)
    assert "violates foreign key constraint" in msg
    assert "Constraint: invoices_vendor_id_fkey" in msg
    assert "Detail: Key (vendor_id)" in msg
    assert "Hint: Insert the vendor first." in msg


# --------------------------------------------------------- guardrail units ---
@pytest.mark.parametrize("sql, expected_notice", REJECTED_QUERIES)
def test_rejected_queries_never_hit_database(mock_pg_client, sql, expected_notice):
    client, mock_conn, _ = mock_pg_client

    res = execute_model_query(client, sql)

    assert isinstance(res, ModelQueryResponse)
    assert res.success is False
    assert res.retryable is True
    assert res.sqlstate is None
    assert res.db_response is None
    assert expected_notice in res.notice
    mock_conn.cursor.assert_not_called()


# --------------------------------------------------------- execution units ---
@pytest.mark.parametrize("sql", ALLOWED_QUERIES)
def test_allowed_queries_pass_guardrail_and_execute_normalized_sql(mock_pg_client, sql):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.description = [("col",)]
    mock_cursor.fetchmany.return_value = [{"col": 1}]

    res = execute_model_query(client, sql)

    assert res.success is True
    mock_cursor.execute.assert_called_once()
    executed_sql = mock_cursor.execute.call_args.args[0]
    assert isinstance(executed_sql, str) and len(executed_sql) > 0


def test_select_serializes_uuid_and_decimal_rows(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    row_id = uuid.uuid4()
    mock_cursor.description = [("id",), ("total",)]
    mock_cursor.fetchmany.return_value = [{"id": row_id, "total": Decimal("93.50")}]

    res = execute_model_query(client, "SELECT id, total FROM invoices;")

    assert res.success is True
    assert res.retryable is False
    assert res.notice == "Query succeeded. Returned 1 row(s)."
    parsed = json.loads(res.db_response)
    assert parsed == [{"id": str(row_id), "total": "93.50"}]
    mock_cursor.fetchmany.assert_called_once_with(MAX_ROWS + 1)


def test_select_truncates_when_exceeding_max_rows(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.description = [("n",)]
    # Simulate 101 rows returned by fetchmany(MAX_ROWS + 1)
    mock_cursor.fetchmany.return_value = [{"n": i} for i in range(MAX_ROWS + 1)]

    res = execute_model_query(client, "SELECT * FROM invoices;")

    assert res.success is True
    assert f"Returned {MAX_ROWS} row(s)." in res.notice
    assert f"Result truncated to the first {MAX_ROWS} rows" in res.notice
    assert len(json.loads(res.db_response)) == MAX_ROWS


def test_insert_or_update_without_returning_uses_rowcount(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.description = None
    mock_cursor.rowcount = 2

    res = execute_model_query(
        client,
        "INSERT INTO vendors (name, tax_id) VALUES ('A', '1'), ('B', '2');",
    )

    assert res.success is True
    assert res.notice == "Query succeeded. 2 row(s) affected."
    assert json.loads(res.db_response) == {"rows_affected": 2}
    mock_cursor.fetchmany.assert_not_called()


def test_database_error_returns_structured_retryable_flag(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.execute.side_effect = _make_pg_error(
        sqlstate="23503",
        primary="insert or update on table \"invoices\" violates foreign key constraint",
        constraint="invoices_vendor_id_fkey",
    )

    res = execute_model_query(
        client,
        "INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total) "
        "VALUES ('d9621261-3d73-4c6a-9631-1504b1db8c96', 'INV-1', '2026-01-01', '2026-01-31', 'USD', 100, 10, 110)",
    )

    assert res.success is False
    assert res.retryable is True
    assert res.sqlstate == "23503"
    assert res.db_response is None
    assert "invoices_vendor_id_fkey" in res.notice


def test_duplicate_key_error_is_marked_non_retryable(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.execute.side_effect = _make_pg_error(
        sqlstate="23505",
        primary="duplicate key value violates unique constraint",
        constraint="invoices_invoice_number_key",
    )

    res = execute_model_query(client, "INSERT INTO vendors (name) VALUES ('Acme');")

    assert res.success is False
    assert res.retryable is False
    assert res.sqlstate == "23505"


# ----------------------------------------------------------- integration ---
@pytest.mark.integration
def test_live_insert_returning_select_and_update_workflow(live_pg_client):
    # 1. Insert vendor and get generated UUID via RETURNING
    ins_vendor = execute_model_query(
        live_pg_client,
        "INSERT INTO vendors (name) VALUES ('Integration Test Corp') RETURNING id, name;",
    )
    assert ins_vendor.success is True, ins_vendor.notice
    vendor_rows = json.loads(ins_vendor.db_response)
    vendor_id = vendor_rows[0]["id"]

    # 2. Insert linked invoice
    ins_inv = execute_model_query(
        live_pg_client,
        f"INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total) "
        f"VALUES ('{vendor_id}', 'INV-TEST-001', '2026-01-01', '2026-01-31', 'USD', 100.00, 10.00, 110.00) "
        f"RETURNING id, total;",
    )
    assert ins_inv.success is True, ins_inv.notice
    inv_id = json.loads(ins_inv.db_response)[0]["id"]

    # 3. Update with WHERE (keep subtotal + tax == total)
    upd = execute_model_query(
        live_pg_client,
        f"UPDATE invoices SET subtotal = 110.00, total = 120.00 WHERE id = '{inv_id}';",
    )
    assert upd.success is True, upd.notice
    assert json.loads(upd.db_response) == {"rows_affected": 1}

    # 4. Verify via JOIN SELECT
    sel = execute_model_query(
        live_pg_client,
        f"SELECT v.name, i.invoice_number, i.total FROM invoices i "
        f"JOIN vendors v ON v.id = i.vendor_id WHERE i.id = '{inv_id}';",
    )
    assert sel.success is True
    rows = json.loads(sel.db_response)
    assert rows == [{"name": "Integration Test Corp", "invoice_number": "INV-TEST-001", "total": "120.00"}]


@pytest.mark.integration
def test_live_foreign_key_violation_is_retryable_and_recovers(live_pg_client):
    bad_fk = execute_model_query(
        live_pg_client,
        "INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total) "
        "VALUES ('00000000-0000-0000-0000-000000000000', 'INV-BAD-FK', '2026-01-01', '2026-01-31', 'USD', 10, 1, 11);",
    )
    assert bad_fk.success is False
    assert bad_fk.retryable is True
    assert bad_fk.sqlstate == "23503"

    # Outer transaction must still be usable thanks to conn.transaction() savepoint
    follow_up = execute_model_query(live_pg_client, "SELECT 1 AS alive;")
    assert follow_up.success is True
    assert json.loads(follow_up.db_response) == [{"alive": 1}]


@pytest.mark.integration
def test_live_pg_sleep_hits_encoder_statement_timeout(live_pg_client):
    conn = live_pg_client.get_connection()
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '100ms';")

    res = execute_model_query(live_pg_client, "SELECT pg_sleep(10);")

    assert res.success is False
    assert res.retryable is False
    assert res.sqlstate == "57014"  # query_canceled due to statement timeout
    