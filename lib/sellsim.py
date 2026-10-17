"""Stateful sell tests on a private fork: catches honeypots, DELAYED honeypots and funds-taken tokens.

A fresh ordinary wallet buys (tokens leave the real pool), then we move the chain forward and try to
sell at several later points and sizes while watching the wallet's balance. Then each owner power is
switched on and the sell is retried.  Nothing is broadcast.

Transfers are made directly between the pool and the wallet (token-level), which is where token-side
rules live (cooldowns, max sell, blacklists, balance edits). Router/AMM mechanics are not part of it.
"""
import asyncio
from lib.evm import encode_call, decode1, fmt, short
from lib.rpc import ZERO
from lib import tracer, v4hook, args as argsmod

BUYER = "0x00000000000000000000000000000000000Bb1ee"
BUYER2 = "0x00000000000000000000000000000000000Bb2ee"
SCHEDULES = [("same block", 0, 0), ("+1 block", 1, 0), ("+10 blocks", 10, 0), ("+1 hour", 300, 3600), ("+24 hours", 500, 86400)]


async def _why(f, to, data, sender):
    """Replay a failed tx as eth_call to extract the revert reason (best effort)."""
    ok, err = await f.try_call(to, data, sender)
    if ok:
        return "reverted (no reason captured)"
    m = str(err)
    import re
    hx = re.search(r"0x08c379a0[0-9a-fA-F]+", m)
    if hx:
        try:
            raw = bytes.fromhex(hx.group(0)[10:])
            ln = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + ln].decode(errors="ignore") or "reverted"
        except Exception:
            pass
    m = m.replace("execution reverted", "").strip(" :,")
    return m[:80] or "reverted"


async def _bal(f, token, who):
    return decode1("uint256", await f.eth_call(token, encode_call("balanceOf(address)", [who]))) or 0


def _reason(r):
    if not r:
        return "no receipt"
    return "reverted" if r.get("status") != "0x1" else "ok"


async def _v2_info(f, token, pool):
    """(token0, reserveToken, reserveQuote, quote) for a v2-style pair, else None."""
    ok, t0 = await f.try_call(pool, encode_call("token0()"))
    ok2, t1 = await f.try_call(pool, encode_call("token1()"))
    ok3, res = await f.try_call(pool, encode_call("getReserves()"))
    if not (ok and ok2 and ok3 and res and len(res) >= 194):
        return None
    from lib.evm import decode_out, addr as to_addr
    t0, t1 = to_addr(t0), to_addr(t1)
    r0, r1, _ = decode_out("uint112,uint112,uint32", res)
    if t0.lower() == token.lower():
        return t0, r0, r1, t1
    if t1.lower() == token.lower():
        return t0, r1, r0, t0
    return None


async def _sell(fork, token, pool, who, amount, v2=None):
    """Sell `amount` tokens. v2 pair: REAL swap (transfer to pair + pair.swap, quote paid out to `who`).
    Otherwise: transfer to the pool (token-level test). -> (ok, pool_received_tokens, receipt, quote_out)"""
    f = fork.rpc
    if isinstance(v2, v4hook.Pool):  # REAL v4 swap through the PoolManager (hook runs)
        res = await v4hook.swap(fork, v2, who, False, amount)
        if not res["ok"]:
            return False, 0, {"status": "0x0", "rejected": "v4 swap: " + str(res.get("why"))}, 0
        fee = (res.get("hook_pct") or 0) + (res.get("token_tax_pct") or 0)
        return True, int(amount * (1 - fee / 100)), {"status": "0x1", "transactionHash": res.get("tx")}, res.get("received", 0)
    before = await _bal(f, token, pool)
    if v2:
        t0, rT, rQ, quote = v2
        data = encode_call("transfer(address,uint256)", [pool, amount])
        r = await fork.send(who, token, data, fund=False)
        if not (r and r.get("status") == "0x1"):
            return False, 0, {"status": "0x0", "rejected": "token transfer to pool: " + (r.get("rejected") if r and r.get("rejected") else await _why(f, token, data, who))}, 0
        after = await _bal(f, token, pool)
        # re-read reserves AFTER the transfer: tax tokens often swap their fee inside the transfer, moving reserves
        v2n = await _v2_info(f, token, pool)
        if v2n:
            t0, rT, rQ, quote = v2n
        ain = after - rT
        if ain <= 0:
            return False, 0, {"status": "0x0", "rejected": "pair received nothing"}, 0
        out = ain * 997 * rQ // (rT * 1000 + ain * 997)
        out = out * 97 // 100  # tolerate 0.25–0.3% fee tiers and rounding
        if out <= 0:
            return False, after - before, {"status": "0x0", "rejected": "pool too shallow: swap output would be 0"}, 0
        o0, o1 = (0, out) if t0.lower() == token.lower() else (out, 0)
        qb = await _bal(f, quote, who)
        sdata = encode_call("swap(uint256,uint256,address,bytes)", [o0, o1, who, b""])
        r2 = await fork.send(who, pool, sdata, fund=False)
        ok = bool(r2 and r2.get("status") == "0x1")
        if not ok:
            r2 = {"status": "0x0", "rejected": "pool swap: " + await _why(f, pool, sdata, who)}
        qa = await _bal(f, quote, who)
        if ok:
            r2["cut"] = tracer.flows(await tracer.trace_tx(f, r.get("transactionHash"), diff=False) or {"transfers": []}, token, who, pool)[2]
        return ok, after - before, r2, (qa - qb) if ok else 0
    data = encode_call("transfer(address,uint256)", [pool, amount])
    r = await fork.send(who, token, data, fund=False)
    ok = bool(r and r.get("status") == "0x1")
    if not ok:
        r = {"status": "0x0", "rejected": "token transfer: " + await _why(f, token, data, who)}
    after = await _bal(f, token, pool)
    if ok:
        r["cut"] = tracer.flows(await tracer.trace_tx(f, r.get("transactionHash"), diff=False) or {"transfers": []}, token, who, pool)[2]
    return ok, (after - before) if ok else 0, r, 0


async def _buy(fork, token, pool, who, amount, v4=None, q_per_t=None):
    """Put `amount` tokens (approx.) into `who`. v4: REAL swap paying quote; else a transfer out of the pool.
    -> (ok, receipt-like)"""
    if v4:
        q = max(1, int(amount * q_per_t))
        await v4hook.fund(fork, v4, who, q)
        res = await v4hook.swap(fork, v4, who, True, q)
        return res["ok"], {"status": "0x1" if res["ok"] else "0x0", "rejected": res.get("why"), "tax": (res.get("hook_pct") or 0) + (res.get("token_tax_pct") or 0) if res["ok"] else None}
    r = await fork.send(pool, token, encode_call("transfer(address,uint256)", [who, amount]))
    return bool(r and r.get("status") == "0x1"), r


async def run(fork, rpc, token, pool, probes, worst_case, dec=18, budget=25, v4=None):
    """-> dict(sizes=[...], wallets=..., powers=[...], verdict=..., flags=[...])"""
    import time
    t0 = time.time()
    f = fork.rpc
    out = {"pool": pool, "sizes": [], "powers": [], "flags": [], "note": None}
    q_per_t = None
    if v4:
        st = await v4hook.pool_state(f, v4.pm, v4.id)
        r0, r1 = v4hook.virtual_reserves(st)
        vt, vq = (r0, r1) if v4.t0 else (r1, r0)
        q_per_t = vq / vt if vt else None
        if not q_per_t:
            v4 = None
    v2 = v4 or await _v2_info(f, token, pool)
    out["method"] = "real v4 swaps through the PoolManager (hook included)" if v4 else ("real pool swap (v2)" if v2 else "token transfer to pool")
    pbal = await _bal(f, token, pool)
    if pbal < 10_000:
        out["note"] = "pool holds no tokens to buy"
        return out
    sizes = [("small", pbal // 2000), ("large", pbal // 50)]
    for buyer in (BUYER, BUYER2):
        await fork.impersonate(buyer, fund=True)
    base = await fork.snapshot()

    for name, amt in sizes:
        if time.time() - t0 > budget:
            out["note"] = "time budget reached"; break
        await fork.revert(base); base = await fork.snapshot()
        row = {"size": name, "buy_amount": amt, "sells": []}
        ok_b, r = await _buy(fork, token, pool, BUYER, amt, v4, q_per_t)
        if not ok_b:
            row["buy"] = "FAILED (" + ("v4 buy reverted: " + str(r.get("rejected")) if v4 else "transfer from pool reverted") + ")"; out["sizes"].append(row); continue
        got = await _bal(f, token, BUYER)
        row["buy"] = "ok"; row["got"] = got; row["buy_tax"] = r["tax"] if v4 else ((1 - got / amt) * 100 if amt else None)
        if v4:
            row["buy_amount"] = amt = got
        elif v2:
            await fork.send(BUYER, pool, encode_call("sync()"), fund=False)
            v2 = await _v2_info(f, token, pool)
        if got == 0:
            row["buy"] = "FUNDS TAKEN: buyer received 0 tokens"; out["sizes"].append(row); continue
        # approval must work (routers need it)
        ra = await fork.send(BUYER, token, encode_call("approve(address,uint256)", [getattr(fork, "_v4helper", None) or pool if v4 else pool, got]), fund=False)
        row["approve"] = _reason(ra)
        pos = await fork.snapshot()
        sched = SCHEDULES if name == "small" else [SCHEDULES[0], SCHEDULES[-2]]  # large size: same block + 1 hour only
        for label, blocks, secs in sched:
            if time.time() - t0 > budget:
                break
            await fork.revert(pos); pos = await fork.snapshot()
            if blocks or secs:
                await fork.mine(blocks, secs)
            held = await _bal(f, token, BUYER)
            entry = {"when": label, "held_before": held}
            if held < got:
                entry["balance_shrank"] = got - held
            ok, recv, rr, qout = await _sell(fork, token, pool, BUYER, held, v2)
            entry["ok"] = ok
            if ok:
                entry["sell_tax"] = max(0.0, (1 - recv / held) * 100) if held else None
                entry["quote_out"] = qout
                if rr.get("cut") and held:
                    entry["cut"] = [(a, 100 * v / held) for a, v in rr["cut"][:3]]
            else:
                if rr and rr.get("rejected"):
                    entry["why"] = rr["rejected"]
                # diagnose: can a half sell go through? (max-sell rules)
                await fork.revert(pos); pos = await fork.snapshot()
                if blocks or secs:
                    await fork.mine(blocks, secs)
                ok2, recv2, _, _ = await _sell(fork, token, pool, BUYER, held // 2, v2)
                entry["half_ok"] = ok2
                if ok2:
                    entry["half_tax"] = (1 - recv2 / (held // 2)) * 100 if held else None
                else:
                    await fork.revert(pos); pos = await fork.snapshot()
                    if blocks or secs:
                        await fork.mine(blocks, secs)
                    ok3, _, r3, _ = await _sell(fork, token, pool, BUYER, held // 100, v2)
                    entry["tiny_ok"] = ok3
                    if not ok3 and r3 and r3.get("rejected"):
                        entry["tiny_why"] = r3["rejected"]
            row["sells"].append(entry)
        out["sizes"].append(row)

    # second wallet: per-wallet discrimination (small buy, sell next block)
    await fork.revert(base); base = await fork.snapshot()
    amt = sizes[0][1]
    ok_b, r = await _buy(fork, token, pool, BUYER2, amt, v4, q_per_t)
    if ok_b:
        if v2 and not v4:
            await fork.send(BUYER2, pool, encode_call("sync()"), fund=False); v2 = await _v2_info(f, token, pool)
        await fork.mine(1)
        ok, recv, _, _ = await _sell(fork, token, pool, BUYER2, await _bal(f, token, BUYER2), v2)
        out["wallet2"] = {"ok": ok}

    # owner powers: buyer holds tokens -> switch each power on -> can the buyer still sell? did his balance change?
    runnable = [p for p in probes if p.get("callers") and "note" not in p][:8]
    for p in runnable:
        if time.time() - t0 > budget + 10:
            break
        who = p["callers"][0]
        tries = list(argsmod.variants(p["sel"], p["fn"], p["args"], who))
        if not tries:
            continue
        await fork.revert(base); base = await fork.snapshot()
        ok_b, r = await _buy(fork, token, pool, BUYER, sizes[0][1], v4, q_per_t)
        if not ok_b:
            break
        held0 = await _bal(f, token, BUYER)
        if v2 and not v4:
            await fork.send(BUYER, pool, encode_call("sync()"), fund=False); v2 = await _v2_info(f, token, pool)
        rp = None
        for data in tries:  # worst case first; capped setters still run with a fallback value
            rp = await fork.send(who, token, data)
            if rp and rp.get("status") == "0x1":
                break
        if not (rp and rp.get("status") == "0x1"):
            out["powers"].append({"fn": p["fn"], "by": who, "ran": False}); continue
        traced = tracer.describe(await tracer.annotate(await tracer.trace_tx(f, rp.get("transactionHash"))), token, {token: "token", pool: "pool", BUYER: "buyer", who: "caller"}, dec)
        await fork.mine(1)
        held1 = await _bal(f, token, BUYER)
        ok, recv, _, _ = await _sell(fork, token, pool, BUYER, held1, v2) if held1 else (False, 0, None, 0)
        out["powers"].append({"fn": p["fn"], "by": who, "ran": True, "buyer_delta": held1 - held0, "sell_ok": ok,
                              "sell_tax": (1 - recv / held1) * 100 if ok and held1 else None, "traced": traced})
    out["elapsed"] = time.time() - t0
    out["flags"] = classify(out)
    return out


def classify(out):
    flags = []
    for row in out["sizes"]:
        if row.get("buy", "").startswith("FUNDS TAKEN"):
            flags.append(("bad", f"{row['size']} buy: {row['buy']}"))
            continue
        if row.get("buy") != "ok":
            flags.append(("warn", f"{row['size']} buy failed")); continue
        if row.get("approve") not in ("ok", None):
            flags.append(("bad", f"approve() reverts ({row['size']} buy) — can't sell through any router"))
        sells = row["sells"]
        oks = [s for s in sells if s["ok"]]
        fails = [s for s in sells if not s["ok"]]
        shrank = [s for s in sells if s.get("balance_shrank")]
        if shrank:
            s = max(shrank, key=lambda x: x["balance_shrank"])
            flags.append(("bad", f"FUNDS TAKEN: buyer balance shrank by {100*s['balance_shrank']/row['got']:.0f}% by {s['when']} without selling"))
        if sells and not oks:
            half = [s for s in sells if s.get("half_ok")]
            tiny = [s for s in sells if s.get("tiny_ok")]
            why = next((s.get("why") for s in sells if s.get("why")), "")
            other_ok = any(x.get("buy") == "ok" and any(y["ok"] or y.get("half_ok") or y.get("tiny_ok") for y in x["sells"]) for x in out["sizes"] if x is not row)
            if half:
                flags.append(("warn", f"{row['size']} buy: full sell blocked, half sell works → max-sell/max-tx rule"))
            elif tiny:
                flags.append(("bad", f"SELL CAP: {row['size']} buy can only be sold in tiny pieces (≤1% per tx; {why or 'reverts'}) — effectively a honeypot"))
            elif other_ok:
                flags.append(("warn", f"{row['size']} buy: can't sell in any context ({why or 'reverts'}) — size limit, not a honeypot"))
            else:
                flags.append(("bad", f"HONEYPOT: {row['size']} buy can never be sold (tested {len(sells)} contexts; {why or 'reverts'})"))
        elif fails:
            # contexts are ordered by time: ok -> later fail = delayed trap; fail -> later ok = cooldown
            first_ok_i = next(i for i, s in enumerate(sells) if s["ok"])
            later_fail = next((s for s in sells[first_ok_i + 1:] if not s["ok"]), None)
            if later_fail:
                flags.append(("bad", f"DELAYED HONEYPOT: {row['size']} buy sells at {sells[first_ok_i]['when']} but is blocked at {later_fail['when']} ({later_fail.get('why') or 'reverts'})"))
            if first_ok_i > 0:
                flags.append(("warn", f"cooldown: {row['size']} buy can't sell until {sells[first_ok_i]['when']} ({sells[0].get('why') or 'reverts'})"))
        taxes = [s["sell_tax"] for s in oks if s.get("sell_tax") is not None]
        if taxes and max(taxes) - min(taxes) > 5:
            flags.append(("warn", f"{row['size']} buy: sell tax changes over time ({min(taxes):.0f}% → {max(taxes):.0f}%)"))
        if taxes and max(taxes) > 25:
            flags.append(("bad", f"{row['size']} buy: sell tax {max(taxes):.0f}%"))
    small = next((r for r in out["sizes"] if r["size"] == "small"), None)
    large = next((r for r in out["sizes"] if r["size"] == "large"), None)
    if small and large and small.get("buy") == "ok" and large.get("buy") == "ok":
        s_ok = any(s["ok"] for s in small["sells"]); l_ok = any(s["ok"] for s in large["sells"])
        if s_ok and not l_ok:
            flags.append(("warn", "size-dependent: small sells work, large (2% of pool) sells are blocked — check max-tx/max-wallet limits"))
    if out.get("wallet2") and out["wallet2"].get("ok") is False and small and any(s["ok"] for s in small.get("sells", [])):
        flags.append(("warn", "per-wallet rule: second fresh wallet could not sell"))
    for p in out["powers"]:
        if not p.get("ran"):
            continue
        if p.get("buyer_delta", 0) < 0:
            flags.append(("bad", f"FUNDS TAKEN by owner: {p['fn']} by {short(p['by'])} removed {-p['buyer_delta']} units from the buyer"))
        if p.get("sell_ok") is False:
            flags.append(("bad", f"SELL BLOCKER: after {p['fn']} by {short(p['by'])} the buyer can't sell"))
        elif p.get("sell_tax") is not None and p["sell_tax"] > 25:
            flags.append(("bad", f"after {p['fn']} by {short(p['by'])} sell tax becomes {p['sell_tax']:.0f}%"))
    return flags


def render(out, dec=18):
    if not out.get("sizes"):
        return [f"== SELL TESTS: {out.get('note') or 'not run'}"]
    where = "simulated blocks" if "no fork" in (out.get("method") or "") else "stateful fork"
    L = [f"== SELL TESTS ({out.get('method','')}, fresh wallet, {where}, pool {short(out['pool'])}" + (f", {out['elapsed']:.1f}s)" if out.get("elapsed") else ")")]
    if out.get("note"):
        L.append(f"   {out['note']}")
    for row in out["sizes"]:
        if row.get("buy") != "ok":
            L.append(f"   {row['size']} buy: {row['buy']}"); continue
        parts = []
        for s in row["sells"]:
            if s["ok"]:
                parts.append(f"{s['when']} OK" + (f" tax {s['sell_tax']:.0f}%" if s.get("sell_tax") else ""))
                if s.get("cut") and s["when"] == "same block":
                    cuts = ", ".join(f"{short(a)} {pct:.1f}%" for a, pct in s["cut"] if pct >= 0.05)
                    if cuts:
                        parts[-1] += f" (cut → {cuts})"
            else:
                parts.append(f"{s['when']} FAIL" + (" (half OK)" if s.get("half_ok") else (" (tiny OK)" if s.get("tiny_ok") else (f" (tiny: {s['tiny_why']})" if s.get("tiny_why") else ""))) + (f" [{s['why']}]" if s.get("why") else ""))
        L.append(f"   {row['size']} buy (tax {row['buy_tax']:.1f}%, approve {row.get('approve')}): " + " · ".join(parts))
    if out.get("wallet2"):
        L.append(f"   second wallet sell: {'OK' if out['wallet2']['ok'] else 'FAIL'}")
    for p in out["powers"]:
        if p.get("ran"):
            L.append(f"   after {p['fn']} by {short(p['by'])}: buyer balance {'+' if p['buyer_delta']>=0 else ''}{p['buyer_delta']}, sell {'OK' if p['sell_ok'] else 'BLOCKED'}" + (f" tax {p['sell_tax']:.0f}%" if p.get('sell_tax') else "") +
                     (f"\n      traced: {p['traced']}" if p.get("traced") else ""))
    for lvl, msg in out.get("flags", []):
        L.append(f"   {'✗' if lvl == 'bad' else '!'} {msg}")
    if not out.get("flags") and out["sizes"]:
        L.append("   ✓ sells worked in every tested context (small buy at 5 later times, large buy at 2, second wallet)")
    return L
