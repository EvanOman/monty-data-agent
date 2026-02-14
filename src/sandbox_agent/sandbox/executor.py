import json
import logging
from dataclasses import dataclass
from typing import Any

import pydantic_monty

from ..config import MAX_MONTY_DURATION_SECS
from .functions import ExternalFunctions

logger = logging.getLogger(__name__)

EXTERNAL_FUNCTION_NAMES = ["fetch", "count", "describe", "tables"]


@dataclass
class ExecutionResult:
    output: Any = None
    output_json: str | None = None
    output_type: str = "none"
    error: str | None = None
    code: str = ""
    monty_state: bytes | None = None


def execute_code(code: str, ext_functions: ExternalFunctions) -> ExecutionResult:
    """Compile and run code in Monty with external function bridging to DuckDB."""
    try:
        m = pydantic_monty.Monty(
            code,
            external_functions=EXTERNAL_FUNCTION_NAMES,
        )
    except pydantic_monty.MontySyntaxError as e:
        return ExecutionResult(error=f"Syntax error: {e}", code=code)

    limits = pydantic_monty.ResourceLimits(max_duration_secs=MAX_MONTY_DURATION_SECS)

    try:
        state = m.start(limits=limits)

        while isinstance(state, (pydantic_monty.MontySnapshot, pydantic_monty.MontyFutureSnapshot)):
            if isinstance(state, pydantic_monty.MontySnapshot):
                try:
                    result_val = ext_functions.handle_call(
                        state.function_name, state.args, state.kwargs
                    )
                    state = state.resume(return_value=result_val)
                except Exception as e:
                    state = state.resume(exception=e)
            else:
                # MontyFutureSnapshot - shouldn't happen in sync code, but handle gracefully
                raise RuntimeError("Unexpected async pause in sync execution")

        # state is MontyComplete
        output = state.output
        monty_state = m.dump()

    except pydantic_monty.MontyRuntimeError as e:
        return ExecutionResult(error=f"Runtime error: {e}", code=code)
    except Exception as e:
        return ExecutionResult(error=str(e), code=code)

    return _classify_output(output, code, monty_state)


def _classify_output(output: Any, code: str, monty_state: bytes) -> ExecutionResult:
    """Classify and serialize the output from Monty execution."""
    if output is None:
        return ExecutionResult(output=None, output_type="none", code=code, monty_state=monty_state)

    if isinstance(output, list) and output and isinstance(output[0], dict):
        return ExecutionResult(
            output=output,
            output_json=json.dumps(output, default=str),
            output_type="table",
            code=code,
            monty_state=monty_state,
        )

    if isinstance(output, dict):
        return ExecutionResult(
            output=output,
            output_json=json.dumps(output, default=str),
            output_type="dict",
            code=code,
            monty_state=monty_state,
        )

    if isinstance(output, (int, float, str, bool)):
        return ExecutionResult(
            output=output,
            output_json=json.dumps(output, default=str),
            output_type="scalar",
            code=code,
            monty_state=monty_state,
        )

    # Fallback: try JSON serialization
    try:
        output_json = json.dumps(output, default=str)
    except (TypeError, ValueError):
        output_json = json.dumps(str(output))

    return ExecutionResult(
        output=output,
        output_json=output_json,
        output_type="other",
        code=code,
        monty_state=monty_state,
    )
