from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from services.query_service import ModelQueryResponse, QueryService

query_mcp = FastMCP("query")


@query_mcp.tool(
    version="Zero-point-one",
    title="Execute Model Query to Postgres",
    name="execute_model_query",
    description="Execute raw model generated query. Raw string query were guardrailed by SQLGlot returning informative & decisive error response directly from postgres or SQLGlot.\nRun ONE INSERT, UPDATE or SELECT against the invoice database. UPDATE needs a WHERE clause.",
    output_schema=ModelQueryResponse.model_json_schema(),
)
def execute_model_query_tool(
    query: str | None,
    service: QueryService = Depends(QueryService),
) -> ModelQueryResponse:
    """
    Accepts query string from the caller.
    Inner function execute the query if passed the SQLGlot validation and postgres constraints.\n
    3 consecutive retries are possible. Returns notice if retry is not possible e.g. bad data, constraint failed, bad sql or names.
    Returns postgres response if success.
    """
    return service.execute_model_query(query)
