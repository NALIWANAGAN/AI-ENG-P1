import pytest

def test_singleton_reuses_connection(mock_pg_client):
    client, mock_conn, _ = mock_pg_client

    conn1 = client.get_connection()
    conn2 = client.get_connection()

    assert conn1 is conn2 is mock_conn


def test_reconnects_when_connection_closed(mock_pg_client, monkeypatch):
    client, first_conn, _ = mock_pg_client
    assert client.get_connection() is first_conn

    # Simulate a dropped connection
    first_conn.closed = True
    second_conn = type(first_conn)()
    second_conn.closed = False
    second_conn.broken = False
    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: second_conn)

    assert client.get_connection() is second_conn


@pytest.mark.integration
def test_live_db_select(live_pg_client):
    conn = live_pg_client.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok;")
        assert cur.fetchone() == {"ok": 1}
        