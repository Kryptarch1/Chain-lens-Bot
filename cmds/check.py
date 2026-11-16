"""dd check <chain> <token>  — token safety + LP in one run.

Phases (parallel wherever possible):
  A. reads: code (unwrap 1167 clones / 1967 proxies / beacons), metadata, owner, dexscreener (GoPlus fallback), source, creator
  B. functions: selectors -> names -> classify (standard / view / risky / unknown)
  C. privilege probes: for every risky/unknown state-changing fn, who can call it?
     candidates = owner, creator, addresses found in storage slots 0-15, addresses hardcoded in bytecode
  D. fork: run each candidate-callable risky fn and watch totalSupply/balances; transfer-tax test via the main pool
  E. mint history: every Transfer from 0x0 after the first one = supply raised after launch
  F. LP: who owns liquidity, can they pull it
Prints a compact report ending with VERDICT and NOT CHECKED lines.
"""
import asyncio, json, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import RPC, RPCError, ZERO, DEAD, TRANSFER, run, pad_addr, topic_addr
from lib.evm import encode_call, decode1, addr as to_addr, hardcoded_addresses, fmt, short
from lib import sigs, explorer, lp as lpmod, sim, discover, who as whomod, quicksim, sellsim, authority, tracer, decomp, static, switches, args as argsmod

SLOT_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SLOT_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
SLOT_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
ZOS_IMPL = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
STRANGER = "0x000000000000000000000000000000000000bEEF"


def calldata(sel, args, who, small=False):
    """Calldata for access probes (lib/args.py, modes 'probe' / 'small')."""
    return argsmod.calldata(sel, args, who, "small" if small else "probe")


def clone_target(code):
    """EIP-1167 minimal proxy (and push-20 delegatecall variants) -> implementation address."""
    import re as _re
    h = code[2:].lower()
    if len(h) > 200:
        return None
    m = _re.search(r"363d3d373d3d3d363d73([0-9a-f]{40})5af43d", h) or _re.search(r"3d73([0-9a-f]{40})5af4", h)
    return "0x" + m.group(1) if m else None


async def phase_a(rpc, ex, chain, token):
    fns = ("name()", "symbol()", "decimals()", "totalSupply()", "owner()", "getOwner()", "paused()", "tradingEnabled()", "tradingOpen()")
    code, m, pairs, gpl, source, creator = await asyncio.gather(
        rpc.code(token),
        rpc.batch([("eth_call", [{"to": token, "data": encode_call(f)}, "latest"]) for f in fns] +
                  [("eth_getStorageAt", [token, s, "latest"]) for s in (SLOT_IMPL, SLOT_ADMIN, SLOT_BEACON, ZOS_IMPL)] +
                  [("eth_getStorageAt", [token, hex(i), "latest"]) for i in range(16)]),
        explorer.dexscreener_token(chain, token), explorer.goplus(rpc.cfg["id"], token), ex.source(token), ex.creator(token))

    def s(x):
        try:
            return decode1("string", x)
        except Exception:
            try:
                return bytes.fromhex(x[2:]).rstrip(b"\x00").decode(errors="ignore").strip() or None
            except Exception:
                return None

    def u(x):
        try:
            return decode1("uint256", x)
        except Exception:
            return None

    def a(x):
        v = to_addr(x) if x and len(x) >= 66 else None
        return None if v == ZERO else v

    def b(x):
        return (u(x) == 1) if x and x != "0x" else None

    slots = m[13:29]
    slot_addrs = []
    for i, v in enumerate(slots):
        if v and len(v) == 66 and v[2:26] == "0" * 24 and v[26:] != "0" * 40:
            slot_addrs.append(("0x" + v[26:], i))
    info = {"code": code, "name": s(m[0]), "symbol": s(m[1]), "decimals": u(m[2]) if m[2] and m[2] != "0x" else 18,
            "supply": u(m[3]), "owner": a(m[4]) or a(m[5]), "paused": b(m[6]), "trading": b(m[7]) if m[7] and m[7] != "0x" else b(m[8]),
            "impl": a(m[9]), "admin": a(m[10]), "beacon": a(m[11]), "zos_impl": a(m[12]), "slot_addrs": slot_addrs,
            "pairs": sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0)),
            "goplus": gpl or {}, "source": source, "creator": (creator or {}).get("creator"), "clone": None}
    if not info["pairs"] and gpl and gpl.get("dex"):
        info["pairs"] = [{"pairAddress": d.get("pair"), "dexId": (d.get("name") or "").lower(), "labels": ["v4"] if "v4" in (d.get("liquidity_type") or "").lower() else (["v3"] if "v3" in (d.get("liquidity_type") or "").lower() else []),
                          "liquidity": {"usd": float(d.get("liquidity") or 0)}, "quoteToken": {"symbol": "?"}, "pairCreatedAt": None, "src": "goplus"} for d in gpl["dex"] if d.get("pair")]
        info["pairs"].sort(key=lambda p: -(p["liquidity"]["usd"] or 0))
    try:
        hint = min([p["pairCreatedAt"] / 1000 for p in info["pairs"] if p.get("pairCreatedAt")] or [0]) or None
        info["created_block"], info["created_tx"] = await asyncio.wait_for(discover.creation_block(rpc, ex, token, hint), 15)
    except asyncio.TimeoutError:
        info["created_block"], info["created_tx"] = None, None
    if not info["creator"] and info.get("created_tx"):
        # no explorer creator API (X Layer…): the creation tx's sender, read on-chain
        try:
            t = await rpc.call("eth_getTransactionByHash", [info["created_tx"]])
            info["creator"] = (t or {}).get("from")
        except Exception:
            pass
    if not info["creator"] and (gpl or {}).get("creator_address"):
        info["creator"] = gpl["creator_address"].lower()  # GoPlus, last resort
    if info["created_block"] and not info["pairs"]:
        found = await discover.pools_from_receipts(rpc, [info["created_tx"]], token) if info.get("created_tx") else []
        if not found:
            found = await discover.pools_from_transfers(rpc, ex, token, info["created_block"])
        if not found:
            try:
                found = await asyncio.wait_for(discover.find_pools(rpc, token, info["created_block"]), 20)
            except asyncio.TimeoutError:
                found = []
        info["pairs"] = await discover.enrich_pools(rpc, found, token)
        info["pools_src"] = "on-chain scan"
    for p in info["pairs"]:
        p.setdefault("seed_txs", [info["created_tx"]] if info.get("created_tx") else [])
    await promote_launch_pool(rpc, info, token)
    if (rpc.cfg.get("v4") or {}).get("any_manager"):
        # many PoolManagers on this chain: each v4 pool carries its own, and the main pool's becomes the chain's
        # v4 config for the rest of this check (LP, sell tests, hook sims all read rpc.cfg["v4"])
        todo = [p for p in info["pairs"][:2] if len(p.get("pairAddress") or "") == 66 and not p.get("pm")]
        for p, pm in zip(todo, await asyncio.gather(*[discover.v4_manager_of(rpc, p["pairAddress"]) for p in todo])):
            p["pm"] = pm
        main = info["pairs"][0] if info["pairs"] else {}
        if main.get("pm"):
            rpc.cfg = {**rpc.cfg, "v4": {"pool_manager": main["pm"], "any_manager": True}}
    if info["beacon"]:
        ok, r = await rpc.try_call(info["beacon"], encode_call("implementation()"))
        if ok and r and len(r) >= 66:
            info["impl"] = to_addr(r)
    ct = clone_target(code)
    if ct:
        info["clone"] = ct
        info["impl"] = info["impl"] or ct
    if info["impl"]:
        info["impl_code"] = await rpc.code(info["impl"])
    info["address"] = token.lower()
    return info


async def promote_launch_pool(rpc, info, token):
    """The pool created in the token's own launch tx holds the dev's liquidity. DexScreener can't price pools quoted in
    another meme/launchpad token (liquidity usd = None), so sorting by USD put it LAST and the check judged some later
    pool a stranger opened (PHIGHT: Hatchery launch pool paired with BITCOIN, locked; a $500 ETH side pool, unlocked).
    If an unpriced launch pool still holds live liquidity, it becomes the main pool; the market line keeps the priced pair."""
    pairs = info["pairs"]
    if not info.get("created_tx") or len(pairs) < 2:
        return
    try:
        launch = await asyncio.wait_for(discover.pools_from_receipts(rpc, [info["created_tx"]], token), 8)
    except Exception:
        return
    ids = {p["pairAddress"].lower() for p in launch}
    if not ids or pairs[0]["pairAddress"].lower() in ids:
        return
    lp_by_id = {p["pairAddress"].lower(): p for p in launch}
    for i, p in enumerate(pairs):
        pid = p["pairAddress"].lower()
        if pid not in ids or (p.get("liquidity") or {}).get("usd") is not None:
            continue
        if len(pid) == 66:  # v4: must still hold liquidity in its PoolManager
            from lib import lp as _lp
            pm = p.get("pm") or lp_by_id[pid].get("pm") or (rpc.cfg.get("v4") or {}).get("pool_manager")
            if not pm or not await _lp.v4_pool_liquidity(rpc, pm, pid):
                continue
            p.setdefault("pm", pm)
        info["market_pair"] = pairs[0]
        p["launch_pool"] = True
        if ((p.get("quoteToken") or {}).get("address") or "").lower() in ("", token.lower()):
            # DexScreener lists the pair the other way round (our token as quote): the real quote is its base token
            p["quoteToken"] = p.get("baseToken") if ((p.get("baseToken") or {}).get("address") or "").lower() not in ("", token.lower()) \
                else lp_by_id[pid].get("quoteToken")
        pairs.insert(0, pairs.pop(i))
        return


async def phase_b(info):
    code = info.get("impl_code") or info["code"]
    rows = sigs.selectors_from_bytecode(code)
    names = await sigs.names_for([r[0] for r in rows])
    return [{"sel": sel, "name": names.get(sel), "args": args, "mut": mut, "cls": sigs.classify(sel, names.get(sel), mut)} for sel, args, mut in rows]


async def phase_c(rpc, token, info, fns):
    """Who can call each risky/unknown state-changing function."""
    raw = []
    for a in [info.get("owner"), info.get("creator")] + [x for x, _ in info["slot_addrs"]] + hardcoded_addresses(info.get("impl_code") or info["code"])[:8]:
        if a and a.lower() not in [c.lower() for c in raw] and a.lower() not in (token.lower(), ZERO):
            raw.append(a)
    # keep only addresses that exist on-chain (have code or have sent a tx) — drops string/metadata junk
    chk = await rpc.batch([("eth_getTransactionCount", [a, "latest"]) for a in raw] + [("eth_getCode", [a, "latest"]) for a in raw])
    n = len(raw)
    cands = [a for i, a in enumerate(raw) if (chk[i] and int(chk[i], 16) > 0) or (chk[n + i] and len(chk[n + i]) > 2)][:8]
    targets = [f for f in fns if f["cls"] in ("risky", "unknown", "other") and f["mut"] not in ("view", "pure")][:30]
    results = []

    async def probe(f):
        out = {"fn": f["name"] or f"unnamed {f['sel']}", "sel": f["sel"], "args": f["args"], "callers": [], "stranger": None}
        data = calldata(f["sel"], f["args"], cands[0] if cands else STRANGER)
        if data is None:
            out["note"] = "args not probeable"
            return out
        ok, _ = await sim.override_call(rpc, token, data, STRANGER)
        out["stranger"] = ok
        if not ok:
            res = await asyncio.gather(*[sim.override_call(rpc, token, calldata(f["sel"], f["args"], c), c) for c in cands])
            out["callers"] = [c for c, (k, _) in zip(cands, res) if k]
            if not out["callers"] and cands and any(a.strip().startswith(("uint", "int")) for a in f["args"].split(",")):
                # big test values can fail a value check (e.g. "fee can only be lowered") -> retry small so access is still seen
                ok2, _ = await sim.override_call(rpc, token, calldata(f["sel"], f["args"], STRANGER, True), STRANGER)
                if ok2:
                    out["stranger"] = True
                else:
                    res = await asyncio.gather(*[sim.override_call(rpc, token, calldata(f["sel"], f["args"], c, True), c) for c in cands])
                    out["callers"] = [c for c, (k, _) in zip(cands, res) if k]
        return out

    results = await asyncio.gather(*[probe(f) for f in targets])
    info["steal"] = await free_transfer_probe(rpc, token, info, cands)
    return results, cands


async def free_transfer_probe(rpc, token, info, cands):
    """transferFrom(pool -> stranger) with NO allowance, as a stranger and as each candidate (eth_call: tx.origin = from).
    Catches backdoors like 'allowance is only charged when tx.origin != deployer' (ASSETS on hyper): whoever passes can
    take any holder's tokens, incl. the pool's -> drain the LP although the LP tokens are burned."""
    pools = [p.get("pairAddress") for p in info.get("pairs") or [] if len(p.get("pairAddress") or "") == 42][:2]
    if not pools:
        return None
    sel = "0x23b872dd"  # transferFrom(address,address,uint256)

    async def ok(victim, who):
        k, r = await rpc.try_call(token, sel + victim[2:].lower().rjust(64, "0") + STRANGER[2:].lower().rjust(64, "0") + "1".rjust(64, "0"), who)
        return k and (r in ("0x", None) or (len(r) >= 66 and int(r[:66], 16) == 1))
    for pool in pools:
        res = await asyncio.gather(*[ok(pool, w) for w in [STRANGER] + cands])
        if any(res):
            return {"fn": "transferFrom without approval", "sel": sel, "args": "address,address,uint256", "steal": True,
                    "victim": pool, "stranger": res[0], "callers": [] if res[0] else [c for c, k in zip(cands, res[1:]) if k]}
    return None


async def contract_probes(rpc, ex, addr):
    """Who can call each privileged function of ANY contract (used for v4 hooks). -> (fns, probes)"""
    code, reads, cr = await asyncio.gather(rpc.code(addr),
        rpc.batch([("eth_call", [{"to": addr, "data": encode_call("owner()")}, "latest"])] + [("eth_getStorageAt", [addr, hex(i), "latest"]) for i in range(16)]),
        ex.creator(addr))
    own = to_addr(reads[0]) if reads[0] and len(reads[0]) >= 66 and int(reads[0], 16) else None
    slot_addrs = [("0x" + v[26:], i) for i, v in enumerate(reads[1:]) if v and len(v) == 66 and v[2:26] == "0" * 24 and v[26:] != "0" * 40]
    mini = {"code": code, "owner": own, "creator": (cr or {}).get("creator"), "slot_addrs": slot_addrs}
    fns = await phase_b(mini)
    probes, _ = await phase_c(rpc, addr, mini, fns)
    return fns, probes


async def phase_h(fork, rpc, ex, token, lps):
    """v4 hook analysis: real swaps through the hook, measured fee, recipients, callbacks, owner powers.
    fork=None: eth_simulateV1 (fast); falls back to a fork if the node can't do it."""
    lp = next((l for l in lps[:1] if l.get("type") == "v4" and l.get("key") and l["key"][4] != ZERO), None)
    if not lp:
        return None
    from lib import v4hook
    try:
        _, hp = await asyncio.wait_for(contract_probes(rpc, ex, lp["key"][4]), 15)
    except Exception:
        hp = []

    async def run_it():
        if fork is None:
            r = await v4hook.analyze_fast(rpc, lp, token, hp)
            if r is not None:
                return r
            f = sim.Fork(rpc.name)
            try:
                return await v4hook.analyze(f, lp, token, hp) if await f.ready() else {"hook": lp["key"][4], "note": "fork failed to start"}
            finally:
                f.close()
        return await v4hook.analyze(fork, lp, token, hp)

    return await run_it()


async def _hook_decomp(rpc, ex, hook):
    """Code view of the hook: Slither if verified (falls back to Heimdall if it won't compile), else Heimdall."""
    try:
        if (await ex.source(hook)).get("verified"):
            st = await asyncio.wait_for(static.analyze(ex, hook), 60)
            if st and not st.get("error"):
                return {"static": st}
        return await asyncio.wait_for(decomp.analyze(await rpc.code(hook)), 30)
    except Exception:
        return None


async def phase_d(chain, token, info, probes, fns=()):
    """Chains without eth_simulateV1: start a fork and run the SAME quick tests against it (anvil simulates + traces)."""
    fork = sim.Fork(chain)
    try:
        if not await fork.ready():
            return {"fork": False, "note": "fork failed to start"}
        return await phase_q(fork.rpc, token, info, probes, fns, cfg=fork.src.cfg) or {"fork": False, "note": "fork could not simulate"}
    finally:
        fork.close()


async def phase_q(rpc, token, info, probes, fns=(), cfg=None):
    """Read-only quick tests (eth_simulateV1): honeypot buy/sell + effect of each privileged function.
    Returns a forkres-shaped dict so verdict/render treat it like fork results. None if the chain lacks simulateV1."""
    pair = info["pairs"][0] if info["pairs"] else None
    pool = None
    if pair:
        pool = pair["pairAddress"] if len(pair["pairAddress"]) == 42 else ((cfg or rpc.cfg).get("v4") or {}).get("pool_manager")
    out = {"fork": True, "quick": True, "runs": [], "tax": None}
    hp = await quicksim.honeypot(rpc, token, pool) if pool else {}
    if hp is None:
        return None
    if hp:
        out["tax"] = {"buy_ok": hp["buy_ok"], "buy_tax": hp["buy_tax"], "sell_ok": hp["sell_ok"], "sell_tax": hp["sell_tax"],
                      "pool": pool, "v4": len(pair["pairAddress"]) == 66, "why": hp.get("why"),
                      "buy_to": hp.get("buy_to") or [], "sell_to": hp.get("sell_to") or [], "amount": hp.get("amount"), "got": hp.get("got")}
    targets = [p for p in probes if p.get("callers") and "note" not in p][:8]
    getters = quicksim.getters_from(fns)

    async def one(p):
        who = p["callers"][0]
        e = None
        for data in argsmod.variants(p["sel"], p["fn"], p["args"], who):  # worst case first, then values a capped setter accepts
            e = await quicksim.fn_effect(rpc, token, pool, who, data, hp or None, getters)
            if not e or e.get("ran"):
                break
        if not e or not e.get("ran"):
            return None
        sup = info.get("supply") or 0
        return {"fn": p["fn"], "by": who, "ok": True, "supply_before": sup, "supply_after": sup + e.get("supply_delta", 0),
                "caller_bal_delta": e.get("caller_delta", 0), "owner_changed": False, "sell_after": e.get("sell_after"),
                "trace": e.get("trace"), "transfers": e.get("transfers") or [], "holder_delta": e.get("holder_delta", 0),
                "changed": e.get("changed") or {}}
    out["runs"] = [r for r in await asyncio.gather(*[one(p) for p in targets]) if r]
    return out


async def phase_s(chain, token, info, probes, lps_task=None, ex=None):
    """Stateful honeypot / delayed-honeypot / funds-taken tests. Fast path: eth_simulateV1, no fork (lib/fastsell);
    fork (lib/sellsim) when the node can't simulate. v4 pools: REAL swaps through the PoolManager + the hook analysis."""
    pair = info["pairs"][0] if info["pairs"] else None
    if not pair:
        return {"note": "no pool"}
    from lib.rpc import RPC as _R
    from lib import fastsell, v4hook
    rpc = _R(chain)
    ex = ex or explorer.Explorer(chain)
    pool = pair["pairAddress"] if len(pair["pairAddress"]) == 42 else (rpc.cfg.get("v4") or {}).get("pool_manager")
    lps = (await asyncio.shield(lps_task)) if lps_task else []
    v4 = None
    if lps and lps[0].get("type") == "v4" and lps[0].get("key") and len(pair["pairAddress"]) == 66:
        v4 = v4hook.Pool(lps[0], token, pool)
    has_hook = bool(v4 and lps[0]["key"][4] != ZERO)

    async def hook_part(fork=None):
        if not has_hook:
            return None
        try:
            return await asyncio.wait_for(phase_h(fork, rpc, ex, token, lps), 40)
        except Exception as e:
            return {"hook": lps[0]["key"][4], "note": f"hook sim failed: {str(e)[:80]}"}

    out, hook = await asyncio.gather(fastsell.run(rpc, token, pool, probes, info["decimals"] or 18, v4=v4), hook_part())
    if out is None:  # node can't simulate this: fork
        from cmds.sim import calldata as worst_case
        fork = sim.Fork(chain)
        try:
            if not await fork.ready():
                return {"note": "fork failed to start", "hook": hook}
            out = await sellsim.run(fork, rpc, token, pool, probes, worst_case, info["decimals"] or 18, budget=18, v4=v4)
        finally:
            fork.close()
    out["hook"] = hook
    out["flags"] = list(out.get("flags") or []) + list((hook or {}).get("flags") or [])
    return out


async def phase_mints(rpc, token, info):
    """Was supply raised after launch? Archive totalSupply at creation vs now; fallback: scan Transfer-from-0x0 logs."""
    cb = info.get("created_block")
    if cb:
        sc = await discover.supply_change(rpc, token, cb)
        if sc:
            then, now = sc
            return {"method": "archive", "then": then, "now": now, "raised": now - then if now > then else 0}
    try:
        latest = await rpc.block_number()
        if not cb and rpc.cfg.get("rps") and not rpc.private:
            return {"error": "creation block unknown and RPC is rate-limited — needs explorer key"}
        from lib import hypersync
        floor = 0 if hypersync.available(rpc.cfg.get("id")) else max(0, latest - (rpc.logs_range * 800 if rpc.private else min(rpc.logs_range * 20, 2_000_000)))
        start = max(cb or floor, floor)
        logs = await rpc.get_logs(token, [TRANSFER, pad_addr(ZERO)], from_block=start, to_block=latest, concurrency=8)
    except RPCError as e:
        return {"error": str(e)[:80]}
    mints = [{"block": int(l["blockNumber"], 16), "to": topic_addr(l["topics"][2]), "amount": int(l["data"], 16) if l["data"] != "0x" else 0, "tx": l["transactionHash"]} for l in logs]
    first = min((m["block"] for m in mints), default=None)
    later = [m for m in mints if first is not None and m["block"] > first + 5]
    return {"method": "logs", "n": len(mints), "later": later, "raised": sum(m["amount"] for m in later), "scanned": latest - start}


async def phase_e(rpc, ex, info):
    async def one(pair):
        try:
            return await asyncio.wait_for(lpmod.analyze_pair(rpc, ex, rpc.cfg, pair, info.get("creator"), pair.get("pairCreatedAt")), 45)
        except asyncio.TimeoutError:
            return {"type": "?", "status": "UNCLEAR", "reasons": ["LP analysis timed out for this pool"], "dex": pair.get("dexId")}
        except Exception as e:
            return {"type": "?", "status": "UNCLEAR", "reasons": [f"lp analysis error: {str(e)[:90]}"], "dex": pair.get("dexId")}
    pairs = info["pairs"][:2]
    liq = lambda p: float((p.get("liquidity") or {}).get("usd") or 0)
    if len(pairs) == 2 and liq(pairs[0]) and liq(pairs[1]) < 0.1 * liq(pairs[0]):
        # a side pool under 10% of the main one can't move the answer, but a busy one can burn the whole HyperSync
        # minute (15 requests) and leave the MAIN pool's scan incomplete -> list it, don't scan it
        info.setdefault("_side_pools", []).append(pairs.pop())
    return list(await asyncio.gather(*[one(p) for p in pairs]))


def rpc_recent_only(info):
    return info.get("_recent_only")


def hook_getter_tax(hk):
    """(name, pct, raw) of the highest live basis-point tax/fee getter on the v4 hook, e.g. taxBps=9999 → 99.99%."""
    rows = [(k, v / 100, v) for k, v in (hk.get("fee_reads") or {}).items()
            if "bps" in k.lower() and any(w in k.lower() for w in ("tax", "fee")) and not k.isupper() and v <= 10_000]
    return max(rows, key=lambda r: r[1], default=None)


def verdict(info, fns, probes, forkres, mints, lps):
    bad, warn, notes = [], [], []
    hk = (forkres.get("_who") or {}).get("hook") or {}
    hs = (forkres.get("sell") or {}).get("hook") or {}
    gt = hook_getter_tax(hk) if not (hs.get("buy") or hs.get("sell")) else None
    if gt and gt[1] >= 50:
        bad.append(f"v4 hook tax getter {gt[0]}={gt[2]} → {gt[1]:.2f}% taken (quick read; deep check measures it with real swaps)")
    dec = info["decimals"] or 18
    for r in forkres.get("runs", []):
        if r["ok"] and r["supply_after"] and r["supply_before"] and r["supply_after"] > r["supply_before"]:
            label = "MINT WORKS" if "mint" in r["fn"].lower() else "HIDDEN MINT"
            bad.append(f"{label}: {r['fn']} by {short(r['by'])} raised supply {fmt(r['supply_before'],dec,0)} → {fmt(r['supply_after'],dec,0)} (tested)")
        elif r["ok"] and r["caller_bal_delta"] > 0:
            bad.append(f"{r['fn']} moved {fmt(r['caller_bal_delta'],dec,0)} tokens to caller {short(r['by'])} (fork test)")
        elif r["ok"] and r["owner_changed"]:
            warn.append(f"{r['fn']} changes owner (callable by {short(r['by'])})")
        if r.get("ok") and r.get("sell_after") is False and (forkres.get("tax") or {}).get("sell_ok"):
            bad.append(f"SELL BLOCKER: after {r['fn']} by {short(r['by'])}, holders can't sell")
    if mints.get("raised"):
        extra = ""
        if mints.get("later"):
            extra = f" → {', '.join(short(m['to']) for m in mints['later'][:3])} (tx {mints['later'][0]['tx'][:12]}…)"
        elif mints.get("method") == "archive":
            extra = f" ({fmt(mints['then'],dec,0)} at launch → {fmt(mints['now'],dec,0)} now)"
        bad.append(f"SUPPLY RAISED AFTER LAUNCH: +{fmt(mints['raised'],dec,0)} tokens{extra}")
    st = info.get("steal")
    if st:
        who = "ANY wallet" if st["stranger"] else ", ".join(short(c) for c in st["callers"])
        bad.append(f"BACKDOOR: {who} can transferFrom any holder (incl. the pool {short(st['victim'])}) with NO approval → can drain the pool and wallets")
    privileged = [p for p in probes if p.get("callers")]
    open_to_all = {p["sel"] for p in probes if p.get("stranger")}  # any wallet can call it: not an owner power
    risky = [f for f in fns if f["cls"] == "risky" and f["sel"] not in open_to_all]
    if risky:
        pr = [p["fn"] for p in privileged if any(p["sel"] == f["sel"] for f in risky)]
        callers = sorted(set(short(c) for p in privileged for c in p["callers"]))
        warn.append(f"privileged functions: {', '.join(f['name'] for f in risky[:8])}" + (f" — callable by {', '.join(callers)}" if pr else " — no known address could call them (owner may be renounced or hidden)"))
    unk = [p for p in privileged if p["fn"].startswith("unnamed")]
    if unk:
        warn.append(f"{len(unk)} UNNAMED function(s) callable by a privileged address: {', '.join(p['sel'] for p in unk[:4])}")
    if info.get("impl") and not info.get("clone"):
        warn.append(f"UPGRADEABLE proxy (impl {short(info['impl'])}" + (f", admin {short(info['admin'])}" if info.get("admin") else "") + ") — code can change")
    if info.get("paused"):
        bad.append("transfers currently PAUSED")
    if info.get("trading") is False:
        warn.append("trading flag is off (sells may be blocked until enabled)")
    t = forkres.get("tax")
    if t:
        if not t["buy_ok"]:
            bad.append("transfer FROM pool reverted (buys blocked?)")
        elif not t["sell_ok"]:
            bad.append("transfer TO pool reverted — HONEYPOT pattern (can buy, can't sell)")
        if t.get("sell_tax") is not None and t["sell_tax"] > 25:
            bad.append(f"sell tax {t['sell_tax']:.0f}%")
        elif t.get("sell_tax") is not None and t["sell_tax"] > 5:
            warn.append(f"sell tax {t['sell_tax']:.1f}%")
        if t.get("buy_tax") is not None and t["buy_tax"] > 10:
            warn.append(f"buy tax {t['buy_tax']:.1f}%")
    for g in (forkres.get("auth") or {}).get("grantable", []):
        bad.append(f"POWER CAN BE GRANTED LATER: {short(g['by'])} holds {g['via']} and can give himself {g['role']} → {', '.join(g['enables'][:3])}")
    if ((forkres.get("auth") or {}).get("owner") or {}).get("pending"):
        warn.append(f"pending ownership handover to {short(forkres['auth']['owner']['pending'])}")
    for lvl, msg in (forkres.get("sell") or {}).get("flags", []):
        if "SELL BLOCKER" in msg and any("SELL BLOCKER" in b and msg.split(" by ")[0].split("after ")[-1] in b for b in bad):
            continue  # same finding already reported by the quick test
        (bad if lvl == "bad" else warn).append(msg)
    for i, lp in enumerate(lps):
        tag = f"{'main' if i == 0 else 'secondary'} pool, {lp.get('dex')} {lp.get('type')}"
        reasons = lp.get("reasons") or ["?"]
        key = next((r for r in reasons if "can remove" in r or "callable by" in r or "removed" in r), reasons[0])
        if i > 0:  # secondary pools: informative only (often community LPs)
            if lp.get("status") in ("PULLED", "UNLOCKED"):
                warn.append(f"LP {lp.get('status')} ({tag}): {key}")
            continue
        if lp.get("status") == "PULLED":
            bad.append(f"LP PULLED ({tag}): {key}")
        elif lp.get("status") == "UNLOCKED":
            bad.append(f"LP UNLOCKED ({tag}): {key}")
        elif lp.get("status") in ("PARTIAL", "UNCLEAR"):
            warn.append(f"LP {lp.get('status')} ({tag}): {key}")
        h = lp.get("hook")
        if h and any(p in h["permissions"] for p in ("beforeSwap", "beforeSwapReturnDelta", "afterSwapReturnDelta")):
            warn.append(f"v4 hook can alter swaps ({', '.join(h['permissions'][:3])}) — {h.get('name')}" + ("" if h.get("verified") else ", UNVERIFIED"))
        if h and any(p in h["permissions"] for p in ("beforeRemoveLiquidity", "beforeAddLiquidity")):
            warn.append("v4 hook controls liquidity add/remove")
    if any(e.startswith("code analysis") for e in info.get("_extra", [])):
        warn.append("code analysis still running in the background — re-check shortly for the code-read findings")
    for lvl, m in switches.findings(info.get("switches")):
        (bad if lvl == "bad" else warn).append(m + " (simulated)")
    for lvl, m in decomp.findings(info.get("decomp")) + static.findings(info.get("static")):
        fn = m.split(" ")[0].split("(")[0]
        if any(fn in x for x in bad):  # already proven by a simulation: don't repeat the code-read version
            continue
        (bad if lvl == "bad" else warn).append(m + " (read from code)")
    if not forkres.get("fork"):
        notes.append(("honeypot/owner-power tests not run on this chain — ask for `dd sim`") if not forkres.get("note") else f"tests failed ({forkres['note']})")
    elif (forkres.get("tax") or {}).get("v4") and not ((forkres.get("sell") or {}).get("hook") or {}).get("sell"):
        notes.append("v4 hook fee not measured (quick check only reads its fee getters) — ask for a deep check to measure it with real swaps")
    if mints.get("error"):
        notes.append(f"mint history scan failed ({mints['error']})")
    if not info.get("source", {}).get("verified"):
        notes.append("source not verified — functions read from bytecode" + ("" if info.get("decomp") else " (ask for a deep check to decompile it)"))
    if not info["pairs"]:
        notes.append("no DEX pool found (DexScreener, GoPlus, on-chain scan)" + ("" if info.get("created_block") else " — creation block unknown, on-chain pool scan skipped") + " — LP not checked")
    if rpc_recent_only(info):
        notes.append("free RPC on this chain can't search old logs reliably — pool/LP/mint history may be incomplete (needs paid RPC)")
    status = "NOT SAFE" if bad else ("CAUTION" if warn else "LOOKS SAFE")
    if status == "LOOKS SAFE":
        # never reassure when a core question went unanswered: LP, honeypot, functions
        t = forkres.get("tax") or {}
        sell = forkres.get("sell") or {}
        missing = []
        if not lps or lps[0].get("status") == "UNCLEAR":
            missing.append("LP status")
        if not (t.get("buy_ok") is not None or sell.get("sizes")):
            missing.append("honeypot test")
        if not fns:
            missing.append("function list")
        core = ("reads", "functions", "privilege probes", "LP", "quick tests", "fork tests")
        missing += [e.split(" timed out")[0].split(" failed")[0] for e in info.get("_extra", []) if e.startswith(core)]
        if missing:
            status = "INCOMPLETE"
            notes.insert(0, "verdict withheld — not checked: " + ", ".join(dict.fromkeys(missing)))
    return status, bad, warn, notes


def tracer_findings(r, info):
    """What the trace of a privileged call proved (observed, not inferred): moving holders' tokens, dangerous opcodes."""
    out = []
    token = (info.get("address") or "").lower()
    dec = info["decimals"] or 18
    tr = r.get("trace")
    moved = [t for t in (r.get("transfers") or []) if t["token"] == token and t["from"] not in (r["by"].lower(), ZERO, token) and t["amount"]]
    if moved:
        t = max(moved, key=lambda x: x["amount"])
        out.append(f"{r['fn']} by {short(r['by'])} MOVES OTHER HOLDERS' TOKENS ({fmt(t['amount'],dec,0)} {short(t['from'])}→{short(t['to'])}, traced)")
    if tr:
        for op in tracer.danger_ops(tr, [info.get("impl")]):
            if op in ("delegatecall", "selfdestruct"):
                out.append(f"{r['fn']} by {short(r['by'])} does a {op} to outside code (traced)")
        pools = {(p.get("pairAddress") or "").lower() for p in info.get("pairs") or []}
        for a, fn in tracer.external_calls(tr, token, [info.get("impl")])[:2]:
            if a not in pools and not str(fn).startswith(("balanceOf", "transfer", "0x")):
                out.append(f"{r['fn']} by {short(r['by'])} calls outside contract {short(a)}.{fn} (traced)")
    return out


def answers(info, fns, probes, forkres, mints, lps):
    """The three questions the user cares about most, answered first in plain words."""
    dec = info["decimals"] or 18
    L = ["== ANSWERS"]
    # 1. LP locked?
    if not lps:
        L.append("   LP locked?        ? — no pool found")
    else:
        lp = lps[0]
        rs = lp.get("reasons") or []
        st = lp.get("status")
        key = rs[0] if st == "LOCKED" and rs else next((r for r in rs if "can remove" in r or "callable by" in r or "removed" in r), rs[0] if rs else "")
        word = {"LOCKED": "YES", "UNLOCKED": "NO", "PULLED": "NO — ALREADY PULLED", "PARTIAL": "PARTLY", "UNCLEAR": "UNCLEAR"}.get(st, st)
        L.append(f"   LP locked?        {word} — {key}" + (" — BUT the pool's tokens can be taken without the LP (backdoor below)"
                                                            if info.get("steal") and st in ("LOCKED", "PARTIAL") else ""))
    # 2. malicious functions
    mal = []
    for r in forkres.get("runs", []):
        if r.get("supply_after") and r.get("supply_before") is not None and r["supply_after"] > r["supply_before"]:
            mal.append(f"{'mint' if 'mint' in r['fn'].lower() else 'HIDDEN mint'} {r['fn']} by {short(r['by'])} (+{fmt(r['supply_after']-r['supply_before'],dec,0)} tested)")
        elif r.get("holder_delta", 0) < 0:
            silent = not any(t["from"] == quicksim.BUYER.lower() for t in r.get("transfers") or [])
            mal.append(f"FUNDS TAKEN: {r['fn']} by {short(r['by'])} removes a holder's tokens" + (" silently (no Transfer event)" if silent else "") + " (simulated)")
        elif r.get("sell_after") is False and (forkres.get("tax") or {}).get("sell_ok"):
            mal.append(f"sell blocker {r['fn']} by {short(r['by'])} (holders can't sell after it)")
        elif r.get("caller_bal_delta", 0) > 0:
            mal.append(f"{r['fn']} by {short(r['by'])} sends tokens to caller")
        else:
            for m in tracer_findings(r, info):
                mal.append(m)
    p = info.get("steal")
    if p:
        who = "ANYONE" if p["stranger"] else ", ".join(short(c) for c in p["callers"])
        mal.append(f"BACKDOOR: {who} can move ANY holder's tokens without approval — incl. the pool's {short(p['victim'])}"
                   " → can drain the LP even though LP tokens are burned/locked (simulated transferFrom)")
    tested = {r["fn"] for r in forkres.get("runs", [])}
    for p in probes:
        nm = (p["fn"] or "").lower()
        if p["fn"] in tested and not any(p["fn"] in m for m in mal):
            continue
        if any(w in nm for w in ("blacklist", "blocklist", "bot", "pause", "freeze", "settax", "setfee", "seize", "confiscate")) and not any(p["fn"] in m for m in mal):
            who = ", ".join(short(c) for c in p.get("callers", [])) or ("ANYONE" if p.get("stranger") else "no known caller")
            mal.append(f"{p['fn']} ({who})")
    for lvl, m in decomp.findings(info.get("decomp")) + static.findings(info.get("static")):
        if lvl == "bad" and not any(m.split("(")[0] in x for x in mal):
            mal.append(m + " (read from code)")
    for lvl, m in switches.findings(info.get("switches")):
        if lvl == "bad":
            mal.append(m.replace("SELL BLOCKER (switch): ", "sell blocker: ") + " (simulated)")
    unk = [p for p in probes if p["fn"].startswith("unnamed") and p.get("callers")]
    if unk:
        mal.append(f"{len(unk)} unnamed owner-only fn(s)")
    if info.get("impl") and not info.get("clone"):
        mal.append("upgradeable proxy — code can be changed")
    auth = forkres.get("auth") or {}
    for g in auth.get("grantable", []):
        mal.append(f"{', '.join(g['enables'][:3])} can be ENABLED later by {short(g['by'])} via {g['via']}→{g['role']}")
    if (auth.get("owner") or {}).get("pending"):
        mal.append(f"pending owner {short(auth['owner']['pending'])} (not really renounced)")
    L.append(f"   Malicious fns?    {'YES — ' + '; '.join(mal[:5]) if mal else f'NONE found ({len(fns)} functions checked)'}")
    ctrl = auth.get("controllers") or {}
    own = (auth.get("owner") or {}).get("address")
    if own and own in ctrl:
        L.append(f"   Who controls?     {ctrl[own].get('label', '?')} ({short(own)})")
    # 3. ruggable / mintable / honeypot
    t = forkres.get("tax")
    sell = forkres.get("sell") or {}
    sflags = sell.get("flags", [])
    sbad = [m for l, m in sflags if l == "bad"]
    if sell.get("sizes"):
        n = sum(len(r.get("sells", [])) for r in sell["sizes"])
        if any(m.startswith(("HONEYPOT", "SELL CAP")) or "FUNDS TAKEN: buyer" in m for m in sbad):
            hp = "YES — " + next(m for m in sbad if m.startswith(("HONEYPOT", "SELL CAP")) or "FUNDS TAKEN: buyer" in m)
        elif any(m.startswith("DELAYED HONEYPOT") for m in sbad):
            hp = "DELAYED — " + next(m for m in sbad if m.startswith("DELAYED HONEYPOT"))
        elif any("SELL BLOCKER" in m or "FUNDS TAKEN by owner" in m for m in sbad):
            hp = "NOT NOW, but owner can trap holders — " + next(m for m in sbad if "SELL BLOCKER" in m or "FUNDS TAKEN by owner" in m)
        else:
            taxes = [x.get("sell_tax") for r in sell["sizes"] for x in r.get("sells", []) if x.get("sell_tax") is not None]
            btax = next((r.get("buy_tax") for r in sell["sizes"] if r.get("buy_tax") is not None), None)
            hp = f"NO — sold fine in {n} contexts, 2 sizes, 2 wallets, up to 24h later" + (f" (buy tax {btax:.1f}%, sell tax {max(taxes):.1f}%)" if taxes and btax is not None else "")
            if any(l == "warn" for l, _ in sflags):
                hp += " · note: " + "; ".join(m for l, m in sflags if l == "warn")[:140]
    elif t:
        if not t["buy_ok"]:
            hp = "buys revert"
        elif not t["sell_ok"]:
            hp = "YES — sells revert"
        else:
            hp = f"NO (buy tax {t['buy_tax']:.1f}%, sell tax {t['sell_tax']:.1f}%)" if t.get("sell_tax") is not None else "NO"
    else:
        hp = "not tested"
    mint_yes = bool(mints.get("raised")) or any("mint" in m.lower() for m in mal) or any("mint" in (p["fn"] or "").lower() and p.get("callers") for p in probes)
    if not mint_yes and any("mint" in e.lower() for g in auth.get("grantable", []) for e in g["enables"]):
        mint_yes = True
    mint_fns = [p for p in probes if "mint" in (p["fn"] or "").lower() and p.get("callers")]
    why = (f" — supply raised +{fmt(mints['raised'],dec,0)} after launch" if mints.get("raised") else
           (f" — {mint_fns[0]['fn']} callable by {short(mint_fns[0]['callers'][0])}" if mint_fns else
            next((f" — {m}" for m in mal if "mint" in m.lower()), "")))
    mint_txt = ("YES" + why) if mint_yes else "NO"
    rug = []
    if lps and lps[0].get("status") in ("UNLOCKED", "PULLED"):
        rug.append("LP pull")
    if any("sell blocker" in m for m in mal) or (t and t["buy_ok"] and not t["sell_ok"]) or any("SELL BLOCKER" in m or m.startswith(("HONEYPOT", "DELAYED", "SELL CAP")) for m in sbad):
        rug.append("sell block")
    if any("FUNDS TAKEN" in m for m in sbad) or any(m.startswith("FUNDS TAKEN") for m in mal):
        rug.append("funds taken")
    if mint_yes:
        rug.append("mint & dump")
    if info.get("steal"):
        rug.append("pool drain via approval-free transferFrom")
    hk = (forkres.get("_who") or {}).get("hook") or {}
    hs = sell.get("hook") or {}
    hb, hsl = (hs.get("buy") or {}).get("hook_pct"), (hs.get("sell") or {}).get("hook_pct")
    if (hb is not None or hsl is not None) and "v4" in (sell.get("method") or ""):
        hp = hp.replace("buy tax", "buy fee").replace("sell tax", "sell fee").replace("later (", "later, incl. hook fee (") if "incl." not in hp else hp
        hp += f" · hook takes {hb or 0:.2f}% buy / {hsl or 0:.2f}% sell (measured)"
    elif hb is not None or hsl is not None:
        hp += f" + hook fee {hb or 0:.2f}% buy / {hsl or 0:.2f}% sell (measured, real v4 swaps)"
    elif hk.get("fee_reads"):
        hp += " + hook fee getters: " + ", ".join(f"{k}={v}" for k, v in list(hk["fee_reads"].items())[:3])
    elif hk.get("swap_fee_pct"):
        hp += f" + pool hook fee {hk['swap_fee_pct']:g}% per swap"
    stx = max([x.get("sell_tax") or 0 for r in sell.get("sizes", []) for x in r.get("sells", [])] + [hsl or 0, (t or {}).get("sell_tax") or 0])
    gt = hook_getter_tax(hk) if hb is None and hsl is None else None
    if stx >= 90 and not hp.startswith("YES"):
        hp = f"YES — sells lose {stx:.2f}% (measured) · " + hp.replace("NO — sold fine", "sells go through")
        rug.append(f"{stx:.0f}% sell tax")
    elif gt and gt[1] >= 50 and not hp.startswith("YES"):
        hp = f"LIKELY — hook getter {gt[0]}={gt[2]} ({gt[1]:.2f}%) · {hp.replace("NO (", "plain transfer test (", 1)} · deep check measures it with real swaps"
        rug.append(f"hook tax {gt[1]:.0f}%")
    L.append(f"   Honeypot?         {hp}")
    L.append(f"   Mintable?         {mint_txt}")
    L.append(f"   Ruggable?         {'YES — ' + ', '.join(rug) if rug else 'no rug path found'}")
    dev = ((forkres or {}).get("_who") or {}).get("dev") or {}
    pc = dev.get("past_coins")
    if pc:
        dead = sum(1 for c in pc if c.get("dead"))
        L.append(f"   Serial dev?       YES — {len(pc)} earlier coin{'s' if len(pc) > 1 else ''}, {dead} dead: " +
                 ", ".join(f"{c['symbol']} {c['ca'][:6]}…{c['ca'][-4:]} ({'dead' if c.get('dead') else 'live'})" for c in pc[:4]) +
                 (" …" if len(pc) > 4 else ""))
    elif pc == [] and dev.get("history"):
        L.append("   Serial dev?       NO — " + (f"no earlier coin in its newest 1000 txs (of {dev.get('nonce')})"
                                               if dev.get("deploys_floor") else "first coin from this wallet"))
    elif dev.get("address") and not dev.get("history"):
        web = info.get("_explorer_web") or ""
        L.append(f"   Serial dev?       NOT CHECKED — no wallet history API on {info.get('_chain', 'this chain')} ({dev.get('nonce', '?')} txs from the dev)"
                 + (f": look at {web}/address/{dev['address']}" if web else ""))
    who = (forkres or {}).get("_who") or {}
    if who.get("farcaster"):
        from lib import farcaster
        role, addr, u = who["farcaster"][0]
        L.append(f"   Dev identity?     {role.upper() if role == 'dev' else role}: {farcaster.describe(u)} on Farcaster"
                 f" — {'owns the account' if u['how'] == 'custody' else 'signed proof the wallet is theirs'}")
    elif who.get("farcaster_checked"):
        L.append("   Dev identity?     no Farcaster account linked to the dev or its funders")
    return L


def render(chain, token, info, fns, probes, cands, forkres, mints, lps, ex, t0):
    L = []
    dec = info["decimals"] or 18
    L.append(f"== TOKEN {info.get('name')} ({info.get('symbol')})  {chain}  {token}")
    L.append(f"   {ex.link('a', token)}")
    L.extend(answers(info, fns, probes, forkres, mints, lps))
    L.append("== DETAILS")
    L.append(f"   supply {fmt(info['supply'] or 0, dec, 0)}  decimals {dec}  code {len(info['code'])//2} B  verified: {info['source'].get('name') or 'NO'}")
    L.append(f"   created block {info.get('created_block') or '?'}  owner {info.get('owner') or '-'}  creator {info.get('creator') or '-'}" +
             (f"  CLONE of {info['clone']}" if info.get("clone") else "") +
             (f"  PROXY→{info['impl']}" if info.get("impl") and not info.get("clone") else "") + (f"  admin {info['admin']}" if info.get("admin") else ""))
    if cands:
        L.append(f"   privileged candidates: {', '.join(short(c) for c in cands)}")
    by = {}
    for f in fns:
        by.setdefault(f["cls"], []).append(f)
    L.append(f"   functions {len(fns)}: standard {len(by.get('standard',[]))} · view {len(by.get('view',[]))} · other {len(by.get('other',[]))} · RISKY {len(by.get('risky',[]))} · UNKNOWN {len(by.get('unknown',[]))}")
    pm = {p["sel"]: p for p in probes}
    for f in by.get("risky", [])[:12] + by.get("other", [])[:6] + by.get("unknown", [])[:6]:
        p = pm.get(f["sel"], {})
        who = ("ANYONE" if p.get("stranger") else (", ".join(short(c) for c in p.get("callers", [])) or "no known caller")) if p else ""
        mark = "!" if f["cls"] == "risky" else ("?" if f["cls"] == "unknown" else "·")
        L.append(f"     {mark} {f['name'] or f['sel']+'('+f['args']+')'}  ← {who}")
    if info.get("static"):
        L.extend(static.render(info["static"]))
    if info.get("switches") is not None:
        L.extend(switches.render(info["switches"]))
    if info.get("decomp"):
        L.extend(decomp.render(info["decomp"]))
    labels = {token: "token", info.get("creator") or "": "creator", info.get("owner") or "": "owner", ZERO: "0x0"}
    labels.update({p["pairAddress"]: "pool" for p in info.get("pairs") or [] if len(p.get("pairAddress", "")) == 42})
    for r in forkres.get("runs", []):
        L.append(f"   fork {r['fn']} by {short(r['by'])}: {'OK' if r['ok'] else 'reverted'}  supply {fmt(r['supply_before'],dec,0)} → {fmt(r['supply_after'],dec,0)}" +
                 (f"  caller +{fmt(r['caller_bal_delta'],dec,0)}" if r['caller_bal_delta'] > 0 else "") +
                 (f"\n      changes: " + ", ".join(f"{k} {v[0]} → {v[1]}" for k, v in list(r["changed"].items())[:5]) if r.get("changed") else "") +
                 (f"\n      traced (run alone on current state): {tracer.describe(r['trace'], token, labels, dec, info.get('symbol') or 'tokens', [info.get('impl')])}" if r.get("trace") else ""))
    t = forkres.get("tax")
    if t:
        L.append(f"   fork transfer test via pool {short(t['pool'])}: buy {'OK' if t['buy_ok'] else 'FAIL'}" +
                 (f" tax {t['buy_tax']:.1f}%" if t.get("buy_tax") is not None else "") +
                 f" · sell {'OK' if t['sell_ok'] else 'FAIL'}" + (f" tax {t['sell_tax']:.1f}%" if t.get("sell_tax") is not None else ""))
        for what, lst, base in (("buy", t.get("buy_to"), t.get("amount")), ("sell", t.get("sell_to"), t.get("got"))):
            if lst and base:
                L.append(f"      {what} cut goes to: " + ", ".join(f"{labels.get(a, short(a))} {100*v/base:.1f}%" for a, v in lst[:3]))
    if mints.get("method") == "archive":
        L.append(f"   supply at launch {fmt(mints['then'],dec,0)} → now {fmt(mints['now'],dec,0)}" + ("  ✗ RAISED" if mints.get("raised") else "  (unchanged)" if mints['now'] == mints['then'] else "  (lower: burns)"))
    elif mints.get("n") is not None:
        L.append(f"   mints: {mints['n']} in last {mints['scanned']:,} blocks" + (f"; AFTER LAUNCH: {len(mints['later'])} (+{fmt(mints['raised'],dec,0)})" if mints.get("later") else ""))
    gp = info.get("goplus") or {}
    if gp:
        L.append(f"   goplus: honeypot {gp.get('is_honeypot')} mintable {gp.get('is_mintable')} blacklist {gp.get('is_blacklisted')} tax {gp.get('buy_tax')}/{gp.get('sell_tax')} holders {gp.get('holder_count')} (cross-check only)")
    if info["pairs"]:
        p = info.get("market_pair") or info["pairs"][0]
        if p.get("src") == "onchain":
            L.append(f"== MARKET (not on DexScreener; {len(info['pairs'])} pool(s) found by on-chain scan from block {info.get('created_block')})")
        elif p.get("src") == "goplus":
            L.append(f"== MARKET (dexscreener has no listing; pools from GoPlus) liq ${(p['liquidity']['usd'] or 0):,.0f}  pools {len(info['pairs'])}")
        else:
            age = (time.time() - (p.get("pairCreatedAt") or 0) / 1000) / 3600 if p.get("pairCreatedAt") else None
            L.append(f"== MARKET ${float(p.get('priceUsd') or 0):.8f}  mcap ${(p.get('marketCap') or p.get('fdv') or 0):,.0f}  liq ${((p.get('liquidity') or {}).get('usd') or 0):,.0f}  vol24 ${((p.get('volume') or {}).get('h24') or 0):,.0f}" +
                     (f"  age {age:.1f}h" if age is not None else "") + f"  pairs {len(info['pairs'])}")
            inf = p.get("info") or {}
            soc = [s.get("url") for s in inf.get("socials", [])] + [w.get("url") for w in inf.get("websites", [])]
            if soc:
                L.append(f"   links: {' '.join(soc[:4])}")
    for lp in lps:
        L.append("== " + lpmod.render(lp, ex))
    for p in info.get("_side_pools") or []:
        L.append(f"   side pool {p.get('dexId')} {short(p['pairAddress'])} liq ${((p.get('liquidity') or {}).get('usd') or 0):,.0f}"
                 " (<10% of the main pool) — not analysed")
    if (forkres.get("sell") or {}).get("sizes") or (forkres.get("sell") or {}).get("note"):
        L.extend(sellsim.render(forkres["sell"], dec))
    if (forkres.get("sell") or {}).get("hook"):
        from lib import v4hook
        hk_addr = forkres["sell"]["hook"].get("hook") or ""
        L.extend(v4hook.render(forkres["sell"]["hook"], dec, {**labels, hk_addr: "hook", "_token": token.lower()}))
    if forkres.get("auth"):
        L.extend(authority.render(forkres["auth"]))
    status, bad, warn, notes = verdict(info, fns, probes, forkres, mints, lps)
    L.append(f"== VERDICT: {status}")
    for b in bad:
        L.append(f"   ✗ {b}")
    for w in warn:
        L.append(f"   ! {w}")
    if notes:
        L.append(f"== NOT CHECKED: {'; '.join(notes)}")
    L.append(f"   ({time.time()-t0:.1f}s)")
    return "\n".join(L)


DEBUG = os.environ.get("DD_DEBUG")


DEADLINE = [0.0]  # absolute time the whole check must be printed by (DD_BUDGET seconds, 0 = unlimited)
BACKGROUND = ("slither", "decompile", "hook code")  # analyses that keep running detached and cache themselves (lib/bg.py)


async def timed(name, coro, limit, default, notes):
    """Run one phase with its own limit, clamped to the check's deadline. A timeout yields the phase's default and a
    '<name> timed out' note — verdict() turns notes on core phases into INCOMPLETE, so partial output can't reassure."""
    t = time.time()
    if DEADLINE[0]:
        limit = max(0.5, min(limit, DEADLINE[0] - time.time()))
    try:
        r = await asyncio.wait_for(coro, limit)
    except asyncio.TimeoutError:
        if name in BACKGROUND:
            notes.append(f"code analysis ({name}) finishing in background — re-check shortly for the code section")
        else:
            notes.append(f"{name} timed out after {limit:.0f}s")
        r = default
    except Exception as e:
        notes.append(f"{name} failed: {str(e)[:70]}")
        r = default
    if DEBUG:
        print(f"[{name} {time.time()-t:.1f}s]", file=sys.stderr)
    return r


QUOTE_SYMBOLS = ("USDC", "USDT", "DAI", "USDS", "SUSDS", "USDE", "USD0", "FDUSD", "USDB", "USDT0", "USDH", "USDHL", "PYUSD", "FRAX", "WBTC", "CBBTC")


async def pool_to_token(rpc, addr):
    """A pasted POOL address (DexScreener pair links) -> the traded token: the side that isn't the wrapped native or
    a stable/major quote. None when it isn't a v2/v3-style pool."""
    try:
        r = await rpc.batch([("eth_call", [{"to": addr, "data": encode_call(f)}, "latest"]) for f in ("token0()", "token1()")])
        if not all(x and len(x) == 66 for x in r):
            return None
        t0, t1 = (to_addr(x).lower() for x in r)
        if t0 == t1 or addr.lower() in (t0, t1):
            return None
        syms = await rpc.batch([("eth_call", [{"to": t, "data": encode_call("symbol()")}, "latest"]) for t in (t0, t1)])
        def is_quote(t, sraw):
            try:
                sym = decode1("string", sraw).upper()
            except Exception:
                sym = ""
            return t == (rpc.cfg.get("wnative") or "").lower() or sym in QUOTE_SYMBOLS or sym in ("WETH", "WHYPE", "WBNB", "WAVAX", "WS", "WMON")
        q0, q1 = is_quote(t0, syms[0]), is_quote(t1, syms[1])
        return t1 if q0 and not q1 else t0 if q1 and not q0 else t0
    except Exception:
        return None


async def main(chain, token, fast=False, as_json=False):
    t0 = time.time()
    budget = float(os.environ.get("DD_BUDGET", "15"))
    DEADLINE[0] = t0 + budget - 1.5 if budget > 0 else 0.0  # margin for interpreter start-up and printing
    from lib import hypersync
    hypersync.DEADLINE[0] = DEADLINE[0]
    rpc = RPC(chain)
    ex = explorer.Explorer(rpc.name)
    extra = []
    # the pool test runs alongside the reads (no extra latency); only a pasted POOL address restarts them on its token
    reads = asyncio.create_task(timed("reads", phase_a(rpc, ex, rpc.name, token), 45, None, extra))
    tok = await pool_to_token(rpc, token)
    if tok:
        reads.cancel()
        print(f"   ({token} is a pool → checking its token {tok})", file=sys.stderr if as_json else sys.stdout)
        token = tok
        reads = asyncio.create_task(timed("reads", phase_a(rpc, ex, rpc.name, token), 45, None, extra))
    info = await reads
    if info is not None:
        info["_chain"], info["_explorer_web"] = rpc.name, ((rpc.cfg.get("explorer") or {}).get("web") or "").rstrip("/")
        info["_recent_only"] = (rpc.cfg.get("logs_recent_only") or rpc.cfg.get("logs_unreliable")) and not getattr(rpc, "private", None)
    if info is None:
        print(f"could not read {token} on {rpc.name}: {'; '.join(extra)}"); return
    if len(info["code"]) <= 2:
        print(f"{token} has no code on {rpc.name} — not a contract"); return
    # everything that needs only the reads starts now; functions/probes run alongside
    mints_task = asyncio.create_task(timed("mint history", phase_mints(rpc, token, info), 40, {}, extra))
    people_task = asyncio.create_task(timed("who", whomod.gather_people(rpc, ex, token, info), 20, {}, extra))
    lps_task = asyncio.create_task(timed("LP", phase_e(rpc, ex, info), 75, [], extra))
    code_for = info.get("impl_code") or info["code"]
    unverified = not (info.get("source") or {}).get("verified")
    code_addr = info.get("impl") or token
    st_cached = static.cached(ex, code_addr) if not unverified else None
    read_bytecode = unverified or bool((st_cached or {}).get("error"))  # unverified, or verified but it won't compile
    dc_task = st_task = None
    if not fast:
        if read_bytecode:
            dc_task = asyncio.create_task(timed("decompile", decomp.analyze(code_for), 35, None, extra))
        if not unverified and not (st_cached or {}).get("error"):
            st_task = asyncio.create_task(timed("slither", static.analyze(ex, code_addr), 95, None, extra))

    async def hook_after():
        return await whomod.hook_info(rpc, ex, await asyncio.shield(lps_task))
    hook_task = asyncio.create_task(timed("hook info", hook_after(), 10, {}, extra))

    async def hook_code_after():  # the hook's Slither/Heimdall view, no longer nested inside the sell tests
        lps_ = await asyncio.shield(lps_task)
        lp = next((l for l in lps_[:1] if l.get("type") == "v4" and l.get("key") and l["key"][4] != ZERO), None)
        return await _hook_decomp(rpc, ex, lp["key"][4]) if lp else None
    hc_task = asyncio.create_task(timed("hook code", hook_code_after(), 60, None, extra)) if not fast else None
    fns = await timed("functions", phase_b(info), 20, [], extra)
    probes, cands = await timed("privilege probes", phase_c(rpc, token, info, fns), 30, ([], []), extra)
    no_sim = bool(rpc.cfg.get("no_sim"))  # chain has no eth_simulateV1: go straight to the fork, don't try first
    q_task = None if no_sim else asyncio.create_task(timed("quick tests", phase_q(rpc, token, info, probes, fns), 20, None, extra))
    d_task = asyncio.create_task(timed("fork tests", phase_d(rpc.name, token, info, probes, fns), 75, {"fork": False}, extra)) if no_sim else None
    s_task = asyncio.create_task(timed("sell tests", phase_s(rpc.name, token, info, probes, lps_task, ex), 90, {"note": "timed out"}, extra)) if not fast else None
    a_task = asyncio.create_task(timed("control", authority.analyze(rpc, ex, token, info, fns, probes), 25, {}, extra))
    lps = await lps_task
    forkres = (await q_task) if q_task else None
    sellres = await s_task if s_task else {"note": "deep sell tests not run — ask for a deep check (honeypot over time, sell caps, funds taken)"}
    if forkres is None:  # chain lacks eth_simulateV1 (flagged, or just found out) -> the same tests on a fork
        forkres = await d_task if d_task else await timed("fork tests", phase_d(rpc.name, token, info, probes, fns), 75, {"fork": False}, extra)
    mints = await mints_task
    info["decomp"] = (await dc_task) if dc_task else (decomp.cached(code_for) if read_bytecode else None)
    info["static"] = (await st_task) if st_task else st_cached
    # owner switches Slither found: flip each one in storage and see if a holder can still sell (needs eth_simulateV1)
    if (info["static"] or {}).get("ok") and forkres and (forkres.get("tax") or {}).get("buy_ok") and forkres.get("quick"):
        hp = {"buy_ok": True, "amount": forkres["tax"]["amount"], "got": forkres["tax"]["got"]}
        info["switches"] = await timed("switch tests", switches.run(rpc, token, forkres["tax"]["pool"], hp, info["static"], probes), 15, None, extra)
    if hc_task and isinstance(sellres.get("hook"), dict):
        sellres["hook"]["decomp"] = await hc_task
    forkres = forkres or {"fork": False}
    forkres["sell"] = sellres
    forkres["auth"] = await a_task
    whoinfo = await people_task or {}
    whoinfo["hook"] = await hook_task or {}
    forkres["_who"] = whoinfo
    if as_json:
        print(json.dumps({"info": {k: v for k, v in info.items() if k not in ("code", "impl_code", "source", "goplus", "pairs", "decomp", "static", "switches")},
                          "fns": fns, "probes": probes, "fork": forkres, "mints": mints,
                          "lps": [{k: v for k, v in l.items() if k != "positions"} for l in lps], "who": whoinfo, "not_checked": extra}, default=str, indent=1))
    else:
        info["_extra"] = extra
        out = render(rpc.name, token, info, fns, probes, cands, forkres, mints, lps, ex, t0)
        wl = whomod.render(whoinfo, ex, rpc.cfg.get("symbol", "ETH"))
        if wl:
            out = out.replace("== VERDICT:", "\n".join(wl) + "\n== VERDICT:", 1)
        if extra:
            out = out.replace("   (" + out.rsplit("   (", 1)[1], "== ALSO NOT CHECKED: " + "; ".join(extra) + "\n   (" + out.rsplit("   (", 1)[1])
        print(out)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit("usage: dd check <chain> <token> [--deep] [--json]")
    run(main(a[0], a[1], fast="--deep" not in sys.argv, as_json="--json" in sys.argv))
