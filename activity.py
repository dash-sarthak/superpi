#!/usr/bin/env python3
"""activity.py: observer over runs/activity.db (the phase-2 activity table).

Read-only. Gives the orchestrator (doctor role) the phase-2 metrics:
per-model rollups (done rate, assisted saves, loop incidents, escalations,
note rejections, compactions, ask_reasoner rate, tasks/hour) and the recent
run list. Fails soft when the db does not exist yet.

usage: activity.py [runs-dir]        (default: runs)
"""

import json
import sqlite3
import sys
from pathlib import Path


def load(runs_dir):
    """Merge every activity.db under runs_dir (flat or per-model layout)."""
    dbs = sorted(Path(runs_dir).rglob("activity.db"))
    if not dbs:
        return None
    events = []
    for db in dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT run_id, task_id, model, turn, type, tool, ok, seconds, meta, ts "
                "FROM events ORDER BY id").fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for run_id, task_id, model, turn, type_, tool, ok, seconds, meta, ts in rows:
            try:
                meta = json.loads(meta) if meta else {}
            except json.JSONDecodeError:
                meta = {}
            events.append({"run_id": run_id, "task_id": task_id, "model": model,
                           "turn": turn, "type": type_, "tool": tool, "ok": ok,
                           "seconds": seconds, "meta": meta, "ts": ts})
    events.sort(key=lambda e: e["ts"] or 0)
    return events or None


def rollup(events):
    """Per-model metrics grouped by run."""
    runs = {}
    for e in events:
        r = runs.setdefault(e["run_id"], {"model": e["model"], "task": e["task_id"],
                                          "status": "?", "wall": 0.0, "assisted": False,
                                          "tool_errors": 0, "loops": 0, "escal": 0,
                                          "note_rej": 0, "compactions": 0,
                                          "reasoner": 0, "events": 0})
        r["events"] += 1
        t = e["type"]
        if t == "run_end":
            r["status"] = e["meta"].get("status", "?")
            r["wall"] = e["meta"].get("wall", 0.0) or 0.0
        elif t == "tool_result" and not e["ok"]:
            r["tool_errors"] += 1
        elif t == "loop_suspected":
            r["loops"] += 1
        elif t == "harness_escalation":
            r["escal"] += 1
        elif t == "note_rejected":
            r["note_rej"] += 1
        elif t == "context_compaction":
            r["compactions"] += 1
        elif t == "tool_result" and e["tool"] == "ask_reasoner":
            r["reasoner"] += 1
        elif t == "assisted_report":
            r["assisted"] = True
    return runs


def main():
    runs_dir = sys.argv[1] if len(sys.argv) > 1 else "runs"
    events = load(runs_dir)
    if not events:
        print(f"no activity db at {Path(runs_dir) / 'activity.db'} (or it is empty); "
              "run the harness first")
        return 0

    runs = rollup(events)
    models = sorted({r["model"] for r in runs.values()})
    hdr = (f"{'model':<18} {'runs':>4} {'done':>5} {'asst':>4} {'loops':>5} "
           f"{'escal':>5} {'reason':>6} {'terr':>4} {'nrej':>4} {'cmpct':>5} {'h/run':>6}")
    print(hdr)
    print("-" * len(hdr))
    for model in models:
        rs = [r for r in runs.values() if r["model"] == model]
        done = sum(1 for r in rs if r["status"] == "done")
        wall = sum(r["wall"] for r in rs if r["status"] == "done")
        print(f"{model:<18} {len(rs):>4} {done:>5} "
              f"{sum(1 for r in rs if r['assisted']):>4} "
              f"{sum(r['loops'] for r in rs):>5} "
              f"{sum(r['escal'] for r in rs):>5} "
              f"{sum(r['reasoner'] for r in rs):>6} "
              f"{sum(r['tool_errors'] for r in rs):>4} "
              f"{sum(r['note_rej'] for r in rs):>4} "
              f"{sum(r['compactions'] for r in rs):>5} "
              f"{(wall / 3600 * 60 / max(done, 1)):>6.1f}"
              if done else
              f"{model:<18} {len(rs):>4} 0")
    if any(r["status"] == "done" for r in runs.values()):
        print("\n(h/run = minutes of wall time per completed task)")

    print(f"\nrecent runs (last {min(15, len(runs))}):")
    for run_id, r in sorted(runs.items())[-15:]:
        flags = (" ASSISTED" if r["assisted"] else "") + \
                (" LOOPS" if r["loops"] else "") + \
                (" ESCAL" if r["escal"] else "")
        print(f"  {run_id:<44} {r['status']:<18} wall={r['wall']:>6.1f}s{flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
