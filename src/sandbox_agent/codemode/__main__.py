"""Entry point for standalone Code Mode MCP server.

Usage: uv run python -m sandbox_agent.codemode
"""

from .server import mcp

mcp.run()
