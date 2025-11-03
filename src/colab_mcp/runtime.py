from fastmcp import FastMCP

# Create an MCP server
mcp = FastMCP("runtime")


# Add an eval tool
@mcp.tool()
def eval(code: str):
    """(Eventually) Evaluates code in a Colab kernel."""
    # But for now, just evals here.
    return eval(code)
