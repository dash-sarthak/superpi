#!/usr/bin/env python3
"""superpi runner v1: execute the task suite sequentially, grade notes, summarize.

Model-agnostic by design: the endpoint and model are CLI args, so the same suite
re-runs against any server for the bench-off.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def normalize(text):
    """Lowercase, strip everything but letters, digits and dots (for numeric keywords)."""
    return re.sub(r"[^a-z0-9.]", "", text.lower())

def grade(task, run_dir):
    """Collect all note text from the workspace + report summary, check keywords."""
    texts = []
    for md in run_dir.rglob("*.md"):
        try:
            texts.append(md.read_text())
        except Exception:
            pass
    result_file = run_dir / "result.json"
    if result_file.exists():
        result = json.loads(result_file.read_text())
        report = result.get("report") or {}
        texts.append(str(report.get("summary", "")))
    blob = normalize(" ".join(texts))
    missing = [k for k in task.get("expected_keywords", [])
               if normalize(k) not in blob]
    return {"keywords_missing": missing,
            "likely_correct": not missing}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--suite", default="suite.json")
    p.add_argument("--endpoint", default="http://127.0.0.1:8081")
    p.add_argument("--model", default="qwen3-1.7b")
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--only", default=None, help="comma-separated task ids to run")
    p.add_argument("--reasoner-timeout", type=int, default=90)
    args = p.parse_args()

    suite = json.loads((ROOT / args.suite).read_text())
    if args.only:
        wanted = {t.strip() for t in args.only.split(",")}
        suite = [t for t in suite if t["id"] in wanted]

    rows = []
    for task in suite:
        print(f"=== {task['id']}: {task['prompt'][:70]}...", flush=True)
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "harness.py"),
             "--task-id", task["id"], "--prompt", task["prompt"],
             "--endpoint", args.endpoint, "--model", args.model,
             "--runs-dir", args.runs_dir,
             "--reasoner-timeout", str(args.reasoner_timeout)],
            capture_output=True, text=True,
            timeout=task.get("max_turns", 12) * 150 + args.reasoner_timeout + 120)
        if proc.returncode != 0:
            print(f"    harness exit {proc.returncode}: {proc.stderr[-300:]}", flush=True)
        # newest run dir for this task id
        candidates = sorted((ROOT / args.runs_dir).glob(f"{task['id']}-*"))
        run_dir = candidates[-1] if candidates else None
        g = grade(task, run_dir) if run_dir else {"likely_correct": False,
                                                  "keywords_missing": ["no run dir"]}
        result = json.loads((run_dir / "result.json").read_text()) if run_dir else {}
        rows.append({"task_id": task["id"], "status": result.get("status", "missing"),
                     "turns": result.get("turns"), "wall": result.get("wall_seconds"),
                     "tokens_out": result.get("completion_tokens"),
                     "tool_calls": result.get("tool_calls"),
                     "tool_errors": result.get("tool_errors"),
                     "schema_errors": result.get("schema_errors"),
                     "ask_reasoner": result.get("ask_reasoner"),
                     "reasoner_timeout": result.get("reasoner_timeout"),
                     "bench_wall": round(time.time() - t0, 1), **g})
        print(f"    -> {rows[-1]['status']} | turns={rows[-1]['turns']} "
              f"| wall={rows[-1]['wall']}s | keywords missing: {g['keywords_missing']}",
              flush=True)

    out = ROOT / args.runs_dir / f"suite_results-{args.model.replace('/', '_')}.json"
    out.write_text(json.dumps(rows, indent=2))
    done = sum(1 for r in rows if r["status"] == "done")
    correct = sum(1 for r in rows if r.get("likely_correct"))
    print(f"\n{'task':<16} {'status':<9} {'turns':>5} {'wall':>7} {'tok':>5} "
          f"{'tools':>5} {'err':>4} {'sch':>4} {'askR':>4} {'ok?':>5}")
    for r in rows:
        print(f"{r['task_id']:<16} {r['status']:<9} {str(r['turns']):>5} "
              f"{str(r['wall']):>7} {str(r['tokens_out']):>5} {str(r['tool_calls']):>5} "
              f"{str(r['tool_errors']):>4} {str(r['schema_errors']):>4} "
              f"{str(r['ask_reasoner']):>4} {str(r['likely_correct']):>5}")
    print(f"\ndone: {done}/{len(rows)}   likely_correct: {correct}/{len(rows)}   "
          f"results: {out}")

if __name__ == "__main__":
    main()
