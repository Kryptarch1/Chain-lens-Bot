"""Access-gate classification (Slither): real owner checks in both styles are gates; `require(msg.sender != X)` only
excludes X and must NOT be read as owner-only (false 'malicious' alarms on GM+, 2026-10-17).
Usage: .venv/bin/python tests/gates.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import static

SRC = os.path.join(os.path.dirname(__file__), "..", "fixtures", "src", "G.sol")
WANT = {"setFeeRequire": True, "setFeeIfRevert": True, "setPref": False}


async def main():
    st = await static.analyze_local({"G.sol": open(SRC).read()}, "0.8.24", "G")
    got = {f["name"]: f["protected"] for f in st.get("fns", [])}
    fails = [n for n, w in WANT.items() if got.get(n) != w]
    for n, w in WANT.items():
        print(f"{'PASS' if got.get(n) == w else 'FAIL'} {n}: owner-only={got.get(n)} (want {w})")
    print("all passed" if not fails else f"{len(fails)} failed")

run(main())
