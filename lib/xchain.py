"""Cross-chain activity of a set of wallets (for `dd holders` profiles).

Per wallet, on every chain we have a private node for:
  activity   tx count now and 7 / 30 / 90 days ago (eth_getTransactionCount at past blocks: one batch per chain)
  coins      tokens it received / sent in the last 7 days on each chain where it was active (one multi-wallet
             Transfer log scan per chain and direction), with today's state on DexScreener
  bridges    its cross-chain swaps from the Relay and Across APIs (by user / depositor): when, from -> to, amount
Only reads; nothing is guessed. Wallets that trade through an app relayer have tx count 0 everywhere (their trades
still show up as token transfers).
"""
import asyncio, os, time
from collections import Counter, defaultdict
from lib.rpc import RPC, CHAINS, ROOT, TRANSFER, pad_addr, topic_addr
from lib import explorer
from lib.trace import _get as tget

SOLANA_ID = 792703809
EXTRA_IDS = {1: "eth", 10: "op", 56: "bsc", 137: "polygon", 8453: "base", 42161: "arb", 43114: "avax", 999: "hyper",
             SOLANA_ID: "solana", 7565164: "solana", 324: "zksync", 59144: "linea", 81457: "blast", 534352: "scroll",
             130: "unichain", 2741: "abstract", 80094: "berachain", 146: "sonic", 1868: "soneium", 57073: "ink", 143: "monad",
             99901: "elysium", 4326: "mega", 1337: "hyperliquid", 9745: "plasma", 8333: "bitcoin", 728126428: "tron",
             9286185: "eclipse", 33139: "apechain", 2020: "ronin", 42220: "celo", 5000: "mantle", 34443: "mode", 7777777: "zora"}
BORING = {"USDC", "USDT", "USDC.E", "USDBC", "DAI", "WETH", "ETH", "WBNB", "BNB", "WAVAX", "AVAX", "WHYPE", "HYPE", "WMON",
          "MON", "USDT0", "USD₮0", "BUSD", "FDUSD", "USDE", "WBTC", "CBBTC", "BTCB", "STETH", "WSTETH", "CBETH", "RETH",
          "USDG", "$USDG", "USGD", "PYUSD", "USD1", "RLUSD", "USDS", "SUSDE", "GHO", "LUSD", "FRAX", "EURC"}


def chain_name(cid):
    try:
        cid = int(cid)
    except Exception:
        return str(cid)
    for k, v in CHAINS.items():
        if v.get("id") == cid:
            return k
    if cid in EXTRA_IDS:
        return EXTRA_IDS[cid]
    if 100_000_000 < cid < 101_000_000:
        return f"deBridge-chain-{cid - 100_000_000}"  # deBridge's own ids for non-EVM-id chains
    return str(cid)


def private_chains():
    return [c for c in CHAINS if os.path.exists(os.path.join(ROOT, "keys", f"rpc_{c}"))]


async def _activity(ch, addrs, now):
    """-> {addr: {'total','d7','d30','d90'}} for one chain, or None if the node can't answer old-block nonces."""
    from lib.lp import estimate_block
    rpc = RPC(ch)
    latest = await rpc.block_number()
    bl = {}
    for k, days in (("d7", 7), ("d30", 30), ("d90", 90)):
        b, _ = await estimate_block(rpc, now - days * 86400, latest)
        bl[k] = b + 2000  # estimate_block keeps a 2000-block safety margin before ts; undo it for the count
    calls = [("eth_getTransactionCount", [a, "latest"]) for a in addrs]
    for k in ("d7", "d30", "d90"):
        calls += [("eth_getTransactionCount", [a, hex(min(bl[k], latest))]) for a in addrs]
    r = await rpc.batch(calls)
    n = len(addrs)
    out = {}
    for i, a in enumerate(addrs):
        tot = int(r[i], 16) if r[i] else None
        if tot is None:
            continue
        x = {"total": tot}
        for j, k in enumerate(("d7", "d30", "d90")):
            v = r[n * (j + 1) + i]
            x[k] = tot - int(v, 16) if v else None
        out[a] = x
    return out, {"latest": latest, **bl}


async def _coins(ch, addrs, from_block, to_block):
    """Token transfers in/out of the wallets on one chain in [from_block, to_block] -> {addr: {token: {'in','out'}}}"""
    rpc = RPC(ch)
    pads = [pad_addr(a) for a in addrs]
    res = {a: defaultdict(lambda: {"in": 0, "out": 0}) for a in addrs}
    ins, outs = await asyncio.gather(rpc.get_logs(None, [TRANSFER, None, pads], from_block=from_block, to_block=to_block),
                                     rpc.get_logs(None, [TRANSFER, pads], from_block=from_block, to_block=to_block))
    aset = set(addrs)
    for l in ins:
        if len(l.get("topics") or []) == 3:
            a = topic_addr(l["topics"][2]).lower()
            if a in aset:
                res[a][l["address"].lower()]["in"] += 1
    for l in outs:
        if len(l.get("topics") or []) == 3:
            a = topic_addr(l["topics"][1]).lower()
            if a in aset:
                res[a][l["address"].lower()]["out"] += 1
    return res


async def _bridges(addr):
    """Relay requests by user + Across deposits by depositor -> list of {time, src, dst, amount, symbol, usd, via}."""
    out = []
    from lib.trace import relay_requests, relay_parse
    r, a, dl = await asyncio.gather(relay_requests(user=addr, limit=20),
                                    tget("https://app.across.to/api/deposits", {"depositor": addr, "limit": 20}),
                                    tget("https://stats-api.dln.trade/api/Orders/filteredList", method="POST",
                                         body={"skip": 0, "take": 20, "filter": addr}))
    for o in (dl or {}).get("orders") or []:
        g, t = o.get("giveOfferWithMetadata") or {}, o.get("takeOfferWithMetadata") or {}
        gm = g.get("metadata") or {}
        try:
            amt = int((g.get("amount") or {}).get("bigIntegerValue") or 0) / 10 ** int(gm.get("decimals") or 18)
        except Exception:
            amt = None
        cid = lambda x: int((x.get("chainId") or {}).get("bigIntegerValue") or 0)
        out.append({"time": o.get("creationTimestamp"), "src": chain_name(cid(g)), "dst": chain_name(cid(t)),
                    "amount": f"{amt:.4g}" if amt is not None else None, "symbol": gm.get("symbol"),
                    "out_symbol": (t.get("metadata") or {}).get("symbol"), "usd": 0.0, "via": "deBridge"})
    for x in r or []:
        if x.get("status") != "success":
            continue
        p = relay_parse(x)
        try:
            usd = float(p["usd"] or 0)
        except Exception:
            usd = 0.0
        out.append({"time": p["time"], "src": chain_name(p["src_chain"]), "dst": chain_name(p["dst_chain"]), "amount": p["amount"],
                    "symbol": p["symbol"], "out_symbol": p["out_symbol"], "usd": usd, "via": "Relay"})
    from lib.trace import _decimals
    for x in a if isinstance(a, list) else []:
        if x.get("status") not in ("filled", None):
            continue
        try:
            ts = int(time.mktime(time.strptime((x.get("depositBlockTimestamp") or "")[:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
        except Exception:
            ts = None
        try:
            dec = await _decimals(x.get("originChainId"), x.get("inputToken"))
            amt = int(x.get("inputAmount") or 0) / 10 ** dec
        except Exception:
            amt = None
        usd = float(x.get("inputPriceUsd") or 0) * (amt or 0)
        out.append({"time": ts, "src": chain_name(x.get("originChainId")), "dst": chain_name(x.get("destinationChainId")),
                    "amount": f"{amt:.4g}" if amt is not None else None, "symbol": "" if not usd else "", "out_symbol": None,
                    "usd": usd, "via": "Across"})
    out.sort(key=lambda b: -(b["time"] or 0))
    read = [n for n, v in (("Relay", r), ("Across", a), ("deBridge", dl)) if v is not None]
    return {"list": out, "read": read}


async def collect(home, addrs, now=None):
    """-> {addr: {'chains': {chain: activity}, 'coins': {chain: [...]}, 'bridges': [...]}}, notes"""
    now = now or int(time.time())
    from lib.wprofile import _ds_tokens
    notes = []
    chains = private_chains()
    act = {}
    res = await asyncio.gather(*[asyncio.wait_for(_activity(ch, addrs, now), 40) for ch in chains], return_exceptions=True)
    blocks = {}
    for ch, r in zip(chains, res):
        if isinstance(r, Exception):
            notes.append(f"activity on {ch} not read ({type(r).__name__})")
            continue
        act[ch], blocks[ch] = r
    # coins in the last 7 days, only on other chains where at least one wallet was active
    scan = [ch for ch in act if ch != home and any((v.get("d7") or 0) > 0 for v in act[ch].values())]
    cres = await asyncio.gather(*[asyncio.wait_for(_coins(ch, [a for a in addrs if (act[ch].get(a, {}).get("d7") or 0) > 0 or True],
                                                        blocks[ch]["d7"], blocks[ch]["latest"]), 60) for ch in scan], return_exceptions=True)
    coins = {}
    for ch, r in zip(scan, cres):
        if isinstance(r, Exception):
            notes.append(f"7-day coins on {ch} not read ({type(r).__name__})")
            continue
        coins[ch] = r
    infos = await asyncio.gather(*[_ds_tokens(ch, sorted({t for a in coins[ch] for t in coins[ch][a]})) for ch in coins])
    info = dict(zip(coins, infos))
    sem = asyncio.Semaphore(3)

    from lib import cache

    async def br(a):
        k = f"bridges:{a}"
        c = cache.get(k)
        if c is not None:
            return c
        async with sem:
            try:
                v = await asyncio.wait_for(_bridges(a), 25)
            except Exception:
                return None
        if len(v["read"]) >= 2:  # Across / deBridge rate-limit bursts: only remember answers from most sources
            cache.set(k, v, ttl=3600)
        return v
    brs = await asyncio.gather(*[br(a) for a in addrs])
    spray = {}
    for ch in coins:
        n_in, n_out = Counter(), Counter()
        for a in coins[ch]:
            for t, io in coins[ch][a].items():
                n_in[t] += 1 if io["in"] else 0
                n_out[t] += 1 if io["out"] else 0
        spray[ch] = {t for t in n_in if n_in[t] >= 4 and n_out[t] == 0}
    out = {}
    for a, b in zip(addrs, brs):
        o = {"chains": {ch: act[ch][a] for ch in act if a in act[ch]}, "coins": {}, "bridges": (b or {}).get("list"),
             "bridges_read": (b or {}).get("read") or []}
        for ch in coins:
            rows = []
            for t, io in coins[ch].get(a, {}).items():
                i = info[ch].get(t) or {}
                sym = i.get("symbol")
                if (sym and sym.upper().lstrip("$") in {x.lstrip("$") for x in BORING}) or t in spray[ch]:
                    continue
                if not io["out"] and (not i or (i.get("liq") or 0) < 1000):
                    continue  # received, never moved, no live pool: an airdrop, not a trade
                state = "no pool" if not i else ("dead" if (i.get("liq") or 0) < 1000 else "alive")
                if state == "alive" and (i.get("mcap") or 0) > (i.get("liq") or 0) * 1000:
                    i = {**i, "mcap": None}  # market cap the pool can't back (fake supply / thin pool)
                rows.append({"token": t, "symbol": sym or "?", "in": io["in"], "out": io["out"], "state": state, "mcap": i.get("mcap"),
                             "liq": i.get("liq")})
            # a token it only received and never moved, with no pool, is usually an airdrop: keep traded ones first
            rows.sort(key=lambda x: (x["state"] == "no pool" and not x["out"], -(x["mcap"] or 0)))
            if rows:
                o["coins"][ch] = rows
        out[a] = o
    return out, notes


def _amt(a):
    try:
        v = float(a)
    except Exception:
        return a or ""
    return f"{v:,.4g}" if v < 1e6 else f"{v:,.0f}"


def _ago(s):
    return f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.0f} d"


def _usd(v):
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}k" if v >= 1e3 else f"${v:,.0f}"


def render(home, x, now):
    if not x:
        return []
    L = []
    ch = x.get("chains") or {}
    act = sorted(((c, v) for c, v in ch.items() if v.get("total")), key=lambda cv: -((cv[1].get("d30") or 0), cv[1]["total"])[0])
    if act:
        L.append("     chains (txs total / last 30 d / last 7 d): " + " · ".join(
            f"{c}{'*' if c == home else ''} {v['total']:,}/{v.get('d30') or 0}/{v.get('d7') or 0}" for c, v in act))
        busiest = max(act, key=lambda cv: cv[1].get("d30") or 0)
        h30 = (ch.get(home) or {}).get("d30") or 0
        if busiest[0] != home and (busiest[1].get("d30") or 0) > max(10, 2 * h30):
            L.append(f"     ** MAINLY ACTIVE ON {busiest[0].upper()} ({busiest[1]['d30']} txs in 30 d vs {h30} on {home}) **")
    else:
        L.append("     chains: no own txs on any chain we read (trades only through an app relayer)")
    for c, rows in (x.get("coins") or {}).items():
        traded = [r for r in rows if r["out"] or r["state"] != "no pool"]
        if not traded:
            continue
        alive = sum(1 for r in traded if r["state"] == "alive")
        L.append(f"     {c} coins last 7 d: {len(traded)} ({alive} alive) — " + ", ".join(
            f"{r['symbol']} " + ((_usd(r['mcap']) if r['mcap'] else "thin pool") if r["state"] == "alive" else r["state"]) + (" sold" if r["out"] else "")
            for r in traded[:7]))
    b = x.get("bridges")
    read = x.get("bridges_read") or []
    miss = [n for n in ("Relay", "Across", "deBridge") if n not in read]
    tail = f" (not read: {', '.join(miss)})" if miss else ""
    if b:
        tot = sum(r["usd"] for r in b)
        routes = Counter(f"{r['src']}→{r['dst']}" for r in b)
        last = b[0]
        vias = "/".join(sorted({r["via"] for r in b}))
        L.append(f"     cross-chain swaps: {len(b)} ({vias}) " + (f"· ${tot:,.0f} moved where priced " if tot else "") + "· routes: "
                 + ", ".join(f"{k} ×{v}" for k, v in routes.most_common(4))
                 + f" · last: {last['src']}→{last['dst']} {_amt(last['amount'])} {last['symbol'] or ''}".rstrip()
                 + (f" (${last['usd']:,.0f})" if last["usd"] else "")
                 + (f", {_ago(now - last['time'])} ago" if last.get("time") else "") + tail)
    elif read:
        L.append(f"     cross-chain swaps: none on {', '.join(read)}{tail}")
    else:
        L.append("     cross-chain swaps: not read (bridge APIs rate-limited or down)")
    return L
