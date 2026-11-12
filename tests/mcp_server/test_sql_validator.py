import pytest

from mcp_server.sql_validator import sql_validator

ALLOWED_REASON = "This SQL query is allowed."


# ---------------------------------------------------------------- allowed ---
@pytest.mark.parametrize(
    "sql",
    [
        # SELECT basics
        "SELECT * FROM vendors;",
        "SELECT * FROM vendors",                     # no semicolon
        "select * from vendors;",                    # lowercase
        "SeLeCt * FrOm vendors",                     # mixed case
        "SELECT 1;",
        "   SELECT 1   ",                            # surrounding whitespace
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
        "INSERT INTO vendors (name, tax_id) VALUES ('A', '1'), ('B', '2')",       # multi-row
        "INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total) "
        "VALUES ('d9621261-3d73-4c6a-9631-1504b1db8c96', 'INV-1', '2026-01-01', '2026-01-31', 'USD', 100.00, 10.00, 110.00)",
        "INSERT INTO line_items (invoice_id, description, quantity, unit_price, line_total) "
        "SELECT id, 'Widget', 1.5, 0.33, 0.50 FROM invoices WHERE invoice_number = 'INV-1'",  # INSERT ... SELECT
        "INSERT INTO vendors (name) VALUES ('a') RETURNING id",
        "INSERT INTO vendors (id, name) VALUES ('d9621261-3d73-4c6a-9631-1504b1db8c96', 'a') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",                      # upsert, no WHERE needed
        # UPDATE with WHERE
        "UPDATE invoices SET total = 110 WHERE id = 'd9621261-3d73-4c6a-9631-1504b1db8c96'",
        "UPDATE invoices SET total = 1 WHERE vendor_id IN (SELECT id FROM vendors WHERE name = 'Acme')",
        # dangerous-looking text that is only DATA (proves we parse, not regex-match)
        "INSERT INTO vendors (name) VALUES ('x; DROP TABLE vendors')",
        "SELECT 'DROP TABLE vendors'",
        "SELECT 1 /* ; DROP TABLE vendors */",
        "SELECT 1 -- DROP TABLE vendors",
        # documented policy decisions
        "SELECT 1;;",                                # one real statement + an empty one: harmless
        "SELECT pg_sleep(10)",                       # allowed here; the DB role's statement_timeout stops it
    ],
)
def test_allowed(sql):
    r = sql_validator.check_sql(sql)
    assert r.allowed, r.reason
    assert r.reason == ALLOWED_REASON
    assert r.normalized_sql  # allowed queries always come back with normalized SQL


# --------------------------------------------------------------- rejected ---
# (sql, text that must appear in the reason the LLM will read)
REJECTED = [
    # DDL
    ("DROP TABLE invoices", "Only INSERT, UPDATE and SELECT"),
    ("drop table invoices", "Only INSERT, UPDATE and SELECT"),
    ("DROP TABLE IF EXISTS invoices CASCADE", "Only INSERT, UPDATE and SELECT"),
    ("ALTER TABLE invoices ADD COLUMN x int", "Only INSERT, UPDATE and SELECT"),
    ("CREATE TABLE evil (x int)", "Only INSERT, UPDATE and SELECT"),
    ("CREATE TABLE t AS SELECT * FROM vendors", "Only INSERT, UPDATE and SELECT"),   # SELECT hidden in CREATE
    ("CREATE INDEX i ON invoices (total)", "Only INSERT, UPDATE and SELECT"),
    ("TRUNCATE invoices", "Only INSERT, UPDATE and SELECT"),
    # DELETE, in all shapes
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
    ("EXPLAIN ANALYZE DELETE FROM invoices", "Only INSERT, UPDATE and SELECT"),     # EXPLAIN ANALYZE really executes
    # allowed statement types hiding something dangerous
    ("SELECT * INTO new_table FROM vendors", "INTO"),
    ("WITH d AS (DELETE FROM invoices RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE invoices SET total = 1 RETURNING *) SELECT * FROM u", "INSERT or UPDATE"),
    ("WITH i AS (INSERT INTO vendors (name) VALUES ('a') RETURNING *) SELECT * FROM i", "INSERT or UPDATE"),
    # UPDATE without a top-level WHERE
    ("UPDATE invoices SET total = 0", "WHERE"),
    ("UPDATE invoices SET total = (SELECT 1 WHERE true)", "WHERE"),                 # WHERE only inside a subquery
    ("UPDATE invoices SET total = 0 -- WHERE id = 1", "WHERE"),                     # WHERE only in a comment
    ("UPDATE invoices SET total = 0 /* WHERE id = 1 */", "WHERE"),
    ("UPDATE vendors SET name = 'WHERE'", "WHERE"),                                 # WHERE only inside a string
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


@pytest.mark.parametrize("sql, reason_part", REJECTED)
def test_rejected(sql, reason_part):
    r = sql_validator.check_sql(sql)
    assert not r.allowed
    assert reason_part in r.reason, f"unexpected reason: {r.reason}"
    assert r.normalized_sql is None  # nothing runnable is ever returned for a rejected query


# ------------------------------------------------------------ normalization ---
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("select * from vendors;", "SELECT * FROM vendors"),
        ("SELECT * FROM vendors", "SELECT * FROM vendors"),
        ("  select   1  ", "SELECT 1"),
    ],
)
def test_normalized_sql(raw, expected):
    r = sql_validator.check_sql(raw)
    assert r.allowed
    assert r.normalized_sql == expected


def test_semicolon_is_optional():
    with_semi = sql_validator.check_sql("SELECT * FROM vendors;")
    without = sql_validator.check_sql("SELECT * FROM vendors")
    assert with_semi.allowed and without.allowed
    assert with_semi.normalized_sql == without.normalized_sql
    