"""First native funding of many wallets from our own archive node — no explorer, no HyperSync (its free plan is
15 requests/min and runs out on every BSC holder scan).

Per wallet, a binary search over blocks (all wallets searched together, one batch per step, ~27 steps on BSC):
  1. if it ever signed a tx: the first block where its nonce > 0 (nonce only goes up)
  2. the first block (before that) where its native balance rose above dust: before a wallet's first own tx nothing can
     take its balance away, so that is monotone. Dust (< 1e12 wei, e.g. 1e-18 BNB 'activation' markers apps send) is ignored.
  3. the funding tx inside that block: a plain transfer to the wallet, else (contract payout: bridge, router, CEX
     contract) the block's call trace.
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


async def first_funding_many(chain, addrs, nonces, balances, latest):
    """-> {addr: {'from','tx','time','block'} | {'none': True} | {'error': ...}}. nonces/balances = values at `latest`."""
    rpc = RPC(chain)
    out = {}
    signed = [a for a in addrs if (nonces.get(a) or 0) > 0]
    first_tx = {}
    if signed:
        first_tx = await _search(rpc, signed, lambda a: 0, lambda a: latest,
                                 {"calls": lambda a, b: [("eth_getTransactionCount", [a, hex(b)])],
                                  "ok": lambda r: (_int(r[0]) or 0) > 0})
    todo = []
    for a in addrs:
        if a in first_tx:
            todo.append(a)
        elif (balances.get(a) or 0) > DUST:
            todo.append(a)
        else:
            out[a] = {"none": True}
    if not todo:
        return out
    hi = {a: first_tx.get(a, latest) for a in todo}
    fund_b = await _search(rpc, todo, lambda a: 0, lambda a: hi[a],
                           {"calls": lambda a, b: [("eth_getBalance", [a, hex(b)])],
                            "ok": lambda r: (_int(r[0]) or 0) > DUST})
    blocks = sorted({b for b in fund_b.values()})
    bl = await rpc.batch([("eth_getBlockByNumber", [hex(b), True]) for b in blocks])
    byb = {b: x for b, x in zip(blocks, bl) if x}
    need_trace = []
    for a in todo:
        b = fund_b[a]
        blk = byb.get(b)
        if not blk:
            out[a] = {"error": "block not read"}
            continue
        ts = _int(blk.get("timestamp"))
        hit = [t for t in blk.get("transactions") or [] if (t.get("to") or "").lower() == a and (_int(t.get("value") or "0x0") or 0) > DUST]
        if hit:
            t = hit[0]
            out[a] = {"from": t["from"].lower(), "tx": t["hash"], "time": str(ts), "block": b}
        else:
            need_trace.append((a, b, ts, blk))
    # payouts by a contract (bridge, router, exchange contract): find the call in the block's trace
    sem = asyncio.Semaphore(4)

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
        return a, {"error": "funding call not found in block trace", "block": b}
    for a, r in await asyncio.gather(*[tr(*x) for x in need_trace[:30]]):
        out[a] = r
    for a, b, _, _ in need_trace[30:]:
        out[a] = {"error": "not traced (limit)", "block": b}
    return out
