from typing import Any

import psycopg
from fastmcp.dependencies import Depends

from dependencies import get_db_connection

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


class SchemaRepository:
    def __init__(self, conn: psycopg.Connection = Depends(get_db_connection)):
        self._conn = conn

    def fetch_table_schema(self, table: str) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(COLUMNS_SQL, (table,))
            columns = cur.fetchall()

            cur.execute(CONSTRAINTS_SQL, (table,))
            constraints = cur.fetchall()

        return {
            "columns": columns,
            "constraints": constraints,
        }
