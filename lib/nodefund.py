"""First native funding of many wallets from our own archive node — no explorer, no HyperSync (its free plan is
15 requests/min and runs out on every BSC holder scan).

Per wallet, ONE binary search over blocks (all wallets searched together, one batch per step, ~27 steps on BSC):
  1. the first block where it signed a tx (nonce > 0) OR held more than dust. Before a wallet's first own tx nothing can
     take its balance away, so that is monotone. Dust (< 1e12 wei, e.g. 1e-18 BNB 'activation' markers apps send) is ignored.
  2. the funding tx inside that block: a plain transfer to the wallet; its own 0-gas first tx = sponsored app account
     (nobody funded it); else (contract payout: bridge, router, CEX contract) the block's call trace.
A wallet that never held more than dust and never signed (an app / relayer account) -> {'none': True}.
"""
import asyncio
from lib.rpc import RPC

DUST = 10 ** 12


def _int(x):
    try:
        return int(x, 16)
    except Exception:
        return None


async def _search(rpc, items, lo_of, hi_of, test):
    """items: list of keys; first block b in [lo, hi] where test(key, value_at_b) is True (assumed monotone)."""
    lo = {k: lo_of(k) for k in items}
    hi = {k: hi_of(k) for k in items}
    while True:
        act = [k for k in items if lo[k] < hi[k]]
        if not act:
            return hi
        mids = {k: (lo[k] + hi[k]) // 2 for k in act}
        res = await rpc.batch([c for k in act for c in test["calls"](k, mids[k])])
        n = len(test["calls"](act[0], 0))
        for i, k in enumerate(act):
            if test["ok"](res[i * n:(i + 1) * n]):
                hi[k] = mids[k]
            else:
                lo[k] = mids[k] + 1


def _find_call(node, addr):
    if not isinstance(node, dict):
        return None
    if (node.get("to") or "").lower() == addr and _int(node.get("value") or "0x0"):
        return node
    for c in node.get("calls") or []:
        r = _find_call(c, addr)
        if r:
            return r
    return None


async def first_funding_many(chain, addrs, nonces, balances, latest, max_trace=150):
    """-> {addr: {'from','tx','time','block'} | {'none': True[, 'sponsored': True]} | {'error': ...}}.
    nonces/balances = values at `latest`."""
    rpc = RPC(chain)
    out = {}
    todo = []
    for a in addrs:
        if (nonces.get(a) or 0) > 0 or (balances.get(a) or 0) > DUST:
            todo.append(a)
        else:
            out[a] = {"none": True}
    if not todo:
        return out
    # one search: the first block where the wallet either signed a tx or held more than dust. Both only ever go up
    # until then (an account's balance only drops through its own txs), so the test is monotone.
    fb = await _search(rpc, todo, lambda a: 0, lambda a: latest,
                       {"calls": lambda a, b: [("eth_getTransactionCount", [a, hex(b)]), ("eth_getBalance", [a, hex(b)])],
                        "ok": lambda r: (_int(r[0]) or 0) > 0 or (_int(r[1]) or 0) > DUST})
    blocks = sorted(set(fb.values()))
    sem_b = asyncio.Semaphore(6)

    async def getb(chunk):
        async with sem_b:
            return await rpc.batch([("eth_getBlockByNumber", [hex(b), True]) for b in chunk], chunk=5)
    parts = await asyncio.gather(*[getb(blocks[i:i + 20]) for i in range(0, len(blocks), 20)])
    bl = [x for p in parts for x in p]
    byb = {b: x for b, x in zip(blocks, bl) if x}
    need_trace, cand = [], []
    for a in todo:
        b = fb[a]
        blk = byb.get(b)
        if not blk:
            out[a] = {"error": "block not read"}
            continue
        ts = _int(blk.get("timestamp"))
        txs = blk.get("transactions") or []
        hit = [t for t in txs if (t.get("to") or "").lower() == a and (_int(t.get("value") or "0x0") or 0) > DUST]
        own = [t for t in txs if (t.get("from") or "").lower() == a]
        if hit:
            t = hit[0]
            out[a] = {"from": t["from"].lower(), "tx": t["hash"], "time": str(ts), "block": b}
        elif own and not any((_int(t.get("gasPrice") or "0x0") or 0) > 0 for t in own[:1]):
            # its first tx paid 0 gas (a sponsored / gasless app account): nobody funded it
            out[a] = {"none": True, "sponsored": True}
        else:
            cand.append((a, b, ts, blk))
    # still nothing at or below dust in that block = its nonce went up through an EIP-7702 authorisation inside an
    # app relayer's tx (a smart-wallet account the app runs, gas sponsored): nobody funded it
    if cand:
        bb = await rpc.batch([("eth_getBalance", [a, hex(b)]) for a, b, _, _ in cand])
        for x, v in zip(cand, bb):
            if v is not None and (_int(v) or 0) <= DUST:
                out[x[0]] = {"none": True, "sponsored": True, "app7702": True}
            else:
                need_trace.append(x)
    # payouts by a contract (bridge, router, exchange contract): find the call in the block's trace
    sem = asyncio.Semaphore(6)

    async def tr(a, b, ts, blk):
        async with sem:
            try:
                r = await asyncio.wait_for(rpc.call("debug_traceBlockByNumber", [hex(b), {"tracer": "callTracer"}]), 40)
            except Exception as e:
                return a, {"error": f"trace failed ({type(e).__name__})", "block": b}
        for i, x in enumerate(r or []):
            c = _find_call((x or {}).get("result") or x, a)
            if c:
                txs = blk.get("transactions") or []
                h = (x or {}).get("txHash") or (txs[i]["hash"] if i < len(txs) else None)
                return a, {"from": (c.get("from") or "").lower(), "tx": h, "time": str(ts), "block": b, "internal": True}
        if any((t.get("from") or "").lower() == a for t in blk.get("transactions") or []):
            # signed its first tx in that block with no BNB coming in first: gas paid by someone else's bundle
            return a, {"none": True, "sponsored": True}
        return a, {"error": "funding call not found in block trace", "block": b}
    for a, r in await asyncio.gather(*[tr(*x) for x in need_trace[:max_trace]]):
        out[a] = r
    for a, b, _, _ in need_trace[max_trace:]:
        out[a] = {"error": "not traced (limit)", "block": b}
    return out
