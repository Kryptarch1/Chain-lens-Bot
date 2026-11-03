"""On-chain discovery that doesn't depend on DexScreener:
- creation block of a contract (explorer -> binary search on eth_getCode)
- every pool for a token on ANY dex fork (v2 PairCreated / v3 PoolCreated / v4 Initialize, any factory)
- supply at creation vs now (archive eth_call) -> "supply raised after launch"
"""
import asyncio
from lib.rpc import RPCError, pad_addr, topic_addr, ZERO
from lib.evm import topic, encode_call, decode1, addr as to_addr

PAIR_CREATED = topic("PairCreated(address,address,address,uint256)")
POOL_CREATED = topic("PoolCreated(address,address,uint24,int24,address)")
ALGEBRA_POOL = topic("Pool(address,address,address)")
V4_INIT = topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
TRANSFER_T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_T = "0x" + "0" * 64


async def creation_block(rpc, ex, addr, hint_ts=None):
    """-> (block, tx) or (None, None)."""
    try:
        c = await ex.creator(addr)
        if c.get("tx"):
            r = await rpc.receipt(c["tx"])
            if r:
                return int(r["blockNumber"], 16), c["tx"]
    except Exception:
        pass
    # first mint (Transfer from 0x0) via HyperSync -> launch tx; works on chains where the explorer has no creator API
    try:
        from lib import hypersync
        if hypersync.available(rpc.cfg.get("id")):
            latest = await rpc.block_number()
            logs = []
            if hint_ts:  # pool creation time known: the token was minted shortly before it -> small window first
                from lib.lp import estimate_block
                a, _ = await estimate_block(rpc, hint_ts - 86400, latest)
                b, _ = await estimate_block(rpc, hint_ts + 3600, latest)
                logs = await rpc.get_logs(addr, [TRANSFER_T, ZERO_T], a, min(latest, b + 5000))
            if not logs:
                logs, _ = await hypersync.get_logs(rpc.cfg["id"], addr, [TRANSFER_T, ZERO_T], 0, latest)
            if logs:
                first = logs[0]
                return int(first["blockNumber"], 16), first["transactionHash"]
    except Exception:
        pass
    # binary search on code existence (needs archive state)
    try:
        hi = await rpc.block_number()
        lo = 0
        code = await rpc.call("eth_getCode", [addr, hex(max(1, hi - 1))])
        if len(code) <= 2:
            return None, None
        # verify the node really serves historical state: code must NOT exist at block 1
        early = await rpc.call("eth_getCode", [addr, hex(1)])
        if len(early) > 2:
            return None, None  # node ignores the block param (e.g. HyperEVM public RPC)
        for _ in range(40):
            if hi - lo <= 1:
                break
            mid = (lo + hi) // 2
            c = await rpc.call("eth_getCode", [addr, hex(mid)])
            if len(c) > 2:
                hi = mid
            else:
                lo = mid
        return hi, None
    except RPCError:
        return None, None


def v4_manager_ok(cfg, addr):
    """Is `addr` a PoolManager we accept? The configured one, or any emitter on chains with many v4 copies
    (`"any_manager": true`, e.g. Elysium: every DEX/launchpad runs its own PoolManager)."""
    v4 = cfg.get("v4") or {}
    return bool(v4) and (v4.get("any_manager") or not v4.get("pool_manager") or addr.lower() == v4["pool_manager"].lower())


async def v4_manager_of(rpc, pool_id):
    """The PoolManager that initialized `pool_id` (one log query, no address filter) -> address or None."""
    try:
        logs = await rpc.get_logs(None, [V4_INIT, pool_id], 0, await rpc.block_number())
    except RPCError:
        return None
    logs = [l for l in logs if v4_manager_ok(rpc.cfg, l["address"])]
    return logs[0]["address"].lower() if logs else None


async def find_pools(rpc, token, from_block, span=None):
    """All pools whose creation event names `token` as token0/token1, on any factory.
    -> list of dicts shaped like DexScreener pairs (pairAddress, dexId, labels, created_block, factory, quote)."""
    span = span or rpc.logs_range * 20
    latest = await rpc.block_number()
    to_block = min(latest, from_block + span)
    t = pad_addr(token)
    queries = [
        ("v2", [PAIR_CREATED, t]), ("v2", [PAIR_CREATED, None, t]),
        ("v3", [POOL_CREATED, t]), ("v3", [POOL_CREATED, None, t]),
        ("v3", [ALGEBRA_POOL, t]), ("v3", [ALGEBRA_POOL, None, t]),
        ("v4", [V4_INIT, None, t]), ("v4", [V4_INIT, None, None, t]),
    ]
    v4 = rpc.cfg.get("v4") or {}
    pm_addr = None if v4.get("any_manager") else v4.get("pool_manager")
    res = await asyncio.gather(*[rpc.get_logs(pm_addr if (kind == "v4" and pm_addr) else None, q, from_block, to_block) for kind, q in queries], return_exceptions=True)
    pools, seen = [], set()
    for (kind, q), logs in zip(queries, res):
        if isinstance(logs, Exception):
            continue
        for l in logs:
            tps = l["topics"]
            if kind == "v4":
                if not v4_manager_ok(rpc.cfg, l["address"]):
                    continue
                pid = tps[1]
                c0, c1 = topic_addr(tps[2]), topic_addr(tps[3])
                hook = to_addr(l["data"][2:][128:192])
                p = {"pairAddress": pid, "labels": ["v4"], "dexId": "uniswap-v4", "hook": hook, "pm": l["address"].lower()}
            else:
                c0, c1 = topic_addr(tps[1]), topic_addr(tps[2])
                d = l["data"][2:]
                if kind == "v2":
                    pa = to_addr(d[0:64])
                elif q[0] == ALGEBRA_POOL:
                    pa = to_addr(d[0:64])
                else:
                    pa = to_addr(d[64:128])
                p = {"pairAddress": pa, "labels": ["v3"] if kind == "v3" else [], "dexId": f"{kind}@{l['address'][:10]}"}
            if p["pairAddress"] in seen:
                continue
            seen.add(p["pairAddress"])
            quote = c1 if c0.lower() == token.lower() else c0
            p.update({"created_block": int(l["blockNumber"], 16), "factory": l["address"], "quoteToken": {"address": quote, "symbol": None},
                      "liquidity": {"usd": None}, "src": "onchain"})
            pools.append(p)
    return pools


async def enrich_pools(rpc, pools, token):
    """Quote symbol + quote-side balance (real money in the pool) for v2/v3; v4 balances live in the PoolManager."""
    calls = []
    for p in pools:
        q = p["quoteToken"]["address"]
        calls.append(("eth_call", [{"to": q, "data": encode_call("symbol()")}, "latest"]) if q != ZERO else None)
        calls.append(("eth_call", [{"to": q, "data": encode_call("decimals()")}, "latest"]) if q != ZERO else None)
        calls.append(("eth_call", [{"to": q, "data": encode_call("balanceOf(address)", [p["pairAddress"]])}, "latest"])
                     if (q != ZERO and len(p["pairAddress"]) == 42) else None)
    real = [c for c in calls if c]
    out = await rpc.batch(real) if real else []
    it = iter(out)
    for p in pools:
        q = p["quoteToken"]["address"]
        sym = next(it) if q != ZERO else None
        dec = next(it) if q != ZERO else None
        bal = next(it) if (q != ZERO and len(p["pairAddress"]) == 42) else None
        try:
            p["quoteToken"]["symbol"] = decode1("string", sym) if sym else rpc.cfg.get("symbol")
        except Exception:
            p["quoteToken"]["symbol"] = "?"
        try:
            d = decode1("uint8", dec) if dec else 18
            p["quote_balance"] = decode1("uint256", bal) / 10 ** d if bal else None
        except Exception:
            p["quote_balance"] = None
    pools.sort(key=lambda p: -(p.get("quote_balance") or 0))
    return pools


async def supply_change(rpc, token, block):
    """totalSupply shortly after creation vs now (needs archive). -> (then, now) or None."""
    try:
        pre = await rpc.call("eth_getCode", [token, hex(max(1, block - 1))])
        if len(pre) > 2:
            return None  # node ignores historical block numbers
        then = await rpc.call("eth_call", [{"to": token, "data": "0x18160ddd"}, hex(block + 2)])
        now = await rpc.call("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])
        return int(then, 16), int(now, 16)
    except (RPCError, ValueError):
        return None


async def pools_from_receipts(rpc, txs, token):
    """Pools created in specific txs (e.g. the token's creation tx) — no log search needed."""
    t = token.lower()
    pools, seen = [], set()
    for h in [x for x in txs if x]:
        r = await rpc.receipt(h)
        if not r:
            continue
        blk = int(r["blockNumber"], 16)
        for l in r["logs"]:
            tps = l["topics"]
            if not tps:
                continue
            sig = tps[0]
            p = None
            if sig == V4_INIT and len(tps) >= 4 and v4_manager_ok(rpc.cfg, l["address"]):
                c0, c1 = topic_addr(tps[2]), topic_addr(tps[3])
                if t in (c0.lower(), c1.lower()):
                    p = {"pairAddress": tps[1], "labels": ["v4"], "dexId": "uniswap-v4", "hook": to_addr(l["data"][2:][128:192]), "pm": l["address"].lower()}
            elif sig in (PAIR_CREATED, POOL_CREATED, ALGEBRA_POOL) and len(tps) >= 3:
                c0, c1 = topic_addr(tps[1]), topic_addr(tps[2])
                if t in (c0.lower(), c1.lower()):
                    d = l["data"][2:]
                    pa = to_addr(d[64:128]) if sig == POOL_CREATED else to_addr(d[0:64])
                    p = {"pairAddress": pa, "labels": [] if sig == PAIR_CREATED else ["v3"], "dexId": ("v2" if sig == PAIR_CREATED else "v3") + "@" + l["address"][:10]}
            if p and p["pairAddress"] not in seen:
                seen.add(p["pairAddress"])
                quote = c1 if c0.lower() == t else c0
                p.update({"created_block": blk, "factory": l["address"], "quoteToken": {"address": quote, "symbol": None},
                          "liquidity": {"usd": None}, "src": "onchain", "seed_txs": [h]})
                pools.append(p)
    return pools


async def pools_from_transfers(rpc, ex, token):
    """Use the token's first transfers (explorer) to find its pools: recipients that are pools, or txs that touched the v4 PoolManager."""
    txs = await ex.token_transfers(token, limit=100, sort="asc")
    if not txs:
        return []
    pm = (rpc.cfg.get("v4") or {}).get("pool_manager", "").lower()
    recips, hashes = [], []
    for t in txs:
        to = (t.get("to") or "").lower()
        if to and to not in recips:
            recips.append(to)
        if to == pm and t.get("hash") not in hashes:
            hashes.append(t["hash"])
    recips = recips[:25]
    calls = []
    for a in recips:
        calls += [("eth_call", [{"to": a, "data": "0x0dfe1681"}, "latest"]), ("eth_call", [{"to": a, "data": "0xd21220a7"}, "latest"])]
    res = await rpc.batch(calls) if calls else []
    pools = []
    for i, a in enumerate(recips):
        t0, t1 = res[2 * i], res[2 * i + 1]
        if t0 and t1 and len(t0) >= 66 and len(t1) >= 66:
            c0, c1 = to_addr(t0), to_addr(t1)
            if token.lower() in (c0, c1):
                ok, f = await rpc.try_call(a, "0xddca3f43")  # fee() -> v3-style
                is_v3 = ok and f and f != "0x"
                first = next((x for x in txs if (x.get("to") or "").lower() == a), {})
                pools.append({"pairAddress": a, "labels": ["v3"] if is_v3 else [], "dexId": "v3" if is_v3 else "v2",
                              "quoteToken": {"address": c1 if c0 == token.lower() else c0, "symbol": None}, "liquidity": {"usd": None},
                              "created_block": int(first.get("blockNumber") or 0) or None, "src": "onchain", "seed_txs": [first.get("hash")] if first.get("hash") else []})
    if hashes:
        pools += await pools_from_receipts(rpc, hashes[:6], token)
    return pools
