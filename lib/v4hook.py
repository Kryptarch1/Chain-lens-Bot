"""Uniswap v4 hooks, measured on a private fork with REAL swaps through the PoolManager.

A tiny router (fixtures/src/V4Helper.sol) is deployed on the fork. A fresh wallet buys and sells through it, so the
hook runs exactly as for a real trade. The hook's cut is exact, no price math:
    PoolManager `Swap` event = what the pool itself traded   vs   helper `Deltas` = what the trader paid / got
    hook cut = trader paid − pool got (input side)  +  pool gave − trader got (output side)
Also: who received the cut (trace), what each callback does (trace), whether outsiders can add/remove liquidity,
and what each hook owner function does to the next sell (fee raised? sells blocked?).
"""
import asyncio, json, os
from lib.evm import encode_call, decode_out, topic, addr as to_addr, short, fmt
from lib.rpc import ZERO
from lib import tracer, args as argsmod
from eth_utils import keccak

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "out", "V4Helper.sol", "V4Helper.json")
SWAP_T = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
DELTAS_T = topic("Deltas(int128,int128)")
ERC6909_T = topic("Transfer(address,address,address,uint256,uint256)")
KEY_T = "(address,address,uint24,int24,address)"
DEPLOYER = "0x00000000000000000000000000000000000dE1e4"
TRADER = "0x00000000000000000000000000000000000Bb4ee"


def _s128(h):
    v = int(h, 16)
    return v - (1 << 256) if v >> 255 else v


async def helper(fork, pm):
    """Deploy the router once per fork."""
    h = getattr(fork, "_v4helper", None)
    if h and len(await fork.rpc.code(h)) > 2:  # a snapshot revert can remove it: redeploy then
        return h
    code = json.load(open(ART))["bytecode"]["object"]
    from eth_abi import encode
    h = await fork.deploy(DEPLOYER, "0x" + code.replace("0x", "") + encode(["address"], [pm]).hex())
    fork._v4helper = h
    return h


async def pool_state(rpc, pm, pool_id, block="latest"):
    """-> (sqrtPriceX96, tick, lpFee pips, active liquidity) from PoolManager storage."""
    base = int.from_bytes(keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")
    r = await rpc.batch([("eth_call", [{"to": pm, "data": encode_call("extsload(bytes32)", [(base + o).to_bytes(32, "big")])}, block]) for o in (0, 3)])
    s0, liq = int(r[0] or "0x0", 16), int(r[1] or "0x0", 16) & ((1 << 128) - 1)
    sqrtp = s0 & ((1 << 160) - 1)
    tick = (s0 >> 160) & 0xFFFFFF
    tick = tick - (1 << 24) if tick >> 23 else tick
    return {"sqrtp": sqrtp, "tick": tick, "lp_fee": (s0 >> 208) & 0xFFFFFF, "liquidity": liq}


def virtual_reserves(st):
    """(reserve0, reserve1) a constant-product pool with the same active liquidity would hold."""
    if not st["sqrtp"] or not st["liquidity"]:
        return 0, 0
    return st["liquidity"] * (1 << 96) // st["sqrtp"], st["liquidity"] * st["sqrtp"] // (1 << 96)


class Pool:
    def __init__(self, lp, token, pm):
        c0, c1, fee, ts, hook = lp["key"]
        self.key = (c0, c1, fee, ts, hook)
        self.id, self.pm, self.token = lp["pool_id"], pm, token.lower()
        self.hook = hook.lower()
        self.t0 = c0.lower() == self.token  # token is currency0?
        self.quote = (c1 if self.t0 else c0).lower()


async def _bal(f, cur, who):
    if cur == ZERO:
        return int(await f.call("eth_getBalance", [who, "latest"]), 16)
    ok, r = await f.try_call(cur, encode_call("balanceOf(address)", [who]))
    return int(r, 16) if ok and r and r != "0x" else 0


async def fund(fork, pool, who, quote_amount):
    """Give `who` gas money + enough quote currency for a buy."""
    await fork.impersonate(who, fund=True)
    if pool.quote != ZERO:
        try:
            await fork.rpc.call("anvil_dealERC20", [who, pool.quote, hex(quote_amount * 3)])
        except Exception:
            return False
    return True


async def swap(fork, pool, who, buy, amount):
    """Real exact-input swap via the helper. buy=True pays quote for token. -> dict"""
    f = fork.rpc
    h = await helper(fork, pool.pm)
    if not h:
        return {"ok": False, "why": "helper deploy failed"}
    pay, get = (pool.quote, pool.token) if buy else (pool.token, pool.quote)
    zero_for_one = (pay == pool.key[0].lower())
    value = 0
    if pay == ZERO:
        value = amount
    else:  # exact-amount approval for this one swap
        ra = await fork.send(who, pay, encode_call("approve(address,uint256)", [h, amount]), fund=False)
        if not (ra and ra.get("status") == "0x1"):
            return {"ok": False, "why": "approve reverted"}
    b0 = await _bal(f, get, who)
    data = encode_call(f"swap({KEY_T},bool,uint256)", [pool.key, zero_for_one, amount])
    r = await fork.send(who, h, data, value=value, gas=5_000_000, fund=False)
    out = {"ok": bool(r and r.get("status") == "0x1"), "amount_in": amount, "tx": (r or {}).get("transactionHash")}
    if not out["ok"]:
        out["why"] = (r or {}).get("rejected") or await _why(f, h, data, who, value)
        return out
    out["received"] = await _bal(f, get, who) - b0
    out.update(measure(r.get("logs", []), pool, h, zero_for_one, out["received"], get))
    return out


def measure(logs, pool, h, zero_for_one, received, get):
    """Exact hook cut from one swap's logs: PoolManager Swap event (what the pool traded) vs the helper's Deltas event
    (what the trader paid/got). Works on fork receipts and on eth_simulateV1 logs alike."""
    out = {}
    for lg in logs:
        tp = lg.get("topics") or []
        addr = (lg.get("address") or "").lower()
        if tp and tp[0] == SWAP_T and addr == pool.pm.lower() and tp[1].lower() == pool.id.lower():
            d = lg["data"][2:]
            out["pool"] = (_s128(d[0:64]), _s128(d[64:128])); out["fee_pips"] = int(d[320:384], 16)
        elif tp and tp[0] == ERC6909_T and addr == pool.pm.lower() and len(tp) == 4 and to_addr(tp[1]) == ZERO:
            out.setdefault("credits", []).append((to_addr(tp[2]), to_addr(tp[3]), int(lg["data"][-64:], 16)))  # PoolManager claim minted
        elif tp and tp[0] == DELTAS_T and addr == h.lower():
            d = lg["data"][2:]
            out["caller"] = (_s128(d[0:64]), _s128(d[64:128]))
    if "pool" in out and "caller" in out:
        pi, po = (0, 1) if zero_for_one else (1, 0)
        p_in, p_out = abs(out["pool"][pi]), abs(out["pool"][po])
        c_in, c_out = abs(out["caller"][pi]), abs(out["caller"][po])
        in_cut = max(0.0, (c_in - p_in) / c_in) if c_in else 0.0
        out_cut = max(0.0, (p_out - c_out) / p_out) if p_out else 0.0
        out["hook_pct"] = 100 * (1 - (1 - in_cut) * (1 - out_cut))
        out["lp_pct"] = out["fee_pips"] / 1e4
        out["token_tax_pct"] = max(0.0, 100 * (1 - received / c_out)) if c_out and get != ZERO and received is not None else 0.0
        out["hook_gain"] = (c_in - p_in if in_cut else 0, p_out - c_out if out_cut else 0)
    return out


def runtime_code(pm):
    """The helper's runtime bytecode with the PoolManager address patched into its immutable slots (for state overrides)."""
    a = json.load(open(ART))["deployedBytecode"]
    code = bytearray(bytes.fromhex(a["object"].replace("0x", "")))
    word = bytes.fromhex(pm[2:].lower().rjust(64, "0"))
    for refs in (a.get("immutableReferences") or {}).values():
        for r in refs:
            code[r["start"]:r["start"] + r["length"]] = word
    return "0x" + code.hex()


def _unwrap(m):
    """v4 wraps hook reverts in WrappedError(address target, bytes4 sel, bytes reason, bytes details): dig out the hook's reason."""
    import re
    from eth_abi import decode
    hx = re.search(r"0x90bfb865([0-9a-fA-F]+)", m)
    if not hx:
        return None
    try:
        target, sel, reason, _ = decode(["address", "bytes4", "bytes", "bytes"], bytes.fromhex(hx.group(1)))
        inner = reason.hex()
        if inner.startswith("08c379a0"):
            txt = decode(["string"], bytes.fromhex(inner[8:]))[0]
        else:
            txt = ("custom error 0x" + inner[:8]) if inner else "reverted without reason"
        return f"hook {short(target)} reverted: {txt}"
    except Exception:
        return None


async def _why(f, to, data, sender, value=0):
    ok, err = await f.try_call(to, data, sender, value)
    import re
    m = str(err)
    w = _unwrap(m)
    if w:
        return w
    hx = re.search(r"0x08c379a0[0-9a-fA-F]+", m)
    if hx:
        try:
            raw = bytes.fromhex(hx.group(0)[10:]); ln = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + ln].decode(errors="ignore") or "reverted"
        except Exception:
            pass
    sel = re.search(r"0x[0-9a-fA-F]{8}", m)
    return (m.replace("execution reverted", "").strip(" :,")[:90]) or "reverted"


async def recipients(fork, pool, res, who):
    """Where the hook's cut went, from the swap's trace: ERC20/native paid out by the PoolManager or hook, ERC6909 credits."""
    tr = await tracer.annotate(await tracer.trace_tx(fork.rpc, res.get("tx")))
    if not tr:
        return None, None
    skip = {who.lower(), pool.pm.lower(), (getattr(fork, "_v4helper", "") or "").lower()}
    got = {}
    for t in tr["transfers"]:
        if t["to"] not in skip and t["from"] in (pool.pm.lower(), pool.hook, who.lower(), pool.token):
            got[(t["to"], t["token"])] = got.get((t["to"], t["token"]), 0) + t["amount"]
    for n in tr["native"]:
        if n["to"] not in skip and n["from"] in (pool.pm.lower(), pool.hook):
            got[(n["to"], ZERO)] = got.get((n["to"], ZERO), 0) + n["value"]
    for a, c, v in res.get("credits") or []:  # cut kept inside the PoolManager as ERC6909 claims
        got[(a, c + "*")] = got.get((a, c + "*"), 0) + v
    return tr, sorted(((a, c, v) for (a, c), v in got.items()), key=lambda x: -x[2])


def callback_map(tr, pool):
    """What each hook callback actually did in this tx: calls it made (to PoolManager or outside contracts)."""
    pm, hook = pool.pm.lower(), pool.hook
    calls, out = tr["calls"], {}
    for i, c in enumerate(calls):
        if c["from"] != pm or c["to"] != hook or not c.get("sel"):
            continue
        name = (c.get("fn") or c["sel"]).split("(")[0]
        acts = []
        for x in calls[i + 1:]:
            if x["depth"] <= c["depth"]:
                break
            if x["depth"] == c["depth"] + 1 or (x["from"] == hook and x["type"] != "STATICCALL"):
                if x["type"] == "STATICCALL":
                    continue
                fn = (x.get("fn") or x.get("sel") or "?").split("(")[0]
                where = "PoolManager" if x["to"] == pm else ("token" if x["to"] == pool.token else short(x["to"]))
                a = f"{where}.{fn}"
                if a not in acts:
                    acts.append(a)
        e = out.setdefault(name, {"acts": [], "reverted": bool(c.get("error"))})
        e["acts"] += [a for a in acts if a not in e["acts"]]
    writes = len(tr["storage"].get(hook, []))
    return out, writes


async def liquidity_test(fork, pool, who, st):
    """Can an outsider add (then remove) liquidity? Returns dict with the callback map for add/remove."""
    h = await helper(fork, pool.pm)
    ts = pool.key[3] or 60
    tl = (-887272 // ts + 1) * ts
    tu = -tl
    liq = max(1, st["liquidity"] // 100_000)
    # outsider needs both currencies: approve the helper for whatever it holds
    for cur in (pool.key[0].lower(), pool.key[1].lower()):
        if cur != ZERO:
            b = await _bal(fork.rpc, cur, who)
            await fork.send(who, cur, encode_call("approve(address,uint256)", [h, b]), fund=False)
    data = encode_call(f"liquidity({KEY_T},int24,int24,int256,bytes32)", [pool.key, tl, tu, liq, b"\x01" * 32])
    r = await fork.send(who, h, data, value=10 ** 20 if ZERO in (pool.key[0].lower(),) else 0, gas=5_000_000, fund=False)
    out = {"add_ok": bool(r and r.get("status") == "0x1")}
    if not out["add_ok"]:
        out["why"] = (r or {}).get("rejected") or await _why(fork.rpc, h, data, who, 10 ** 20)
        return out
    tr = await tracer.annotate(await tracer.trace_tx(fork.rpc, r.get("transactionHash"), diff=False))
    out["add_map"] = callback_map(tr, pool)[0] if tr else {}
    data2 = encode_call(f"liquidity({KEY_T},int24,int24,int256,bytes32)", [pool.key, tl, tu, -liq, b"\x01" * 32])
    r2 = await fork.send(who, h, data2, gas=5_000_000, fund=False)
    out["remove_ok"] = bool(r2 and r2.get("status") == "0x1")
    if not out["remove_ok"]:
        out["remove_why"] = (r2 or {}).get("rejected") or await _why(fork.rpc, h, data2, who)
    else:
        tr2 = await tracer.annotate(await tracer.trace_tx(fork.rpc, r2.get("transactionHash"), diff=False))
        out["remove_map"] = callback_map(tr2, pool)[0] if tr2 else {}
    return out


async def analyze(fork, lp, token, hook_probes=(), worst_case=None, budget=25):
    """Full hook analysis on a fork. -> dict"""
    import time
    t0 = time.time()
    pm = fork.src.cfg["v4"]["pool_manager"]
    pool = Pool(lp, token, pm)
    f = fork.rpc
    st = await pool_state(f, pm, pool.id)
    out = {"hook": pool.hook, "pool_id": pool.id, "lp_fee_pips": st["lp_fee"], "dynamic": pool.key[2] == 0x800000}
    if not st["liquidity"]:
        out["note"] = "pool has no active liquidity — nothing to swap against"
        return out
    r0, r1 = virtual_reserves(st)
    vq = r1 if pool.t0 else r0
    amt = max(1, vq // 2000)  # 0.05% of the pool's quote side: tiny price impact
    if not await fund(fork, pool, TRADER, amt):
        out["note"] = "could not fund test wallet with quote token"
        return out
    base = await fork.snapshot()
    buy = await swap(fork, pool, TRADER, True, amt)
    out["buy"] = buy
    if buy["ok"]:
        tr, rec = await recipients(fork, pool, buy, TRADER)
        out["buy_to"] = rec
        if tr:
            out["swap_map"], out["hook_writes"] = callback_map(tr, pool)
        held = await _bal(f, pool.token, TRADER)
        after_buy = await fork.snapshot()
        sell = await swap(fork, pool, TRADER, False, held)
        out["sell"] = sell
        if sell["ok"]:
            tr2, rec2 = await recipients(fork, pool, sell, TRADER)
            out["sell_to"] = rec2
            if tr2:
                m2, w2 = callback_map(tr2, pool)
                for k, v in m2.items():
                    e = out.setdefault("swap_map", {}).setdefault(k, {"acts": [], "reverted": False})
                    e["acts"] += [a for a in v["acts"] if a not in e["acts"]]
        # later sell (+1h): fees that change with time (launch fees decaying / rising)
        await fork.revert(after_buy); after_buy = await fork.snapshot()
        await fork.mine(300, 3600)
        later = await swap(fork, pool, TRADER, False, held)
        out["sell_1h"] = later
        # outsider liquidity
        await fork.revert(after_buy); after_buy = await fork.snapshot()
        if time.time() - t0 < budget:
            try:
                out["liquidity"] = await liquidity_test(fork, pool, TRADER, st)
            except Exception as e:
                out["liquidity"] = {"add_ok": None, "why": str(e)[:80]}
        # hook owner powers -> retry the sell
        out["powers"] = []
        for p in [p for p in hook_probes if p.get("callers") and "note" not in p][:6]:
            if time.time() - t0 > budget:
                break
            who = p["callers"][0]
            ran = None
            for data in argsmod.variants(p["sel"], p["fn"], p["args"], who):
                if not data:
                    continue
                await fork.revert(after_buy); after_buy = await fork.snapshot()
                rp = await fork.send(who, pool.hook, data)
                if rp and rp.get("status") == "0x1":
                    ran = data; break
            if not ran:
                out["powers"].append({"fn": p["fn"], "by": who, "ran": False}); continue
            s2 = await swap(fork, pool, TRADER, False, held)
            out["powers"].append({"fn": p["fn"], "by": who, "ran": True, "sell_ok": s2["ok"], "why": s2.get("why"),
                                  "hook_pct": s2.get("hook_pct"), "got": s2.get("received")})
    await fork.revert(base)
    out["elapsed"] = time.time() - t0
    out["flags"] = classify(out)
    return out


def classify(out):
    flags = []
    b, s = out.get("buy") or {}, out.get("sell") or {}
    if b and not b.get("ok"):
        flags.append(("bad", f"real v4 BUY through the hook reverts ({b.get('why')})"))
    if b.get("ok") and s and not s.get("ok"):
        flags.append(("bad", f"HONEYPOT (hook): buy works, real v4 SELL reverts ({s.get('why')})"))
    later = out.get("sell_1h") or {}
    if s.get("ok") and later and not later.get("ok"):
        flags.append(("bad", f"DELAYED HONEYPOT (hook): sell works now, reverts 1h later ({later.get('why')})"))
    for side, r in (("buy", b), ("sell", s)):
        if r.get("ok") and (r.get("hook_pct") or 0) > 25:
            flags.append(("bad", f"hook takes {r['hook_pct']:.1f}% on {side}"))
        elif r.get("ok") and (r.get("hook_pct") or 0) > 5:
            flags.append(("warn", f"hook takes {r['hook_pct']:.1f}% on {side}"))
    if s.get("ok") and later.get("ok") and abs((later.get("hook_pct") or 0) - (s.get("hook_pct") or 0)) > 1:
        flags.append(("warn", f"hook fee changes with time: sell {s.get('hook_pct', 0):.1f}% now → {later.get('hook_pct', 0):.1f}% after 1h"))
    base_fee = s.get("hook_pct") or 0
    for p in out.get("powers", []):
        if not p.get("ran") or not s.get("ok"):  # sells already fail: owner powers add nothing
            continue
        if p.get("sell_ok") is False:
            flags.append(("bad", f"SELL BLOCKER (hook): after {p['fn']} by {short(p['by'])} sells revert ({p.get('why')})"))
        elif (p.get("hook_pct") or 0) > base_fee + 5:
            flags.append(("bad" if p["hook_pct"] > 25 else "warn", f"hook owner can raise the fee: {p['fn']} by {short(p['by'])} → sell fee {base_fee:.1f}% → {p['hook_pct']:.1f}%"))
    return flags


def render(out, dec=18, labels=None):
    labels = {k.lower(): v for k, v in (labels or {}).items()}
    lab = lambda a: labels.get(a, short(a))
    if not out:
        return []
    L = [f"== HOOK SIM (real v4 swaps through hook {short(out['hook'])}" + (f", {out['elapsed']:.1f}s)" if out.get("elapsed") else ")")]
    if out.get("note"):
        L.append(f"   {out['note']}"); return L
    for side in ("buy", "sell", "sell_1h"):
        r = out.get(side)
        if not r:
            continue
        name = {"buy": "buy", "sell": "sell", "sell_1h": "sell +1h"}[side]
        if not r["ok"]:
            L.append(f"   {name}: REVERTS — {r.get('why')}"); continue
        if r.get("hook_pct") is None:
            L.append(f"   {name}: ok (hook cut not measurable)"); continue
        L.append(f"   {name}: ok · hook takes {r['hook_pct']:.2f}% · LP fee {r['lp_pct']:.2f}%" + (f" · token tax {r['token_tax_pct']:.1f}%" if r.get("token_tax_pct", 0) > 0.05 else ""))
    for side in ("buy_to", "sell_to"):
        rec = out.get(side)
        if rec:
            def cur(c):
                kept = c.endswith("*"); c = c.rstrip("*")
                n = "native" if c == ZERO else ("token" if c == labels.get("_token") else short(c))
                return n + (", kept as PoolManager credit" if kept else "")
            L.append(f"   {side.split('_')[0]} cut goes to: " + ", ".join(f"{lab(a)} ({cur(c)})" for a, c, v in rec[:3]))
    m = out.get("swap_map") or {}
    if m:
        L.append(f"   callbacks on {'buy (traced)' if out.get('fast') else 'swap'}: " + " · ".join(f"{k} → {', '.join(v['acts'][:3]) or 'nothing external'}" for k, v in m.items()) +
                 (f" · writes {out['hook_writes']} hook slot(s)" if out.get("hook_writes") else ""))
    lq = out.get("liquidity")
    if lq:
        if lq.get("add_ok") is False:
            L.append(f"   outsider add-liquidity: BLOCKED by hook ({lq.get('why')})")
        elif lq.get("add_ok"):
            L.append(f"   outsider add-liquidity: ok · remove: {'ok' if lq.get('remove_ok') else 'BLOCKED (' + str(lq.get('remove_why')) + ')'}" +
                     ("" if not (lq.get("add_map") or lq.get("remove_map")) else
                      " · callbacks: " + " · ".join(f"{k} → {', '.join(v['acts'][:2]) or 'nothing external'}" for k, v in {**(lq.get('add_map') or {}), **(lq.get('remove_map') or {})}.items())))
    for p in out.get("powers", []):
        if p.get("ran"):
            L.append(f"   after hook {p['fn']} by {short(p['by'])}: sell " + ("REVERTS — " + str(p.get("why")) if not p["sell_ok"] else f"ok, hook takes {p.get('hook_pct') or 0:.1f}%"))
        else:
            L.append(f"   · hook {p['fn']} by {short(p['by'])}: reverted with test args")
    if out.get("decomp"):
        from lib import decomp, static
        if out["decomp"].get("static"):
            L.extend("   " + l.strip() if not l.startswith("==") else "   code (verified hook, Slither):" for l in static.render(out["decomp"]["static"]))
        else:
            L.extend("   " + l.strip() if not l.startswith("==") else "   code (unverified hook, decompiled):" for l in decomp.render(out["decomp"]))
    for lvl, msg in out.get("flags", []):
        L.append(f"   {'✗' if lvl == 'bad' else '!'} {msg}")
    if not out.get("flags") and (out.get("sell") or {}).get("ok"):
        n = sum(1 for p in out.get("powers", []) if p.get("ran"))
        L.append("   ✓ real buy and sell through the hook work" + (f"; {n} hook owner function(s) run, none raised the fee or blocked sells" if n else "; no hook owner function was callable to test"))
    return L


NATIVE_LOG = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # eth_simulateV1 traceTransfers: native moves appear as Transfer logs from here


async def analyze_fast(rpc, lp, token, hook_probes=(), budget=20, pm=None, block=None):
    """block: build on this past block instead of the latest (pinned regression tests)."""
    """Same analysis as analyze(), without a fork: eth_simulateV1 requests in parallel (lib/fastsell.Plan).
    Callback map comes from one debug_traceCall of the buy (if the node traces). -> dict, or None to use the fork."""
    import time
    from lib import fastsell
    t0 = time.time()
    pm = pm or rpc.cfg["v4"]["pool_manager"]
    pool = Pool(lp, token, pm)
    p = fastsell.Plan()
    try:
        why = await p.init(rpc, token, pm, pool, block)
    except Exception:
        return None
    if why:
        return None
    st = await pool_state(rpc, pm, pool.id, hex(p.n))
    out = {"hook": pool.hook, "pool_id": pool.id, "lp_fee_pips": st["lp_fee"], "dynamic": pool.key[2] == 0x800000, "fast": True}
    W, H = fastsell.BUYER, fastsell.V4H
    tokens = max(1, p.pbal // 2000)
    zf_buy = pool.quote == pool.key[0].lower()

    def swap_res(calls, buy, received=None):
        c = calls[-1]
        r = {"ok": c["ok"], "logs": c["logs"]}
        if not c["ok"]:
            r["why"] = c.get("err"); return r
        m = measure(c["logs"], pool, H, zf_buy if buy else not zf_buy, received, pool.token if buy else pool.quote)
        r.update(m)
        r["received"] = received
        return r

    buy_blk = p.block(1, p.buy(W, tokens) + [p.bal(W)], first=True)
    r = await p.run([buy_blk])
    if r is None:
        return None
    got = fastsell._u(r[0][-1]["ret"])
    out["buy"] = swap_res(r[0][:-1], True, got)
    if not out["buy"]["ok"] or not got:
        out["elapsed"] = time.time() - t0; out["flags"] = classify(out); return out
    k1h, ts1h = p.later(300, 3600)
    sell_blk = p.block(2, p.sell(W, got))
    late_blk = p.block(k1h, p.sell(W, got)); late_blk["time"] = ts1h
    tl = (-887272 // (pool.key[3] or 60) + 1) * (pool.key[3] or 60)
    liq = max(1, st["liquidity"] // 100_000)
    lq_calls = [{"from": W, "to": c, "data": encode_call("approve(address,uint256)", [H, 2 ** 255])} for c in (pool.key[0].lower(), pool.key[1].lower()) if c != ZERO]
    add = {"from": W, "to": H, "data": encode_call(f"liquidity({KEY_T},int24,int24,int256,bytes32)", [pool.key, tl, -tl, liq, b"\x01" * 32]),
           "gas": 5_000_000, **({"value": 10 ** 20} if pool.key[0].lower() == ZERO else {})}
    rem = {"from": W, "to": H, "data": encode_call(f"liquidity({KEY_T},int24,int24,int256,bytes32)", [pool.key, tl, -tl, -liq, b"\x01" * 32]), "gas": 5_000_000}
    rs, rl, rq = await asyncio.gather(p.run([buy_blk, sell_blk]), p.run([buy_blk, late_blk]),
                                      p.run([buy_blk, p.block(2, lq_calls + [add]), p.block(3, [rem])]))
    if rs:
        out["sell"] = swap_res(rs[-1], False)
    if rl:
        out["sell_1h"] = swap_res(rl[-1], False)
    if rq:
        a, b = rq[-2][-1], rq[-1][-1]
        out["liquidity"] = {"add_ok": a["ok"], "why": a.get("err"), "remove_ok": b["ok"], "remove_why": b.get("err")} if a["ok"] else {"add_ok": False, "why": a.get("err")}
    # who received the cut: ERC-20 / native moves paid out by the PoolManager or hook, plus PoolManager credits (ERC-6909)
    for side in ("buy", "sell"):
        res = out.get(side) or {}
        if not res.get("ok"):
            continue
        got_ = {}
        skip = {W.lower(), pm.lower(), H.lower()}
        for lg in res["logs"]:
            tp = lg.get("topics") or []
            if len(tp) == 3 and tp[0].lower() == tracer.TRANSFER:
                frm, to = to_addr(tp[1]), to_addr(tp[2])
                cur = ZERO if (lg.get("address") or "").lower() == NATIVE_LOG else (lg.get("address") or "").lower()
                if to not in skip and frm in (pm.lower(), pool.hook, W.lower(), pool.token):
                    got_[(to, cur)] = got_.get((to, cur), 0) + int(lg.get("data") or "0x0", 16)
        for a_, c_, v in res.get("credits") or []:
            got_[(a_, c_ + "*")] = got_.get((a_, c_ + "*"), 0) + v
        out[side + "_to"] = sorted(((a_, c_, v) for (a_, c_), v in got_.items()), key=lambda x: -x[2])
    # callback map: trace the buy once with the same overrides (nodes without debug_* just skip it)
    try:
        bc = p.buy(W, tokens)[-1]
        tx = {"from": W, "to": H, "data": bc["data"], "gas": hex(5_000_000), **({"value": hex(bc["value"])} if bc.get("value") else {})}
        ov = {**p.ov}
        tr = await asyncio.wait_for(rpc.call("debug_traceCall", [tx, hex(p.n), {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "stateOverrides": ov}]), 8)
        tr = await tracer.annotate(tracer.summarize(tr))
        out["swap_map"], out["hook_writes"] = callback_map(tr, pool)
    except Exception:
        pass
    # hook owner powers: switch each on, then the holder sells
    async def power(pr):
        who = pr["callers"][0]
        for data in argsmod.variants(pr["sel"], pr["fn"], pr["args"], who):
            rr = await p.run([buy_blk, p.block(2, [{"from": who, "to": pool.hook, "data": data, "gas": 3_000_000}] + p.sell(W, got))])
            if rr is None:
                return None
            if not rr[-1][0]["ok"]:
                continue
            s2 = swap_res(rr[-1][1:], False)
            return {"fn": pr["fn"], "by": who, "ran": True, "sell_ok": s2["ok"], "why": s2.get("why"), "hook_pct": s2.get("hook_pct")}
        return {"fn": pr["fn"], "by": who, "ran": False}
    runnable = [x for x in hook_probes if x.get("callers") and "note" not in x][:6]
    out["powers"] = [x for x in await asyncio.gather(*[power(x) for x in runnable]) if x]
    for side in ("buy", "sell", "sell_1h"):
        (out.get(side) or {}).pop("logs", None)
    out["elapsed"] = time.time() - t0
    out["flags"] = classify(out)
    return out
