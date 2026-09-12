# Resume point — phase 2 MiniCPM re-bench (paused 2026-09-12, laptop heat)

All phase-2 code is done, tested, and pushed (main @ b7399c0). The Qwen3-1.7B
phase-2 validation is complete (see PHASE2_REPORT.md). What remains is the
MiniCPM re-bench under the v2 harness.

## Where the MiniCPM5-1B run stopped

Server was up (MiniCPM5-1B, 4 slots x 8K, 1087 MiB). Suite got through 5 tasks
before being killed (partial, one interrupted run dir t06):

- t01: done (ASSISTED report) — the forced-convergence path fired and graded
  keyword+number CORRECT
- t02: no_report, t03: no_report, t04: no_report
- t05: server_error at turn 7 (server was already unhealthy — do not trust)

## Resume sequence

1. `nix develop -c llama-server -m models/MiniCPM5-1B-Q4_K_M.gguf -a minicpm5-1b \
   --n-gpu-layers 99 -c 32768 -np 4 --cache-type-k q8_0 --cache-type-v q8_0 \
   --port 8081 --host 127.0.0.1`
2. `python3 runner.py --model minicpm5-1b` (runs land in runs/minicpm5-1b/)
3. Kill server; same launch with `models/MiniCPM5-2B-Q4_K_M.gguf -a minicpm5-2b
   -c 32768 -np 2` (16K/slot per phase-1 attempt 2; v2 compaction should also
   make 4x8K survivable — worth an A/B)
4. `python3 runner.py --model minicpm5-2b`
5. `python3 recovery_check.py runs/minicpm5-1b` (and -2b) — the standing
   dropout metric vs phase-1's ~0 recovery
6. Append a MiniCPM re-bench addendum to PHASE2_REPORT.md; commit + push

Phase-1 baselines to compare against:
- MiniCPM5-1B: 4/10 done, 2/10 keyword-correct, ~0 recovery after rejection
- MiniCPM5-2B: 3/10 done, 3/10 keyword, 7/8 rejection recovery but non-convergence
