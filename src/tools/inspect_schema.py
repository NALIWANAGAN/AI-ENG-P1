from typing import Any
from pydantic import BaseModel, Field
from client.postgres_client import PostgresClient

# define not allowed tables
ALLOWED_TABLES = {
    "vendors",
    "invoices",
    "line_items"
}

# extract column definition
COLUMNS_SQL = """
select
    column_name,
    data_type,
    udt_name,
    character_maximum_length AS max_length,
    is_nullable = 'YES' AS nullable,
    column_default AS default_value
from information_schema.columns
where table_schema = 'public'
    and table_name = %s
order by ordinal_position;
"""

# extract table constraints
CONSTRAINTS_SQL = """
select
    con.conname AS constraint_name,
    case con.contype
        when 'p' then 'primary key'
        when 'f' then 'foreign key'
        when 'u' then 'unique'
        when 'c' then 'check'
    end as constraint_type,
    pg_get_constraintdef(con.oid, true) as definition
from pg_catalog.pg_constraint con
join pg_catalog.pg_class rel on rel.oid = con.conrelid
join pg_catalog.pg_namespace nsp on nsp.oid = con.connamespace
where nsp.nspname = 'public'
  and rel.relname = %s
  and con.contype IN ('p', 'f', 'u', 'c')
order by con.contype, con.conname;
"""

class TableInformation(BaseModel):
    success: bool = False
    notice: str = Field(..., description="Schema response notice")
    not_allowed_tables: list[str] | None = Field(
        default=None,
        description="Not allowed tables might accidentally passed to the tool."
    )
    table_information: dict | None = Field(
        default=None, 
        description="Table information including columns, types, nullability, defaults, and constraints (PK, FK, UNIQUE, CHECK)."
    )

class InspectSchema:
    def __init__(self, dbc: PostgresClient):
        self.dbc = dbc

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

        conn = self.dbc.get_connection()
        with conn.cursor() as cur:
            for table in sorted(requested):
                cur.execute(COLUMNS_SQL, (table,))
                columns = cur.fetchall()

                cur.execute(CONSTRAINTS_SQL, (table,))
                constraints = cur.fetchall()

                table_information[table] = {
                    "columns": columns,
                    "constraints": constraints,
                }

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
    