"""Decompiler (Heimdall) fixtures: our own contracts' bytecode must yield the known powers.
Usage: .venv/bin/python tests/decomp.py"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import decomp

OUT = os.path.join(os.path.dirname(__file__), "..", "fixtures", "out")
CASES = [  # (artifact, contract, phrases that must appear in the CODE section)
    ("T.sol", "T", ["drain(address) (only owner) changes totalSupply", "'cooldown'", "'max sell'", "setBlocked(bool) [only owner]"]),
    ("H.sol", "FeeHook", ["setFee(uint256) [fixed address", "setBlocked(bool) [fixed address"]),
]


async def main():
    fails = 0
    for f, c, must in CASES:
        code = json.load(open(os.path.join(OUT, f, c + ".json")))["deployedBytecode"]["object"]
        d = await decomp.analyze(code)
        txt = "\n".join(decomp.render(d))
        miss = [m for m in must if m not in txt]
        fails += bool(miss)
        print(f"{'PASS' if not miss else 'FAIL'} {c}" + (f": missing {miss}\n{txt}" if miss else ""))
    print("all passed" if not fails else f"{fails} failed")

run(main())
