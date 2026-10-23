"""Live smoke tests: dd check on every known token, compare verdict + required phrases, report time.
must_contain = stable facts (code, rugs that already happened) -> FAIL if missing.
drift        = facts the chain can change later (who holds the LP, a fee the owner may lower) -> DRIFT warning only.
Deterministic tests live in tests/all.py (fixtures + pinned-block forks).
Usage: .venv/bin/python tests/run.py [name ...]
"""
import json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cases = json.load(open(os.path.join(ROOT, "tests", "cases.json")))
only = set(sys.argv[1:])
PY = os.path.join(ROOT, ".venv", "bin", "python")
fails = 0
for c in cases:
    if only and c["name"] not in only:
        continue
    t = time.time()
    p = subprocess.run([PY, os.path.join(ROOT, "cmds", c.get("cmd", "check") + ".py"), c["chain"], c["token"]] + c.get("args", []), capture_output=True, text=True, timeout=400,
                       env={**os.environ, "DD_BUDGET": "120"})  # accuracy suite: cold DEEP cases may legitimately exceed 15 s
    out = p.stdout
    dt = time.time() - t
    problems = []
    if c.get("expect") and f"VERDICT: {c['expect']}" not in out:
        got = next((l for l in out.splitlines() if "VERDICT:" in l), "no verdict").strip()
        problems.append(f"expected VERDICT {c['expect']}, got '{got}'")
    if c.get("expect_lp") and f"→ {c['expect_lp']}" not in out:
        problems.append(f"expected main LP {c['expect_lp']}")
    for m in c.get("must_contain", []):
        if m.lower() not in out.lower():
            problems.append(f"missing '{m}'")
    drifted = [m for m in c.get("drift", []) if m.lower() not in out.lower()]
    status = "FAIL" if problems else ("DRIFT" if drifted else "PASS")
    fails += bool(problems)
    note = "; ".join(problems) or (f"on-chain state changed? missing {drifted} — re-check this token, update the case" if drifted else "")
    print(f"{status:5} {c['name']:11} {c['chain']:6} {dt:5.1f}s  {note}")
    if problems and os.environ.get("DD_SHOW"):
        print(out)
print(f"\n{'all passed' if not fails else f'{fails} failed'}")
sys.exit(1 if fails else 0)
