"""Deep sell tests WITHOUT a fork: each scenario is one eth_simulateV1 request (several simulated blocks), all sent in
parallel. Same scenarios and output as lib/sellsim.py (the fork version), so classify() / render() are shared:
fresh wallet buys, then sells at same block / +1 / +10 blocks / +1 h / +24 h, 2 sizes, a 2nd wallet, and each owner power
switched on before selling. Nothing is deployed or broadcast — helper contracts are placed by state override.

  v2 pair   real router-style sell (fixtures/src/SellHelper.sol: transferFrom seller -> pair, then pair.swap)
  v4 pool   real PoolManager swaps (fixtures/src/V4Helper.sol), so the hook runs; quote token funded by balance-slot override
  other     token transfer to the pool (token-level rules only)
Block-number jumps are capped by the node (geth-style nodes: ~250). If a node can't jump, time still moves and the
output says so. Returns None when the chain can't simulate -> the caller falls back to the fork version.
"""
import asyncio, json, os, time
from eth_utils import keccak
from lib import quicksim, sellsim, v4hook, cache, args as argsmod
from lib.evm import encode_call, decode1, short
from lib.rpc import ZERO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELLH = "0x0000000000000000000000000000000000005e11"
V4H = "0x000000000000000000000000000000000004a4e1"
BUYER, BUYER2 = sellsim.BUYER, sellsim.BUYER2
KEY_T = v4hook.KEY_T
FUNDS = hex(10 ** 22)
FUNDS32 = "0x" + (10 ** 22).to_bytes(32, "big").hex()
QUOTE_FUNDS32 = "0x" + (2 ** 200).to_bytes(32, "big").hex()  # storage overrides need full 32-byte values on strict nodes


def _runtime(f, c):
    return json.load(open(os.path.join(ROOT, "fixtures", "out", f, c + ".json")))["deployedBytecode"]["object"]


def _u(ret):
    try:
        return int(ret, 16) if ret and ret != "0x" else 0
    except ValueError:
        return 0


async def balance_key(rpc, token, chain, holder):
    """The exact storage key holding `holder`'s balance of an ERC-20, so a test wallet can be funded by override.
    Tries the usual mapping slots (Solidity + Vyper layouts), then asks the node which keys balanceOf(holder) reads
    (access list / prestate trace) — that also covers proxies with hashed (ERC-7201) storage. -> key hex or None"""
    ck = f"balkey:{chain}:{token.lower()}:{holder.lower()}"
    c = cache.get(ck)
    if c:
        return c if c != "none" else None
    probe = 0x1234567890abcdef
    call = {"to": token, "data": encode_call("balanceOf(address)", [holder])}
    keys = []
    try:  # keys the call actually touches
        al = await rpc.call("eth_createAccessList", [dict(call, **{"from": holder}), "latest"])
        keys = [k for e in al.get("accessList", []) if e["address"].lower() == token.lower() for k in e.get("storageKeys", [])]
    except Exception:
        pass
    if not keys:
        try:
            tr = await rpc.call("debug_traceCall", [call, "latest", {"tracer": "prestateTracer"}])
            keys = list(((tr or {}).get(token.lower()) or {}).get("storage", {}).keys())
        except Exception:
            pass
    for sl in list(range(0, 21)) + [51, 101, 151]:
        a, b = bytes.fromhex(holder[2:].rjust(64, "0")), sl.to_bytes(32, "big")
        keys += ["0x" + keccak(a + b).hex(), "0x" + keccak(b + a).hex()]
    keys = list(dict.fromkeys(keys))
    pv = "0x" + probe.to_bytes(32, "big").hex()
    res = await rpc.batch([("eth_call", [call, "latest", {token: {"stateDiff": {k: pv}}}]) for k in keys])
    for k, r in zip(keys, res):
        if r and _u(r) == probe:
            cache.set(ck, k); return k
    cache.set(ck, "none", ttl=86400)
    return None


class Plan:
    """Everything one deep check's scenarios share: pinned base block, pool kind, helper code, funding overrides."""

    async def init(self, rpc, token, pool, v4=None, block=None):
        self.rpc, self.token, self.pool, self.v4 = rpc, token.lower(), pool, v4
        b = await rpc.block(block if block is not None else "latest")
        self.n, self.t = int(b["number"], 16), int(b["timestamp"], 16)
        self.ov = {BUYER: {"balance": FUNDS}, BUYER2: {"balance": FUNDS}}
        if v4:
            self.kind = "v4"
            self.ov[V4H] = {"code": v4hook.runtime_code(v4.pm)}
            st = await v4hook.pool_state(rpc, v4.pm, v4.id, hex(self.n))
            r0, r1 = v4hook.virtual_reserves(st)
            vt, vq = (r0, r1) if v4.t0 else (r1, r0)
            if not vt or not vq:
                return "v4 pool has no active liquidity"
            self.q_per_t = vq / vt
            self.pbal = vt
            if v4.quote != ZERO:
                ks = await asyncio.gather(*[balance_key(rpc, v4.quote, rpc.name, w) for w in (BUYER, BUYER2)])
                if not all(ks):
                    return "could not find the quote token's balance storage to fund the test wallet"
                # quote tokens can be worth fractions of a cent (PEPE): fund far more than any test buy needs
                self.ov[v4.quote] = {"stateDiff": {k: QUOTE_FUNDS32 for k in ks}}
        else:
            self.v2 = await sellsim._v2_info(rpc, token, pool)
            self.kind = "v2" if self.v2 else "token"
            if self.v2:
                self.ov[SELLH] = {"code": _runtime("SellHelper.sol", "SellHelper")}
            self.pbal = _u(await rpc.eth_call(token, encode_call("balanceOf(address)", [pool]), block=hex(self.n)))
        # how far can this node jump block numbers?
        self.gap = 1
        for g in (250, 10):
            try:
                r = await quicksim.simulate_blocks(rpc, [{"number": self.n + 1, "time": self.t + 2, "calls": []},
                                                         {"number": self.n + 1 + g, "time": self.t + 86400, "calls": []}], hex(self.n))
            except Exception:
                r = None
            if r is not None:
                self.gap = g; break
        return None

    # ---- call builders ----
    def buy(self, w, tokens):
        if self.kind == "v4":
            q = max(1, int(tokens * self.q_per_t))
            zf1 = self.v4.quote == self.v4.key[0].lower()
            sw = {"from": w, "to": V4H, "data": encode_call(f"swap({KEY_T},bool,uint256)", [self.v4.key, zf1, q]), "gas": 5_000_000}
            if self.v4.quote == ZERO:
                return [dict(sw, value=q)]
            return [{"from": w, "to": self.v4.quote, "data": encode_call("approve(address,uint256)", [V4H, q])}, sw]
        calls = [{"from": self.pool, "to": self.token, "data": encode_call("transfer(address,uint256)", [w, tokens])}]
        if self.kind == "v2":
            calls.append({"from": w, "to": self.pool, "data": encode_call("sync()")})
        return calls

    def sell(self, w, amount):
        if self.kind == "v4":
            zf1 = self.token == self.v4.key[0].lower()
            return [{"from": w, "to": self.token, "data": encode_call("approve(address,uint256)", [V4H, amount])},
                    {"from": w, "to": V4H, "data": encode_call(f"swap({KEY_T},bool,uint256)", [self.v4.key, zf1, amount]), "gas": 5_000_000}]
        if self.kind == "v2":
            return [{"from": w, "to": self.token, "data": encode_call("approve(address,uint256)", [SELLH, amount])},
                    {"from": w, "to": SELLH, "data": encode_call("sellV2(address,address,uint256)", [self.token, self.pool, amount]), "gas": 3_000_000}]
        return [{"from": w, "to": self.token, "data": encode_call("transfer(address,uint256)", [self.pool, amount])}]

    def bal(self, w):
        return {"from": w, "to": self.token, "data": encode_call("balanceOf(address)", [w]), "gas": 200_000}

    def block(self, k, calls, first=False):
        """k = blocks after the base block (1 = the first simulated block)."""
        return {"number": self.n + k, "time": self.t + 2 + (k - 1) * 12, "calls": calls, **({"stateOverrides": self.ov} if first else {})}

    def later(self, blocks, secs):
        """(block offset, time) for a sell `blocks`/`secs` after the buy, within what the node allows."""
        k = 1 + min(blocks, self.gap) if blocks else 1
        return k, self.t + 2 + max(secs, (k - 1) * 12)

    async def run(self, blocks):
        return await quicksim.simulate_blocks(self.rpc, blocks, hex(self.n))

    def sell_result(self, w, amount, res):
        """Read one sell call's outcome from its simulated result."""
        c = res[-1]
        e = {"ok": c["ok"]}
        if not c["ok"]:
            e["why"] = c.get("err") or "reverted"
            return e
        if self.kind == "v4":
            m = v4hook.measure(c["logs"], self.v4, V4H, self.token == self.v4.key[0].lower(), None, self.v4.quote)
            e["sell_tax"] = (m.get("hook_pct") or 0)
            e["hook_pct"] = m.get("hook_pct")
            e["quote_out"] = abs((m.get("caller") or (0, 0))[0 if self.v4.quote == self.v4.key[0].lower() else 1])
        else:
            tr = [t for x in res for t in x["transfers"] if t["token"] == self.token and t["from"] == w.lower()]
            got = sum(t["amount"] for t in tr if t["to"] == self.pool.lower())
            e["sell_tax"] = max(0.0, (1 - got / amount) * 100) if amount else None
            e["quote_out"] = _u(c["ret"]) if self.kind == "v2" else 0
            cut = {}
            for t in tr:
                if t["to"] != self.pool.lower():
                    cut[t["to"]] = cut.get(t["to"], 0) + t["amount"]
            e["cut"] = [(a, 100 * v / amount) for a, v in sorted(cut.items(), key=lambda x: -x[1])[:3]] if amount else []
        return e


async def run(rpc, token, pool, probes, dec=18, v4=None):
    """-> sellsim-shaped dict, or None if this chain/pool can't be tested this way (caller uses the fork)."""
    t0 = time.time()
    p = Plan()
    try:
        why = await p.init(rpc, token, pool, v4)
    except Exception as e:
        return None
    if why:
        return None
    out = {"pool": pool, "sizes": [], "powers": [], "flags": [], "note": None,
           "method": {"v4": "real v4 swaps through the PoolManager (hook included)", "v2": "real pool swap (v2, router-style)",
                      "token": "token transfer to pool"}[p.kind] + ", eth_simulateV1 (no fork)"}
    if p.gap < 250:
        out["note"] = f"this node lets simulated blocks jump at most {p.gap} block(s): later sells are tested by time, block counts are capped"
    if p.pbal < 10_000:
        out["note"] = "pool holds no tokens to buy"
        return out
    sizes = [("small", p.pbal // 2000), ("large", p.pbal // 50)]

    # phase 1: buys (what each wallet ends up holding)
    async def buy_only(w, amt):
        r = await p.run([p.block(1, p.buy(w, amt) + [p.bal(w)], first=True)])
        if r is None:
            return None
        calls = r[0]
        ok = all(c["ok"] for c in calls[:-1])
        out_ = {"ok": ok, "got": _u(calls[-1]["ret"]) if ok else 0, "why": next((c.get("err") for c in calls[:-1] if not c["ok"]), None)}
        if ok and p.kind == "v4":  # the buy's own cost: hook cut + any token tax on the way out of the PoolManager
            m = v4hook.measure(calls[-2]["logs"], p.v4, V4H, p.v4.quote == p.v4.key[0].lower(), out_["got"], p.token)
            out_["tax"] = (m.get("hook_pct") or 0) + (m.get("token_tax_pct") or 0)
        return out_

    buys = await asyncio.gather(buy_only(BUYER, sizes[0][1]), buy_only(BUYER, sizes[1][1]), buy_only(BUYER2, sizes[0][1]))
    if any(b is None for b in buys):
        return None

    # phase 2: sells at each time, in parallel
    async def sell_at(w, amt, got, blocks, secs, amount=None):
        amount = amount or got
        bcalls = p.buy(w, amt)
        if not blocks and not secs:  # same block
            r = await p.run([p.block(1, bcalls + [p.bal(w)] + p.sell(w, amount), first=True)])
            if r is None:
                return None
            res = r[0][len(bcalls) + 1:]
            held = _u(r[0][len(bcalls)]["ret"])
        else:
            k, ts = p.later(blocks, secs)
            blk2 = p.block(k, [p.bal(w)] + p.sell(w, amount)); blk2["time"] = ts
            r = await p.run([p.block(1, bcalls, first=True), blk2])
            if r is None:
                return None
            res = r[-1][1:]  # nodes may return the skipped-over filler blocks too: ours is the last
            held = _u(r[-1][0]["ret"])
        e = p.sell_result(w, amount, res)
        e["held_before"] = held
        return e

    async def schedule(label, blocks, secs, amt, got):
        e = await sell_at(BUYER, amt, got, blocks, secs)
        if e is None:
            return None
        if e["held_before"] < got:
            e["balance_shrank"] = got - e["held_before"]
            if not e["ok"] and e["held_before"]:  # sold what was left instead
                e2 = await sell_at(BUYER, amt, got, blocks, secs, e["held_before"])
                if e2:
                    e2["held_before"], e2["balance_shrank"] = e["held_before"], e["balance_shrank"]; e = e2
        if not e["ok"]:  # diagnose max-sell rules: does half / 1% go through?
            held = e["held_before"] or got
            h, tny = await asyncio.gather(sell_at(BUYER, amt, got, blocks, secs, held // 2), sell_at(BUYER, amt, got, blocks, secs, max(1, held // 100)))
            e["half_ok"] = bool(h and h["ok"])
            if e["half_ok"] and h.get("sell_tax") is not None:
                e["half_tax"] = h["sell_tax"]
            if not e["half_ok"]:
                e["tiny_ok"] = bool(tny and tny["ok"])
                if tny and not tny["ok"]:
                    e["tiny_why"] = tny.get("why")
        e["when"] = label
        return e

    jobs, rows = [], []
    for (name, amt), b in zip(sizes, buys[:2]):
        row = {"size": name, "buy_amount": amt, "sells": []}
        rows.append(row)
        if not b["ok"]:
            row["buy"] = f"FAILED ({'v4 buy reverted: ' if p.kind == 'v4' else 'transfer from pool reverted: '}{b.get('why')})"; continue
        if b["got"] == 0:
            row["buy"] = "FUNDS TAKEN: buyer received 0 tokens"; continue
        row.update(buy="ok", got=b["got"], approve="ok")
        row["buy_tax"] = b.get("tax", 0.0) if p.kind == "v4" else ((1 - b["got"] / amt) * 100 if amt else 0.0)
        sched = sellsim.SCHEDULES if name == "small" else [sellsim.SCHEDULES[0], sellsim.SCHEDULES[-2]]
        for label, blocks, secs in sched:
            jobs.append((row, schedule(label, blocks, secs, amt, b["got"])))
    w2 = buys[2]
    w2_job = sell_at(BUYER2, sizes[0][1], w2["got"], 1, 0) if w2 and w2["ok"] and w2["got"] else None

    # owner powers: buy, power switched on, next block the holder sells
    async def power(pr):
        who = pr["callers"][0]
        amt = sizes[0][1]
        for data in argsmod.variants(pr["sel"], pr["fn"], pr["args"], who):
            bc = p.buy(BUYER, amt)
            blk1 = p.block(1, bc + [p.bal(BUYER), {"from": who, "to": p.token, "data": data, "gas": 3_000_000}], first=True)
            r = await p.run([blk1, p.block(2, [p.bal(BUYER)] + p.sell(BUYER, buys[0]["got"]))])
            if r is None:
                return None
            if not r[0][-1]["ok"]:
                continue  # this argument set was rejected: try the next
            held0, held1 = _u(r[0][len(bc)]["ret"]), _u(r[-1][0]["ret"])
            e = p.sell_result(BUYER, buys[0]["got"], r[-1][1:])
            if held1 < held0 and not e["ok"] and held1:
                r2 = await p.run([blk1, p.block(2, p.sell(BUYER, held1))])
                if r2:
                    e = p.sell_result(BUYER, held1, r2[-1])
            return {"fn": pr["fn"], "by": who, "ran": True, "buyer_delta": held1 - held0, "sell_ok": e["ok"],
                    "sell_tax": e.get("sell_tax") if e["ok"] else None, "why": e.get("why")}
        return {"fn": pr["fn"], "by": who, "ran": False}

    runnable = [x for x in probes if x.get("callers") and "note" not in x][:8] if buys[0]["ok"] and buys[0]["got"] else []
    results = await asyncio.gather(*[j for _, j in jobs], *([w2_job] if w2_job else []), *[power(x) for x in runnable])
    if any(r is None for r in results):
        return None
    for (row, _), e in zip(jobs, results[:len(jobs)]):
        row["sells"].append(e)
    if w2_job:
        out["wallet2"] = {"ok": results[len(jobs)]["ok"]}
    out["powers"] = list(results[len(jobs) + (1 if w2_job else 0):])
    out["sizes"] = rows
    out["elapsed"] = time.time() - t0
    out["flags"] = sellsim.classify(out)
    return out
