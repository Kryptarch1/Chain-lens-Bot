"""Who is behind a token: launch tx, launcher contract, template, dev wallet profile, hook source summary.
Everything runs in parallel and returns plain dicts; render() turns it into a few compact lines.
"""
import asyncio, re, sys, time
from lib import sigs
from lib.rpc import ZERO


async def _fn_name(sel):
    n = await sigs.names_for([sel])
    return n.get(sel) or sel


def _owner_only_fns(src):
    """From verified source: state-changing external/public functions guarded by onlyOwner/onlyRole/admin checks."""
    out = []
    for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)([^{;]*)", src):
        name, mods = m.group(1), m.group(3)
        if "view" in mods or "pure" in mods:
            continue
        if re.search(r"only(Owner|Admin|Role|Operator|Governance|Authorized|Manager)|auth\b|requiresAuth", mods):
            out.append(name)
    return sorted(set(out))


def _hook_swap_fee(src):
    """Hardcoded per-swap fee in a hook's own source, e.g. 'fee=basis*400/10000' -> 4.0 (%). None if not found."""
    import json as _json
    try:
        j = _json.loads(src[1:-1]) if src.startswith("{{") else _json.loads(src)
        own = "\n".join(v["content"] for k, v in j.get("sources", {}).items() if not k.startswith("@") and "node_modules" not in k)
    except Exception:
        own = src
    hits = re.findall(r"fee\s*=\s*\w+\s*\*\s*(\d{1,4})\s*/\s*10000", own, re.I)
    return max(int(h) for h in hits) / 100 if hits else None


def _fee_constants(src):
    out = []
    for m in re.finditer(r"(?:constant|immutable)[^;=]*?\b(\w*(?:FEE|Fee|fee|TAX|Tax|tax|BPS)\w*)\s*(?:=\s*([^;]+))?;", src):
        out.append(f"{m.group(1)}{'=' + m.group(2).strip()[:20] if m.group(2) else ''}")
    return out[:6]


async def launch_info(rpc, ex, info, light=False):
    """Decode the token's creation tx: who sent it, to which contract, which function, value."""
    h = info.get("created_tx")
    if not h:
        return {}
    tx = await rpc.tx(h)
    if not tx:
        return {}
    sel = (tx.get("input") or "0x")[:10]
    to = tx.get("to")
    out = {"tx": h, "from": tx["from"], "to": to, "value": int(tx["value"], 16), "fn": await _fn_name(sel) if len(sel) == 10 else "create",
           "block": int(tx["blockNumber"], 16) if tx.get("blockNumber") else None}
    if to and not light:
        await launch_target(rpc, ex, out)
    return out


async def launch_target(rpc, ex, out):
    """Name of the contract the launch tx called (factory / launchpad) — separate so it runs in parallel."""
    src, code = await asyncio.gather(ex.source(out["to"]), rpc.code(out["to"]))
    out["to_name"] = src.get("name") or ("unverified contract" if len(code) > 2 else "EOA")
    out["to_verified"] = src.get("verified")


async def template_info(rpc, ex, info):
    impl = info.get("clone") or info.get("impl")
    if not impl:
        return {}
    src, cr = await asyncio.gather(ex.source(impl), ex.creator(impl))
    return {"address": impl, "name": src.get("name"), "verified": src.get("verified"), "creator": (cr or {}).get("creator")}


async def dev_profile(rpc, ex, dev, token):
    """Funding, age, balance, recent deploys/launches from the explorer (Etherscan/Blockscout)."""
    if not dev:
        return {}
    bal, nonce, fund, txs = await asyncio.gather(rpc.balance(dev), rpc.nonce(dev), ex.funded_by(dev), ex.txlist(dev, limit=10000))  # full history: age, deploys, past coins
    out = {"address": dev, "balance": bal, "nonce": nonce, "funded_by": fund, "history": bool(txs)}
    if txs:
        ts = [int(t.get("timeStamp") or 0) for t in txs if str(t.get("timeStamp") or "").isdigit()]
        out["first_seen"] = min(ts) if ts else None
        creates = [t for t in txs if not t.get("to") or t.get("contractAddress")]
        launches = [t for t in txs if re.search(r"launch|create|deploy|mint\(string", (t.get("functionName") or ""), re.I)]
        out["deploys"] = len(creates)
        if len(txs) >= 1000 and (nonce or 0) > len(txs):  # explorer returned only the newest 1000: count is a floor
            out["deploys_floor"] = True
            try:
                old = await ex.es("account", "txlist", address=dev, sort="asc", page=1, offset=1)
                if old and str(old[0].get("timeStamp") or "").isdigit():
                    out["first_seen"] = min(out["first_seen"] or 1e12, int(old[0]["timeStamp"]))
            except Exception:
                pass
        out["launch_calls"] = [{"fn": (t.get("functionName") or "")[:40], "to": t.get("to"), "time": int(t.get("timeStamp") or 0), "hash": t.get("hash")} for t in launches[:5]]
        outs = [t for t in txs if (t.get("from") or "").lower() == dev.lower() and int(t.get("value") or 0) > 0 and t.get("to")]
        out["eth_out"] = sum(int(t["value"]) for t in outs)
    async def funder():
        if fund.get("from"):
            fsrc, fnonce = await asyncio.gather(ex.source(fund["from"]), rpc.nonce(fund["from"]))
            out["funder_label"] = fsrc.get("name") or (f"wallet with {fnonce:,} txs" + (" (exchange/service?)" if fnonce > 5000 else ""))

    async def coins():
        try:
            out["past_coins"] = await asyncio.wait_for(past_coins(rpc, ex, dev, token, txs), 4)
        except Exception:
            out["past_coins"] = None  # unknown, not "none"
    await asyncio.gather(funder(), coins())
    return out


LP_SYMBOLS = ("UNI-V2", "CAKE-LP", "SLP", "UNI-V3-POS", "CAMELOT", "APE-LP", "ALP", "BLP", "SUSHI")


async def past_coins(rpc, ex, dev, token, txs):
    """Every earlier token this dev launched — including ones created THROUGH their own launch contract (a token minted
    from 0x0 inside a dev tx) — with name, symbol, CA, launch time and what it looks like now. Newest first, max 8.
    Cost: one receipt batch, one name/symbol batch, one DexScreener call."""
    from lib.rpc import TRANSFER
    from lib.evm import encode_call, decode1
    from lib import explorer as exmod
    mine = [t for t in txs if (t.get("from") or "").lower() == dev.lower()]
    if not mine:
        return []
    found, minted = {}, {}  # ca -> launch time; minted-only ca -> block of the dev tx
    for t in mine:  # direct deploys over the WHOLE history (txlist has contractAddress, no receipt needed)
        ca = (t.get("contractAddress") or "").lower()
        if ca and ca != token.lower():
            found.setdefault(ca, int(t.get("timeStamp") or 0))
    mine = mine[:40]  # launch-contract mints: receipts of the latest dev txs only
    rcs = await rpc.batch([("eth_getTransactionReceipt", [t["hash"]]) for t in mine])
    for t, r in zip(mine, rcs):
        if not r:
            continue
        ts = int(t.get("timeStamp") or 0)
        if r.get("contractAddress"):
            ca = r["contractAddress"].lower()
            if ca != token.lower():
                found.setdefault(ca, ts)
        for lg in r.get("logs", []):  # ERC-20 mint = Transfer(0x0 -> x) with 3 topics (4 topics = an NFT)
            if lg.get("topics") and lg["topics"][0] == TRANSFER and len(lg["topics"]) == 3 and int(lg["topics"][1], 16) == 0:
                ca = lg["address"].lower()
                if ca != token.lower() and ca not in found and ca not in minted:
                    minted[ca] = (int(r["blockNumber"], 16), ts)
    # a mint inside a dev tx is the dev's coin only if the token was born in that block (launch contract);
    # existing tokens minted TO the dev (DAI, USDS, vault shares) already had code one block earlier
    mc = list(minted)[:20]
    if mc:
        codes = await rpc.batch([("eth_getCode", [ca, hex(minted[ca][0] - 1)]) for ca in mc])
        for ca, c in zip(mc, codes):
            if c is not None and (c or "0x") in ("0x", "0x0"):
                found.setdefault(ca, minted[ca][1])
    cas = list(found)[:60]
    if not cas:
        return []
    reads = await rpc.batch([("eth_call", [{"to": ca, "data": encode_call(fn)}, "latest"]) for ca in cas for fn in ("name()", "symbol()")])
    coins = []
    for i, ca in enumerate(cas):
        try:
            name, sym = decode1("string", reads[2 * i]), decode1("string", reads[2 * i + 1])
        except Exception:
            continue  # not an ERC-20 (a pair, a hook, a locker)
        if not sym or sym.upper() in LP_SYMBOLS or "LP" in sym.upper().split("-"):
            continue
        coins.append({"ca": ca, "name": name, "symbol": sym, "launched": found[ca]})
    coins = coins[:30]  # DexScreener takes 30 addresses per call
    if coins:  # one DexScreener call for all of them: live pools, market cap, liquidity
        d = await exmod._get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(c["ca"] for c in coins), timeout=6)
        best = {}
        for pr in (d or {}).get("pairs") or []:
            ca = (pr.get("baseToken") or {}).get("address", "").lower()
            liq = float((pr.get("liquidity") or {}).get("usd") or 0)
            if liq > best.get(ca, {}).get("liq", -1):
                best[ca] = {"liq": liq, "mcap": float(pr.get("fdv") or pr.get("marketCap") or 0)}
        for c in coins:
            b = best.get(c["ca"])
            c["liq"], c["mcap"] = (b["liq"], b["mcap"]) if b else (0.0, 0.0)
            if not b or b["liq"] < 500:
                c["state"], c["dead"] = "no pool / dead", True
            elif b["mcap"] <= b["liq"] * 1.15:  # market cap = the liquidity itself: nobody holds it any more
                c["state"], c["dead"] = f"dead: mcap ${b['mcap']:,.0f} ≈ its own liquidity (no holders left)", True
            else:
                c["state"], c["dead"] = f"live: mcap ${b['mcap']:,.0f}, liq ${b['liq']:,.0f}", False
    return coins


FEE_GETTER_WORDS = ("fee", "tax", "bps")
NOT_GETTER = ("set", "redeem", "claim", "collect", "withdraw", "lower", "raise", "update", "take", "distribute", "swap", "sync", "accrue", "harvest", "skim")


async def fee_reads(rpc, hook):
    """Read every no-argument fee-like getter of the hook (works on unverified hooks): {'currentFeeBps': 150, ...}"""
    from lib import sigs
    rows = sigs.selectors_from_bytecode(await rpc.code(hook))
    names = await sigs.names_for([r[0] for r in rows])
    tg = [(sel, names[sel].split("(")[0]) for sel, args, mut in rows
          if args == "" and names.get(sel) and mut not in ("payable",) and any(w in names[sel].lower() for w in FEE_GETTER_WORDS)
          and not names[sel].lower().startswith(NOT_GETTER)][:12]
    if not tg:
        return {}
    res = await rpc.batch([("eth_call", [{"to": hook, "data": sel}, "latest"]) for sel, _ in tg])
    out = {}
    for (sel, nm), r in zip(tg, res):
        if r and len(r) == 66:
            v = int(r, 16)
            if v < 10 ** 12 and not (v in (100, 10_000, 1_000_000) and nm.isupper() and "MAX" not in nm):  # skip denominators
                out[nm] = v
    return dict(sorted(out.items(), key=lambda kv: kv[0].isupper()))  # live values (camelCase) before CONSTANTS


async def hook_info(rpc, ex, lps):
    hooks = [l["hook"]["address"] for l in lps if l.get("hook")]
    if not hooks:
        return {}
    h = hooks[0]
    src, fr = await asyncio.gather(ex.source(h), fee_reads(rpc, h))
    s = src.get("source", "")
    return {"address": h, "name": src.get("name"), "verified": src.get("verified"),
            "owner_only": _owner_only_fns(s) if s else None, "fees": _fee_constants(s) if s else [],
            "swap_fee_pct": _hook_swap_fee(s) if s else None, "fee_reads": fr}


async def gather_people(rpc, ex, token, info):
    """Launch tx, template and dev profile — independent of the LP analysis, so it runs in parallel with it."""
    dev = info.get("creator")
    launch = await launch_info(rpc, ex, info, light=True)  # only the tx: the dev is known now, the rest runs below
    if launch.get("from") and launch["from"].lower() != (dev or "").lower():
        dev = launch["from"]  # creator was a factory/launcher contract -> the human is whoever sent the launch tx
    from lib import trace as tracemod, xscan, testnets
    async def money_trail():
        hops = 3 if "--deep" in sys.argv else 2  # a 3rd hop rarely changes the picture and costs ~2 s
        return await tracemod.trace(rpc.name, dev, max_hops=hops) if dev else []
    async def test_history():
        return await asyncio.wait_for(testnets.scan(dev, info.get("impl_code") or info.get("code")), 15) if dev else {}
    async def target():
        if launch.get("to"):
            await launch_target(rpc, ex, launch)

    async def launch_time():
        return int((await rpc.block(launch["block"]))["timestamp"], 16) if launch.get("block") else None
    res = await asyncio.gather(template_info(rpc, ex, info), dev_profile(rpc, ex, dev, token), money_trail(),
                               xscan.lookup(rpc.name, token), test_history(), farcaster_ids([dev]), target(), launch_time(), return_exceptions=True)
    tpl, devp, trail, xs, tn, fc, _, launch_ts = [r if not isinstance(r, Exception) else {} for r in res]
    launch_ts = launch_ts if isinstance(launch_ts, int) else None
    fc = dict(fc or {})
    wallets = [(h.get("bridge") or {}).get("sender") or h.get("funder") for h in trail or [] if not h.get("end")]
    wallets = [a for a in wallets if a and a.lower() not in fc]
    if wallets:  # funders of the dev: one more batched (cached) call, after the trail is known
        try:
            fc.update(await farcaster_ids(wallets))
        except Exception:
            pass
    found = []
    for role, a in [("dev", dev)] + [(f"funder{'' if i == 0 else f' (hop {i+1})'}", w) for i, w in enumerate(
            (h.get("bridge") or {}).get("sender") or h.get("funder") for h in trail or [] if not h.get("end"))]:
        if a and fc.get(a.lower()):
            found.append((role, a, fc[a.lower()]))
    return {"launch": launch, "template": tpl, "dev": devp, "trail": trail or [], "xscan": xs or {}, "testnets": tn or {},
            "launch_ts": launch_ts, "farcaster": found, "farcaster_checked": dev is not None and dev.lower() in fc}


async def farcaster_ids(addrs):
    """Farcaster accounts that verified these wallets (time-boxed: optional data)."""
    from lib import farcaster
    addrs = [a for a in addrs if a]
    return await asyncio.wait_for(farcaster.lookup(addrs), 3.5) if addrs else {}


async def gather_all(rpc, ex, token, info, lps):
    w = await gather_people(rpc, ex, token, info)
    w["hook"] = await hook_info(rpc, ex, lps)
    return w


def render(w, ex, sym):
    if not w:
        return []
    L = ["== WHO"]
    la, tp, d, hk = w.get("launch") or {}, w.get("template") or {}, w.get("dev") or {}, w.get("hook") or {}
    if la:
        L.append(f"   launched by {la['from']} via {la.get('to_name') or '-'} {la.get('to') or ''} · {la['fn'][:60]} · paid {la['value']/1e18:.4g} {sym}")
    if tp:
        L.append(f"   template {tp['address']} ({tp.get('name') or 'unverified'}) made by {tp.get('creator') or '?'}")
    if d:
        age = f"{(time.time()-d['first_seen'])/86400:.1f}d old" if d.get("first_seen") else "age ?"
        fb = d.get("funded_by") or {}
        L.append(f"   dev {d['address']}: {d['nonce']} txs, {d['balance']/1e18:.4g} {sym}, {age}" +
                 (f", {d['deploys']}{'+' if d.get('deploys_floor') else ''} contract deploys" if d.get("deploys") is not None else "") +
                 (f", sent out {d['eth_out']/1e18:.4g} {sym}" if d.get("eth_out") else ""))
        trail = w.get("trail") or []
        if trail:
            from lib import trace as tracemod
            L.append(f"   money trail: {tracemod.one_line(trail)}")
            first = next((h for h in trail if h.get("tx")), None)
            if first and first.get("bridge"):
                L.append(f"     bridge in-tx {first['bridge'].get('in_tx')} · payout {first['tx']}")
            elif first:
                L.append(f"     first funding tx {first['tx']}")
        elif fb.get("from"):
            L.append(f"   funded by {fb['from']} ({d.get('funder_label','?')})" + (f" tx {fb['tx']}" if fb.get('tx') else ""))
        elif not d.get("history"):
            L.append("   dev history: not available on this chain's explorer key")
        pc = d.get("past_coins")
        if pc:
            L.append(f"   earlier coins by this dev ({len(pc)}{'+' if len(pc) >= 30 else ''}):")
            for c in pc[:8]:
                when = time.strftime("%m-%d %H:%M", time.gmtime(c["launched"])) if c.get("launched") else "?"
                L.append(f"     {c['name']} ({c['symbol']}) {c['ca']} · launched {when} · {c.get('state', '?')}")
            if len(pc) > 8:
                L.append(f"     … {len(pc) - 8} more ({sum(1 for c in pc[8:] if c.get('dead'))} dead)")
        elif pc is None and d.get("history"):
            L.append("   earlier coins by this dev: could not be checked (receipts unavailable)")
        for lc in d.get("launch_calls", [])[:3]:
            L.append(f"   earlier: {time.strftime('%m-%d %H:%M', time.gmtime(lc['time']))} {lc['fn']} → {lc['to']}")
    from lib import xscan, testnets, farcaster
    L.extend(farcaster.render(w.get("farcaster") or []))
    L.extend(xscan.render(w.get("xscan")))
    L.extend(testnets.render(w.get("testnets"), w.get("launch_ts")))
    if hk and hk.get("swap_fee_pct"):
        L.append(f"   ! pool hook takes {hk['swap_fee_pct']:g}% of every swap (on top of any token tax)")
    if hk:
        L.append(f"   hook {hk['address']} {hk.get('name') or 'unverified'}" +
                 (f" · owner-only: {', '.join(hk['owner_only'][:6]) or 'none'}" if hk.get("owner_only") is not None else "") +
                 (f" · {', '.join(hk['fees'])}" if hk.get("fees") else "") +
                 (f" · fee getters now: {', '.join(f'{k}={v}' for k, v in list(hk['fee_reads'].items())[:4])}" if hk.get("fee_reads") else ""))
    return L
