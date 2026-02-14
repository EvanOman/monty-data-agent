def test_execute_sql(duckdb_store):
    result = duckdb_store.execute_sql("SELECT * FROM test_table ORDER BY id")
    assert len(result) == 3
    assert result[0]["id"] == 1
    assert result[0]["name"] == "a"


def test_get_table_names(duckdb_store):
    names = duckdb_store.get_table_names()
    assert "test_table" in names


def test_describe_table(duckdb_store):
    cols = duckdb_store.describe_table("test_table")
    col_names = [c["column_name"] for c in cols]
    assert "id" in col_names
    assert "name" in col_names


def test_schema_context(duckdb_store):
    ctx = duckdb_store.get_schema_context()
    # Schema context is built from the DATASETS list, not the actual tables
    assert isinstance(ctx, str)
