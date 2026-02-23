"""Function metadata registry for Code Mode's search tool."""

FUNCTION_REGISTRY = {
    "fetch": {
        "signature": "fetch(table, columns=None, where=None, order_by=None, limit=None) -> list[dict]",
        "description": "Fetch rows from a table. Returns list of dicts (one per row).",
        "parameters": {
            "table": "str - table name",
            "columns": "list[str] | None - columns to select (default: all)",
            "where": "dict | None - equality filters, e.g. {'survived': 1, 'sex': 'female'}",
            "order_by": "str | None - column with optional ASC/DESC, e.g. 'fare DESC'",
            "limit": "int | None - max rows to return",
        },
        "examples": [
            'fetch("titanic")',
            'fetch("titanic", columns=["name", "fare"], order_by="fare DESC", limit=10)',
            'fetch("titanic", where={"survived": 1, "sex": "female"})',
        ],
    },
    "count": {
        "signature": "count(table, where=None) -> int",
        "description": "Count rows in a table, optionally filtered.",
        "parameters": {
            "table": "str - table name",
            "where": "dict | None - equality filters",
        },
        "examples": [
            'count("titanic")',
            'count("titanic", where={"survived": 1})',
        ],
    },
    "describe": {
        "signature": "describe(table_name) -> list[dict]",
        "description": "Get column metadata (name, type, nullable) for a table.",
        "parameters": {
            "table_name": "str - table name",
        },
        "examples": [
            'describe("titanic")',
        ],
    },
    "tables": {
        "signature": "tables() -> list[str]",
        "description": "List all available table names.",
        "parameters": {},
        "examples": [
            "tables()",
        ],
    },
}


def build_datasets_metadata(duckdb_store) -> dict:
    """Build dataset metadata dict from DuckDB for the search tool."""
    datasets = {}
    for name in duckdb_store.get_table_names():
        cols = duckdb_store.describe_table(name)
        datasets[name] = {
            "columns": {c["column_name"]: c["column_type"] for c in cols},
        }
    return datasets
