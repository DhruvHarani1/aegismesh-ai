"""FastMCP server exposing the four AegisMesh actuation tools.

    pip install mcp
    python -m aegismesh.mcp_server --config mesh.json          # stdio transport

Any MCP client (Claude Desktop, Claude Code, a LangGraph agent via
langchain-mcp-adapters) can then call get_mesh_topology, get_link_telemetry,
update_link_cost and quarantine_node against the live mesh.
"""

import argparse

from .config import MeshConfig
from .mesh_tools import MeshClient


def build(config_path):
    try:
        from mcp.server.fastmcp import FastMCP            # mcp 1.x
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer as FastMCP   # mcp 2.x renamed it
        except ImportError:
            raise SystemExit("The 'mcp' package is required: pip install mcp")
    client = MeshClient(MeshConfig(config_path))
    mcp = FastMCP("aegismesh")

    @mcp.tool()
    def get_mesh_topology() -> dict:
        """Active nodes, edges and real-time link costs from the live link-state database."""
        return client.get_mesh_topology()

    @mcp.tool()
    def get_link_telemetry(node_a: str, node_b: str) -> dict:
        """Packet drop %, latency variance and CRC error counts for the edge node_a-node_b."""
        return client.get_link_telemetry(node_a, node_b)

    @mcp.tool()
    def update_link_cost(node_a: str, node_b: str, new_metric: int) -> dict:
        """Override a link weight; triggers LSA flooding and Dijkstra recomputation mesh-wide."""
        return client.update_link_cost(node_a, node_b, new_metric)

    @mcp.tool()
    def quarantine_node(node_id: str, reason: str) -> dict:
        """Purge a node from every FIB and reject its raw socket traffic."""
        return client.quarantine_node(node_id, reason)

    return mcp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="mesh.json")
    build(ap.parse_args().config).run()
