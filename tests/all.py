"""Run every test. Deterministic suites first (our own contracts + real tokens frozen at a past block), then the live
smoke tests (today's chain state; DRIFT = the chain changed, not necessarily a bug).
Usage: .venv/bin/python tests/all.py [--no-live]"""
import os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
SUITES = [("sell fixtures", "fixtures.py"), ("v4 hook fixtures", "hooks.py"), ("owner switches", "switches.py"),
          ("decompiler", "decomp.py"), ("dd code", "code.py"), ("roles", "roles.py"), ("pinned real hooks", "pinned.py")]
if "--no-live" not in sys.argv:
    SUITES.append(("live smoke", "run.py"))

failed = []
for name, f in SUITES:
    t = time.time()
    p = subprocess.run([PY, os.path.join(ROOT, "tests", f)], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    bad = [l for l in lines if l.startswith("FAIL")]
    drift = [l for l in lines if l.startswith("DRIFT")]
    ok = bool(lines) and lines[-1].strip() == "all passed" and not bad  # every suite ends with "all passed" / "N failed"
    if not ok and not bad:
        bad = (lines[-1:] or []) + (p.stderr.strip().splitlines()[-1:] if p.stderr.strip() else [])
    print(f"{'PASS' if ok else 'FAIL'}  {name:18} {time.time()-t:6.1f}s" + (f"  ({len(drift)} drift)" if drift else ""))
    for l in bad + drift:
        print("      " + str(l))
    if not ok:
        failed.append(name)
print("\nall passed" if not failed else f"\nFAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
