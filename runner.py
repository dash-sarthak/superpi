#!/usr/bin/env python3
"""superpi runner v2: execute the task suite, grade with numbers, summarize.

Phase-2 additions over v1:
- per-model run archives: everything lands in runs/<model-slug>/ (never delete)
- passes the artifact contract (suite "artifact") and --calc-required to the harness
- numeric sub-fact grading (suite "expected_numbers") with the same tolerance
  rules the harness verification uses (integers exact, floats 0.5%)
- --runs N: best-of-N runs per task; a task counts as correct only on majority
- --fetch-check: fetch one cited URL per task and confirm the expected fact
  string appears on the page (logged, not gating)

Model-agnostic by design: endpoint and model are CLI args.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import harness  # for the shared number-matching rules


def normalize(text):
    """Lowercase, strip everything but letters, digits and dots (for numeric keywords)."""
    return re.sub(r"[^a-z0-9.]", "", text.lower())


def number_found(num, raw_blob):
    """Is `num` present in raw_blob under harness grading rules?"""
    tokens = harness._number_tokens(raw_blob)
    values = [float(t) for t in tokens if re.fullmatch(r"\d+(?:\.\d+)?", t)]
    return harness._number_sourced(harness._norm_num(num), tokens, values)


def collect_texts(run_dir):
    """All note text from the workspace + report summary."""
    texts = []
    for md in run_dir.rglob("*.md"):
        try:
            texts.append(md.read_text())
        except Exception:
            pass
    result_file = run_dir / "result.json"
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text())
            texts.append(str((result.get("report") or {}).get("summary", "")))
        except Exception:
            pass
    return texts


def fetch_check(run_dir, facts, timeout=10):
    """Fetch the first cited URL of the note; do expected facts appear there?
    Returns 'pass' | 'fail' | 'no_url' | 'error' | 'skip' (no note)."""
    urls = []
    for text in collect_texts(run_dir):
        urls = [u.rstrip(".,;:!?") for u in harness._cited_urls(text)]
        if urls:
            break
    if not urls:
        return "no_url" if run_dir and run_dir.exists() else "skip"
    try:
        req = urllib.request.Request(urls[0], headers={"User-Agent": "superpi-grader/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read(2_000_000).decode("utf-8", errors="replace")
    except Exception:
        return "error"
    blob = normalize(page)
    return "pass" if all(normalize(f) in blob for f in facts) else "fail"


def grade(task, run_dir):
    """Keyword + numeric grading of one run directory."""
    empty = {"keywords_missing": ["no run dir"], "numbers_missing": [],
             "likely_correct": False}
    if not run_dir:
        return empty
    texts = collect_texts(run_dir)
    blob_kw = normalize(" ".join(texts))
    raw_blob = "\n".join(texts)
    keywords_missing = [k for k in task.get("expected_keywords", [])
                        if normalize(k) not in blob_kw]
    numbers_missing = [n for n in task.get("expected_numbers", [])
                       if not number_found(n, raw_blob)]
    return {"keywords_missing": keywords_missing,
            "numbers_missing": numbers_missing,
            "likely_correct": not keywords_missing and not numbers_missing}


def run_once(task, args, runs_root, run_index):
    label = f"{task['id']}#{run_index}" if run_index else task["id"]
    print(f"=== {label}: {task['prompt'][:70]}...", flush=True)
    t0 = time.time()
    cmd = [sys.executable, str(ROOT / "harness.py"),
           "--task-id", task["id"], "--prompt", task["prompt"],
           "--endpoint", args.endpoint, "--model", args.model,
           "--runs-dir", str(runs_root),
           "--reasoner-timeout", str(args.reasoner_timeout)]
    if task.get("artifact"):
        cmd += ["--artifact", task["artifact"]]
    if task.get("calc_required"):
        cmd += ["--calc-required"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=task.get("max_turns", 12) * 150
                          + args.reasoner_timeout + 120)
    if proc.returncode != 0:
        print(f"    harness exit {proc.returncode}: {proc.stderr[-300:]}", flush=True)
    candidates = sorted(runs_root.glob(f"{task['id']}-*"))
    run_dir = candidates[-1] if candidates else None
    result = {}
    if run_dir and (run_dir / "result.json").exists():
        result = json.loads((run_dir / "result.json").read_text())
    g = grade(task, run_dir)
    row = {"task_id": task["id"], "run": run_index,
           "status": result.get("status", "missing"),
           "task_status": ((result.get("report") or {}).get("status")
                           or result.get("status", "missing")),
           "assisted": result.get("assisted", False),
           "turns": result.get("turns"), "wall": result.get("wall_seconds"),
           "tokens_out": result.get("completion_tokens"),
           "tokens_meta": result.get("tokens_meta"),
           "tool_calls": result.get("tool_calls"),
           "tool_errors": result.get("tool_errors"),
           "note_rejections": result.get("note_rejections"),
           "loop_incidents": result.get("loop_incidents"),
           "escalations": result.get("escalations"),
           "compactions": result.get("compactions"),
           "ask_reasoner": result.get("ask_reasoner"),
           "run_dir": str(run_dir) if run_dir else None,
           "bench_wall": round(time.time() - t0, 1), **g}
    print(f"    -> {row['status']}{' (assisted)' if row['assisted'] else ''} "
          f"| turns={row['turns']} | wall={row['wall']}s "
          f"| kw miss: {g['keywords_missing']} | num miss: {g['numbers_missing']}",
          flush=True)
    return row


def aggregate(per_runs, task, n):
    """Majority vote across the N runs of one task."""
    statuses = [r["task_status"] for r in per_runs]
    done = sum(1 for s in statuses if s == "done")
    correct = sum(1 for r in per_runs if r.get("likely_correct"))
    assisted = sum(1 for r in per_runs if r.get("assisted"))
    return {"task_id": task["id"], "runs": n, "done_runs": done,
            "correct_runs": correct, "assisted_runs": assisted,
            "status_majority": "done" if done * 2 > n else "incomplete",
            "likely_correct": correct * 2 > n,
            "keywords_missing": sorted({k for r in per_runs
                                        for k in r.get("keywords_missing", [])}),
            "numbers_missing": sorted({k for r in per_runs
                                       for k in r.get("numbers_missing", [])}),
            "wall_total": round(sum(r.get("bench_wall", 0) for r in per_runs), 1)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--suite", default="suite.json")
    p.add_argument("--endpoint", default="http://127.0.0.1:8081")
    p.add_argument("--model", default="qwen3-1.7b")
    p.add_argument("--runs-dir", default="runs",
                   help="base dir; runs land in <base>/<model-slug>/")
    p.add_argument("--runs", type=int, default=1,
                   help="best-of-N runs per task, majority grading")
    p.add_argument("--only", default=None, help="comma-separated task ids to run")
    p.add_argument("--reasoner-timeout", type=int, default=90)
    p.add_argument("--fetch-check", action="store_true",
                   help="after each task, fetch one cited URL and check the facts")
    args = p.parse_args()

    suite = json.loads((ROOT / args.suite).read_text())
    if args.only:
        wanted = {t.strip() for t in args.only.split(",")}
        suite = [t for t in suite if t["id"] in wanted]

    slug = args.model.replace("/", "_")
    runs_root = Path(args.runs_dir) / slug
    runs_root.mkdir(parents=True, exist_ok=True)

    all_rows, agg_rows = [], []
    for task in suite:
        per_runs = [run_once(task, args, runs_root, i) for i in range(args.runs)]
        all_rows.extend(per_runs)
        if args.runs > 1:
            agg = aggregate(per_runs, task, args.runs)
            agg_rows.append(agg)
            print(f"    == {task['id']}: {agg['done_runs']}/{args.runs} done, "
                  f"{agg['correct_runs']}/{args.runs} correct -> "
                  f"{'CORRECT' if agg['likely_correct'] else 'INCORRECT'}", flush=True)
        if args.fetch_check:
            last = per_runs[-1]
            facts = (task.get("expected_numbers", [])
                     or task.get("expected_keywords", []))[:2]
            fc = fetch_check(Path(last["run_dir"]) if last["run_dir"] else None, facts)
            last["fetch_check"] = fc
            print(f"    fetch-check: {fc} (facts: {facts})", flush=True)

    out = runs_root / f"suite_results-{slug}.json"
    payload = {"model": args.model, "runs_per_task": args.runs,
               "tasks": agg_rows if args.runs > 1 else all_rows,
               "per_run_rows": all_rows if args.runs > 1 else None}
    out.write_text(json.dumps(payload, indent=2))

    width = max(len(r["task_id"]) for r in (agg_rows or all_rows))
    if args.runs > 1:
        print(f"\n{'task':<{width}} {'done':>4} {'ok':>3} {'asst':>4} {'wall_tot':>8} {'ok?':>5}")
        for r in agg_rows:
            print(f"{r['task_id']:<{width}} {r['done_runs']:>4} {r['correct_runs']:>3} "
                  f"{r['assisted_runs']:>4} {r['wall_total']:>8.1f} "
                  f"{str(r['likely_correct']):>5}")
    else:
        print(f"\n{'task':<{width}} {'status':<9} {'asst':>4} {'wall':>7} "
              f"{'tok':>6} {'meta':>5} {'tools':>5} {'err':>4} {'nrej':>4} {'loop':>4} "
              f"{'esc':>3} {'cmp':>3} {'askR':>4} {'ok?':>5}")
        for r in all_rows:
            print(f"{r['task_id']:<{width}} {r['status']:<9} "
                  f"{str(r['assisted'])[:1]:>4} {str(r['wall']):>7} "
                  f"{str(r['tokens_out']):>6} {str(r['tokens_meta']):>5} "
                  f"{str(r['tool_calls']):>5} {str(r['tool_errors']):>4} "
                  f"{str(r['note_rejections']):>4} {str(r['loop_incidents']):>4} "
                  f"{str(r['escalations']):>3} {str(r['compactions']):>3} "
                  f"{str(r['ask_reasoner']):>4} {str(r['likely_correct']):>5}")
    done = sum(1 for r in all_rows if r["task_status"] == "done")
    correct_rows = agg_rows if args.runs > 1 else all_rows
    correct = sum(1 for r in correct_rows if r.get("likely_correct"))
    total = len(agg_rows) if args.runs > 1 else len(all_rows)
    print(f"\ndone: {done}/{len(all_rows)} runs   correct: {correct}/{total} tasks   "
          f"results: {out}")


if __name__ == "__main__":
    sys.exit(main())
