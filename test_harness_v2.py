#!/usr/bin/env python3
"""test_harness_v2.py: offline self-tests for the phase-2 deterministic layer.

No server, no network. Run: python3 test_harness_v2.py
Exits non-zero on the first failing section.
"""

import json
import tempfile
from pathlib import Path

import harness
import runner

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def ctx_with(evidence, calc=(), calc_required=False, workspace=None):
    return {"evidence": list(evidence),
            "calc_results": list(calc),
            "calc_required": calc_required,
            "workspace": workspace,
            "tool_timeout": 30}


# --------------------------------------------------------------- verify_note

def test_verify_note():
    print("verify_note:")
    ctx = ctx_with(
        ["Find the TDP of the i7-9850H. Then compute 15% of that value. Write tdp.md.",
         '{"text": "1. Intel Core i7-9850H | https://www.intel.com/ark/191057 | 45 W TDP"}'],
        calc=[("0.15 * 45", 6.749999999999999)], calc_required=True)

    note = "TDP is 45 W per https://www.intel.com/ark/191057. 15% is 6.75 W (calc)."
    check("sourced note passes", harness.verify_note(ctx, note) == [])

    check("fabricated number fails",
          any("48" in p for p in harness.verify_note(ctx, "TDP is 48 W per https://www.intel.com/ark/191057")))

    check("unfetched URL fails",
          any("example.com" in p for p in harness.verify_note(ctx, "TDP is 45 W per https://example.com/fake")))

    check("in-text arithmetic without calc fails",
          any("no calc call" in p for p in harness.verify_note(
              ctx, "TDP is 45 W. 45 * 0.2 = 9 W per https://www.intel.com/ark/191057")))

    check("calc-backed arithmetic passes",
          all("no calc call" not in p for p in harness.verify_note(
              ctx, "TDP is 45 W. 45 * 0.15 = 6.75 W per https://www.intel.com/ark/191057")))

    check("calc_required without calc fails",
          any("requires computation" in p for p in harness.verify_note(
              ctx_with(["45 W"], calc_required=True), "TDP is 45 W")))

    moon = ctx_with(['{"text": "Moon surface gravity 1.62 m/s2, see https://en.wikipedia.org/wiki/Moon"}',
                     '{"ok": true, "data": 3.5136}'])
    check("rounded calc result passes (float tolerance)",
          harness.verify_note(moon, "gravity 1.62 m/s^2, fall time 3.51 s. https://en.wikipedia.org/wiki/Moon.") == [])

    lhc = ctx_with(["LHC first beams 2008"])
    check("integer year drift fails (no tolerance on ints)",
          any("2009" in p for p in harness.verify_note(
              lhc, "first beams in 2009, see https://home.cern")))

    check("small integers stoplisted",
          harness.verify_note(ctx_with(["x"]), "step 3 of 5") == [])


# --------------------------------------------------------------- write gate

def test_write_gate():
    print("write gate (DISPATCH write_file):")
    tmp = Path(tempfile.mkdtemp())
    ctx = ctx_with(["speed of light 299792.458 km/s"], workspace=tmp)
    ctx["verify_notes"] = True

    env, flags = harness.execute_tool(ctx, "write_file",
                                      '{"path": "light.md", "content": "c = 123456 km/s"}')
    check("unsourced write rejected", env.get("ok") is False and "REJECTED" in env["error"])
    check("rejected write leaves no file", not (tmp / "light.md").exists())

    env, _ = harness.execute_tool(ctx, "write_file",
                                  '{"path": "light.md", "content": "c = 299792.458 km/s"}')
    check("sourced write accepted", env.get("ok") is True)
    check("file written", (tmp / "light.md").read_text().startswith("c = 299792.458"))

    ctx["verify_notes"] = False
    env, _ = harness.execute_tool(ctx, "write_file",
                                  '{"path": "light.md", "content": "c = 111111 km/s"}')
    check("--no-verify-notes disables the gate", env.get("ok") is True)


# --------------------------------------------------------------- artifact contract

def test_artifact_contract():
    print("artifact contract (report gate):")
    tmp = Path(tempfile.mkdtemp())
    ctx = ctx_with([], workspace=tmp)
    ctx["required_artifacts"] = ["tdp.md"]

    report = json.dumps({"status": "done", "summary": "done", "artifacts": []})
    env, _ = harness.execute_tool(ctx, "report", report)
    check("missing artifact rejects report",
          env.get("ok") is False and "do not exist" in env.get("error", ""))

    (tmp / "tdp.md").write_text("45 W")
    try:
        harness.execute_tool(ctx, "report", report)
        check("present artifact lets report through", False)
    except harness.RunComplete:
        check("present artifact lets report through", True)
    except Exception as e:
        check("present artifact lets report through", False, str(e))


# --------------------------------------------------------------- compaction

def test_compaction():
    print("compaction:")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "function":
                                     {"name": "web_search", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 500})
    out = harness.compact_messages(msgs, keep_recent=2)
    check("head preserved", out[0]["content"] == "sys" and out[1]["content"] == "task")
    ok = True
    for i, m in enumerate(out):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            replies = {o.get("tool_call_id") for o in out[i + 1:]
                       if o.get("role") == "tool"}
            if not ids <= replies:
                ok = False
    check("tool_call/tool pairing intact", ok)
    check("shrinks the transcript", len(out) < len(msgs),
          f"{len(msgs)} -> {len(out)}")
    check("elision breadcrumbs present",
          any(str(m.get("content", "")).startswith("[elided") for m in out))


# --------------------------------------------------------------- assisted report

def test_assisted_report():
    print("assisted report:")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "moon.md").write_text("3.5136 s fall time")
    ctx = ctx_with([], workspace=tmp)
    ctx["required_artifacts"] = ["moon.md"]
    check("artifacts_on_disk finds the note", harness.artifacts_on_disk(ctx) == ["moon.md"])
    r = harness.assisted_report(ctx)
    check("status done + flag", r["status"] == "done" and r.get("harness_assisted") is True)
    check("summary carries the note", "3.5136" in r["summary"])


# --------------------------------------------------------------- runner grading

def test_runner_grading():
    print("runner grading:")
    tmp = Path(tempfile.mkdtemp())
    rd = tmp / "t01-120000"
    (rd / "workspace").mkdir(parents=True)
    (rd / "workspace" / "tdp.md").write_text("TDP: 45 W. 15% is 6.75 W.\n"
                                             "https://www.intel.com/ark/191057")
    task = {"id": "t01", "expected_keywords": ["45"],
            "expected_numbers": ["45", "6.75"]}
    check("correct note grades correct", runner.grade(task, rd)["likely_correct"])

    (rd / "workspace" / "tdp.md").write_text("TDP: 48 W. 15% is 7.2 W.")
    g = runner.grade(task, rd)
    check("hallucinated note fails on numbers", "45" in g["numbers_missing"])

    (rd / "workspace" / "light.md").write_text("c = 299,792.458 km/s; 1,079,252,848.8 km")
    t5 = {"id": "t05", "expected_keywords": [],
          "expected_numbers": ["299792.458", "1079252848.8"]}
    check("comma-grouped floats pass", runner.grade(t5, rd)["likely_correct"])

    agg = runner.aggregate(
        [{"status": "done", "likely_correct": True, "assisted": False,
          "keywords_missing": [], "numbers_missing": [], "bench_wall": 10},
         {"status": "done", "likely_correct": False, "assisted": False,
          "keywords_missing": ["x"], "numbers_missing": [], "bench_wall": 10},
         {"status": "no_report", "likely_correct": False, "assisted": True,
          "keywords_missing": ["x"], "numbers_missing": ["y"], "bench_wall": 10}],
        task, 3)
    check("majority vote 1/3 -> incorrect", agg["likely_correct"] is False)
    agg2 = runner.aggregate(
        [{"status": "done", "likely_correct": True, "assisted": False,
          "keywords_missing": [], "numbers_missing": [], "bench_wall": 10},
         {"status": "done", "likely_correct": True, "assisted": False,
          "keywords_missing": [], "numbers_missing": [], "bench_wall": 10},
         {"status": "done", "likely_correct": False, "assisted": False,
          "keywords_missing": ["x"], "numbers_missing": [], "bench_wall": 10}],
        task, 3)
    check("majority vote 2/3 -> correct", agg2["likely_correct"] is True)


# --------------------------------------------------------------- activity db

def test_activity_db():
    print("activity db:")
    import io
    import activity
    tmp = Path(tempfile.mkdtemp())
    conn = harness.open_activity_db(str(tmp))
    check("db opens", conn is not None)
    sink = io.StringIO()
    ctx = {"db": conn, "run_id": "t01-1", "task_id": "t01", "model": "m",
           "transcript": sink}
    harness.log_event(ctx, {"type": "run_meta"})
    harness.log_event(ctx, {"type": "note_rejected", "problems": ["x"]})
    harness.log_event(ctx, {"type": "run_end", "status": "done", "wall": 5.0})
    conn.close()
    events = activity.load(str(tmp))
    check("events readable", events and len(events) == 3)
    check("ok/none round-trip", events[0]["ok"] is None and events[1]["ok"] is None)
    # db failures must never kill a run
    ctx_bad = {"db": None, "transcript": io.StringIO()}
    harness.db_event(ctx_bad, {"type": "run_meta"})  # must not raise
    check("db_event no-ops without a db", True)


def main():
    test_verify_note()
    test_write_gate()
    test_artifact_contract()
    test_compaction()
    test_assisted_report()
    test_runner_grading()
    test_activity_db()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all sections passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
