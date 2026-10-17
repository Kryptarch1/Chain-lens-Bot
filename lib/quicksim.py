"""Read-only quick tests with eth_simulateV1 (no fork, ~50-150 ms each):
- honeypot: buy (pool -> buyer) then sell (buyer -> pool) -> can sell? real buy/sell tax?
- owner-function effects: run a privileged function as its caller, then read supply / caller balance, then try a sell
Falls back to None when the chain's RPC doesn't support eth_simulateV1 (caller can use a fork instead).
"""
import asyncio
from lib.rpc import RPCError
from lib.evm import encode_call, addr as to_addr
from lib import tracer

BUYER = "0x00000000000000000000000000000000000Bb1ee"


async def simulate(rpc, calls):
    """calls: list of {from,to,data}. -> list of {ok, ret} or None if unsupported."""
    # blockOverrides lifts the block gas limit (HyperEVM small blocks are 3M gas) so multi-step sequences fit
    body = [{"blockStateCalls": [{"blockOverrides": {"gasLimit": hex(30_000_000)},
                                  "calls": [{"from": c["from"], "to": c["to"], "data": c["data"], "gas": hex(c.get("gas", 2_000_000))} for c in calls]}],
             "validation": False}, "latest"]
    try:
        r = await rpc.call("eth_simulateV1", body)
    except RPCError as e:
        m = str(e).lower()
        if "does not exist" in m or "not available" in m or "not supported" in m or "method not found" in m:
            return None
        raise
    out = []
    for c in r[0]["calls"]:
        out.append({"ok": c.get("status") == "0x1", "ret": c.get("returnData") or "0x", "err": (c.get("error") or {}).get("message"),
                    "transfers": _transfers(c.get("logs") or [])})
    return out


async def simulate_blocks(rpc, blocks, base="latest"):
    """Several simulated blocks in one request. blocks: [{calls:[{from,to,data,gas?}], stateOverrides?, time_offset?, block_offset?}]
    -> [[{ok, ret, err, transfers}] per block] or None if the RPC can't simulate. base: block to build on (pinned number hex
    when blocks jump in time/number — 'latest' can move between our read and the request)."""
    bsc = []
    for b in blocks:
        bo = {"gasLimit": hex(30_000_000)}
        if b.get("number"):
            bo["number"] = hex(b["number"])
        if b.get("time"):
            bo["time"] = hex(b["time"])
        e = {"blockOverrides": bo, "calls": [{"from": c["from"], "to": c["to"], "data": c["data"], "gas": hex(c.get("gas", 2_000_000)),
                                               **({"value": hex(c["value"])} if c.get("value") else {})} for c in b["calls"]]}
        if b.get("stateOverrides"):
            e["stateOverrides"] = b["stateOverrides"]
        bsc.append(e)
    try:
        r = await rpc.call("eth_simulateV1", [{"blockStateCalls": bsc, "validation": False, "traceTransfers": True}, base])
    except RPCError as e:
        m = str(e).lower()
        if "does not exist" in m or "not available" in m or "not supported" in m or "method not found" in m:
            return None
        raise
    return [[{"ok": c.get("status") == "0x1", "ret": c.get("returnData") or "0x", "err": _err(c), "logs": c.get("logs") or [],
              "transfers": _transfers(c.get("logs") or [])} for c in blk["calls"]] for blk in r]


def _err(c):
    """Readable revert reason (Error(string), v4 hook WrappedError, or the node's message)."""
    e = c.get("error") or {}
    if not e and c.get("status") == "0x1":
        return None
    data = e.get("data") or c.get("returnData") or ""
    if isinstance(data, str) and data.startswith("0x08c379a0") and len(data) > 138:
        try:
            raw = bytes.fromhex(data[10:]); ln = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + ln].decode(errors="ignore") or "reverted"
        except Exception:
            pass
    if isinstance(data, str) and "90bfb865" in data[:12]:
        from lib import v4hook
        w = v4hook._unwrap(data)
        if w:
            return w
    return (e.get("message") or "reverted").replace("execution reverted", "").strip(" :,") or "reverted"


def _transfers(logs):
    """ERC20 Transfer events emitted by one simulated call -> where tokens actually moved."""
    out = []
    for lg in logs:
        tp = lg.get("topics") or []
        if len(tp) == 3 and tp[0].lower() == tracer.TRANSFER:
            out.append({"token": (lg.get("address") or "").lower(), "from": to_addr(tp[1]), "to": to_addr(tp[2]), "amount": _u(lg.get("data"))})
    return out


def _others(transfers, token, sender, receiver):
    """Everyone except sender/receiver who ended up holding `token` after this call (fee wallets etc.)."""
    got = {}
    for t in transfers:  # only tokens taken out of this trade: Transfer(sender -> X); a contract dumping old fees isn't the cut
        if t["token"] == token.lower() and t["from"] == sender.lower() and t["to"] not in (sender.lower(), receiver.lower()):
            got[t["to"]] = got.get(t["to"], 0) + t["amount"]
    return sorted(got.items(), key=lambda x: -x[1])


def _u(x):
    try:
        return int(x, 16) if x and x != "0x" else 0
    except ValueError:
        return 0


async def honeypot(rpc, token, pool):
    """-> dict(buy_ok, sell_ok, buy_tax, sell_tax) or None if unsupported / not testable."""
    bal = await rpc.try_call(token, encode_call("balanceOf(address)", [pool]))
    pbal = _u(bal[1]) if bal[0] else 0
    if pbal < 1000:
        return {}  # nothing in the pool to buy: not testable (None means "chain can't simulate")
    amt = pbal // 1000
    buy = {"from": pool, "to": token, "data": encode_call("transfer(address,uint256)", [BUYER, amt])}
    r1 = await simulate(rpc, [buy, {"from": BUYER, "to": token, "data": encode_call("balanceOf(address)", [BUYER])}])
    if r1 is None:
        return None
    if not r1[0]["ok"]:
        return {"buy_ok": False, "sell_ok": False, "buy_tax": None, "sell_tax": None, "amount": amt, "pool": pool, "why": r1[0]["err"]}
    got = _u(r1[1]["ret"])
    r2 = await simulate(rpc, [buy, {"from": BUYER, "to": token, "data": encode_call("transfer(address,uint256)", [pool, got])},
                              {"from": BUYER, "to": token, "data": encode_call("balanceOf(address)", [pool])}])
    sell_ok = r2[1]["ok"]
    sell_tax = None
    if sell_ok and got:
        received = _u(r2[2]["ret"]) - (pbal - amt)
        sell_tax = max(0.0, (1 - received / got) * 100)
    return {"buy_ok": True, "sell_ok": sell_ok, "buy_tax": max(0.0, (1 - got / amt) * 100) if amt else None,
            "sell_tax": sell_tax, "amount": amt, "got": got, "pool": pool, "why": None if sell_ok else r2[1]["err"],
            "buy_to": _others(r1[0]["transfers"], token, pool, BUYER),            # who took a cut of the buy
            "sell_to": _others(r2[1]["transfers"], token, BUYER, pool) if sell_ok else []}  # who took a cut of the sell


async def fn_effect(rpc, token, pool, caller, data, hp, getters=()):
    """Run a privileged call inside one simulation and read what changed:
    supply, caller balance, a holder's balance (silent drains), every no-arg getter before/after
    (fees, limits, flags, owner, pair — works without call tracing), and whether the holder can still sell.
    getters: [(name, selector)]. -> dict or None if unsupported."""
    calls, at = [], {}

    def add(label, frm, data_, gas=200_000):  # reads are cheap; the block holds 30M gas in total
        at[label] = len(calls)
        calls.append({"from": frm, "to": token, "data": data_, "gas": gas})

    has_buy = bool(hp and hp.get("buy_ok"))
    add("sup0", caller, encode_call("totalSupply()"))
    add("bal0", caller, encode_call("balanceOf(address)", [caller]))
    for nm, sel in getters:
        add("g0:" + nm, caller, sel)
    if has_buy:
        add("buy", pool, encode_call("transfer(address,uint256)", [BUYER, hp["amount"]]), 2_000_000)
    add("call", caller, data, 3_000_000)
    add("sup1", caller, encode_call("totalSupply()"))
    add("bal1", caller, encode_call("balanceOf(address)", [caller]))
    for nm, sel in getters:
        add("g1:" + nm, caller, sel)
    if has_buy and hp.get("got"):
        add("hold1", BUYER, encode_call("balanceOf(address)", [BUYER]))
        add("sell", BUYER, encode_call("transfer(address,uint256)", [pool, hp["got"]]), 2_000_000)
    r, tr = await asyncio.gather(simulate(rpc, calls), tracer.trace_call(rpc, caller, token, data))
    if r is None:
        return None
    c = r[at["call"]]
    out = {"ran": c["ok"], "err": c["err"], "transfers": c["transfers"], "trace": await tracer.annotate(tr) if tr and not tr.get("error") else None}
    if c["ok"]:
        out["supply_delta"] = _u(r[at["sup1"]]["ret"]) - _u(r[at["sup0"]]["ret"])
        out["caller_delta"] = _u(r[at["bal1"]]["ret"]) - _u(r[at["bal0"]]["ret"])
        changed = {}
        for nm, _ in getters:
            a0, a1 = r[at["g0:" + nm]], r[at["g1:" + nm]]
            if a0["ok"] and a1["ok"] and a0["ret"] != a1["ret"]:
                changed[nm] = (_word(a0["ret"]), _word(a1["ret"]))
        out["changed"] = changed
        if "sell" in at:
            out["holder_delta"] = _u(r[at["hold1"]]["ret"]) - hp["got"]  # <0 = the call took a holder's tokens (even without a Transfer event)
            out["sell_after"] = r[at["sell"]]["ok"]
    return out


def _word(ret):
    """Readable getter value: small numbers as ints, addresses shortened, anything else as hex."""
    if not ret or ret == "0x":
        return "0x"
    if len(ret) == 66:
        v = int(ret, 16)
        if v == int(BUYER, 16):
            return "test-wallet"
        if v < 10 ** 30:
            return v
        if ret[2:26] == "0" * 24:
            return "0x" + ret[-40:][:4] + "…" + ret[-4:]
    return ret[:12] + "…"


def getters_from(fns, limit=24):
    """No-argument view functions worth watching (skips ERC-20 metadata already read elsewhere)."""
    skip = {"0x06fdde03", "0x95d89b41", "0x313ce567", "0x18160ddd"}
    out = [((f["name"] or f["sel"]).split("(")[0], f["sel"]) for f in fns
           if f.get("args") == "" and f.get("mut") in ("view", "pure") and f["sel"] not in skip]
    return out[:limit]
