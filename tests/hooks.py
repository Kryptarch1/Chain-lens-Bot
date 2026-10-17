"""Malicious v4 hook fixtures on an ETH fork: our own fee hook (fixtures/src/H.sol) on a real PoolManager pool.
Checks the hook sim (lib/v4hook) and the v4 sell tests (lib/sellsim with real swaps) catch each kind.
Usage: .venv/bin/python tests/hooks.py [mode ...]"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eth_abi import encode
from eth_utils import keccak
from lib.rpc import RPC, run, ZERO
from lib import sim, sellsim, v4hook
from lib.evm import encode_call, selector
from cmds.sim import calldata as worst_case

OUT = os.path.join(os.path.dirname(__file__), "..", "fixtures", "out")
art = lambda f, c: json.load(open(os.path.join(OUT, f, c + ".json")))["bytecode"]["object"]
DEPLOYER = "0x00000000000000000000000000000000000dE1ee"
TREASURY = "0x000000000000000000000000000000000000Fee5"
HOOK_AT = "0x00000000000000000000000000000000Fee40044"  # low 14 bits = afterSwap(1<<6) | afterSwapReturnDelta(1<<2)
CASES = {  # mode: (fee bps, hook-sim flag that must appear, sell-test flag that must appear)
    0: (300, "hook owner can raise the fee", None),
    1: (300, "HONEYPOT (hook)", "HONEYPOT"),
    2: (300, "DELAYED HONEYPOT (hook)", "DELAYED HONEYPOT"),
}
KEY_T = "(address,address,uint24,int24,address)"


async def setup(fork, mode, fee):
    f = fork.rpc
    pm = fork.src.cfg["v4"]["pool_manager"]
    token = await fork.deploy(DEPLOYER, "0x" + art("H.sol", "HT").replace("0x", "") + encode(["address", "uint256"], [DEPLOYER, 10 ** 27]).hex())
    assert token, "token deploy failed"
    tmp = await fork.deploy(DEPLOYER, "0x" + art("H.sol", "FeeHook").replace("0x", "") + encode(["address"] * 4, [pm, DEPLOYER, TREASURY, token]).hex())
    assert tmp, "hook deploy failed"
    await f.call("anvil_setCode", [HOOK_AT, await f.code(tmp)])
    await fork.send(DEPLOYER, HOOK_AT, encode_call("setup(uint256,uint256)", [mode, fee]))
    key = (ZERO, token, 3000, 60, HOOK_AT)
    r = await fork.send(DEPLOYER, pm, encode_call(f"initialize({KEY_T},uint160)", [key, 1000 * 2 ** 96]), gas=1_000_000)
    assert r and r.get("status") == "0x1", f"initialize failed {r}"
    h = await v4hook.helper(fork, pm)
    await fork.send(DEPLOYER, token, encode_call("approve(address,uint256)", [h, 10 ** 27]))
    r = await fork.send(DEPLOYER, h, encode_call(f"liquidity({KEY_T},int24,int24,int256,bytes32)", [key, -887220, 887220, 10 ** 21, b"\x00" * 32]),
                        value=10 ** 19, gas=5_000_000)
    assert r and r.get("status") == "0x1", f"add liquidity failed {r}"
    pool_id = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"], list(key))).hex()
    return token, {"type": "v4", "key": key, "pool_id": pool_id}


async def main(modes):
    fails = 0
    probes = [{"fn": "setFee(uint256)", "sel": selector("setFee(uint256)"), "args": "uint256", "callers": [DEPLOYER]},
              {"fn": "setBlocked(bool)", "sel": selector("setBlocked(bool)"), "args": "bool", "callers": [DEPLOYER]}]
    for mode in modes:
        fee, want_hook, want_sell = CASES[mode]
        fork = await sim.start("eth")
        assert fork, "fork did not start (twice)"
        try:
            t0 = time.time()
            token, lp = await setup(fork, mode, fee)
            base = await fork.snapshot()
            pm = fork.src.cfg["v4"]["pool_manager"]
            fast = await v4hook.analyze_fast(fork.rpc, lp, token, probes, pm=pm)  # eth_simulateV1 engine, same expectations
            hk = await v4hook.analyze(fork, lp, token, probes, worst_case)
            await fork.revert(base)
            pool = v4hook.Pool(lp, token, fork.src.cfg["v4"]["pool_manager"])
            ss = await sellsim.run(fork, RPC("eth"), token, pool.pm, [], worst_case, v4=pool)
        finally:
            fork.close()
        hflags = " | ".join(m for _, m in hk.get("flags", []))
        sflags = " | ".join(m for _, m in ss.get("flags", []))
        fee_ok = abs(((hk.get("buy") or {}).get("hook_pct") or 0) - fee / 100) < 0.05  # measured = configured
        fflags = " | ".join(m for _, m in (fast or {}).get("flags", []))
        fast_ok = bool(fast) and want_hook.lower() in fflags.lower() and abs(((fast.get("buy") or {}).get("hook_pct") or 0) - fee / 100) < 0.05
        ok = want_hook.lower() in hflags.lower() and (want_sell is None or want_sell.lower() in sflags.lower()) and fee_ok and fast_ok
        print(f"    fast hook engine: {fflags or 'no flags'}")
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} hook mode {mode} ({time.time()-t0:.1f}s): measured buy fee {(hk.get('buy') or {}).get('hook_pct')}% (set {fee/100}%)\n"
              f"    hook sim: {hflags or 'no flags'}\n    sell tests: {sflags or 'no flags'}")
        if not ok or os.environ.get("DD_SHOW"):
            print("\n".join("    " + l for l in v4hook.render(hk, labels={TREASURY: "treasury", HOOK_AT: "hook"})))
            print("\n".join("    " + l for l in sellsim.render(ss)))
    print("all passed" if not fails else f"{fails} failed")


if __name__ == "__main__":
    run(main([int(x) for x in sys.argv[1:]] or list(CASES)))
