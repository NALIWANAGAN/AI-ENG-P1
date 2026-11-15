from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from services.schema_service import ALLOWED_TABLES, SchemaService, TableInformation

schema_mcp = FastMCP("schema")


@schema_mcp.tool(
    version="Zero-point-one",
    title="Inspect Table Schema",
    name="inspect_table_schema",
    description=f"Inspect the table(s) schema arg such as columns, types, nullability, defaults, and constraints (PK, FK, UNIQUE, CHECK) for the requested allow-listed tables.\nAllowed tables: {ALLOWED_TABLES}",
    output_schema=TableInformation.model_json_schema(),
)
def inspect_table_schema_tool(
    table_names: list[str] | None = None,
    service: SchemaService = Depends(SchemaService),
) -> TableInformation:
    """
    Accepts table list string from the caller.
    Inner function validates the table existence and availability.
    Returns table information if TableInformation.success through TableInformation.table_information.
    """
    return service.inspect_table_schema(table_names)
