"""Safe stdio MCP server used by OpenPPX eval baselines."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP(name="openppx-eval-mcp")


@server.tool()
def echo_context(token: str) -> dict[str, str]:
    """Echo one eval token for MCP trajectory validation."""
    return {"status": "ok", "token": token}


if __name__ == "__main__":
    server.run(transport="stdio")
