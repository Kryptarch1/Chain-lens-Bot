"""dd sim <chain> <token>  — full dry-run suite on a private fork. Run ONLY when the user asks for simulations.

1. Baseline: buy and sell through the main pool (v2/v3 pair, or the v4 PoolManager) -> real transfer tax.
2. Every privileged function a known address can call is EXECUTED on the fork, one at a time from the same
   post-buy state, and we record what changed: totalSupply, caller balance, owner, and whether a holder can
   still sell afterwards (and at what tax).
3. Chained attacks: after each function, any mint-like function is retried by the same caller
   (catches "setController(self) then mint").
Nothing is broadcast: everything happens on a local anvil fork that is thrown away.
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import RPC, run, ZERO
from lib.evm import encode_call, decode1, fmt, short
from lib import explorer, sim, rugsim, tracer, args as argsmod
from cmds.check import phase_a, phase_b, phase_c, phase_e, phase_h, STRANGER

BUYER = "0x00000000000000000000000000000000000Bb1ee"


def calldata(sel, fn_name, args, caller):
    """Worst-case calldata for a privileged function (lib/args.py, mode 'worst')."""
    return argsmod.calldata(sel, args, caller, "worst", fn_name)


async def bal(f, token, who):
    return decode1("uint256", await f.eth_call(token, encode_call("balanceOf(address)", [who]))) or 0


async def supply(f, token):
    return decode1("uint256", await f.eth_call(token, encode_call("totalSupply()"))) or 0


async def sell_test(fork, token, pool, amount):
    """BUYER sends `amount` back to the pool. -> (ok, tax%)"""
    f = fork.rpc
    before = await bal(f, token, pool)
    r = await fork.send(BUYER, token, encode_call("transfer(address,uint256)", [pool, amount]))
    if not (r and r.get("status") == "0x1"):
        return False, None
    after = await bal(f, token, pool)
    return True, (1 - (after - before) / amount) * 100 if amount else None


async def main(chain, token):
    t0 = time.time()
    rpc = RPC(chain)
    ex = explorer.Explorer(rpc.name)
    info = await phase_a(rpc, ex, rpc.name, token)
    if len(info["code"]) <= 2:
        print(f"{token} has no code on {rpc.name}"); return
    fns, lps = await asyncio.gather(phase_b(info), phase_e(rpc, ex, info))
    probes, cands = await phase_c(rpc, token, info, fns)
    dec = info["decimals"] or 18
    sym = info.get("symbol") or "TOKEN"
    pair = info["pairs"][0] if info["pairs"] else None
    pool = None
    if pair:
        pool = pair["pairAddress"] if len(pair["pairAddress"]) == 42 else (rpc.cfg.get("v4") or {}).get("pool_manager")
    L = [f"== SIM {info.get('name')} ({sym})  {rpc.name}  {token}",
         f"   private fork of {rpc.name} at the latest block · nothing is broadcast"]
    fork = sim.Fork(rpc.name)
    try:
        if not await fork.ready():
            print("fork failed to start"); return
        f = fork.rpc
        # 0. rug simulation on the main pool (the part that matters most)
        if lps:
            try:
                res = await asyncio.wait_for(rugsim.run_all(fork, rpc, lps[0], token), 90)
            except asyncio.TimeoutError:
                res = [{"who": "-", "how": "rug sim", "ok": None, "note": "timed out"}]
            except Exception as e:
                res = [{"who": "-", "how": "rug sim", "ok": None, "note": f"error: {str(e)[:80]}"}]
            L.extend(rugsim.render(res, lps[0]))
        else:
            L.append("== RUG SIM: no pool found")
        # v4 hook: real swaps through the hook (measured fee, recipients, callbacks, hook owner powers)
        if lps and lps[0].get("type") == "v4" and lps[0].get("key") and lps[0]["key"][4] != ZERO:
            from lib import v4hook
            snap_h = await fork.snapshot()
            try:
                hk = await asyncio.wait_for(phase_h(fork, rpc, ex, token, lps), 45)
                L.extend(v4hook.render(hk, dec, {token: "token", lps[0]["key"][4]: "hook", "_token": token.lower()}))
            except Exception as e:
                L.append(f"== HOOK SIM failed: {str(e)[:80]}")
            await fork.revert(snap_h)
        sup0 = await supply(f, token)
        held = 0
        base = None
        base_tax = 0.0
        # 1. baseline buy/sell
        if pool:
            pbal = await bal(f, token, pool)
            if pbal > 1000:
                test = pbal // 1000
                r1 = await fork.send(pool, token, encode_call("transfer(address,uint256)", [BUYER, test]))
                held = await bal(f, token, BUYER)
                buy_ok = bool(r1 and r1.get("status") == "0x1")
                buy_tax = (1 - held / test) * 100 if buy_ok and test else None
                base = await fork.snapshot()  # state: BUYER holds tokens
                s_ok, s_tax = await sell_test(fork, token, pool, held) if held else (False, None)
                base_tax = s_tax or 0.0
                await fork.revert(base); base = await fork.snapshot()
                kind = "v4 PoolManager" if len(pair["pairAddress"]) == 66 else "pool"
                L.append(f"== BASELINE via {kind} {short(pool)}: buy {'OK' if buy_ok else 'FAIL'}" + (f" tax {buy_tax:.1f}%" if buy_tax is not None else "") +
                         f" · sell {'OK' if s_ok else 'FAIL'}" + (f" tax {s_tax:.1f}%" if s_tax is not None else ""))
                if len(pair["pairAddress"]) == 66:
                    L.append("   (v4: token-level tax only; the hook's own fee is measured in HOOK SIM above)")
        else:
            L.append("== BASELINE: no pool found — buy/sell not simulated")
        if base is None:
            base = await fork.snapshot()
        # 2. privileged functions
        runnable = [p for p in probes if p.get("callers") and "note" not in p]
        mint_like = [p for p in probes if "mint" in (p["fn"] or "").lower() or "issue" in (p["fn"] or "").lower()]
        L.append(f"== OWNER POWERS: {len(runnable)} function(s) callable by a privileged address" + ("" if runnable else " — nothing to run"))
        for p in runnable[:12]:
            who = p["callers"][0]
            data = calldata(p["sel"], p["fn"], p["args"], who)
            if data is None:
                L.append(f"   · {p['fn']}: args not simulatable"); continue
            await fork.revert(base); base = await fork.snapshot()
            cbal0 = await bal(f, token, who)
            own0 = (await f.try_call(token, encode_call("owner()")))[1]
            r = await fork.send(who, token, data)
            ok = bool(r and r.get("status") == "0x1")
            if not ok:
                L.append(f"   · {p['fn']} by {short(who)}: reverted with worst-case args"); continue
            effects = []
            tr = await tracer.annotate(await tracer.trace_tx(f, r.get("transactionHash")))
            traced = tracer.describe(tr, token, {token: "token", pool or "": "pool", BUYER: "buyer", who: "caller"}, dec, sym, [info.get("impl")])
            s1 = await supply(f, token)
            if s1 != sup0:
                effects.append(f"supply {fmt(sup0,dec,0)} → {fmt(s1,dec,0)}")
            d = await bal(f, token, who) - cbal0
            if d:
                effects.append(f"caller {'+' if d > 0 else ''}{fmt(d,dec,0)} {sym}")
            own1 = (await f.try_call(token, encode_call("owner()")))[1]
            if own1 != own0:
                effects.append("owner changed")
            if held and pool:
                snap2 = await fork.snapshot()
                s_ok, s_tax = await sell_test(fork, token, pool, held)
                await fork.revert(snap2)
                effects.append("holders can't sell" if not s_ok else (f"sell tax {base_tax:.1f}% → {s_tax:.1f}%" if s_tax is not None and s_tax > base_tax + 1 else "sell still OK"))
            # chained: retry mint-like fns by the same caller
            for m in mint_like:
                if m["sel"] == p["sel"]:
                    continue
                md = calldata(m["sel"], m["fn"], m["args"], who)
                if not md:
                    continue
                snap3 = await fork.snapshot()
                before = await supply(f, token)
                rm = await fork.send(who, token, md)
                after = await supply(f, token)
                await fork.revert(snap3)
                if rm and rm.get("status") == "0x1" and after > before:
                    effects.append(f"then {m['fn']} by same caller mints +{fmt(after-before,dec,0)}")
            bad = any(k in " ".join(effects) for k in ("supply", "can't sell", "% →", "mints", "owner changed")) or (d and d > 0)
            if tr:
                moved = [t for t in tr["transfers"] if t["token"] == token.lower() and t["from"] not in (who.lower(), ZERO, token.lower())]
                if moved:
                    effects.append("MOVES OTHER HOLDERS' TOKENS"); bad = True
                if any(o in ("delegatecall", "selfdestruct") for o in tracer.danger_ops(tr, [info.get("impl")])):
                    bad = True
            L.append(f"   {'✗' if bad else '·'} {p['fn']} by {short(who)}: {'; '.join(effects) or 'no visible effect'}" + (f"\n      traced: {traced}" if traced else ""))
    finally:
        fork.close()
    L.append(f"   ({time.time()-t0:.1f}s)")
    print("\n".join(L))


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit("usage: dd sim <chain> <token>")
    run(main(a[0], a[1]))
