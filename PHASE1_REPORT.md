# Phase 1 report: one 1B agent vs a 10-task research suite

Date: 2026-09-11. Model: Qwen3-1.7B Q4_K_M via llama-server 0.4.0, 4 slots x 8K, KV q8_0, T1000 4GB.
Harness: `harness.py` (~460 lines, stdlib-only), runner `runner.py`, suite `suite.json`.
Runs: `runs/` (one directory per task, JSONL transcripts, result.json each).

## Headline numbers

- 10/10 runs finished with status "done". Zero crashes, zero budget exhaustions, zero infinite loops. The deterministic envelope held on every run.
- Auto-graded (keyword) correct: 7/10. Manually fact-checked correct: **4/10** (t01, t02-half, t07-half, t10).
- Totals: 37 turns, 78 tool calls, 36 tool errors (35 were batch-guard rejections or schema repairs), 12,299 completion tokens, ~5.2 min wall, ~312 s average run.
- ask_reasoner: invoked **0 times in 10 tasks**, including runs that were actively failing.

## Verified notes

| task | verdict | note |
|---|---|---|
| t01 TDP + 15% | correct | 45 W, 6.75 W, real Intel URL |
| t02 llama.cpp release | half | plausible version b10909, hallucinated date "March 15, 2024" |
| t03 GPU compare | wrong | 888 CUDA cores (real: 896), 1080 PS5 units (real: 1152) |
| t04 searxng engines | wrong | dumped raw snippets, named no engines |
| t05 light x 3600 | wrong | 10,793,518,728 km (real: 1,079,252,848.8) — digit scramble |
| t06 world population | wrong | "10 billion by 2026" (UN: ~2080s), cited itself ("source: pop.md") |
| t07 llama-server -np | mostly right | correct content (default 1, memory caveat), fabricated org "Llama-Labs" in URL |
| t08 GIL status | wrong | PEP 622 (that is pattern matching) + wrong versions; also wrote .txt, not the requested .md |
| t09 moon fall time | wrong | 3.500 s (real: 3.5136) — did not use calc result |
| t10 LHC first beams | correct | 2008 |

## Failure modes (ranked, with evidence)

1. **Calc bypass / arithmetic hallucination.** On all three calculation tasks the model either did math in text or scrambled digits despite a working calc tool and an explicit rule. The batch guard's rejections (9-13 per calc task) correlate with the model giving up on the tool. Cost: t05, t09 wrong; t01 survived only after guard enforcement.
2. **Confident fabrication of specifics.** Dates, counts, PEP numbers, org names, and URLs come out shape-correct and content-wrong ("March 15, 2024", "888 cores", "PEP 622", "Llama-Labs"). fetch() was almost never used to verify a source before citing it.
3. **Citation laziness.** Notes cite themselves ("source: pop.md") or fabricate plausible paths. No note was ever verified by fetching the cited URL.
4. **No synthesis on open-ended research.** t04 returned pasted search snippets when asked to name engines.
5. **Instruction drift on deliverables.** t08 wrote the wrong filename and still reported done; artifact verification only checks files the report claims, not files the task requested.
6. **Zero self-escalation.** The model never called ask_reasoner, even while fabricating. Escalation must be harness-triggered, not model-initiated.

## What worked (keep as-is)

- Uniform envelopes, per-tool timeouts, transcripts: every failure above is replayable from JSONL.
- Batch guard + corrective message: turned the phase-0 "speculative batching" failure into recoverable rejections; t01 went from wrong to correct through it.
- Report artifact verification: caught false "done" claims twice (t01 first attempt, t05/t09 paths), forcing real writes.
- Prefix caching: repeated system prompts are nearly free at the server.

## Grading gaps found (fix in phase 2)

- Keyword matching passed three hallucinated notes (t03 "cuda", t06 "billion", t08 "pep").
- Phase 2 grading needs: numeric sub-fact extraction, and fetch-and-compare of at least one cited URL per note.

## Addendum: model bench-off (same suite, same harness, same config)

Both models ran the identical suite with the final harness config (2048-token per-turn cap, server parse-failure recovery).

| | Qwen3-1.7B | MiniCPM5-1B |
|---|---|---|
| Runs completed (status done) | 10/10 | 4/10 |
| Crashes | 0 | 0 (was 4 before parse-recovery) |
| Budget exhaustions (no_report) | 0 | 6/10 |
| Keyword-graded correct | 6/10 | 2/10 |
| Manually fact-checked | ~2-3/10 (high variance: t01 flipped 45W→65W between runs) | not measured (runs archived late) |
| Wall total | ~4.6 min | ~8.4 min |
| Tokens out | ~12.3k | ~25.6k |
| VRAM (4 slots x 8K) | 3187 MiB | 1087 MiB |

**MiniCPM5 failure signature:** after a rejection (batch guard or artifact check), it drops out of the tool protocol entirely and produces thinking-plus-plain-text turns until the budget dies (t05 trace: did the work by T5, then six consecutive tool-less turns). It does not recover; Qwen3 recovers from identical situations.

**Qwen3 failure signature:** completes everything, fabricates specifics at a roughly 60-70% rate (dates, counts, PEP numbers, URLs), and even fabricates calc transcripts. Run-to-run variance on facts is high; single-run grading is noisy.

**Verdict:** Qwen3-1.7B is the only viable primary for task execution today. MiniCPM5's 1.1GB footprint keeps it valuable for narrow roles without multi-step convergence (classification, summarization), and its protocol-dropout makes it a good test case for harness-assisted convergence rules.

**The load-bearing conclusion:** both models fabricate at rates that make the phase-2 verification layer (verify_note, rule-based escalation) the critical path, not an enhancement. The orchestrator/reasoner tier is not optional for this hardware class.

**Phase 2 additions from the bench:** (6) harness-assisted report when expected artifacts exist and the model goes tool-silent for 2+ turns; (7) per-model run archives (runs/<model>/), never delete; (8) fact grading needs best-of-3 runs per task.

## Phase 2 implications

1. Escalation to the reasoner becomes rule-based in the harness: trigger on (a) 2+ consecutive tool errors, (b) a fact stated without a fetched source, (c) any calc where the tool result was not the value written to the note.
2. A `verify_note` harness step after write_file: extract numbers and URLs from the note, re-check numeric claims through calc, optionally fetch the cited URL and confirm the fact string appears. Deterministic, no extra model calls.
3. Task templates should carry the required artifact filename; the harness enforces it on report, closing the t08 gap.
4. The dispatcher should split multi-fact tasks into single-fact tasks: every failure above got worse with task breadth (t03, t06).
5. Calc-heavy tasks need a cheaper anti-bypass: reject any write_file whose content contains arithmetic-looking patterns not present in prior calc results (deterministic diff, no LLM).
