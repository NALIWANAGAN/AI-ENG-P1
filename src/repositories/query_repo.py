import psycopg
from fastmcp.dependencies import Depends
from psycopg.rows import dict_row

from dependencies import get_db_connection


class QueryRepository:
    def __init__(self, conn: psycopg.Connection = Depends(get_db_connection)):
        self._conn = conn

    def execute_statement(
        self,
        normalized_sql: str,
        max_rows: int,
    ) -> tuple[list[dict[str, object]] | None, int, bool]:
        with self._conn.transaction(), self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(normalized_sql)  # run what sqlglot parsed, not the raw string

            if cur.description is not None:  # SELECT, or INSERT/UPDATE ... RETURNING
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                return rows[:max_rows], cur.rowcount, truncated

            return None, cur.rowcount, False
