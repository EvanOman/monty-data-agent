import logging
import re

logger = logging.getLogger(__name__)


class ExternalFunctions:
    """Handles external function calls from Monty, routing them to DuckDB."""

    def __init__(self, duckdb_store) -> None:
        self._db = duckdb_store

    def handle_call(self, function_name: str, args: tuple, kwargs: dict) -> object:
        handler = getattr(self, f"_handle_{function_name}", None)
        if handler is None:
            raise ValueError(f"Unknown external function: {function_name}")
        return handler(*args, **kwargs)

    def _handle_fetch(
        self, table_name: str, columns=None, where=None, order_by=None, limit=None
    ) -> list[dict]:
        """Fetch rows from a table with optional filtering."""
        valid_tables = self._db.get_table_names()
        if table_name not in valid_tables:
            raise ValueError(f"Unknown table: {table_name}. Available: {', '.join(valid_tables)}")

        col_expr = "*"
        if columns:
            for c in columns:
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", c):
                    raise ValueError(f"Invalid column name: {c}")
            col_expr = ", ".join(columns)

        query = f"SELECT {col_expr} FROM {table_name}"

        if where and isinstance(where, dict):
            conditions = []
            for col, val in where.items():
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", col):
                    raise ValueError(f"Invalid column name: {col}")
                if isinstance(val, str):
                    safe_val = val.replace("'", "''")
                    conditions.append(f"{col} = '{safe_val}'")
                elif isinstance(val, (int, float)):
                    conditions.append(f"{col} = {val}")
                elif val is None:
                    conditions.append(f"{col} IS NULL")
            if conditions:
                query += " WHERE " + " AND ".join(conditions)

        if order_by:
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*(\s+(ASC|DESC))?$", order_by, re.IGNORECASE):
                raise ValueError(f"Invalid order_by: {order_by}")
            query += f" ORDER BY {order_by}"

        if limit is not None:
            query += f" LIMIT {int(limit)}"

        logger.info("Fetch query: %s", query[:200])
        return self._db.execute_sql(query)

    def _handle_count(self, table_name: str, where=None) -> int:
        """Count rows in a table."""
        valid_tables = self._db.get_table_names()
        if table_name not in valid_tables:
            raise ValueError(f"Unknown table: {table_name}. Available: {', '.join(valid_tables)}")

        query = f"SELECT COUNT(*) as cnt FROM {table_name}"

        if where and isinstance(where, dict):
            conditions = []
            for col, val in where.items():
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", col):
                    raise ValueError(f"Invalid column name: {col}")
                if isinstance(val, str):
                    safe_val = val.replace("'", "''")
                    conditions.append(f"{col} = '{safe_val}'")
                elif isinstance(val, (int, float)):
                    conditions.append(f"{col} = {val}")
                elif val is None:
                    conditions.append(f"{col} IS NULL")
            if conditions:
                query += " WHERE " + " AND ".join(conditions)

        result = self._db.execute_sql(query)
        return result[0]["cnt"] if result else 0

    def _handle_describe(self, table_name: str) -> list[dict]:
        logger.info("Describing table: %s", table_name)
        return self._db.describe_table(table_name)

    def _handle_tables(self) -> list[str]:
        return self._db.get_table_names()
