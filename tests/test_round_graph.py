import asyncio

from graphs.round_graph import run_round


def make_exec(claims=None):
    calls = []

    async def exec_fn(prompt, feedback):
        calls.append({"prompt": prompt, "feedback": feedback})
        return {"claims": claims or ["did the thing"], "summary": "s"}

    return exec_fn, calls


def gate_fn_from(seq):
    """A gate_fn that returns one result per call from seq ('green'/'red')."""
    it = iter(seq)

    async def gate_fn():
        s = next(it)
        return [{"name": "g", "status": s, "exit_code": 0 if s == "green" else 1,
                 "output_tail": f"output for {s}", "note": ""}]

    return gate_fn


def test_green_first_try():
    exec_fn, calls = make_exec()
    final = asyncio.run(run_round("P", exec_fn, gate_fn_from(["green"])))
    assert final["status"] == "green"
    assert final["attempt"] == 1
    assert len(calls) == 1 and calls[0]["feedback"] is None


def test_two_corrections_then_green():
    exec_fn, calls = make_exec()
    final = asyncio.run(run_round("P", exec_fn, gate_fn_from(["red", "red", "green"])))
    assert final["status"] == "green"
    assert final["attempt"] == 3
    # corrections carry the red gate's output back into produce
    assert "output for red" in calls[1]["feedback"]
    assert "output for red" in calls[2]["feedback"]


def test_escalates_after_cap():
    exec_fn, calls = make_exec()
    final = asyncio.run(run_round("P", exec_fn, gate_fn_from(["red", "red", "red"])))
    assert final["status"] == "escalated"
    assert final["attempt"] == 3
    assert len(calls) == 3  # cap respected: no 4th produce


def test_gate_results_in_terminal_state():
    exec_fn, _ = make_exec()
    final = asyncio.run(run_round("P", exec_fn, gate_fn_from(["red", "green"])))
    assert final["gate_results"][0]["status"] == "green"
