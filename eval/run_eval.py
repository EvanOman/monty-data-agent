"""Evaluation suite for all orchestration modes.

Sends the same queries to each mode multiple times, collecting:
- Success/failure
- Latency (wall clock + server-reported)
- Step counts (turns, tool calls, artifacts)
- Plan structure (task count, batch count)
- Response text for qualitative comparison

Usage:
    uv run python eval/run_eval.py [--port 19878] [--runs 3] [--output eval/results.json]
"""

import argparse
import asyncio
import json
import sys
import time

import httpx

# --- Test queries: simple, medium, complex ---

QUERIES = [
    {
        "id": "simple",
        "question": "What is the average age of Titanic passengers?",
        "expected_type": "scalar",  # single number
    },
    {
        "id": "medium",
        "question": "Compare survival rates by passenger class on the Titanic. Which class had the highest survival rate?",
        "expected_type": "table",  # multi-row comparison
    },
    {
        "id": "complex",
        "question": "For the Titanic dataset, compute the average age and survival rate by class, then identify which class had the best combination of younger average age and higher survival rate.",
        "expected_type": "multi_step",  # needs parallel subtasks
    },
]

MODES = [
    "standard",
    "codemode",
    "pydantic_ai",
    # "temporal",  # Excluded: needs Temporal serialization fix for dataclass round-trip
    "parallel",
    "pydantic_graph_mode",
    "graph_state",
]


async def run_single_query(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    question: str,
    timeout: float = 120,
) -> dict:
    """Send a single query and collect all SSE events. Returns a result dict."""
    t_start = time.time()

    events = []
    text_parts = []
    artifacts = []
    code_blocks = []
    errors = []
    status_msgs = []
    current_event = None

    try:
        async with client.stream(
            "POST",
            f"{base_url}/api/chat",
            json={"message": question, "mode": mode},
            timeout=timeout,
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    etype = current_event or "unknown"
                    events.append({"event": etype, "data": data})

                    if etype == "text":
                        text_parts.append(data)
                    elif etype == "artifact":
                        try:
                            artifacts.append(json.loads(data))
                        except json.JSONDecodeError:
                            artifacts.append({"raw": data})
                    elif etype == "code":
                        code_blocks.append(data)
                    elif etype == "error":
                        errors.append(data)
                    elif etype == "status":
                        status_msgs.append(data)

    except httpx.ReadTimeout:
        errors.append("TIMEOUT")
    except Exception as e:
        errors.append(f"CONNECTION_ERROR: {e}")

    elapsed = time.time() - t_start
    full_text = "".join(text_parts)

    # Extract timing from done event
    timing = {}
    for evt in events:
        if evt.get("event") == "done":
            try:
                done_data = json.loads(evt.get("data", "{}"))
                timing = done_data.get("timing", {})
            except json.JSONDecodeError:
                pass

    # Determine success
    success = len(errors) == 0 and len(full_text) > 20

    return {
        "mode": mode,
        "success": success,
        "wall_time_s": round(elapsed, 2),
        "server_time_ms": timing.get("total_ms", 0),
        "turns": timing.get("turns", 0),
        "tool_calls": timing.get("tool_calls", 0),
        "artifact_count": len(artifacts),
        "code_block_count": len(code_blocks),
        "text_length": len(full_text),
        "text_preview": full_text[:500] if full_text else "",
        "errors": errors,
        "status_messages": status_msgs,
        "plan": timing.get("plan", []),
        "event_count": len(events),
    }


async def run_eval(base_url: str, num_runs: int) -> dict:
    """Run the full evaluation suite."""
    all_results = {}

    for query in QUERIES:
        query_id = query["id"]
        question = query["question"]
        all_results[query_id] = {
            "question": question,
            "expected_type": query["expected_type"],
            "runs": {},
        }

        for mode in MODES:
            mode_results = []
            print(f"\n  [{query_id}] {mode}: ", end="", flush=True)

            for _run_idx in range(num_runs):
                async with httpx.AsyncClient() as client:
                    result = await run_single_query(client, base_url, mode, question)
                    mode_results.append(result)
                    status = "OK" if result["success"] else "FAIL"
                    print(f"{status}({result['wall_time_s']}s) ", end="", flush=True)

            all_results[query_id]["runs"][mode] = mode_results

    return all_results


def compute_summary(all_results: dict) -> dict:
    """Compute aggregate statistics from raw results."""
    summary = {}

    for query_id, query_data in all_results.items():
        summary[query_id] = {}
        for mode, runs in query_data["runs"].items():
            successes = [r for r in runs if r["success"]]
            failures = [r for r in runs if not r["success"]]

            avg_wall = sum(r["wall_time_s"] for r in successes) / len(successes) if successes else 0
            avg_server = (
                sum(r["server_time_ms"] for r in successes) / len(successes) if successes else 0
            )
            avg_turns = sum(r["turns"] for r in successes) / len(successes) if successes else 0
            avg_tools = sum(r["tool_calls"] for r in successes) / len(successes) if successes else 0
            avg_artifacts = (
                sum(r["artifact_count"] for r in successes) / len(successes) if successes else 0
            )
            avg_text_len = (
                sum(r["text_length"] for r in successes) / len(successes) if successes else 0
            )

            # Plan details from successful runs
            plan_task_counts = []
            for r in successes:
                if r.get("plan"):
                    plan_task_counts.append(len(r["plan"]))

            summary[query_id][mode] = {
                "success_rate": f"{len(successes)}/{len(runs)}",
                "avg_wall_time_s": round(avg_wall, 2),
                "avg_server_time_ms": round(avg_server),
                "avg_turns": round(avg_turns, 1),
                "avg_tool_calls": round(avg_tools, 1),
                "avg_artifacts": round(avg_artifacts, 1),
                "avg_text_length": round(avg_text_len),
                "avg_plan_tasks": round(sum(plan_task_counts) / len(plan_task_counts), 1)
                if plan_task_counts
                else "N/A",
                "errors": [e for r in failures for e in r.get("errors", [])],
            }

    return summary


def print_summary_table(summary: dict) -> None:
    """Print a formatted comparison table."""
    for query_id, modes in summary.items():
        print(f"\n{'=' * 100}")
        print(f"  Query: {query_id}")
        print(f"{'=' * 100}")
        print(
            f"  {'Mode':<22} {'Success':<10} {'Wall(s)':<10} {'Server(ms)':<12} "
            f"{'Turns':<8} {'Tools':<8} {'Arts':<8} {'Text':<8} {'Plan Tasks':<10}"
        )
        print("-" * 100)
        for mode in MODES:
            if mode in modes:
                m = modes[mode]
                print(
                    f"  {mode:<22} {m['success_rate']:<10} {m['avg_wall_time_s']:<10} "
                    f"{m['avg_server_time_ms']:<12} {m['avg_turns']:<8} {m['avg_tool_calls']:<8} "
                    f"{m['avg_artifacts']:<8} {m['avg_text_length']:<8} {str(m['avg_plan_tasks']):<10}"
                )
                if m["errors"]:
                    for err in m["errors"][:2]:
                        print(f"    ERROR: {err[:80]}")


async def main():
    parser = argparse.ArgumentParser(description="Evaluate all orchestration modes")
    parser.add_argument("--port", type=int, default=19878, help="App port")
    parser.add_argument("--runs", type=int, default=3, help="Runs per query per mode")
    parser.add_argument("--output", type=str, default="eval/results.json", help="Output file")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}"

    # Verify server is reachable
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/api/conversations", timeout=5)
            resp.raise_for_status()
    except Exception as e:
        print(f"ERROR: Cannot reach server at {base_url}: {e}")
        print(
            f"Start the server with: PORT={args.port} uv run uvicorn sandbox_agent.main:app --port {args.port}"
        )
        sys.exit(1)

    print(f"Evaluation suite: {len(QUERIES)} queries x {len(MODES)} modes x {args.runs} runs")
    print(f"Server: {base_url}")
    print(f"Total API calls: {len(QUERIES) * len(MODES) * args.runs}")

    all_results = await run_eval(base_url, args.runs)

    # Save raw results
    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n\nRaw results saved to {args.output}")

    # Compute and print summary
    summary = compute_summary(all_results)

    # Save summary
    summary_path = args.output.replace(".json", "_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print_summary_table(summary)


if __name__ == "__main__":
    asyncio.run(main())
