"""MCP server for Code Mode — exposes search and execute tools."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP

from ..engine.duckdb_store import DuckDBStore
from ..engine.executor import execute_code
from ..engine.functions import ExternalFunctions
from .registry import FUNCTION_REGISTRY, build_datasets_metadata

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    duckdb_store: DuckDBStore
    ext_functions: ExternalFunctions
    search_metadata: dict


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Initialize DuckDB and load datasets at startup."""
    logger.info("Code Mode MCP server starting...")
    duckdb_store = DuckDBStore()
    await duckdb_store.load_datasets()
    ext_functions = ExternalFunctions(duckdb_store)
    datasets = build_datasets_metadata(duckdb_store)
    search_metadata = {"functions": FUNCTION_REGISTRY, "datasets": datasets}
    logger.info("Code Mode MCP server ready")
    try:
        yield ServerContext(
            duckdb_store=duckdb_store,
            ext_functions=ext_functions,
            search_metadata=search_metadata,
        )
    finally:
        duckdb_store.close()
        logger.info("Code Mode MCP server shut down")


mcp = FastMCP("code-mode", lifespan=server_lifespan)


@mcp.tool()
def search(code: str, ctx: Context) -> str:
    """Discover available data functions and dataset schemas.

    Execute Python code against a metadata dict containing:
    - `functions`: dict of function signatures, descriptions, parameters, examples
    - `datasets`: dict of table schemas with column names and types

    Examples:
    - `list(functions.keys())` to see all function names
    - `datasets["titanic"]` to see titanic schema
    - `functions["fetch"]` to see fetch function details
    """
    server_ctx: ServerContext = ctx.request_context.lifespan_context
    metadata = server_ctx.search_metadata

    # Execute the code in a simple eval context with the metadata available
    safe_builtins = {
        "list": list,
        "dict": dict,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "len": len,
        "sorted": sorted,
        "set": set,
        "tuple": tuple,
        "enumerate": enumerate,
        "zip": zip,
        "min": min,
        "max": max,
        "sum": sum,
        "any": any,
        "all": all,
        "isinstance": isinstance,
        "type": type,
        "print": print,
    }
    namespace = {
        "__builtins__": safe_builtins,
        "functions": metadata["functions"],
        "datasets": metadata["datasets"],
    }
    try:
        result = eval(code, namespace)  # noqa: S307
        return json.dumps(result, indent=2, default=str)
    except SyntaxError:
        try:
            exec(code, namespace)  # noqa: S102
            return "Code executed (no return value — use an expression to get output)"
        except Exception as e:
            return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def execute(code: str, ctx: Context) -> str:
    """Run Python code in the Monty sandbox.

    The code can call fetch(), count(), describe(), and tables() to access datasets.
    Returns JSON with the result type, data, and any errors.
    """
    server_ctx: ServerContext = ctx.request_context.lifespan_context
    result = execute_code(code, server_ctx.ext_functions)

    response = {
        "type": result.output_type,
        "data": result.output_json,
        "error": result.error,
    }
    return json.dumps(response, default=str)
