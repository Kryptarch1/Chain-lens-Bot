"""Rug simulations on a private fork: actually remove the liquidity as whoever controls it, and measure what
they walk away with. Nothing is broadcast.

v2  : LP-token holder sends LP to the pair and calls burn()
v3  : NFT owner calls decreaseLiquidity + collect on the position manager
v4  : NFT owner burns the position through the v4 PositionManager (BURN_POSITION + TAKE_PAIR)
v4* : custom LP managers / lockers / hooks that own liquidity directly: the contract's owner/controller tries every
      removal-type function with the real pool key, ticks and liquidity; success = position liquidity drops on-chain.
"""
import asyncio, itertools
from eth_abi import encode
from lib.rpc import ZERO, DEAD
from lib.evm import encode_call, decode1, decode_out, addr as to_addr, fmt, short
from lib.lp import v4_current_liquidity, REMOVAL_WORDS, NOT_REMOVAL_PREFIX
from lib import sigs

MAXU128 = 2 ** 128 - 1
DEADLINE = 2 ** 40


async def _bal(f, cur, who):
    if cur.lower() == ZERO:
        return int(await f.call("eth_getBalance", [who, "latest"]), 16)
    ok, r = await f.try_call(cur, encode_call("balanceOf(address)", [who]))
    return int(r, 16) if ok and r and r != "0x" else 0


async def _sym_dec(rpc, cur):
    if cur.lower() == ZERO:
        return rpc.cfg.get("symbol", "ETH"), 18
    s = await rpc.try_call(cur, encode_call("symbol()"))
    d = await rpc.try_call(cur, encode_call("decimals()"))
    try:
        sym = decode1("string", s[1]) if s[0] else "?"
    except Exception:
        sym = "?"
    try:
        dec = decode1("uint8", d[1]) if d[0] else 18
    except Exception:
        dec = 18
    return sym, dec


async def _measure(fork, who_list, currencies, action, sender=None):
    """Run `action()` (returns receipt), return ({cur: total delta across who_list incl. gas refund}, receipt).
    The sender is funded for gas BEFORE the 'before' snapshot so the top-up isn't counted as proceeds."""
    f = fork.rpc
    if sender:
        await fork.impersonate(sender, fund=True)
    before = {c: [await _bal(f, c, w) for w in who_list] for c in currencies}
    r = await action()
    gas = 0
    if r and r.get("gasUsed") and r.get("effectiveGasPrice"):
        gas = int(r["gasUsed"], 16) * int(r["effectiveGasPrice"], 16)
    after = {c: [await _bal(f, c, w) for w in who_list] for c in currencies}
    deltas = {}
    for c in currencies:
        dlt = sum(a - b for a, b in zip(after[c], before[c]))
        if c.lower() == ZERO and r and r.get("from", "").lower() in [w.lower() for w in who_list]:
            dlt += gas
        deltas[c] = dlt
    return deltas, r


def _fmt_out(deltas, meta):
    parts = []
    for c, d in deltas.items():
        if d > 0:
            sym, dec = meta[c]
            parts.append(f"{fmt(d, dec, 4 if dec <= 8 or d < 10 ** dec * 1000 else 0)} {sym}")
    return " + ".join(parts) or "nothing"


async def sim_v2(fork, rpc, lp, token):
    pair = lp["pair"]
    f = fork.rpc
    t0 = to_addr(await rpc.eth_call(pair, encode_call("token0()")))
    t1 = to_addr(await rpc.eth_call(pair, encode_call("token1()")))
    meta = {t0: await _sym_dec(rpc, t0), t1: await _sym_dec(rpc, t1)}
    out = []
    for h in [x for x in lp.get("holders", []) if x["who"] not in ("dead", "zero")][:3]:
        holder = h["address"]
        lpbal = decode1("uint256", await f.eth_call(pair, encode_call("balanceOf(address)", [holder])))
        if not lpbal:
            continue
        snap = await fork.snapshot()
        await fork.impersonate(holder)

        async def act():
            r1 = await fork.send(holder, pair, encode_call("transfer(address,uint256)", [pair, lpbal]), fund=False)
            if not (r1 and r1.get("status") == "0x1"):
                return r1
            return await fork.send(holder, pair, encode_call("burn(address)", [holder]), fund=False)
        d, r = await _measure(fork, [holder], [t0, t1], act, sender=holder)
        await fork.revert(snap)
        ok = bool(r and r.get("status") == "0x1")
        out.append({"who": holder, "share": h["pct"], "how": "transfer LP to pair + burn()", "ok": ok, "out": _fmt_out(d, meta) if ok else None,
                    "kind": (h.get("info") or {}).get("kind")})
    return out


async def sim_v3(fork, rpc, lp):
    out = []
    f = fork.rpc
    for p in lp.get("positions", [])[:3]:
        owner, nfpm, tid = p.get("owner"), p.get("nft"), p.get("id")
        if not (owner and owner.startswith("0x") and isinstance(tid, int)):
            out.append({"who": str(owner)[:40], "how": "position NFT", "ok": False, "note": "NFT burned / not removable"}); continue
        pos = decode_out("uint96,address,address,address,uint24,int24,int24,uint128,uint256,uint256,uint128,uint128",
                         await f.eth_call(nfpm, encode_call("positions(uint256)", [tid])))
        t0, t1, liq = pos[2], pos[3], pos[7]
        meta = {t0: await _sym_dec(rpc, t0), t1: await _sym_dec(rpc, t1)}
        snap = await fork.snapshot()

        async def act():
            r1 = await fork.send(owner, nfpm, encode_call("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))", [(tid, liq, 0, 0, DEADLINE)]), fund=False)
            if not (r1 and r1.get("status") == "0x1"):
                return r1
            return await fork.send(owner, nfpm, encode_call("collect((uint256,address,uint128,uint128))", [(tid, owner, MAXU128, MAXU128)]), fund=False)
        d, r = await _measure(fork, [owner], [t0, t1], act, sender=owner)
        await fork.revert(snap)
        ok = bool(r and r.get("status") == "0x1")
        out.append({"who": owner, "how": f"NFT #{tid}: decreaseLiquidity + collect", "ok": ok, "out": _fmt_out(d, meta) if ok else None,
                    "kind": (p.get("info") or {}).get("kind")})
    return out


async def sim_v4_posm(fork, rpc, cfg, lp, p, meta):
    posm = p.get("posm") or cfg["v4"]["posm"]
    owner, tid = p["owner"], p["nft"]
    c0, c1 = lp["key"][0], lp["key"][1]
    actions = bytes([0x03, 0x11])  # BURN_POSITION, TAKE_PAIR
    params = [encode(["uint256", "uint128", "uint128", "bytes"], [tid, 0, 0, b""]),
              encode(["address", "address", "address"], [c0, c1, owner])]
    unlock = encode(["bytes", "bytes[]"], [actions, params])
    snap = await fork.snapshot()
    d, r = await _measure(fork, [owner], [c0, c1], lambda: fork.send(owner, posm, encode_call("modifyLiquidities(bytes,uint256)", [unlock, DEADLINE]), fund=False), sender=owner)
    await fork.revert(snap)
    ok = bool(r and r.get("status") == "0x1")
    kind = (p.get("info") or {}).get("kind")
    how = f"NFT #{tid}: burn position via PositionManager"
    if kind == "contract":
        how += f" (NFT is held by contract {(p.get('info') or {}).get('name')}; real pull depends on that contract's code)"
    return {"who": owner, "how": how, "ok": ok, "out": _fmt_out(d, meta) if ok else None, "kind": kind}


def _split_types(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _arg_sets(types, key, pos, caller, tranche_n):
    """Plausible argument sets for a removal function, built from the real pool key / position.
    First uint256 = an id (tranche/position index) we vary; later uint256s = min amounts (0).
    uint128 = liquidity: try the full position, then partial amounts (a tranche can be smaller than the position)."""
    tl, tu = pos["ticks"]
    liq = pos["liq"]
    ids = list(dict.fromkeys(list(range(0, max(3, tranche_n + 1)))[:8]))
    liqs = [liq, liq // 2, liq // 10, liq // 100, 10 ** 15]
    per = []
    ints_seen = 0
    first_u256 = True
    for t in types:
        if t.startswith("("):
            inner = _split_types(t[1:-1])
            if len(inner) == 5 and inner[0] == "address" and inner[1] == "address":
                per.append([tuple(key)])
            else:
                return []
        elif t == "address":
            per.append([caller])
        elif t == "int24":
            per.append([tl] if ints_seen == 0 else [tu]); ints_seen += 1
        elif t == "uint128":
            per.append(liqs)
        elif t == "uint256":
            per.append(ids + [liq] if first_u256 else [0]); first_u256 = False
        elif t.startswith("uint"):
            bits = int(t[4:])
            per.append([min(DEADLINE, 2 ** bits - 1)])
        elif t == "bytes32":
            per.append([bytes.fromhex(pos["salt"].rjust(64, "0"))])
        elif t.startswith("bytes"):
            per.append([b""])
        elif t == "bool":
            per.append([True, False])
        else:
            return []
    # ids vary slowest, liquidity amounts fastest -> each id gets every liquidity size tried
    return list(itertools.islice(itertools.product(*per), 60))


async def sim_v4_contract(fork, rpc, cfg, lp, p, meta):
    """Liquidity owned directly by a contract (LP manager / locker / hook)."""
    contract = p["owner"]
    info = p.get("info") or {}
    pm = cfg["v4"]["pool_manager"]
    key = lp.get("key")
    if not key:
        return {"who": contract, "how": "custom contract", "ok": None, "note": "pool key unknown — cannot build removal call"}
    callers = [c for c in [info.get("controller"), info.get("owner")] if c] or []
    rows = sigs.selectors_from_bytecode(info.get("code") or await rpc.code(contract))
    names = await sigs.names_for([r[0] for r in rows])
    cands = []
    for sel, args, mut in rows:
        if mut in ("view", "pure"):
            continue
        nm = names.get(sel) or ""
        low = nm.lower()
        if (nm and any(w in low for w in REMOVAL_WORDS) and not low.startswith(NOT_REMOVAL_PREFIX)) or not nm:
            cands.append((sel, nm or f"unnamed {sel}", args))
    tranche_n = 0
    ok_, r_ = await rpc.try_call(contract, encode_call("trancheCount()"))
    if ok_ and r_ and r_ != "0x":
        tranche_n = int(r_, 16)
    if not cands:
        return {"who": contract, "how": f"{info.get('name')}", "ok": False, "note": "contract has no function that can remove liquidity → can't be pulled", "contract": contract}
    if not callers:
        return {"who": contract, "how": f"{info.get('name')} — {len(cands)} removal-type fn(s)", "ok": False, "note": "no owner/controller found to call them"}
    tried = 0
    liq0 = await v4_current_liquidity(fork.rpc, pm, lp["pool_id"], contract, p["ticks"][0], p["ticks"][1], p["salt"])
    for sel, nm, args in cands[:10]:
        types = _split_types(args)
        for caller in callers:
            for argset in _arg_sets(types, key, p, caller, tranche_n):
                tried += 1
                try:
                    data = sel + (encode(types, list(argset)).hex() if types else "")
                except Exception:
                    continue
                snap = await fork.snapshot()
                who_list = list(dict.fromkeys([caller, contract] + [c for c in callers]))
                d, r = await _measure(fork, who_list, [key[0], key[1]], lambda: fork.send(caller, contract, data, fund=False), sender=caller)
                liq1 = await v4_current_liquidity(fork.rpc, pm, lp["pool_id"], contract, p["ticks"][0], p["ticks"][1], p["salt"])
                await fork.revert(snap)
                if r and r.get("status") == "0x1" and liq0 and liq1 is not None and liq1 < liq0:
                    pct = 100 * (liq0 - liq1) / liq0
                    return {"who": caller, "how": f"{info.get('name')}.{nm} ({pct:.0f}% of position)", "ok": True,
                            "out": _fmt_out(d, meta), "kind": "contract", "contract": contract}
    # explain the failure: look for a lock-expiry timestamp in the revert data or a remainingLockTime() view
    import time as _t
    now = int(_t.time())
    unlock = None
    for sel, nm, args in cands[:10]:
        types = _split_types(args)
        for argset in _arg_sets(types, key, p, callers[0], tranche_n)[:12]:
            try:
                data = sel + (encode(types, list(argset)).hex() if types else "")
            except Exception:
                continue
            ok, err = await rpc.try_call(contract, data, callers[0])
            if ok:
                continue
            hexdata = err.split("data:")[-1].strip().strip('"') if "data:" in err else ""
            words = [int(hexdata[10 + i:10 + i + 64], 16) for i in range(0, max(0, len(hexdata) - 10), 64) if len(hexdata[10 + i:10 + i + 64]) == 64]
            fut = [w for w in words if now < w < now + 5 * 365 * 86400]
            if fut:
                unlock = max(fut) if unlock is None else max(unlock, max(fut))
    for i in range(max(tranche_n, 1)):
        ok, r = await rpc.try_call(contract, encode_call("remainingLockTime(uint256)", [i]))
        if ok and r and r != "0x" and int(r, 16) > 0:
            unlock = max(unlock or 0, now + int(r, 16))
    note = "no call removed liquidity (looks locked, unless removal needs args we can't guess)"
    if unlock:
        note = f"removal reverts: still LOCKED until ~{_t.strftime('%Y-%m-%d %H:%M UTC', _t.gmtime(unlock))} ({(unlock-now)/86400:.1f} days) — check again after that"
    return {"who": ", ".join(short(c) for c in callers), "how": f"{info.get('name')} — tried {len(cands)} removal-type fn(s), {tried} call(s)",
            "ok": False, "note": note, "contract": contract, "unlock": unlock}


async def run_all(fork, rpc, lp, token):
    """-> list of result dicts for the main pool."""
    t = lp.get("type")
    if t == "v2":
        return await sim_v2(fork, rpc, lp, token)
    if t == "v3":
        return await sim_v3(fork, rpc, lp)
    if t == "v4":
        if not lp.get("key"):
            return [{"who": "-", "how": "v4", "ok": None, "note": "pool key not found — cannot simulate"}]
        meta = {lp["key"][0]: await _sym_dec(rpc, lp["key"][0]), lp["key"][1]: await _sym_dec(rpc, lp["key"][1])}
        res = []
        for p in lp.get("positions", [])[:3]:
            o = p.get("owner") or ""
            if o.startswith("BURNED") or o.lower() in (ZERO, DEAD):
                res.append({"who": "burned", "how": "position NFT burned/dead", "ok": False, "note": "nobody can remove"})
            elif p.get("nft") is not None:
                res.append(await sim_v4_posm(fork, rpc, rpc.cfg, lp, p, meta))
            elif (p.get("info") or {}).get("kind") == "contract":
                res.append(await sim_v4_contract(fork, rpc, rpc.cfg, lp, p, meta))
            elif (p.get("info") or {}).get("kind") == "eoa":
                res.append({"who": o, "how": "direct position", "ok": None, "note": "owned by a plain wallet directly in the PoolManager"})
        return res
    return []


def render(results, lp):
    L = [f"== RUG SIM (main pool, {lp.get('dex')} {lp.get('type')}, real liquidity {('$%s' % format(int(lp['liq_usd']), ',')) if lp.get('liq_usd') else '?'})"]
    if lp.get("status") == "PULLED":
        L.append("   liquidity already removed — " + (lp.get("reasons") or [""])[0])
        return L
    if not results:
        L.append("   nothing to simulate")
    for r in results:
        if r.get("ok"):
            L.append(f"   ✗ {short(r['who']) if r['who'].startswith('0x') else r['who']} CAN PULL: {r['how']} → receives {r['out']}")
        elif r.get("ok") is False:
            L.append(f"   ✓ {r['who'] if not r['who'].startswith('0x') else short(r['who'])}: {r['how']} — {r.get('note') or 'removal reverted'}")
        else:
            L.append(f"   ? {r['how']}: {r.get('note')}")
    return L
