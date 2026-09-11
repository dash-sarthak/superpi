# superpi: local small-model agent swarm, plan v1

Status: planning only. Decision points are marked with **VETO?** where I chose for you.

## 0. What the machine allows

- 6c/12t i7-9850H, 31GB RAM, 312GB disk.
- Quadro T1000: 4GB VRAM. This is the binding constraint. It decides model size, slot count, and KV cache policy.
- searxng is already running on 127.0.0.1:8888 with the JSON API enabled (verified with a live query).
- Nothing else is installed: no ollama, no llama.cpp, no vllm. Docker and nix are available.

## 1. Model picks

Two worker models, both installed, both benchable. Assign roles after a local benchmark rather than trusting any single leaderboard.

### Primary worker: Qwen3-1.7B (Q4_K_M)

- The community tool-calling benchmark (lintware/tool-calling-benchmark) ranks qwen3:1.7b first with a 0.960 agent score across 20 runs. Small-sample leaderboards lie, so treat that as "leading candidate", not truth.
- Hermes-style tool template, 32K context, Apache-2.0, GGUF available everywhere. llama.cpp serves its tool-call format natively.
- ~1.1GB at Q4_K_M. KV cache is the expensive part: roughly 112KB/token at f16, ~57KB at q8_0. A 4-slot, 8K-context server costs about 2.9GB VRAM with CUDA overhead. Fits.

### Primary worker candidate: MiniCPM5-1B (Q4_K_M)

- OpenBMB's 2026 1B-class model, exactly the 1B target. Dense 1.08B, standard LlamaForCausalLM (loads everywhere, no custom code), official OpenBMB GGUF, Apache-2.0.
- GQA with 2 KV heads: roughly 24KB of KV per token at f16, about 4.7x less than Qwen3-1.7B. Best KV economics of all three candidates. A 6-slot, 8K-context server costs about 0.7GB weights plus 0.3GB KV, so the all-GPU two-server topology becomes comfortable.
- Hybrid reasoning with an enable_thinking toggle in the same checkpoint. We run no-think for tool loops (thinking is offloaded by design) and can enable think for the auditor role.
- Tool calling trained in; emits XML-style calls, SGLang has a native minicpm5 parser. Our harness parses tool calls itself, so this is a small adapter.
- Post-training: 400B tokens of SFT, then RL plus on-policy distillation. Claims +16 avg points and a 29-point drop in overlong responses.
- Caveat: all performance claims are self-reported against their own comparison set (LFM2.5-1.2B-Thinking, Qwen3-0.6B, Qwen3.5-0.8B). Qwen3-1.7B has the independent benchmark evidence. The bench-off decides. It is also the cheapest LoRA fine-tune target for phase 3 role specialization, and the community fine-tune ecosystem already builds on it.

### Fast/cheap worker: LFM2.5-1.2B-Instruct (Q4)

- Liquid AI's 2026 hybrid family. Native tool calling, tools as JSON, calls emitted between special tokens. GGUF with llama.cpp support (the lfm2 loader handles the hybrid conv/attention cache).
- Same benchmark: 0.920 agent score at roughly 7x lower latency than Qwen3-1.7B. The hybrid architecture carries a tiny KV cache, which matters a lot here: more parallel slots per GB, and it is fast on CPU, so it can live outside the GPU if VRAM runs out.
- Use for: high-frequency small calls (classification, summarizing tool output, formatting), and as the CPU-side worker tier.

### Micro role (optional): Qwen3-0.6B (Q8)

- Sub-GB. Only useful for mechanical classification (which tool category, is this done or blocked). Skip until a measured need appears.

### Escalation tier (phase 3): Qwen3-4B (Q4_K_M, ~2.5GB) on CPU

- 31GB RAM makes CPU inference viable for occasional calls. 5 to 8 tok/s on this CPU is fine for judging and summarizing, which happen a few times per task, not 50 times.
- Alternative to the 4B: route judgment to me. Decide by measuring my availability.

### Considered and rejected (for now)

- Gemma 3 1B, Llama-3.2-1B: no native tool-call training to speak of at this size.
- SmolLM3-3B: bigger, no clear tool-call edge over Qwen3-1.7B to justify the VRAM.
- Granite 3.3 2B: decent, but the evidence above beats it.
- Community fine-tunes (MiniCPM5-1B-Agentic-Tooluse, Qwen3-1.7B-FC, CallForge-1B): interesting, unverified provenance, single-source claims. Park them as phase-3 bench candidates, never as the foundation.
- Proprietary "Qwen3.7 Max" etc.: not local, irrelevant.

### Bench-off protocol

All three workers (Qwen3-1.7B, MiniCPM5-1B, LFM2.5-1.2B) get downloaded in phase 0 and benched in phase 1 on our actual tool schemas, not someone else's leaderboard: a 30-task local suite covering single and parallel tool calls, schema adherence under repair prompts, searxng query formulation, and ask_reasoner timing. Add Qwen3.5-0.8B as a fourth entrant if its GGUF is out. Role assignment is by measured result, not by card claims.

### Quantization stance

A BFCL-based study measured on a 4GB laptop GPU (dev.to, happynood) found tool calling survives Q4 quantization with modest degradation. Policy: Q4_K_M weights, q8_0 KV cache, per-slot context capped at 8K. Verify actual allocation in phase 0 before believing these numbers.

## 2. Serving topology

Decision: llama-server directly, not ollama, not vLLM/SGLang, not Colibri.

- Ollama wraps the same engine but unloads models after 5m idle, hides slot control behind env vars with version-disputed defaults, and swaps models in and out of VRAM instead of co-locating two on 4GB. Fine as a phase-0 CLI toy, not the stack.
- vLLM/SGLang are too heavy for a 4GB multi-model host. SGLang's minicpm5 tool parser is moot: the harness parses tool calls itself.
- Colibri (pure-C expert-streaming engine for 744B-2.8T MoE models) solves the opposite problem: models vastly larger than memory. Our models fit in VRAM, and its roster has nothing under 7B. Logged as a future experiment: a 284B+ model at under 1 tok/s on this hardware could serve as an offline judge if the swarm ever needs to run without me.

Stack:

- llama-server with --parallel slots and continuous batching. One weight-load, many agents. Runs as systemd services on NixOS (llama-cpp is packaged).
- Server A (GPU): Qwen3-1.7B Q4_K_M, 4 slots x 8K, KV q8_0. ~2.9GB. If the bench-off crowns MiniCPM5-1B instead, swap it in: same role, ~1.5GB total for 6 slots.
- Server B (GPU if it fits, else CPU): LFM2.5-1.2B Q4, 4-6 slots. Its tiny KV cache is the reason both servers can coexist in 4GB.
- llama.cpp router mode in front: one OpenAI-compatible endpoint, the "model" field routes to A or B. Harnesses never know the topology.
- If VRAM budget breaks in practice: drop to 3 slots, or move server B to CPU. Measure, then decide.

## 3. Verdict on your draft

Keep the spine, cut the mesh.

### What is right and stays

- Agent activity table. The best idea in the drawing. Small models cannot hold state; a shared SQLite blackboard externalizes it. Every message, tool event, task, and report becomes a row. The observer reads it, I read it, agents read it through tools.
- Doctor. Right role, wrong species. Loop detection, stall detection, restart, quarantine: all deterministic code, counters and regexes. Never make an LLM watch an LLM for hangs.
- Judge. Stays as a role. Code checks schemas and runs tests; the LLM judge only handles semantic questions ("is this actually responsive to the task").
- Observer. Mostly code (tail the table, detect anomalies), with occasional LLM summarization.

### What I reject and why

- The full mesh. Five 1B agents with 20 directed edges of free-form chat will not negotiate; they will hallucinate protocol messages, repeat themselves, and burn their 8K windows on meta-talk. Small models are good at one thing: picking and filling a tool schema. So peer communication becomes exactly that: a send_message(to, kind, body) tool that writes to the table, and a read_table(query) tool to receive. The mesh in your drawing becomes a star: me at the center, agents as leaves, the table as the post office. Agents can still reach any other agent, but the routing decision is mine, and the transport is always the table. That is 20 failure surfaces replaced by one.
- Dispatcher as an LLM box. Routing is the reasoning they lack and that I already do. Dispatcher collapses into me plus a small code stub for mechanical assignment (least-loaded healthy harness with the required capability).
- Reasoner as a separate local box. The reasoner is me. An agent that hits something it cannot reason about calls ask_reasoner; the request lands in the table; I answer into the table; the harness injects my answer as the tool result. A local reasoner tier (Qwen3-4B on CPU) is a phase-3 addition if I become a bottleneck, not a phase-1 assumption.
- Five agents. Arbitrary. Start with three narrow roles:
  1. researcher: web_search, fetch, calc, notes files.
  2. builder: shell, file tools, sandboxed to its own workspace directory.
  3. auditor: read-only tools, reviews outputs, feeds the judge.
  Scale to five only if the table shows the three are saturated. Idle agents cost nothing in VRAM (slots are shared) but they do cost failure surface.

## 4. Harness design

One harness process per agent (your instinct here is correct): OpenAI-compatible chat client, tool loop, per-agent workspace, per-agent prompt.

The philosophy: the model is the hand, the harness is the brain. Every reasoning step that can be turned into a tool or into harness code, is turned into one.

### Tools per agent (narrow, deterministic)

- web_search(query): hits searxng JSON, returns the top N as preformatted title/url/snippet lines. The model never parses raw JSON.
- fetch(url): cleaned text, truncated to a budget, with a "was truncated" flag.
- calc(expr): python eval inside a jail. 1B models cannot do arithmetic; give them no reason to try.
- read_file / write_file / list_dir: jailed to the agent workspace.
- shell(cmd): jailed cwd, timeout, output truncated. Builder only.
- send_message(to, kind, body) / read_table(query): schema-enforced. The only transport.
- ask_reasoner(question, context): writes a reasoning_request row, blocks, returns my answer.
- report(status, result): terminal tool; harness exits the loop on it.

### Harness-level determinism (where the "thinking offload" actually lives)

- Tool-call parsing per model format (JSON for Qwen3/LFM2.5, XML adapter for MiniCPM5), then normalized to one internal schema.
- Max-turn budget per task, with automatic "ask_reasoner or give up" at 80% of budget.
- JSON schema validation of every tool call; on failure, one repair attempt with the validator's error message, then a forced ask_reasoner.
- Retry with backoff on transport failures.
- Stop-condition enforcement: the model cannot loop forever; the harness kills, records, and notifies the doctor.
- Role system prompt: short, imperative, with two worked tool-call examples. Small models need few-shot examples in the prompt more than large ones do.

## 5. Orchestrator (me)

I run as this pi session. My tools: spawn/kill harnesses, read and write the table, answer reasoning requests, review reports, promote/demote slots between models. Task lifecycle: I write a task row, the dispatcher stub assigns it, the agent loops, asks me when stuck, reports, the judge scores it, I merge the result or re-task. I am also the escalation path for the judge.

**VETO?** I assumed the orchestrator is this pi session (me) and not a local model. The draft's "reasoner" arrow into agents matches ask_reasoner. If you want a fully local reasoner from day one, that changes the VRAM plan and phase 1.

## 6. Phases

- Phase 0, hours: project flake.nix with a devShell (llama-cpp.override { cudaSupport = true; }, uv, sqlite3, jq); no devenv, the host is already flake-based and llama-server belongs in systemd, not a dev shell; curl the three GGUFs from HF (openbmb/MiniCPM5-1B-GGUF, Qwen3-1.7B, LFM2.5-1.2B-Instruct); start server A with 4 slots; verify VRAM allocation matches budget and the cudaSupport override name against the pinned nixpkgs; docker fallback is ghcr.io/ggml-org/llama.cpp:server-cuda plus nvidia-container-toolkit.
- Phase 1, a day: one harness (Python, target under 400 lines) on Qwen3-1.7B with search/fetch/calc/files/ask_reasoner. Ten-task smoke suite of research questions that need search plus arithmetic plus a written note. Log every tool call. This produces the failure-mode list that shapes phase 2.
- Phase 2: SQLite activity table, three harnesses on parallel slots, observer and doctor as code, me orchestrating through table tools. Measure: tasks per hour, ask_reasoner rate, loop incidents, token waste on meta-talk.
- Phase 3: judge tier (Qwen3-4B on CPU, or me), local replica of the tool-calling benchmark with our actual tool schemas to finalize which model takes which role, optional bench of the community fine-tunes.
- Phase 4: hardening. KV eviction and slot admission control, per-agent rate caps, crash recovery from the table, doctor escalation to me.

## 7. Risks

- 4GB VRAM is tight for two servers at once. Mitigations in order: KV q8_0, smaller per-slot context, fewer slots, server B to CPU, drop server B.
- 1B models will fail often at first. The design expects a high ask_reasoner rate early; that is the system working. The metric to watch is whether the rate falls across phase 2 as prompts improve.
- 8K contexts fill fast on research tasks. The harness trims old tool results from the conversation before overflow rather than letting the model manage memory.
- searxng rate limits or blocked engines; fetch() needs a per-domain politeness delay and a robots-respecting fallback.

## 8. First concrete steps (when you say go)

1. nix-shell or docker with llama.cpp; verify llama-server + --parallel on the T1000.
2. Download the worker GGUFs (Qwen3-1.7B, MiniCPM5-1B, LFM2.5-1.2B), start server A, run a raw curl tool-call round trip.
3. Write the harness skeleton with two tools only (web_search, ask_reasoner) and one live task: "find the current llama.cpp --parallel slot syntax and report it". That single loop exercises search, schema enforcement, the table, and my reasoning path.
