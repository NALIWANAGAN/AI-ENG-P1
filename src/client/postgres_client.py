import os
import psycopg
from psycopg.rows import dict_row


class PostgresClient:
    _instance: "PostgresClient | None" = None
    _conn: psycopg.Connection | None = None

    def __init__(self):
        self.dbc = os.getenv("POSTGRES_URL")

    def __new__(cls) -> "PostgresClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.dbc = os.getenv("POSTGRES_URL")
        return cls._instance

    def get_connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            if not self.dbc:
                raise RuntimeError("POSTGRES_URL is not set.")
            self._conn = psycopg.connect(
                self.dbc,
                row_factory=dict_row,
                autocommit=True,
            )
        return self._conn


db_client = PostgresClient()