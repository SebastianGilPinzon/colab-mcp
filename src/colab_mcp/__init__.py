from fastmcp import FastMCP
import asyncio

from colab_mcp import runtime

mcp = FastMCP(name="ColabMCP")

async def setup():
    print("using mcp server: %s" % runtime.mcp)
    await mcp.import_server(runtime.mcp, prefix="runtime")

def main() -> None:
    asyncio.run(setup())
    mcp.run()
