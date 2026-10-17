"""Owner-switch tests (lib/switches.py) on our own token: Slither reads fixtures/src/T.sol locally, the token is
deployed on a BSC fork with a real PancakeSwap pair, then each switch is flipped in storage and a holder sells.
mode 6: `blocked` (set by setBlocked) must be found and flipping it must block sells. mode 0 (clean) must flag nothing.
Usage: .venv/bin/python tests/switches.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import sim, static, switches, quicksim
from lib.evm import selector
from tests.fixtures import setup, DEPLOYER

SRC = os.path.join(os.path.dirname(__file__), "..", "fixtures", "src", "T.sol")


async def main():
    st = await static.analyze_local({"T.sol": open(SRC).read()}, "0.8.24", "T")
    assert st.get("ok"), st
    fails = 0
    for mode, want in ((6, "blocked"), (0, None)):
        fork = await sim.start("bsc")
        assert fork, "fork did not start (twice)"
        try:
            token, pair = await setup(fork, mode, 0)
            hp = await quicksim.honeypot(fork.rpc, token, pair)
            probes = [{"fn": "setBlocked(bool)", "sel": selector("setBlocked(bool)"), "args": "bool", "callers": [DEPLOYER]}]
            res = await switches.run(fork.rpc, token, pair, hp, st, probes)
        finally:
            fork.close()
        bad = [m for l, m in switches.findings(res) if l == "bad"]
        ok = (want is None and not bad) or (want is not None and any(f"`{want}`" in m for m in bad))
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} mode {mode}: " + (" | ".join(bad) or "no sell blockers"))
        if not ok or os.environ.get("DD_SHOW"):
            print("\n".join("    " + l for l in switches.render(res)))
    print("all passed" if not fails else f"{fails} failed")

run(main())
