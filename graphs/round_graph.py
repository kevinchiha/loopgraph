"""The loop inside the node: produce → gate → correct, capped, then escalate.

Pure orchestration — the executor call (`exec_fn`) and gate run (`gate_fn`) are
injected, so this graph is testable with fakes and contains no Claude or
subprocess code of its own. Production wiring lives in activities/execute_round.py.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph

ExecFn = Callable[[str, str | None], Awaitable[dict]]
GateFn = Callable[[], Awaitable[list[dict]]]

MAX_ATTEMPTS = 3


class RoundState(TypedDict, total=False):
    prompt: str            # assembled base prompt (contract + brief + constraints + work item)
    attempt: int
    feedback: str | None   # red-gate output fed into the next produce
    result: dict           # last produce result (claims, summary, ...)
    gate_results: list[dict]
    status: str            # "running" | "green" | "escalated"


def build_round_graph(exec_fn: ExecFn, gate_fn: GateFn, max_attempts: int = MAX_ATTEMPTS):
    async def produce(state: RoundState) -> dict:
        result = await exec_fn(state["prompt"], state.get("feedback"))
        return {"result": result, "attempt": state["attempt"] + 1}

    async def gate(state: RoundState) -> dict:
        results = await gate_fn()
        green = all(g["status"] == "green" for g in results)
        return {"gate_results": results, "status": "green" if green else "running"}

    def _why(g: dict) -> str:
        """What the executor is told about one red gate.

        A timed-out gate has exit_code None and (before the tail was kept) nothing
        in output_tail; its only diagnostic lives in `note`. Leaving that out sent
        back "FAILED (exit None):" with an empty body and burned the whole
        three-attempt cap on a message that said nothing."""
        head = f"GATE {g['name']} FAILED"
        head += f" (exit {g['exit_code']})" if g.get("exit_code") is not None else ""
        if g.get("note"):
            head += f" [{g['note']}]"
        head += f"\ncommand: {g.get('cmd', '')}"
        body = g.get("output_tail") or "(the gate produced no output)"
        return f"{head}\n{body}"

    async def correct(state: RoundState) -> dict:
        feedback = "\n\n".join(
            _why(g) for g in state["gate_results"] if g["status"] == "red"
        )
        return {"feedback": feedback}

    def route(state: RoundState) -> str:
        if state["status"] == "green":
            return "done"
        if state["attempt"] >= max_attempts:
            return "escalate"
        return "correct"

    g = StateGraph(RoundState)
    g.add_node("produce", produce)
    g.add_node("gate", gate)
    g.add_node("correct", correct)
    g.set_entry_point("produce")
    g.add_edge("produce", "gate")
    g.add_conditional_edges("gate", route, {"done": END, "correct": "correct", "escalate": END})
    g.add_edge("correct", "produce")
    return g.compile()


async def run_round(prompt: str, exec_fn: ExecFn, gate_fn: GateFn, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Run one capped produce→gate→correct round. Returns the terminal state."""
    graph = build_round_graph(exec_fn, gate_fn, max_attempts)
    final: dict[str, Any] = await graph.ainvoke(
        {"prompt": prompt, "attempt": 0, "feedback": None, "status": "running"}
    )
    if final["status"] != "green":
        final["status"] = "escalated"
    return final
