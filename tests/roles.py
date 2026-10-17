"""Authority fixture: deploy an AccessControl token with no minter; admin alive (mode 0) vs admin renounced (mode 1)."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eth_abi import encode
from lib.rpc import run
from lib import sim, sigs, authority, explorer
ART = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "out", "R.sol", "R.json")))
DEPLOYER = "0x00000000000000000000000000000000000dE1ee"


async def main():
    fails = 0
    for mode, expect_grantable in ((0, True), (1, False)):
        fork = await sim.start("bsc"); assert fork, "fork did not start (twice)"
        try:
            t0 = time.time()
            token = await fork.deploy(DEPLOYER, "0x" + ART["bytecode"]["object"].replace("0x", "") + encode(["uint8"], [mode]).hex())
            assert token
            await fork.mine(1)
            rpc = fork.rpc
            blk = await rpc.block_number()
            code = await rpc.code(token)
            rows = sigs.selectors_from_bytecode(code); names = await sigs.names_for([r[0] for r in rows])
            fns = [{"sel": s, "name": names.get(s), "args": a, "mut": m, "cls": sigs.classify(s, names.get(s), m)} for s, a, m in rows]
            info = {"owner": None, "creator": DEPLOYER, "created_block": blk - 3, "admin": None}
            a = await authority.analyze(rpc, explorer.Explorer("bsc"), token, info, fns, [])
            got = bool(a.get("grantable"))
            ok = got == expect_grantable
            fails += not ok
            print(f"{'PASS' if ok else 'FAIL'} mode {mode} ({time.time()-t0:.1f}s): grantable={got} roles={ {authority.role_name(k): len(v['members']) for k, v in a['roles'].items()} }")
            print("\n".join("    " + l for l in authority.render(a)))
        finally:
            fork.close()
    print("all passed" if not fails else f"{fails} failed")

run(main())
