"""Real tokens, frozen in time: forks at a FIXED past block, so the answer never drifts (needs the archive node).
Checks the v4 hook measurement on real hooks: exact fee, where it goes, what the callbacks do.
Usage: .venv/bin/python tests/pinned.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import RPC, run, ZERO
from lib import sim, v4hook

CASES = [
    {"name": "WHEEL hook (StandardLaunchHook)", "chain": "rh", "block": 91059800,
     "token": "0xeb579e280648b6f48a840086a9e6631500a20684",
     "lp": {"pool_id": "0x37c81007242853be8807baeb80a1663bcf72a5148135ade6de91c209cffcacf7",
            "key": ("0x322f0929c4625ed5bad873c95208d54e1c003b2d", "0xeb579e280648b6f48a840086a9e6631500a20684", 0, 200, "0x992118de23b4ebe9f527dacbbb7d8d4bbbf8e044")},
     "fee": 4.00, "callback": "PoolManager.mint", "to": "0x992118de23b4ebe9f527dacbbb7d8d4bbbf8e044"},
    {"name": "IMDO hook (unverified fee hook)", "chain": "eth", "block": 26213400,
     "token": "0x87b525bc5ff6d7211f16291219c363e082ed4e9f",
     "lp": {"pool_id": "0x6676c963c163734caa07a9abd5056375ed88e777bbc424834558019446d0dfba",
            "key": (ZERO, "0x87b525bc5ff6d7211f16291219c363e082ed4e9f", 0, 60, "0x5d79a5998cfcb4f17fffcaa0aa603499f73e60cc")},
     "fee": 1.50, "callback": "PoolManager.take", "to_prefix": ("0xaa90", "c3f7"), "fork": False},
]


async def main():
    fails = 0
    for c in CASES:
        # fast engine: eth_simulateV1 straight on the node, built on the pinned block (no fork needed)
        fast = await v4hook.analyze_fast(RPC(c["chain"]), c["lp"], c["token"], block=c["block"])
        if c.get("fork", True):
            fork = await sim.start(c["chain"], block=c["block"])
            if not fork:
                print(f"FAIL {c['name']}: fork at block {c['block']} did not start"); fails += 1; continue
            try:
                out = await v4hook.analyze(fork, c["lp"], c["token"])
            finally:
                fork.close()
        else:  # this node can't serve a fork at an old block (anvil's account fetch needs recent state): fast engine only
            out = fast or {}
        buy, sell = (out.get("buy") or {}), (out.get("sell") or {})
        acts = [a for v in (out.get("swap_map") or {}).values() for a in v["acts"]]
        rec = [a for a, _, _ in (out.get("buy_to") or [])]
        problems = []
        for side, r in (("buy", buy), ("sell", sell)):
            if not r.get("ok") or abs((r.get("hook_pct") or -1) - c["fee"]) > 0.01:
                problems.append(f"{side} fee {r.get('hook_pct')} (want {c['fee']})")
        for side in ("buy", "sell"):  # the eth_simulateV1 engine must measure the same fee
            fr = (fast or {}).get(side) or {}
            if not fr.get("ok") or abs((fr.get("hook_pct") or -1) - c["fee"]) > 0.01:
                problems.append(f"fast engine {side} fee {fr.get('hook_pct')} (want {c['fee']})")
        frec = [a for a, _, _ in ((fast or {}).get("buy_to") or [])]
        if c.get("to") and c["to"] not in frec or c.get("to_prefix") and not any(a.startswith(c["to_prefix"][0]) and a.endswith(c["to_prefix"][1]) for a in frec):
            problems.append(f"fast engine: cut recipient missing ({frec})")
        if c["callback"] not in acts:
            problems.append(f"callback {c['callback']} missing ({acts})")
        if c.get("to") and c["to"] not in rec:
            problems.append(f"cut not paid to {c['to']} ({rec})")
        if c.get("to_prefix") and not any(a.startswith(c["to_prefix"][0]) and a.endswith(c["to_prefix"][1]) for a in rec):
            problems.append(f"cut not paid to {'…'.join(c['to_prefix'])} ({rec})")
        fails += bool(problems)
        print(f"{'PASS' if not problems else 'FAIL'} {c['name']} @ {c['chain']} block {c['block']}: "
              + ("; ".join(problems) or f"hook takes {buy.get('hook_pct'):.2f}% buy / {sell.get('hook_pct'):.2f}% sell"))
    print("all passed" if not fails else f"{fails} failed")

run(main())
