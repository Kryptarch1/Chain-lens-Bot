"""dd sell <chain> <token> — stateful honeypot / delayed-honeypot / funds-taken tests on a private fork."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import RPC, run
from lib import explorer, sim, sellsim
from cmds.check import phase_a, phase_b, phase_c
from cmds.sim import calldata as worst_case


async def main(chain, token):
    t0 = time.time()
    rpc = RPC(chain)
    ex = explorer.Explorer(rpc.name)
    info = await phase_a(rpc, ex, rpc.name, token)
    fns = await phase_b(info)
    probes, _ = await phase_c(rpc, token, info, fns)
    pair = info["pairs"][0] if info["pairs"] else None
    if not pair:
        print("no pool found"); return
    pool = pair["pairAddress"] if len(pair["pairAddress"]) == 42 else (rpc.cfg.get("v4") or {}).get("pool_manager")
    fork = sim.Fork(rpc.name)
    try:
        if not await fork.ready():
            print("fork failed"); return
        out = await sellsim.run(fork, rpc, token, pool, probes, worst_case, info["decimals"] or 18)
    finally:
        fork.close()
    print(f"== {info.get('name')} ({info.get('symbol')}) {rpc.name}")
    print("\n".join(sellsim.render(out)))
    print(f"   (total {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    run(main(a[0], a[1]))
