import json
import logging

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from client.postgres_client import PostgresClient
from guardrail.sql_validator import sql_validator

logger = logging.getLogger(__name__)

MAX_ROWS = 100


class ModelQueryResponse(BaseModel):
    success: bool = False
    retryable: bool = False  # the retry loop (in the client) reads this
    sqlstate: str | None = Field(default=None, description="Postgres error code, if any.")
    notice: str
    db_response: str | None = Field(default=None, description="Postgres response to the model's query.")


def _is_retryable(sqlstate: str | None) -> bool:
    """Model mistakes can be fixed by retrying; infrastructure problems can't."""
    if sqlstate is None:
        return False  # e.g. connection failure
    if sqlstate in {"23505", "42501"}:
        return False  # duplicate -> skip the document; permission denied -> abort and log
    return sqlstate[:2] in {"22", "23", "42"}  # bad data, constraint failed, bad SQL or names


def _describe(exc: psycopg.Error) -> str:
    """Everything useful in a Postgres error, so the model can fix its query."""
    d = exc.diag
    parts = [d.message_primary or str(exc)]
    if d.constraint_name:
        parts.append(f"Constraint: {d.constraint_name}")
    if d.message_detail:
        parts.append(f"Detail: {d.message_detail}")
    if d.message_hint:
        parts.append(f"Hint: {d.message_hint}")
    return " | ".join(parts)


def execute_model_query(dbc: PostgresClient, query: str | None) -> ModelQueryResponse:
    """Run ONE INSERT, UPDATE or SELECT against the invoice database.

    UPDATE needs a WHERE clause. Returns the rows or the exact database error.
    """
    verdict = sql_validator.check_sql(query)
    if not verdict.allowed:
        logger.warning("Guardrail rejected query: %s", verdict.notice)
        return ModelQueryResponse(success=False, retryable=True, notice=verdict.notice)

    try:
        conn = dbc.get_connection()
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute(verdict.normalized_sql)  # run what sqlglot parsed, not the raw string

            if cur.description is not None:  # SELECT, or INSERT/UPDATE ... RETURNING
                rows = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(rows) > MAX_ROWS
                rows = rows[:MAX_ROWS]
                note = f"Query succeeded. Returned {len(rows)} row(s)."
                if truncated:
                    note += f" Result truncated to the first {MAX_ROWS} rows; add a WHERE or LIMIT."
                return ModelQueryResponse(success=True, notice=note, db_response=json.dumps(rows, default=str))

            return ModelQueryResponse(
                success=True,
                notice=f"Query succeeded. {cur.rowcount} row(s) affected.",
                db_response=json.dumps({"rows_affected": cur.rowcount}),
            )
    except psycopg.Error as exc:
        return ModelQueryResponse(
            success=False,
            retryable=_is_retryable(exc.sqlstate),
            sqlstate=exc.sqlstate,
            notice=f"Database error: {_describe(exc)}",
        )
        