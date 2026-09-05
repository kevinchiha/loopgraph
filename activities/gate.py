"""Deterministic gate runner. Gates are code, never model.

gates.yaml format (SPEC.md §5):

    - name: tests
  cmd: "pytest -x -q"
  timeout: 600

`green_when` supports only `exit N` (default: `exit 0`). A gate is a command a
program can fail — anything richer belongs in a script the gate calls.

The activity returns a list of result dicts; a red gate is a *result*, not an
exception, so Temporal does not retry it. It raises only on infrastructure
errors (missing/unparseable gates file) — those MAY be retried, and the
activity is idempotent (it only reads files and runs check commands).
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path

import yaml
from temporalio import activity

DEFAULT_TIMEOUT = 600
OUTPUT_TAIL = 4000  # chars kept per gate, ledger stays bounded

_GREEN_RE = re.compile(r"^exit (\d+)$")


def _kill_group(proc) -> None:
    """Kill the gate and everything it started. proc.kill() signals only the
    shell, so a build's child processes survived the timeout and kept running."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def load_gates(gates_path: str) -> list[dict]:
    """Parse and validate gates.yaml. Raises ValueError on anything malformed."""
    entries = yaml.safe_load(Path(gates_path).read_text())
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{gates_path}: expected a non-empty list of gates")
    for i, g in enumerate(entries):
        if not isinstance(g, dict) or not g.get("name") or not g.get("cmd"):
            raise ValueError(f"{gates_path}: gate #{i} needs name and cmd")
        gw = str(g.get("green_when", "exit 0"))
        if not _GREEN_RE.match(gw):
            raise ValueError(f"{gates_path}: gate {g['name']!r}: unsupported green_when {gw!r} (want 'exit N')")
        g["green_exit"] = int(_GREEN_RE.match(gw).group(1))
        g["timeout"] = int(g.get("timeout", DEFAULT_TIMEOUT))
    return entries


async def _drain(stream, keep: int) -> bytearray:
    """Read a gate's output keeping only the last `keep` bytes.

    Buffering the whole thing grew the worker's heap without bound: a gate that
    prints continuously (a watch mode, a chatty build) would eventually take the
    worker down, and only the tail is ever used anyway."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return buf
        buf.extend(chunk)
        if len(buf) > keep * 2:
            del buf[:-keep]


async def _run_one(gate: dict, workdir: str, heartbeat=None) -> dict:
    proc = await asyncio.create_subprocess_shell(
        gate["cmd"],
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Own process group, so a timeout can kill what the gate forked. Killing
        # the shell alone left `npm run build`'s children running after the gate
        # was declared timed out.
        start_new_session=True,
    )
    # Poll instead of a bare wait_for so long gates (next build, npm ci) keep
    # heartbeating — a silent 10-minute gate would be declared dead by Temporal.
    # asyncio.wait returns the instant the task finishes, so fast gates stay fast.
    task = asyncio.create_task(_drain(proc.stdout, OUTPUT_TAIL * 4))
    step = min(20, gate["timeout"])
    elapsed = 0
    timed_out = False
    while True:
        done, _ = await asyncio.wait({task}, timeout=step)
        if done:
            await proc.wait()
            exit_code, note = proc.returncode, ""
            break
        elapsed += step
        if heartbeat:
            heartbeat(f"gate {gate['name']} running ({elapsed}s)")
        if elapsed >= gate["timeout"]:
            _kill_group(proc)
            timed_out = True
            exit_code, note = None, f"timeout after {gate['timeout']}s"
            break
    if timed_out:
        # Keep whatever it printed before it hung. Throwing the output away left
        # the owner no diagnostics for the failure that most needs them.
        try:
            out = await asyncio.wait_for(task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            out = bytearray()
    else:
        out = task.result()
    green = exit_code == gate["green_exit"]
    return {
        "name": gate["name"],
        "cmd": gate["cmd"],
        "status": "green" if green else "red",
        "exit_code": exit_code,
        "note": note,
        "output_tail": bytes(out).decode(errors="replace")[-OUTPUT_TAIL:],
    }


@activity.defn
async def run_gates(gates_path: str, workdir: str) -> list[dict]:
    """Run every gate in gates_path inside workdir. Returns one result dict per gate."""
    gates = load_gates(gates_path)
    activity.heartbeat(f"loaded {len(gates)} gates")
    results = []
    for g in gates:
        activity.heartbeat(f"running gate {g['name']}")
        results.append(await _run_one(g, workdir, heartbeat=activity.heartbeat))
    return results
