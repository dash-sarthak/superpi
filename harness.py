#!/usr/bin/env python3
"""superpi harness v1: one agent, one task, a tool loop wrapped in a deterministic envelope.

The model is the hand; this file is the brain. Every tool is narrow, every result
is a uniform envelope, every turn is bounded, every event is transcripted.
"""

import argparse
import ast
import json
import math
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POOL = ThreadPoolExecutor(max_workers=4)

# ---------------------------------------------------------------- tools spec

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns the top 5 results as 'title | url | snippet' lines.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch",
        "description": "Fetch a URL and return its readable text (truncated to 4000 chars).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http(s) URL"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "calc",
        "description": "Evaluate an arithmetic expression. Use for ALL math; never compute in your head.",
        "parameters": {"type": "object", "properties": {
            "expr": {"type": "string", "description": "e.g. 0.15 * 45, sqrt(20 / 1.62)"}},
            "required": ["expr"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from your workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path inside the workspace"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a text file into your workspace (max 100 KB).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path inside the workspace"},
            "content": {"type": "string", "description": "File body"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files in your workspace.",
        "parameters": {"type": "object", "properties": {},
            "required": []}}},
    {"type": "function", "function": {
        "name": "ask_reasoner",
        "description": "Ask the orchestrator reasoner a question when stuck or unsure. Blocks until answered.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Precise question"},
            "context": {"type": "string", "description": "What you tried and what happened"}},
            "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "report",
        "description": "Finish the task. Call this when done. This ends the run.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["done", "blocked", "failed"],
                       "description": "Task outcome"},
            "summary": {"type": "string", "description": "One paragraph summary"},
            "artifacts": {"type": "array", "items": {"type": "string"},
                          "description": "Filenames produced, if any"}},
            "required": ["status", "summary"]}}},
]

_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS_SPEC}

# ---------------------------------------------------------------- envelope helpers

def ok(data):
    return {"ok": True, "data": data}

def err(message):
    return {"ok": False, "error": str(message)[:500]}

# ---------------------------------------------------------------- validation

def validate_args(name, args):
    """Tiny validator: object with typed properties, required list, optional enum."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        return [f"unknown tool '{name}'"]
    problems = []
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            problems.append(f"missing required argument '{req}'")
    for key, value in args.items():
        if key not in props:
            problems.append(f"unexpected argument '{key}'")
            continue
        want = props[key].get("type")
        if want == "string" and not isinstance(value, str):
            problems.append(f"'{key}' must be a string")
        elif want == "number" and not isinstance(value, (int, float)):
            problems.append(f"'{key}' must be a number")
        elif want == "array" and not isinstance(value, list):
            problems.append(f"'{key}' must be an array")
        enum = props[key].get("enum")
        if enum and value not in enum:
            problems.append(f"'{key}' must be one of {enum}")
    return problems

def parse_tool_args(raw):
    """OpenAI tool arguments arrive as a JSON string; tolerate code fences."""
    if isinstance(raw, dict):
        return raw, None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"arguments are not valid JSON: {e}"

# ---------------------------------------------------------------- calc

_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10,
          "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "abs": abs, "round": round, "min": min, "max": max}
_CONSTS = {"pi": math.pi, "e": math.e}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b,
           ast.FloorDiv: lambda a, b: a // b}

def calc(expr):
    tree = ast.parse(expr.strip(), mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("only numeric constants allowed")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            return _FUNCS[node.func.id](*(ev(a) for a in node.args))
        raise ValueError(f"disallowed element: {type(node).__name__}")

    return ev(tree)

# ---------------------------------------------------------------- web tools

class _TextExtract(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "iframe"}
    BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

def _http_get(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "superpi-agent/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000).decode("utf-8", errors="replace")

def web_search(query):
    url = "http://127.0.0.1:8888/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"})
    payload = json.loads(_http_get(url, timeout=15))
    results = payload.get("results", [])[:5]
    if not results:
        return ok("no results; try different keywords")
    lines = [f"{i}. {r.get('title', '')} | {r.get('url', '')} | "
             f"{(r.get('content') or '')[:300]}" for i, r in enumerate(results, 1)]
    return ok("\n".join(lines))

def fetch(url):
    if not re.match(r"^https?://", url):
        return err("only http(s) URLs")
    raw = _http_get(url, timeout=20)
    parser = _TextExtract()
    try:
        parser.feed(raw)
        text = "".join(parser.parts)
    except Exception:
        text = raw
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    truncated = len(text) > 4000
    return ok({"text": text[:4000], "was_truncated": truncated})

# ---------------------------------------------------------------- workspace tools

def _jailed(ctx, path):
    base = ctx["workspace"].resolve()
    target = (base / path).resolve()
    if base not in target.parents and target != base:
        raise ValueError("path escapes workspace")
    return target

def read_file(ctx, path):
    target = _jailed(ctx, path)
    return ok({"text": target.read_text()[:4000]})

def write_file(ctx, path, content):
    target = _jailed(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(content))
    return ok(f"wrote {len(str(content))} bytes to {path}")

def list_dir(ctx):
    items = sorted(p.relative_to(ctx["workspace"]).as_posix()
                   for p in ctx["workspace"].rglob("*") if p.is_file())
    return ok(items or "workspace is empty")

# ---------------------------------------------------------------- ask_reasoner

def ask_reasoner(ctx, question, context=""):
    mail = ctx["mail"]
    mail.mkdir(parents=True, exist_ok=True)
    req_path = mail / f"request-{ctx['reasoner_seq']}.json"
    ans_path = req_path.with_suffix(".answer.md")
    req_path.write_text(json.dumps(
        {"question": question, "context": context, "ts": time.time()}, indent=2))
    ctx["reasoner_seq"] += 1
    deadline = time.time() + ctx["reasoner_timeout"]
    print(f"[harness] REASONER REQUEST (answer file: {ans_path})", flush=True)
    print(f"[harness]   Q: {question}", flush=True)
    while time.time() < deadline:
        if ans_path.exists():
            answer = ans_path.read_text()
            return ok({"answer": answer, "note": "from orchestrator reasoner"})
        time.sleep(3)
    return ok({"answer": "", "note": "reasoner did not answer in time; "
              "proceed with best judgment and note the uncertainty in your report"})

# ---------------------------------------------------------------- dispatch

DISPATCH = {
    "web_search": lambda ctx, a: web_search(a["query"]),
    "fetch": lambda ctx, a: fetch(a["url"]),
    "calc": lambda ctx, a: ok(calc(a["expr"])),
    "read_file": lambda ctx, a: read_file(ctx, a["path"]),
    "write_file": lambda ctx, a: write_file(ctx, a["path"], a["content"]),
    "list_dir": lambda ctx, a: list_dir(ctx),
    "ask_reasoner": lambda ctx, a: ask_reasoner(ctx, a["question"], a.get("context", "")),
}

class RunComplete(Exception):
    def __init__(self, payload):
        self.payload = payload

INFO_TOOLS = {"web_search", "fetch", "ask_reasoner"}

def execute_tool(ctx, name, raw_args, info_seen=False):
    t0 = time.time()
    args, parse_error = parse_tool_args(raw_args)
    if parse_error:
        return err(parse_error), {"schema_error": True}
    problems = validate_args(name, args)
    if problems:
        return err("invalid arguments: " + "; ".join(problems)), {"schema_error": True}
    if name in INFO_TOOLS:
        info_seen = True
    if info_seen and name not in INFO_TOOLS:
        return err(f"Rejected: '{name}' was batched in the same turn as an information "
                   f"tool (search/fetch). Read the results first, then call '{name}' "
                   "in your next turn."), {"schema_error": True}
    if name == "report" and args.get("status") == "done":
        missing = [a for a in args.get("artifacts", [])
                   if not (ctx["workspace"] / a).exists()]
        if missing:
            return err(f"Rejected: report claims artifacts that do not exist in the "
                       f"workspace: {missing}. Write those files first (write_file in "
                       "their own turn), then report again. If you truly cannot, "
                       "report with status 'failed' or 'blocked'."), {"schema_error": True}
    if name == "report":
        raise RunComplete(args)
    handler = DISPATCH.get(name)
    if handler is None:
        return err(f"unknown tool '{name}'"), {"schema_error": True}
    try:
        future = POOL.submit(handler, ctx, args)
        envelope = future.result(timeout=ctx["tool_timeout"])
    except RunComplete:
        raise
    except Exception as e:
        envelope = err(f"{type(e).__name__}: {e}")
    envelope["_tool"] = name
    envelope["_seconds"] = round(time.time() - t0, 2)
    return envelope, {"schema_error": False}

# ---------------------------------------------------------------- chat client

def chat(ctx, messages):
    body = {"model": ctx["model"], "messages": messages, "tools": TOOLS_SPEC,
            "tool_choice": "auto", "temperature": ctx["temperature"],
            "top_p": 0.95, "max_tokens": ctx["max_tokens_per_turn"]}
    request = urllib.request.Request(
        ctx["endpoint"] + "/v1/chat/completions",
        json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code == 500 and "parse tool call" in detail:
            raise ToolCallParseError(detail) from e
        raise RuntimeError(f"chat HTTP {e.code}: {detail}") from e

class ToolCallParseError(RuntimeError):
    pass

def echo_safe(message):
    """Strip reasoning_content before echoing an assistant message back."""
    return {k: v for k, v in message.items() if k != "reasoning_content"}

# ---------------------------------------------------------------- main loop

def log_event(ctx, event):
    ctx["transcript"].write(json.dumps(event, default=str) + "\n")
    ctx["transcript"].flush()

def run(args):
    ts = time.strftime("%H%M%S")
    run_id = f"{args.task_id}-{ts}"
    run_dir = Path(args.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = {"workspace": run_dir / "workspace", "mail": run_dir / "mail",
           "reasoner_seq": 0, "reasoner_timeout": args.reasoner_timeout,
           "tool_timeout": args.tool_timeout, "endpoint": args.endpoint,
           "model": args.model, "temperature": args.temperature,
           "max_tokens_per_turn": args.max_tokens_per_turn,
           "transcript": (run_dir / "transcript.jsonl").open("w")}

    system_prompt = (ROOT / "prompts" / "researcher.md").read_text()
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": args.prompt}]
    log_event(ctx, {"type": "run_meta", "run_id": run_id, "task_id": args.task_id,
                    "endpoint": args.endpoint, "model": args.model})
    log_event(ctx, {"type": "user", "content": args.prompt})

    stats = {"prompt_tokens": 0, "completion_tokens": 0, "tool_calls": 0,
             "tool_errors": 0, "schema_errors": 0, "repairs": 0,
             "ask_reasoner": 0, "reasoner_timeout": 0, "nudges": 0}
    report, status, t0 = None, "no_report", time.time()
    warn_at = max(1, int(args.max_turns * 0.8))
    budget_warned = False

    try:
        for turn in range(args.max_turns):
            if turn == warn_at and not budget_warned:
                messages.append({"role": "user", "content":
                    "BUDGET WARNING: turns are almost exhausted. Wrap up now: "
                    "either finish via report(), or call ask_reasoner if truly stuck."})
                stats["nudges"] += 1
                budget_warned = True
            try:
                response = chat(ctx, messages)
            except ToolCallParseError:
                stats["server_parse_recoveries"] = stats.get("server_parse_recoveries", 0) + 1
                log_event(ctx, {"type": "server_parse_error", "turn": turn})
                messages.append({"role": "user", "content":
                    "Your previous tool call was malformed or truncated (too long). "
                    "Repeat it smaller and simpler: shorter content, one call only."})
                response = chat(ctx, messages)
            usage = response.get("usage", {})
            stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
            stats["completion_tokens"] += usage.get("completion_tokens", 0)
            message = response["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            log_event(ctx, {"type": "assistant", "turn": turn,
                            "content": message.get("content") or "",
                            "reasoning": (message.get("reasoning_content") or "")[:2000],
                            "tool_calls": [tc.get("function") for tc in tool_calls]})

            if not tool_calls:
                if turn == args.max_turns - 1:
                    status = "no_report"
                    break
                messages.append(echo_safe(message))
                messages.append({"role": "user", "content":
                    "Respond only through tools. If the task is complete, call report()."})
                stats["nudges"] += 1
                continue

            messages.append(echo_safe(message))
            info_seen = False
            for tc in tool_calls:
                name = tc["function"]["name"]
                envelope, flags = execute_tool(ctx, name,
                                               tc["function"].get("arguments", "{}"),
                                               info_seen=info_seen)
                stats["tool_calls"] += 1
                stats["schema_errors"] += int(flags["schema_error"])
                if envelope.get("ok") is False:
                    stats["tool_errors"] += 1
                    if flags["schema_error"]:
                        stats["repairs"] += 1
                if envelope.get("ok") and name in INFO_TOOLS:
                    info_seen = True
                elif envelope.get("ok") is False and "batched" in str(envelope.get("error", "")):
                    stats["batch_rejections"] = stats.get("batch_rejections", 0) + 1
                if name == "ask_reasoner":
                    stats["ask_reasoner"] += 1
                    data = envelope.get("data") or {}
                    if "did not answer" in str(data.get("note", "")):
                        stats["reasoner_timeout"] += 1
                log_event(ctx, {"type": "tool_result", "turn": turn, "tool": name,
                                "ok": envelope.get("ok"), "seconds": envelope.get("_seconds"),
                                "payload": {k: v for k, v in envelope.items()
                                            if k not in ("_tool", "_seconds")}})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps({k: v for k, v in envelope.items()
                                                        if not k.startswith("_")})})
            if stats.get("batch_rejections", 0) and not budget_warned:
                messages.append({"role": "user", "content":
                    "RULE VIOLATION this turn. From now on, in every turn call ONLY ONE "
                    "kind of tool: either information tools (web_search/fetch) OR ONE "
                    "dependent tool (calc, write_file, or report) that uses values you "
                    "read from earlier tool results. Never mix the two kinds in one turn."})
                stats["nudges"] += 1
    except RunComplete as done:
        report, status = done.payload, "done"
    except Exception as e:
        status = "crash"
        log_event(ctx, {"type": "crash", "error": f"{type(e).__name__}: {e}"})
    finally:
        wall = time.time() - t0
        result = {"run_id": run_id, "task_id": args.task_id, "status": status,
                  "turns": turn + 1 if "turn" in dir() else 0, "wall_seconds": round(wall, 1),
                  "report": report, **stats}
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
        log_event(ctx, {"type": "run_end", "status": status, "wall": wall})
        ctx["transcript"].close()
        print(f"[harness] run {run_id}: {status} in {wall:.0f}s, "
              f"{stats['tool_calls']} tool calls, {stats['completion_tokens']} tokens out",
              flush=True)
    return 0

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--endpoint", default="http://127.0.0.1:8081")
    p.add_argument("--model", default="qwen3-1.7b")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--max-tokens-per-turn", type=int, default=2048)
    p.add_argument("--reasoner-timeout", type=int, default=600)
    p.add_argument("--tool-timeout", type=int, default=30)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--runs-dir", default="runs")
    run(p.parse_args())

if __name__ == "__main__":
    sys.exit(main())
