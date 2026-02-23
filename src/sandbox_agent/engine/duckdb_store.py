import asyncio
import logging

import duckdb

from .datasets import DATASETS, Dataset

logger = logging.getLogger(__name__)


class DuckDBStore:
    def __init__(self) -> None:
        self._conn = duckdb.connect()
        self._conn.execute("INSTALL httpfs; LOAD httpfs;")

    async def load_datasets(self) -> None:
        for ds in DATASETS:
            await self._load_dataset(ds)
        logger.info("All %d datasets loaded into DuckDB", len(DATASETS))

    async def _load_dataset(self, ds: Dataset) -> None:
        def _load() -> None:
            reader = "read_csv_auto" if ds.fmt == "csv" else "read_json_auto"
            self._conn.execute(
                f"CREATE OR REPLACE TABLE {ds.name} AS SELECT * FROM {reader}('{ds.url}')"
            )
            row = self._conn.execute(f"SELECT count(*) FROM {ds.name}").fetchone()
            count = row[0] if row else 0
            logger.info("Loaded %s: %d rows", ds.name, count)

        await asyncio.to_thread(_load)

    def execute_sql(self, query: str) -> list[dict]:
        cursor = self._conn.cursor()
        try:
            result = cursor.execute(query)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
            return [dict(zip(columns, row, strict=True)) for row in rows]
        finally:
            cursor.close()

    def get_table_names(self) -> list[str]:
        cursor = self._conn.cursor()
        try:
            rows = cursor.execute("SHOW TABLES").fetchall()
            return [row[0] for row in rows]
        finally:
            cursor.close()

    def describe_table(self, table_name: str) -> list[dict]:
        cursor = self._conn.cursor()
        try:
            rows = cursor.execute(f"DESCRIBE {table_name}").fetchall()
            return [{"column_name": r[0], "column_type": r[1], "null": r[2]} for r in rows]
        finally:
            cursor.close()

    def get_schema_context(self) -> str:
        lines = ["## Available Tables\n"]
        for ds in DATASETS:
            lines.append(f"### {ds.name}")
            lines.append(f"{ds.description}")
            lines.append(f"~{ds.rows_approx} rows\n")
            try:
                cols = self.describe_table(ds.name)
                lines.append("| Column | Type |")
                lines.append("|--------|------|")
                for c in cols:
                    lines.append(f"| {c['column_name']} | {c['column_type']} |")
            except Exception:
                lines.append("(schema unavailable)")
            lines.append("")
        return "\n".join(lines)

    def close(self) -> None:
        self._conn.close()
