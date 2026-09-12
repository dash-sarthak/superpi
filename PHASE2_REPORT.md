# Phase 2 report: the verification layer

Date: 2026-09-12. Model: Qwen3-1.7B Q4_K_M, llama-server 0.4.0, `-c 32768 -np 4`
(8K/slot), KV q8_0, FA auto, 3187 MiB. Harness v2 (see git log), runner v2,
suite v2. Runs: `runs/qwen3-1.7b/`, activity table: `runs/qwen3-1.7b/activity.db`
(observer: `python3 activity.py`).

## What was built (all deterministic, zero extra model calls)

1. **Note verification at write time.** Evidence = every successful web_search,
   fetch, calc, read_file, ask_reasoner payload, plus the task text. A note is
   rejected if it contains (a) a number that appears in no evidence (integers
   exact match — years and counts get no tolerance, so 888≠896 and 2009≠2008
   fail; floats get 0.5% relative tolerance so rounded calc results pass),
   (b) a URL that was never searched or fetched, (c) in-text arithmetic whose
   result no calc call produced, (d) no calc at all on calc_required tasks.
2. **Artifact contract.** Suite tasks carry the required filename; report(done)
   is rejected until the file exists. Closes the t08 phase-1 gap.
3. **Harness-triggered escalation** after 2 consecutive failed turns (the model
   never self-escalates), with per-run counters for the observer.
4. **Loop control.** Identical web_search/fetch blocked at the third call
   (full-argument hash); identical dependent-tool calls are counted and
   soft-nudged — blocking them deadlocks recovery (see bugs below).
5. **Context-overflow compaction.** `exceed_context_size_error` compacts whole
   assistant/tool groups (protocol-safe), leaves elision breadcrumbs, retries
   down to keep=2, then ends as `context_overflow` instead of crashing.
6. **Forced convergence.** Tool-silent 2+ turns or budget exhaustion with the
   artifact on disk → harness-assisted report (`harness_assisted: true`).
   3+ note rejections → salvage nudge (sourced-only minimal note, explicit
   `UNSOURCED:` lines, honest blocked). Calc-starved rejection → one targeted
   "call calc ALONE next turn" instruction.
7. **chat recovery.** Server parse errors, transient 5xx (backoff), and
   overflow all retry through one wrapper; exhaustion ends as `server_error`.
8. **Runner v2.** Per-model archives, best-of-N majority grading, numeric
   sub-fact grading with the harness tolerance rules, optional fetch-and-compare
   of one cited URL per note. `done` now means report.status=done — a blocked
   report is not a done run.
9. **SQLite activity table** + `activity.py` observer (per-model rollups:
   done rate, loops, escalations, ask_reasoner rate, minutes per task).
10. **Tests.** `test_harness_v2.py`, 34 offline checks, no server needed.

## Full-suite result (same 10 tasks, phase-2 harness)

| | phase 1 (manual truth) | phase 2 |
|---|---|---|
| Runs completed (report delivered) | 10/10 | 8/10 |
| Truly correct notes | 4/10 | 4 (t05, t07, t08-half, t10) |
| Confident fabrications that graded as PASS | 3 (t03, t06, t08 keyword gaps) | 0 surfaced as pass |
| Honest blocked reports | 0 | 2 (t03, t06) |
| ask_reasoner calls | **0** | **9** |
| Fabricated specifics in delivered notes | ~60-70% of notes | 1 (t02: version/date) |
| Wall total | ~4.6 min | ~17.5 min |

Per-task: t02 fabricated release specifics (fetch-check FAIL caught it),
t03/t06 reported blocked rather than fabricating 888-cores / "10 billion by
2026" — the exact claims that keyword-graded PASS in phase 1 while wrong.
t05 flipped from wrong (scrambled digits) to correct-and-verified. t01/t09
burned their budgets replaying unsourced notes (see open problems).

## The load-bearing findings

1. **Verification converts fabrication into visibility.** Phase 1's failure was
   not that the model was wrong — it is a 1.7B model — it was that wrong
   answers arrived shaped like success. Same model, same tasks, now the wrong
   paths end in blocked, UNSOURCED lines, or fetch-check FAIL. The 60-70%
   fabrication rate did not vanish; it lost its disguise.
2. **ask_reasoner 0 → 9.** Escalation had to be harness-triggered, as predicted.
   Given a reasoner on the other end (the orchestrator), 9 escalation windows
   per 10 tasks are directly convertible into solved tasks.
3. **The verifier is a sourcing check, not a truth check.** t02 wrote a
   fabricated version+date that happened to match no-evidence rules only
   weakly (small ints are stoplisted) and passed with a cited URL from search
   snippets. The grading-layer fetch-check caught it. Rule of thumb: the write
   gate stops invented numbers; only fetch-and-compare stops invented facts.
4. **Verification costs turns.** ~3.8x wall time vs phase 1, from rejection
   and repair cycles. Tasks/hour is the phase-2 plan metric to watch; the
   observer now reports it.

## Harness bugs the smoke run flushed out (all fixed)

- llama.cpp from current nixpkgs treats `-c` as TOTAL context across slots:
  `-np 4 --ctx-size 8192` meant 2048/slot and legit overflows. Launch:
  `-c 32768 -np 4`.
- The first duplicate-call key truncated args at 400 chars — with sorted-key
  JSON, `content` comes first, so every note rewrite starting with the same
  words collided and the model could never recover. Deadlocked 3 runs.
- ask_reasoner (90s) exceeded the 30s tool timeout — invisible in phase 1
  because the model never called it.
- RunComplete conflated run completion with task success: blocked reports were
  counted as done.
- One T1000 CUDA fault ("unspecified launch failure") wedged the GPU
  ("GPU requires reset") until a reboot; a llama-server on a wedged GPU runs
  at 1/4 speed and 300s+ prompt evals. Watch for it: `nvidia-smi` throttle
  column.

## Open problems (phase 3 input)

1. **Non-convergence under rejection pressure.** t01/t09 replayed near-identical
   unsourced notes until budget death even with targeted nudges. The salvage
   nudge (3+ rejections) fixes the endpoint: on re-run both tasks end in
   honest blocked reports with explicit `UNSOURCED:` lines (t09 even tried
   inventing `https://moon.md/` as a source; the verifier rejected it). The
   tasks still fail — the search results genuinely do not contain sourced
   facts the model can use (intel.com 403s) — but failure is now loud,
   bounded (~145s vs 240s+), and reportable.
2. **t02-class sourced-but-wrong facts.** Next lever: verify_note fetches one
   cited URL and string-checks the headline fact (planned, not built).
3. **Search quality upstream.** t01's searxng results were Intel download pages
   and intel.com 403s the fetch. A per-domain politeness/fallback fetch path
   (plan section 7) would help.
4. **Grading noise.** Single-run keyword+number grading still mislabels
   borderline notes; best-of-3 (--runs 3) is built and waiting for a quiet
   window.
