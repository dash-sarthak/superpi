#!/usr/bin/env python3
"""recovery_check.py: measure tool-protocol recovery after rejections.

The MiniCPM5-1B failure signature: after a rejection (batch guard, schema
repair, or artifact check), the model drops out of the tool protocol and
produces tool-less turns until the budget dies.

Metric per run: for every rejection (tool_result with ok=false) at turn T,
did the model emit a successful tool call by turn T+2? Pass criterion for
the bench-off addendum: recovery on every rejection across the whole suite.
"""

import json
import sys
from pathlib import Path


def analyze_run(run_dir):
    events = []
    for jl in sorted(run_dir.glob("*.jsonl")):
        for line in jl.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not events:
        return None

    rejections = [e for e in events if e.get("type") == "tool_result" and not e.get("ok")]
    turns_with_tools = sorted({e["turn"] for e in events
                               if e.get("type") == "assistant" and e.get("tool_calls")})
    ok_calls = [e for e in events if e.get("type") == "tool_result" and e.get("ok")]

    recovered, dropped = [], []
    for rej in rejections:
        t = rej["turn"]
        # first turn with tools strictly after the rejection
        nxt = next((u for u in turns_with_tools if u > t), None)
        (recovered if nxt is not None and nxt <= t + 2 else dropped).append(
            {"reject_turn": t, "tool": rej.get("tool"), "next_tool_turn": nxt})

    toolless_tail = 0
    last_tool_turn = max(turns_with_tools, default=-1)
    last_turn = max((e.get("turn", -1) for e in events if e.get("type") in
                     ("assistant", "tool_result", "run_end")), default=-1)
    if last_tool_turn >= 0:
        toolless_tail = last_turn - last_tool_turn

    return {"run": run_dir.name,
            "rejections": len(rejections),
            "recovered": len(recovered),
            "dropped": dropped,
            "successful_tool_calls": len(ok_calls),
            "toolless_tail_turns": max(0, toolless_tail)}


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        print("usage: recovery_check.py <runs-dir>")
        sys.exit(2)
    runs_dir = Path(sys.argv[1])
    results = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        r = analyze_run(run_dir)
        if r:
            results.append(r)
    total_rej = sum(r["rejections"] for r in results)
    total_rec = sum(r["recovered"] for r in results)
    for r in results:
        flag = "" if r["rejections"] == 0 or not r["dropped"] else "  <-- DROPOUT"
        print(f"{r['run']:<40} rej={r['rejections']:>3} recovered={r['recovered']:>3} "
              f"ok_calls={r['successful_tool_calls']:>3} tail={r['toolless_tail_turns']}{flag}")
        for d in r["dropped"]:
            print(f"    dropped: turn {d['reject_turn']} ({d['tool']}) -> next tool turn {d['next_tool_turn']}")
    print(f"\nrecovery rate: {total_rec}/{total_rej} "
          f"({'PASS' if total_rej == total_rec else 'FAIL'}: criterion is recovery on every rejection)")


if __name__ == "__main__":
    main()
