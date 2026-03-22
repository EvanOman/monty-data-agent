"""Re-export prompts from the shared planning module.

This file exists for backward compatibility so existing temporal imports
continue to work.
"""

from ..planning.prompts import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt

__all__ = ["SYNTHESIZE_SYSTEM_PROMPT", "build_plan_prompt", "build_subtask_prompt"]
