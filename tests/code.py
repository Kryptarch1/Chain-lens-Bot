"""dd code on our own token (fixtures/src/T.sol, deployed on a BSC fork, no source on any explorer):
the decompiler's map names are wrong for T (balances show up as storage_map_b in drain), so the TRACE must name them
from on-chain proof: drain(deployer) writes balanceOf[deployer] and totalSupply; setBlocked(true) flips `blocked`.
Usage: .venv/bin/python tests/code.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import sim
from cmds.code import report
from tests.fixtures import setup, DEPLOYER


class NoExplorer:  # our fixture is verified nowhere
    chain = "bsc"
    async def source(self, a): return {"verified": False}
    async def creator(self, a): return {"creator": DEPLOYER}


CASES = [("drain", DEPLOYER, ["== CODE drain(address)", "storage_map_b", "balanceOf[0x0000…e1ee]: 500000000000000000000000000 → 0", "totalSupply: 1000000000000000000000000000 → 500000000000000000000000000", "owner = 0x00000000000000000000000000000000000de1ee"]),
         ("setBlocked", "true", ["blocked: false → true", "result: ok"])]


async def main():
    fork = await sim.start("bsc")
    assert fork, "fork did not start (twice)"
    fails = 0
    try:
        token, pair = await setup(fork, 6, 0)
        for fn, arg, must in CASES:
            out, L = await report(fork.rpc, NoExplorer(), "bsc", token, fn, frm=DEPLOYER, raw_args=arg)
            txt = "\n".join(L)
            miss = [m for m in must if m.lower() not in txt.lower()]
            fails += bool(miss)
            print(f"{'PASS' if not miss else 'FAIL'} dd code {fn}" + (f": missing {miss}\n{txt}" if miss else ""))
            if os.environ.get("DD_SHOW"):
                print(txt)
    finally:
        fork.close()
    print("all passed" if not fails else f"{fails} failed")

run(main())
