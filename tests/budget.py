"""The 15-second promise: with warm caches, a quick check and a deep check each print a verdict in under 15 s,
and the deadline really cuts a check short (DD_BUDGET=3 -> done in ~5 s with INCOMPLETE/NOT CHECKED notes, never a crash).
Live tokens (needs network). Usage: .venv/bin/python tests/budget.py"""
import os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
CHECK = os.path.join(ROOT, "cmds", "check.py")
CASES = [("WHEEL quick (rh)", ["rh", "0xeb579e280648b6f48a840086a9e6631500a20684"]),
         ("ARCADE deep (sonic)", ["sonic", "0xeEb785CD554B773582A21a673546b0889c9A3b01", "--deep"]),
         ("KEEL quick (eth)", ["eth", "0x69f0b2a3f690acd73cf9fe631a6aa9f203a6529f"])]


def run(args, budget=None):
    env = {**os.environ, **({"DD_BUDGET": str(budget)} if budget is not None else {})}
    t = time.time()
    p = subprocess.run([PY, CHECK] + args, capture_output=True, text=True, timeout=300, env=env)
    return time.time() - t, p.stdout + p.stderr


def main():
    fails = 0
    for name, args in CASES:
        run(args)  # warm every cache (first run may compile / decompile)
        dt, out = run(args)
        ok = dt < 15 and "== VERDICT" in out
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}: {dt:.1f}s" + ("" if ok else f" — {'no verdict' if '== VERDICT' not in out else 'too slow'}"))
    dt, out = run(CASES[1][1], budget=3)
    ok = dt < 8 and "== VERDICT" in out and "Traceback" not in out
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} DD_BUDGET=3 cuts the deep check short: {dt:.1f}s, verdict={'== VERDICT' in out}, crash={'Traceback' in out}")
    print("all passed" if not fails else f"{fails} failed")


main()
