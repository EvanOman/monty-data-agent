from claude_agent_sdk import tool


@tool(
    "execute_code",
    "Execute Python code in the Monty sandbox. The code can call sql(), describe(), and tables() to interact with DuckDB datasets. The last expression value is returned as the result.",
    {"code": str},
)
async def execute_code_tool(args: dict) -> dict:
    # This is a placeholder — the actual handler is wired up in client.py
    # where we have access to the executor and DuckDB store
    raise NotImplementedError("Tool handler must be overridden in client.py")
