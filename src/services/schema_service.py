from typing import Any

from fastmcp.dependencies import Depends
from pydantic import BaseModel, Field

from repositories.schema_repo import SchemaRepository

# define not allowed tables
ALLOWED_TABLES = {
    "vendors",
    "invoices",
    "line_items",
}


class TableInformation(BaseModel):
    success: bool = False
    notice: str = Field(..., description="Schema response notice")
    not_allowed_tables: list[str] | None = Field(
        default=None,
        description="Not allowed tables might accidentally passed to the tool.",
    )
    table_information: dict | None = Field(
        default=None,
        description="Table information including columns, types, nullability, defaults, and constraints (PK, FK, UNIQUE, CHECK).",
    )


class SchemaService:
    def __init__(self, repo: SchemaRepository = Depends(SchemaRepository)):
        self._repo = repo

    def inspect_table_schema(
        self,
        table_names: list[str] | None = None,
    ) -> TableInformation:
        """
        Returns columns, types, nullability, defaults, and constraints (PK, FK, UNIQUE, CHECK)
        for the requested allow-listed tables.
        """
        requested: set[str] = set()
        not_allowed_tables: list[str] = []

        if table_names is None:
            requested = set(ALLOWED_TABLES)
        else:
            for t in table_names:
                cleaned = t.strip().lower()
                if cleaned in ALLOWED_TABLES:
                    requested.add(cleaned)
                else:
                    not_allowed_tables.append(t)

        not_allowed_table_notice = (
            f"Unknown or disallowed table(s): {sorted(not_allowed_tables)}. "
            f"Allowed tables: {sorted(ALLOWED_TABLES)}."
            if not_allowed_tables
            else ""
        )

        # If no valid tables remain, return early without querying the DB
        if not requested:
            return TableInformation(
                success=False,
                not_allowed_tables=not_allowed_tables or None,
                notice=f"Error while trying to extract table information. {not_allowed_table_notice}".strip(),
                table_information=None,
            )

        table_information: dict[str, Any] = {}
        for table in sorted(requested):
            table_information[table] = self._repo.fetch_table_schema(table)

        extra_warning = (
            f" Do not use this table for next attempt: {not_allowed_table_notice}"
            if not_allowed_table_notice
            else ""
        )

        return TableInformation(
            success=True,
            not_allowed_tables=not_allowed_tables or None,
            notice=f"These are the table definitions. Use this info to send a proper query.{extra_warning}",
            table_information=table_information,
        )
