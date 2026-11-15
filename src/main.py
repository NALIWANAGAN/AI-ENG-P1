from fastmcp import FastMCP

from tools.query_tools import query_mcp
from tools.schema_tools import schema_mcp

mcp = FastMCP(name="invoice-extractor-mcp", version="0.1.0")
mcp.mount(schema_mcp)
mcp.mount(query_mcp)

if __name__ == "__main__":
    mcp.run()
