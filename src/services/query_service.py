import json
import logging

import psycopg
from fastmcp.dependencies import Depends, Shared
from pydantic import BaseModel, Field

from dependencies import get_sql_validator
from guardrail.sql_validator import ModelSQLValidator
from repositories.query_repo import QueryRepository

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


class QueryService:
    def __init__(
        self,
        repo: QueryRepository = Depends(QueryRepository),
        validator: ModelSQLValidator = Shared(get_sql_validator),
    ):
        self._repo = repo
        self._validator = validator

    def execute_model_query(self, query: str | None) -> ModelQueryResponse:
        """Run ONE INSERT, UPDATE or SELECT against the invoice database.

        UPDATE needs a WHERE clause. Returns the rows or the exact database error.
        """
        verdict = self._validator.check_sql(query)
        if not verdict.allowed:
            logger.warning("Guardrail rejected query: %s", verdict.notice)
            return ModelQueryResponse(success=False, retryable=True, notice=verdict.notice)

        try:
            rows, rowcount, truncated = self._repo.execute_statement(verdict.normalized_sql, MAX_ROWS)

            if rows is not None:  # SELECT, or INSERT/UPDATE ... RETURNING
                note = f"Query succeeded. Returned {len(rows)} row(s)."
                if truncated:
                    note += f" Result truncated to the first {MAX_ROWS} rows; add a WHERE or LIMIT."
                return ModelQueryResponse(success=True, notice=note, db_response=json.dumps(rows, default=str))

            return ModelQueryResponse(
                success=True,
                notice=f"Query succeeded. {rowcount} row(s) affected.",
                db_response=json.dumps({"rows_affected": rowcount}),
            )
        except psycopg.Error as exc:
            return ModelQueryResponse(
                success=False,
                retryable=_is_retryable(exc.sqlstate),
                sqlstate=exc.sqlstate,
                notice=f"Database error: {_describe(exc)}",
            )
