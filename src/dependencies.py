import psycopg
from fastmcp.dependencies import Shared

from client.postgres_client import PostgresClient
from guardrail.sql_validator import ModelSQLValidator


def get_postgres_client() -> PostgresClient:
    return PostgresClient()


def get_db_connection(
    dbc: PostgresClient = Shared(get_postgres_client),
) -> psycopg.Connection:
    return dbc.get_connection()


def get_sql_validator() -> ModelSQLValidator:
    return ModelSQLValidator()
