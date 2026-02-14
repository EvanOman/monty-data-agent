from sandbox_agent.sandbox.executor import execute_code


def test_simple_expression(ext_functions):
    result = execute_code("1 + 2", ext_functions)
    assert result.error is None
    assert result.output == 3
    assert result.output_type == "scalar"


def test_fetch_all(ext_functions):
    result = execute_code('fetch("test_table")', ext_functions)
    assert result.error is None
    assert result.output_type == "table"
    assert len(result.output) == 3


def test_fetch_with_where(ext_functions):
    result = execute_code('fetch("test_table", where={"id": 1})', ext_functions)
    assert result.error is None
    assert result.output_type == "table"
    assert len(result.output) == 1
    assert result.output[0]["name"] == "a"


def test_fetch_with_limit(ext_functions):
    result = execute_code('fetch("test_table", limit=2)', ext_functions)
    assert result.error is None
    assert len(result.output) == 2


def test_fetch_with_order(ext_functions):
    result = execute_code('fetch("test_table", order_by="id DESC")', ext_functions)
    assert result.error is None
    assert result.output[0]["id"] == 3


def test_count(ext_functions):
    result = execute_code('count("test_table")', ext_functions)
    assert result.error is None
    assert result.output == 3
    assert result.output_type == "scalar"


def test_count_with_where(ext_functions):
    result = execute_code('count("test_table", where={"name": "a"})', ext_functions)
    assert result.error is None
    assert result.output == 1


def test_tables_function(ext_functions):
    result = execute_code("tables()", ext_functions)
    assert result.error is None
    assert "test_table" in result.output


def test_describe_function(ext_functions):
    result = execute_code('describe("test_table")', ext_functions)
    assert result.error is None
    assert isinstance(result.output, list)
    col_names = [c["column_name"] for c in result.output]
    assert "id" in col_names


def test_syntax_error(ext_functions):
    result = execute_code("def foo(:", ext_functions)
    assert result.error is not None
    assert "Syntax" in result.error or "syntax" in result.error


def test_invalid_table(ext_functions):
    result = execute_code('fetch("nonexistent")', ext_functions)
    assert result.error is not None


def test_multi_step_python(ext_functions):
    code = """
data = fetch("test_table")
names = []
for r in data:
    names.append(r["name"])
names
"""
    result = execute_code(code, ext_functions)
    assert result.error is None
    assert result.output == ["a", "b", "c"]


def test_monty_state_serialized(ext_functions):
    result = execute_code("1 + 1", ext_functions)
    assert result.monty_state is not None
    assert isinstance(result.monty_state, bytes)
