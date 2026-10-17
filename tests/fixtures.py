"""Deploy fixture tokens on a BSC fork with a real PancakeSwap pair and check the sell-test classifier.
Usage: .venv/bin/python tests/fixtures.py [mode ...]"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eth_abi import encode
from lib.rpc import RPC, run
from lib import sim, sellsim, quicksim, tracer, fastsell
from lib.evm import encode_call, decode1, selector, addr as to_addr
from cmds.sim import calldata as worst_case

ART = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "out", "T.sol", "T.json")))
BYTECODE = ART["bytecode"]["object"]
FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
DEPLOYER = "0x00000000000000000000000000000000000dE1ee"
CASES = {  # mode: (limit, expected flag substring, must_not)
    0: (0, None, "HONEYPOT"),
    1: (0, "HONEYPOT", None),
    2: (30, "DELAYED HONEYPOT", None),
    3: (20, "cooldown", "HONEYPOT"),
    4: (10 ** 22, "SELL CAP", "HONEYPOT:"),
    5: (0, "FUNDS TAKEN by owner", None),
    6: (0, "SELL BLOCKER", None),
}


async def setup(fork, mode, limit):
    f = fork.rpc
    code = "0x" + BYTECODE.replace("0x", "") + encode(["uint8", "uint256"], [mode, limit]).hex()
    token = await fork.deploy(DEPLOYER, code)
    assert token, "deploy failed"
    r = await fork.send(DEPLOYER, FACTORY, encode_call("createPair(address,address)", [token, WBNB]), gas=8_000_000)
    pair = to_addr(await f.eth_call(FACTORY, encode_call("getPair(address,address)", [token, WBNB])))
    assert r and r.get("status") == "0x1" and int(pair, 16), f"createPair failed: {r}"
    await fork.send(DEPLOYER, token, encode_call("setPair(address)", [pair]))
    # seed: 500M tokens + 20 WBNB
    await fork.send(DEPLOYER, WBNB, encode_call("deposit()"), value=20 * 10 ** 18)
    await fork.send(DEPLOYER, WBNB, encode_call("transfer(address,uint256)", [pair, 20 * 10 ** 18]))
    await fork.send(DEPLOYER, token, encode_call("transfer(address,uint256)", [pair, 5 * 10 ** 26]))
    await fork.send(DEPLOYER, pair, encode_call("mint(address)", [DEPLOYER]))
    await fork.mine(1)
    return token, pair


async def main(modes):
    rpc = RPC("bsc")
    fails = 0
    for mode in modes:
        limit, expect, must_not = CASES[mode]
        fork = await sim.start("bsc")  # fresh fork per case: BSC nodes drop old state after ~127 blocks (~95 s)
        assert fork, "fork did not start (twice)"
        try:
            t0 = time.time()
            token, pair = await setup(fork, mode, limit)
            v2 = await sellsim._v2_info(fork.rpc, token, pair)
            probes = [{"fn": "drain(address)", "sel": selector("drain(address)"), "args": "address", "callers": [DEPLOYER]},
                      {"fn": "setBlocked(bool)", "sel": selector("setBlocked(bool)"), "args": "bool", "callers": [DEPLOYER]}] if mode in (5, 6) else []
            quick = ""
            if mode in (5, 6):  # quick check (eth_simulateV1 on the fork) + trace must see the drain without a deep run
                hp = await quicksim.honeypot(fork.rpc, token, pair)
                e = await quicksim.fn_effect(fork.rpc, token, pair, DEPLOYER, worst_case(probes[0]["sel"], probes[0]["fn"], probes[0]["args"], DEPLOYER), hp)
                quick = f"holder_delta={e and e.get('holder_delta')} traced={tracer.describe(e and e.get('trace'), token)}"
                if not (e and e.get("holder_delta", 0) < 0):
                    quick = "QUICK MISSED DRAIN: " + quick
            snap = await fork.snapshot()
            fast = await fastsell.run(fork.rpc, token, pair, probes)  # same scenarios via eth_simulateV1 (anvil supports it)
            await fork.revert(snap)
            out = await sellsim.run(fork, rpc, token, pair, probes, worst_case)
        finally:
            fork.close()
        flags = " | ".join(m for _, m in out["flags"])
        fflags = " | ".join(m for _, m in (fast or {}).get("flags", [])) if fast else "FAST ENGINE RETURNED NOTHING"
        good = lambda fl: (expect is None or expect.lower() in fl.lower()) and (must_not is None or must_not.lower() not in fl.lower())
        ok = good(flags) and fast is not None and good(fflags) and not quick.startswith("QUICK MISSED")
        print(f"    fast engine ({(fast or {}).get('elapsed', 0):.1f}s): {fflags or 'no flags'}")
        if quick:
            print(f"    quick: {quick}")
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} mode {mode} ({time.time()-t0:.1f}s, v2={'yes' if v2 else 'NO'}, method={out.get('method')}): {flags or 'no flags'}")
        if not ok or os.environ.get("DD_SHOW"):
            print("\n".join("    " + l for l in sellsim.render(out)))
    print("all passed" if not fails else f"{fails} failed")


if __name__ == "__main__":
    modes = [int(x) for x in sys.argv[1:]] or list(CASES)
    run(main(modes))
