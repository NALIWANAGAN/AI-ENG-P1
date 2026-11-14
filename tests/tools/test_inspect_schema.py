import pytest
from pydantic import ValidationError
from tools.inspect_schema import (
    ALLOWED_TABLES,
    COLUMNS_SQL,
    CONSTRAINTS_SQL,
    InspectSchema,
    TableInformation,
)


# ------------------------------------------------------------------ unit ---
def test_inspect_all_tables_when_none_passed(mock_pg_client):
    client, mock_conn, mock_cursor = mock_pg_client
    mock_cursor.fetchall.side_effect = [
        [{"column_name": "id", "data_type": "uuid"}],  # invoices cols
        [{"constraint_type": "primary key"}],          # invoices constraints
        [{"column_name": "id", "data_type": "uuid"}],  # line_items cols
        [{"constraint_type": "foreign key"}],          # line_items constraints
        [{"column_name": "id", "data_type": "uuid"}],  # vendors cols
        [{"constraint_type": "primary key"}],          # vendors constraints
    ]

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(table_names=None)

    assert isinstance(result, TableInformation)
    assert result.success is True
    assert result.not_allowed_tables is None
    assert set(result.table_information.keys()) == ALLOWED_TABLES
    # 3 tables * 2 queries (columns + constraints) = 6 execute calls
    assert mock_cursor.execute.call_count == 6


def test_inspect_single_valid_table(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    fake_cols = [{"column_name": "name", "data_type": "text", "nullable": False}]
    fake_cons = [{"constraint_name": "vendors_pkey", "constraint_type": "primary key"}]
    mock_cursor.fetchall.side_effect = [fake_cols, fake_cons]

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(["vendors"])

    assert result.success is True
    assert result.not_allowed_tables is None
    assert result.table_information == {
        "vendors": {
            "columns": fake_cols,
            "constraints": fake_cons,
        }
    }
    assert mock_cursor.execute.call_args_list[0].args == (COLUMNS_SQL, ("vendors",))
    assert mock_cursor.execute.call_args_list[1].args == (CONSTRAINTS_SQL, ("vendors",))


def test_normalizes_whitespace_case_and_duplicates(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.fetchall.side_effect = [[], [], [], []]

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(["  VENDORS ", "vendors", "Invoices\n"])

    assert result.success is True
    assert result.not_allowed_tables is None
    assert list(result.table_information.keys()) == ["invoices", "vendors"]
    # Deduplicated: only 2 tables * 2 queries = 4 calls
    assert mock_cursor.execute.call_count == 4


def test_all_disallowed_tables_exits_early_without_db_call(mock_pg_client):
    client, mock_conn, _ = mock_pg_client

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(["users", "pg_shadow"])

    assert result.success is False
    assert result.table_information is None
    assert result.not_allowed_tables == ["users", "pg_shadow"]
    assert "Unknown or disallowed table(s)" in result.notice
    # Must never open a cursor or hit Postgres
    mock_conn.cursor.assert_not_called()


def test_empty_list_returns_failure_without_db_call(mock_pg_client):
    client, mock_conn, _ = mock_pg_client

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema([])

    assert result.success is False
    assert result.table_information is None
    assert result.not_allowed_tables is None
    mock_conn.cursor.assert_not_called()


def test_mixed_valid_and_disallowed_tables(mock_pg_client):
    client, _, mock_cursor = mock_pg_client
    mock_cursor.fetchall.side_effect = [
        [{"column_name": "id", "data_type": "uuid"}],
        [{"constraint_type": "primary key"}],
    ]

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(["vendors", "secret_audit_log"])

    assert result.success is True
    assert list(result.table_information.keys()) == ["vendors"]
    assert result.not_allowed_tables == ["secret_audit_log"]
    assert "Do not use this table for next attempt" in result.notice
    assert "secret_audit_log" in result.notice


def test_sql_injection_table_name_blocked_by_allowlist(mock_pg_client):
    client, mock_conn, _ = mock_pg_client

    inspector = InspectSchema(client)
    result = inspector.inspect_table_schema(["vendors; DROP TABLE vendors;--"])

    assert result.success is False
    assert result.table_information is None
    assert result.not_allowed_tables == ["vendors; DROP TABLE vendors;--"]
    mock_conn.cursor.assert_not_called()


# ----------------------------------------------------------- integration ---
@pytest.mark.integration
def test_live_schema_inspection_returns_real_columns_and_fks(live_pg_client):
    inspector = InspectSchema(live_pg_client)
    result = inspector.inspect_table_schema(["vendors", "invoices", "line_items"])

    assert result.success is True
    assert set(result.table_information.keys()) == {"vendors", "invoices", "line_items"}

    # Verify real columns exist on invoices
    invoice_cols = {c["column_name"] for c in result.table_information["invoices"]["columns"]}
    assert {"id", "vendor_id", "invoice_number", "total"}.issubset(invoice_cols)

    # Verify foreign key constraint is captured on line_items -> invoices
    line_item_fks = [
        c["definition"]
        for c in result.table_information["line_items"]["constraints"]
        if c["constraint_type"] == "foreign key"
    ]
    assert any("REFERENCES invoices" in fk for fk in line_item_fks)
    