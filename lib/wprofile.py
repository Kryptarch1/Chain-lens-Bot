"""Deep wallet profiles for `dd holders` (the top holders of one token).

Per wallet, from its own history (Etherscan txlist + tokentx, cached) and live reads:
  activity   age, tx count, txs in the 30 days before this buy, last own tx before this buy (dormancy), deployed contracts
  this buy   ETH paid, approx entry market cap, size vs the wallet's usual buy (median of its past swaps)
  past coins distinct tokens it really traded (it signed the tx or later sent the token out), first-seen time,
             today's state on DexScreener (alive + mcap / dead / no pool), early entries (bought within 1 h of the pool),
             coins now worth >= $1M it was early on
  wealth     native + USDC/USDT on eth, base, bsc, arb (live balances, priced)
  holdings   current balances of tokens seen in its history, priced and capped at the token's pool liquidity
  money      largest native move, stablecoin transfers >= $5k
  protocols  the contracts it calls most, named from verified source
Everything is a fact read from the chain or the explorer; labels are added by the analyst, not here.
"""
import asyncio, re, statistics, time
from collections import Counter, defaultdict
from lib.rpc import RPC
from lib.evm import encode_call, decode1
from lib import explorer, cache

STABLES = {  # chain -> {token: (symbol, decimals)}
    "eth": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6), "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
            "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18)},
    "base": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6)},
    "arb": {"0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("USDC", 6), "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": ("USDT", 6)},
    "bsc": {"0x55d398326f99059ff775485246999027b3197955": ("USDT", 18), "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18)},
    "avax": {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": ("USDC", 6), "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": ("USDT", 6)},
}
WRAPPED = {"eth": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "base": "0x4200000000000000000000000000000000000006",
           "arb": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "bsc": "0xbb4cdb9cbd36b01bd1cbaead2de08d9173bc095c",
           "avax": "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7"}
WEALTH_CHAINS = ("eth", "base", "arb", "bsc", "avax")
KNOWN = {  # well-known contracts (names the explorer would also give, saved a call)
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router", "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router",
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap Universal Router (v4)", "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3 Router",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap SwapRouter02", "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6", "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "OpenSea Seaport 1.5", "0x0000000000000068f116a894984e2db1123eb395": "OpenSea Seaport 1.6",
    "0x000000000000ad05ccc4f10045630fb830b95127": "Blur", "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "Lido stETH",
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2": "Aave v3 Pool", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI", "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
}


# Tokenized stocks / funds (NVDAB, TSLAB, ANTc, SPCXon, FXIon...) are priced like the real asset: a wallet buying one
# in its first hour is not a gem call. Two generic tells, no coin list: a legal-entity / fund name, or a stock-like unit
# price with a tiny supply (memecoins launch at fractions of a cent with 100M+ supply).
_RWA_NAME = re.compile(r"(\b(inc|corp|corporation|pbc|ltd|plc|llc|etf|trust|fund|holdings)\b\.?\s*$|tokeni[sz]ed|xstock|\bondo\b)", re.I)


def is_rwa(i):
    name = (i.get("name") or "").strip().rstrip(")").strip()
    if _RWA_NAME.search(name) or _RWA_NAME.search(i.get("name") or ""):
        return True
    price, mcap = i.get("price") or 0, i.get("mcap") or 0
    return price >= 20 and mcap and mcap / price < 10_000_000


def tight_groups(pairs, score):
    """Groups in which EVERY two members are a strong pair (complete linkage), strongest pairs first. Union-find chained
    a whole trench crowd through one weak link each (19 wallets on CLNS, base): A~B and B~C does not make A~C."""
    ok = set(pairs) | {(b, a) for a, b in pairs}
    gid, groups = {}, []
    for a, b in sorted(pairs, key=score, reverse=True):
        ga, gb = gid.get(a), gid.get(b)
        if ga is None and gb is None:
            groups.append([a, b]); gid[a] = gid[b] = len(groups) - 1
        elif ga is not None and gb is not None:
            if ga != gb and all((x, y) in ok for x in groups[ga] for y in groups[gb]):
                for x in groups[gb]:
                    gid[x] = ga
                groups[ga] += groups[gb]; groups[gb] = []
        else:
            g, x = (ga, b) if ga is not None else (gb, a)
            if all((x, y) in ok for y in groups[g]):
                groups[g].append(x); gid[x] = g
    return [g for g in groups if g]


async def _ds_tokens(chain, tokens):
    """DexScreener: token -> best pair (by liquidity) {'symbol','price','liq','mcap','created'}. Cached 10 min."""
    ds = explorer.CHAINS.get(chain, {}).get("dexscreener") or chain
    out, todo = {}, []
    for t in tokens:
        c = cache.get(f"ds2:{chain}:{t}")
        if c is not None:
            out[t] = c
        else:
            todo.append(t)

    async def one(chunk):
        d = await explorer._get(f"https://api.dexscreener.com/tokens/v1/{ds}/{','.join(chunk)}", timeout=20)
        return d if isinstance(d, list) else []
    res = await asyncio.gather(*[one(todo[i:i + 30]) for i in range(0, len(todo), 30)])
    best, first = {}, {}
    for pairs in res:
        for p in pairs:
            t = ((p.get("baseToken") or {}).get("address") or "").lower()
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if t and (t not in best or liq > best[t]["liq"]):
                best[t] = {"symbol": (p.get("baseToken") or {}).get("symbol"), "name": (p.get("baseToken") or {}).get("name"), "price": float(p.get("priceUsd") or 0), "liq": liq,
                           "mcap": p.get("marketCap") or p.get("fdv"), "created": (p.get("pairCreatedAt") or 0) // 1000 or None}
            # the coin's age = its FIRST pool, not its deepest one: a new v4 pool on an old coin (APE on eth) made a
            # buy look 'within 1 h of launch'
            ts = (p.get("pairCreatedAt") or 0) // 1000
            if t and ts:
                first[t] = min(first.get(t) or ts, ts)
    for t, v in best.items():
        if first.get(t):
            v["created"] = first[t]
        v["rwa"] = is_rwa(v)
    for t in todo:
        v = best.get(t, {})
        out[t] = v
        cache.set(f"ds2:{chain}:{t}", v, ttl=600)
    return out


async def _prices():
    """Native coin prices in USD from DexScreener (wrapped tokens)."""
    out = {}
    for ch in WEALTH_CHAINS:
        info = await _ds_tokens(ch, [WRAPPED[ch]])
        out[ch] = (info.get(WRAPPED[ch]) or {}).get("price") or 0
    return out


async def _wealth(addrs, prices):
    """-> addr -> {'chains': {chain: usd}, 'detail': [...], 'usd': total} (native + stables, live)."""
    out = {a: {"chains": {}, "detail": [], "usd": 0.0} for a in addrs}

    async def chain(ch):
        try:
            rpc = RPC(ch)
            st = list(STABLES.get(ch, {}).items())
            calls = [("eth_getBalance", [a, "latest"]) for a in addrs]
            calls += [("eth_call", [{"to": t, "data": encode_call("balanceOf(address)", [a])}, "latest"]) for a in addrs for t, _ in st]
            r = await asyncio.wait_for(rpc.batch(calls), 25)
        except Exception:
            return
        n = len(addrs)
        for i, a in enumerate(addrs):
            nat = int(r[i], 16) / 1e18 if r[i] else 0
            usd = nat * prices.get(ch, 0)
            parts = [f"{nat:.3g} {RPC(ch).cfg.get('symbol', 'ETH')}"] if nat >= 0.001 else []
            for j, (t, (sym, dec)) in enumerate(st):
                v = r[n + i * len(st) + j]
                try:
                    amt = decode1("uint256", v) / 10 ** dec if v and v != "0x" else 0
                except Exception:
                    amt = 0
                if amt >= 1:
                    usd += amt
                    parts.append(f"{amt:,.0f} {sym}")
            if usd >= 1:
                out[a]["chains"][ch] = usd
                out[a]["detail"].append(f"{ch} ${usd:,.0f} ({', '.join(parts)})")
                out[a]["usd"] += usd
    await asyncio.gather(*[chain(ch) for ch in WEALTH_CHAINS])
    return out


def _ts(x):
    try:
        return int(x)
    except Exception:
        return None


async def profile_all(chain, d, n=20):
    """Profile the top `n` holders now + the dev. Adds h['profile'] to d['wallets'] entries; returns notes."""
    t0 = time.time()
    ex, rpc = explorer.Explorer(chain), RPC(chain)
    token = d["token"]
    targets = [h for h in d["wallets"] if "holder" in h["roles"]][:n]
    targets += [h for h in d["wallets"] if h["dev"] and h not in targets]
    addrs = [h["addr"] for h in targets]
    notes = []
    from lib import alchemy
    sem = asyncio.Semaphore(12 if (ex.es_id in explorer.FREE_UNSUPPORTED or chain in alchemy.NETS) and alchemy.available(chain) else 4)

    no_hist = lambda: ex.es_id in explorer.FREE_UNSUPPORTED and not alchemy.available(chain)

    async def hist(a):
        if no_hist():
            return [], []  # the explorer's fallback (HyperSync txlist, 15 req/min) costs ~25 s a wallet when rate limited
        async with sem:
            try:
                tl, tt = await asyncio.gather(asyncio.wait_for(ex.txlist(a, limit=300), 60),
                                              asyncio.wait_for(ex.tokentx(a, limit=1000), 60))
                return tl or [], tt or []
            except Exception as e:
                notes.append(f"history of {a} not read ({type(e).__name__})")
                return [], []
    prices_task = asyncio.create_task(_prices())
    from lib import xchain

    async def xc():
        try:
            return await asyncio.wait_for(xchain.collect(chain, addrs, d["now"]), 150)
        except Exception as e:
            return {}, [f"cross-chain activity failed ({type(e).__name__})"]
    xc_task = asyncio.create_task(xc())
    from lib import chainhist

    async def other_hist():
        """Other-chain history starts as soon as the cheap tx counts are in (not after the slow 7-day coin scans and
        bridge lookups): the wallet's top 4 other chains by recent activity."""
        chains = [c for c in xchain.private_chains() if c != chain]
        res = await asyncio.gather(*[asyncio.wait_for(xchain._activity(c, addrs, d["now"]), 30) for c in chains], return_exceptions=True)
        pairs = []
        for a in addrs:
            cand = {}
            for c, r in zip(chains, res):
                if isinstance(r, Exception) or not r:
                    continue
                v = r[0].get(a) or {}
                if (v.get("d90") or 0) > 0 or (v.get("total") or 0) >= 20:
                    cand[c] = (v.get("d30") or 0) + (v.get("total") or 0) / 1000
            pairs += [(a, c) for c, _ in sorted(cand.items(), key=lambda kv: -kv[1])[:4]]
        if not pairs:
            return {}
        return await asyncio.wait_for(chainhist.collect(pairs, d["now"], await prices_task), 150)
    oh_task = asyncio.create_task(other_hist())

    busy = set()

    async def home_fb():
        # bots with hundreds of txs a month flood the log scan (60 s+ timeouts): leave them out, they are labelled busy
        try:
            act, _ = await asyncio.wait_for(xchain._activity(chain, addrs, d["now"]), 20)
            busy.update(a for a, v in act.items() if (v.get("d30") or 0) > 300)
        except Exception:
            pass
        try:
            return await asyncio.wait_for(chainhist.home_logs(chain, [a for a in addrs if a not in busy], d["now"], 30), 75)
        except Exception as e:
            notes.append(f"{chain} 30-day transfer-log fallback failed ({type(e).__name__})")
            return {}
    fb_task = asyncio.create_task(home_fb()) if no_hist() else None
    hs = await asyncio.gather(*[hist(a) for a in addrs])
    tm = {"history": time.time() - t0}
    # the free Etherscan key can't list wallets on this chain (BSC/Base/Robinhood): never read "no history" as
    # "brand-new"; use the node's last-30-day token transfers instead and say so
    unreadable = set()
    if no_hist():
        unreadable = {a for a, (tl, tt) in zip(addrs, hs) if not tt}
        if unreadable:
            fb = await (fb_task or asyncio.create_task(home_fb()))
            hs = [(tl, fb.get(a, []) if a in unreadable else tt) for a, (tl, tt) in zip(addrs, hs)]
            notes.append(f"{chain} wallet history is NOT readable on the free Etherscan key: {len(unreadable)} profiles use the last 30 days "
                         f"of token transfers from our node instead (age, dormancy and older coins not judged; Etherscan Lite unlocks it)")
    elif fb_task:
        fb_task.cancel()
    prices = await prices_task
    eth_usd = prices.get(chain) or prices.get("eth") or 0
    wealth_task = asyncio.create_task(_wealth(addrs, prices))

    # ---- past coins + holdings candidates
    per, all_tokens = {}, set()
    base = {t for t in STABLES.get(chain, {})} | {WRAPPED.get(chain, "")}
    for a, (tl, tt) in zip(addrs, hs):
        own = {x.get("hash") for x in tl if (x.get("from") or "").lower() == a}
        coins = {}
        seen = set()
        for x in reversed(tt):  # oldest first
            c = (x.get("contractAddress") or "").lower()
            if not c:
                continue
            seen.add(c)
            if c == token or c in base:
                continue
            k = coins.setdefault(c, {"symbol": x.get("tokenSymbol"), "first": None, "in_own": False, "out": False, "n_in": 0, "txs": set()})
            ts = _ts(x.get("timeStamp"))
            if (x.get("to") or "").lower() == a:
                k["n_in"] += 1
                if not k["first"]:
                    k["first"], k["first_tx"] = ts, x.get("hash")
                k["txs"].add(x.get("hash"))
                if x.get("hash") in own:
                    k["in_own"] = True
            elif (x.get("from") or "").lower() == a:
                k["out"] = True
        if a in unreadable:
            real = {c: k for c, k in coins.items() if k["first"]}  # filtered below (pool + not a sprayed airdrop)
        else:
            real = {c: k for c, k in coins.items() if k["first"] and (k["in_own"] or k["out"])}
        per[a] = {"coins": real, "seen": seen, "own": own, "tt_oldest": _ts(tt[-1].get("timeStamp")) if tt else None, "tt_n": len(tt)}
        all_tokens |= set(real) | seen
    all_tokens -= {token}
    if unreadable:
        # transfer logs can't tell a buy from a sprayed fake coin (disperse bots hit hundreds of wallets per tx):
        # keep a coin only if the wallet's first tx of it shows the wallet paying (lib/wprofile._is_buy)
        cand = {(chain, c, a): k.get("first_tx") for a in unreadable for c, k in per[a]["coins"].items()}
        ok, unr = await _real_buys(cand)
        drop = 0
        for a in unreadable:
            keep = {c: k for c, k in per[a]["coins"].items() if (chain, c, a) in ok or (chain, c, a) in unr}
            drop += len(per[a]["coins"]) - len(keep)
            per[a]["coins"] = keep
        notes.append(f"{chain} 30-day coins: {len(cand)} checked, {drop} were sprayed/airdropped (no payment in the tx) and are ignored")
    info = await _ds_tokens(chain, sorted(all_tokens))
    if unreadable:
        from lib.xchain import BORING
        # an airdrop reaches several wallets in ONE tx; real buys are separate txs (so shared buys survive this filter)
        hit = defaultdict(Counter)
        for a in addrs:
            for c, k in per[a]["coins"].items():
                for t in k["txs"]:
                    hit[c][t] += 1
        spray = {c for c, n in hit.items() if n and max(n.values()) >= 3}
        for a in unreadable:
            per[a]["coins"] = {c: k for c, k in per[a]["coins"].items()
                               if (k["out"] or ((info.get(c) or {}).get("liq") and c not in spray))
                               and ((info.get(c) or {}).get("symbol") or "").upper() not in BORING}

    # ---- current balances of every token each wallet has touched (holdings)
    calls, keys = [], []
    for a in addrs:
        for c in per[a]["seen"]:
            if c != token:
                calls.append(("eth_call", [{"to": c, "data": encode_call("balanceOf(address)", [a])}, "latest"]))
                keys.append((a, c))
    bals = await rpc.batch(calls) if calls else []
    held = defaultdict(list)
    dec_cache = {}
    need_dec = sorted({c for _, c in keys})
    decs = await rpc.batch([("eth_call", [{"to": c, "data": encode_call("decimals()")}, "latest"]) for c in need_dec]) if need_dec else []
    for c, v in zip(need_dec, decs):
        try:
            dec_cache[c] = decode1("uint8", v) if v and v != "0x" else 18
        except Exception:
            dec_cache[c] = 18
    for (a, c), v in zip(keys, bals):
        try:
            amt = decode1("uint256", v) / 10 ** dec_cache.get(c, 18) if v and v != "0x" else 0
        except Exception:
            continue
        if amt <= 0:
            continue
        i = info.get(c) or {}
        if c in STABLES.get(chain, {}):
            usd, sym = amt, STABLES[chain][c][0]
        elif c == WRAPPED.get(chain):
            usd, sym = amt * eth_usd, "WETH"
        else:
            usd, sym = amt * (i.get("price") or 0), i.get("symbol") or "?"
            if i.get("liq") is not None and usd > (i.get("liq") or 0):
                usd = i.get("liq") or 0  # a bag worth more than its pool can't be sold for more than the pool holds
        if usd >= 300 and (c in per[a]["coins"] or c in STABLES.get(chain, {}) or c == WRAPPED.get(chain)):
            held[a].append((usd, sym, c))

    # ---- names for the contracts wallets call most
    called = Counter()
    for a, (tl, _) in zip(addrs, hs):
        for x in tl:
            if (x.get("from") or "").lower() == a and (x.get("input") or "0x") != "0x" and x.get("to"):
                called[x["to"].lower()] += 1
    to_name = [c for c, _ in called.most_common(40) if c not in KNOWN]
    names = dict(KNOWN)

    async def nm(c):
        try:
            s = await asyncio.wait_for(ex.source(c), 20)
            names[c] = s.get("name") or None
        except Exception:
            names[c] = None
    await asyncio.gather(*[nm(c) for c in to_name[:25]])
    tm["coins+names"] = time.time() - t0
    wealth = await wealth_task
    xc_res, xc_notes = await xc_task
    tm["cross-chain"] = time.time() - t0
    notes += xc_notes
    # full history on each other chain the wallet is active on (started early, see other_hist)
    ch_hist = {}
    try:
        ch_hist = await oh_task
    except Exception as e:
        notes.append(f"other-chain history failed ({type(e).__name__})")
    if ch_hist:
        unread = sorted({ch for a in ch_hist for ch, v in ch_hist[a].items() if "free Etherscan" in (v.get("error") or "")})
        if unread:
            notes.append(f"other-chain history NOT readable on {', '.join(unread)} with the free Etherscan key (Etherscan Lite covers them)")

    tm["other-chain history"] = time.time() - t0
    # ---- assemble
    supply = d["supply"] / 10 ** d["decimals"]
    for h, (tl, tt) in zip(targets, hs):
        a = h["addr"]
        f = h.get("first") or {}
        buy_ts = f.get("ts") or d["now"]
        own_tx = [x for x in tl if (x.get("from") or "").lower() == a]
        before = [x for x in own_tx if (_ts(x.get("timeStamp")) or 0) < buy_ts - 30 and x.get("hash") != f.get("tx")]
        last_own = max((_ts(x.get("timeStamp")) for x in before), default=None)
        any_before = [_ts(x.get("timeStamp")) for x in tl + tt if (_ts(x.get("timeStamp")) or 0) < buy_ts - 30]
        last_any = max(any_before, default=None)
        n30 = sum(1 for x in own_tx if buy_ts - 30 * 86400 <= (_ts(x.get("timeStamp")) or 0) < buy_ts)
        swaps = [int(x.get("value") or 0) / 1e18 for x in own_tx if int(x.get("value") or 0) > 0 and (x.get("input") or "0x") != "0x"
                 and x.get("hash") != f.get("tx")]
        typical = statistics.median(swaps) if swaps else None
        paid = f.get("value") if f.get("kind") == "bought" else None
        entry_mcap = paid * eth_usd / (f["amount"] / 10 ** d["decimals"]) * supply if paid and f.get("amount") and eth_usd else None
        deployed = [x.get("contractAddress") for x in own_tx if x.get("contractAddress")]
        big = max(tl, key=lambda x: int(x.get("value") or 0), default=None)
        stab = []
        for x in tt:
            c = (x.get("contractAddress") or "").lower()
            if c in STABLES.get(chain, {}):
                v = int(x.get("value") or 0) / 10 ** STABLES[chain][c][1]
                if v >= 5000:
                    stab.append((v, STABLES[chain][c][0], "in" if (x.get("to") or "").lower() == a else "out", _ts(x.get("timeStamp"))))
        p = per[a]
        coins = []
        for c, k in p["coins"].items():
            i = info.get(c) or {}
            early = bool(i.get("created") and k["first"] and k["first"] - i["created"] <= 3600)
            state = "no pool" if not i else ("dead" if (i.get("liq") or 0) < 1000 else "alive")
            coins.append({"token": c, "name": i.get("name"), "symbol": i.get("symbol") or k["symbol"], "first": k["first"], "first_tx": k.get("first_tx"), "state": state,
                          "mcap": i.get("mcap"), "liq": i.get("liq"), "early": early, "rwa": bool(i.get("rwa"))})
        coins.sort(key=lambda x: -(x["first"] or 0))
        now = d["now"]
        win = [x for x in coins if x["state"] == "alive" and (x["mcap"] or 0) >= 1_000_000 and (x["liq"] or 0) >= 50_000
               and (x["mcap"] or 0) <= (x["liq"] or 0) * 500 and not x["rwa"]]
        prot = Counter((x.get("to") or "").lower() for x in own_tx if (x.get("input") or "0x") != "0x" and x.get("to"))
        h["profile"] = {
            "txs_read": len(tl), "tokentx_read": p["tt_n"], "history_from": p["tt_oldest"],
            "last_own_before": last_own, "last_any_before": last_any, "txs_30d_before": n30,
            "dormant_days": round((buy_ts - last_any) / 86400, 1) if last_any else None,
            "app_trader": not own_tx and bool(tt) and a not in unreadable,
            "home_unreadable": a in unreadable, "home_busy": a in busy,
            "paid": paid, "entry_mcap": entry_mcap, "typical_buy": typical, "n_swaps_seen": len(swaps),
            "size_vs_usual": round(paid / typical, 1) if paid and typical else None,
            "deployed": deployed[:5], "n_deployed": len(deployed),
            "biggest_native": (int(big.get("value") or 0) / 1e18, "in" if (big.get("to") or "").lower() == a else "out", _ts(big.get("timeStamp"))) if big and int(big.get("value") or 0) else None,
            "stable_moves": sorted(stab, reverse=True)[:5], "n_stable_moves": len(stab),
            "coins_total": len(coins), "coins_30d": sum(1 for x in coins if x["first"] and now - x["first"] <= 30 * 86400),
            "coins_90d": sum(1 for x in coins if x["first"] and now - x["first"] <= 90 * 86400),
            "alive": sum(1 for x in coins if x["state"] == "alive"), "dead": sum(1 for x in coins if x["state"] != "alive"),
            "early": sum(1 for x in coins if x["early"]), "early_winners": [x for x in win if x["early"]][:6], "winners": win[:6],
            "recent_coins": coins[:8], "coin_list": coins,
            "holdings": sorted(held[a], reverse=True)[:8], "holdings_usd": sum(u for u, _, _ in held[a]),
            "wealth": wealth.get(a, {}), "eth_usd": eth_usd,
            "protocols": [(c, names.get(c) or None, k) for c, k in prot.most_common(5)],
            "xchain": xc_res.get(a), "chain_hist": ch_hist.get(a),
        }
    d["shared"] = await shared_coins(d, targets)
    notes.append(f"profiles: {len(targets)} wallets, history = newest 300 txs + 1000 token transfers each ({time.time() - t0:.1f}s; done at: "
                 + ", ".join(f"{k} {v:.0f}s" for k, v in tm.items()) + ")")
    return notes


def _ago(now, ts):
    if not ts:
        return "?"
    s = now - ts
    return f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.0f} d" if s < 86400 * 730 else f"{s / 86400 / 365:.1f} y"


def render(d, h):
    p = h.get("profile")
    if not p:
        return []
    now = d["now"]
    L = []
    bt = h["first"]["ts"] if h.get("first") and h["first"].get("ts") else now
    act = f"     activity: {p['txs_30d_before']} own txs in the 30 days before this buy"
    if p.get("home_unreadable"):
        c = ((p.get("xchain") or {}).get("chains") or {}).get(d["chain"]) or {}
        act = (f"     activity on {d['chain']}: {c.get('total', '?')} txs signed in its life / {c.get('d30', '?')} in last 30 d / {c.get('d7', '?')} in 7 d"
               f" · its {d['chain']} tx list is NOT readable on the free Etherscan key: wallet age, dormancy and coins older than 30 d not judged"
               + (" · signs no txs itself (trades through an app/relayer, or a smart account)" if c.get("total") in (0, 1) else ""))
    elif p["last_any_before"]:
        act += f" · last activity of any kind before it: {_ago(bt, p['last_any_before'])} earlier"
        other30 = sum((v.get("d30") or 0) for c, v in ((p.get("xchain") or {}).get("chains") or {}).items() if c != d["chain"])
        if p["dormant_days"] and p["dormant_days"] >= 30:
            act += (f"  ** DORMANT {p['dormant_days']:.0f} days on every chain, then bought this **" if not other30 else
                    f" (quiet {p['dormant_days']:.0f} days on {d['chain']} but {other30} txs on other chains in the last 30 d: not dormant)")
    else:
        act += " · no activity before this buy in the history read (brand-new wallet)"
    if p.get("app_trader"):
        act += " · trades only through an app (never signs its own txs; its money sits with the app, so wealth below can read $0)"
    if p["n_deployed"]:
        act += f" · deployed {p['n_deployed']} contract(s)"
    L.append(act)
    if p["paid"]:
        b = f"     this buy: {p['paid']:.4g} {d['native']}"
        if p["entry_mcap"]:
            b += f" at ~${p['entry_mcap']:,.0f} mcap"
        if p["typical_buy"]:
            b += f" · its usual swap {p['typical_buy']:.3g} {d['native']} (median of {p['n_swaps_seen']})"
            if p["size_vs_usual"]:
                b += f" → {p['size_vs_usual']}x usual" + ("  ** OUT OF CHARACTER **" if p["size_vs_usual"] >= 3 else "")
        L.append(b)
    hist_from = f", history read back to {_ago(now, p['history_from'])} ago" if p["history_from"] else ""
    if p.get("home_unreadable"):
        hist_from = " (last 30 d only, from transfer logs)"
        if p.get("home_busy"):
            hist_from = " — NOT SCANNED: bot-level activity (> 300 txs in 30 d) floods the log scan"
    L.append(f"     past coins: {p['coins_total']} traded{hist_from} · {p['coins_30d']} in last 30 d · {p['coins_90d']} in 90 d"
             f" · alive {p['alive']} / dead or no pool {p['dead']} · bought within 1 h of launch: {p['early']}")
    if p["early_winners"]:
        L.append("     ** EARLY on coins now >= $1M: " + ", ".join(f"{x['symbol']} (${x['mcap'] / 1e6:,.1f}M)" for x in p["early_winners"]) + " **")
    elif p["winners"]:
        L.append("     holds/held coins now >= $1M (not early): " + ", ".join(f"{x['symbol']} (${x['mcap'] / 1e6:,.1f}M)" for x in p["winners"][:5]))
    if p["recent_coins"]:
        L.append("     recent: " + " · ".join(
            f"{x['symbol']} {_ago(now, x['first'])} ago "
            + ("dead" if x["state"] == "dead" else "no pool" if x["state"] == "no pool" else f"${(x['mcap'] or 0) / 1000:,.0f}k")
            + (" (early)" if x["early"] else "") for x in p["recent_coins"][:6]))
    w = p["wealth"] or {}
    L.append(f"     wealth now: ${w.get('usd', 0):,.0f} on-chain (native + stables)" + (": " + " | ".join(w.get("detail", [])) if w.get("detail") else ""))
    if p["holdings"]:
        L.append(f"     holdings ${p['holdings_usd']:,.0f}: " + ", ".join(f"{sym} ${u:,.0f}" for u, sym, _ in p["holdings"][:6]))
    m = []
    if p["biggest_native"]:
        v, dr, ts = p["biggest_native"]
        m.append(f"largest native move {v:,.3g} {d['native']} {dr} ({_ago(now, ts)} ago)")
    if p["stable_moves"]:
        m.append(f"{p['n_stable_moves']} stablecoin transfer(s) >= $5k, largest ${p['stable_moves'][0][0]:,.0f} {p['stable_moves'][0][2]}")
    if m:
        L.append("     money: " + " · ".join(m))
    if p["protocols"]:
        L.append("     uses: " + ", ".join(f"{n or c[:10] + '…'} ({k})" for c, n, k in p["protocols"]))
    from lib import xchain, chainhist
    L += xchain.render(d["chain"], p.get("xchain"), now)
    L += chainhist.render(p.get("chain_hist"), now)
    return L


def _similar(sym, name, d):
    """Does another coin's symbol/name look like this token's? (copycat, relaunch, the team's earlier coin)"""
    import difflib
    a = {(d.get("symbol") or "").lower().strip("$ "), (d.get("name") or "").lower().strip("$ ")} - {""}
    b = {(sym or "").lower().strip("$ "), (name or "").lower().strip("$ ")} - {"", "?", "none"}
    for x in a:
        for y in b:
            if x == y or (min(len(x), len(y)) >= 2 and (x in y or y in x)):
                return True
            if min(len(x), len(y)) >= 4 and difflib.SequenceMatcher(None, x, y).ratio() >= 0.75:
                return True
    return False


SWAP_TOPICS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",  # Uniswap/Pancake V2 Swap
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",  # Uniswap V3 Swap
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83",  # PancakeSwap V3 Swap
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",  # Uniswap V4 Swap
    "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c",  # WETH/WBNB Deposit (native wrapped to pay)
}
LAUNCHPADS = {"0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",  # Flap portal (bsc)
              "0x5c952063c7fc8610ffdb798152d69f0b9550762b"}  # four.meme TokenManager (bsc)


def _is_buy(rcpt, token, wallet):
    """A real buy shows the wallet PAYING: a DEX swap / native wrapped in the tx, another token sent out by the wallet, or
    a launchpad curve in the tx. A disperse/airdrop spam tx only moves the spam token (often to many wallets at once)."""
    from lib.rpc import TRANSFER, topic_addr
    for l in (rcpt or {}).get("logs") or []:
        t = l.get("topics") or []
        if not t:
            continue
        addr = (l.get("address") or "").lower()
        if t[0] in SWAP_TOPICS or addr in LAUNCHPADS:
            return True
        if t[0] == TRANSFER and addr != token and len(t) == 3 and topic_addr(t[1]).lower() == wallet:
            return True
    return False


async def _real_buys(pairs):
    """pairs: {(chain, token, wallet): tx} -> set of keys whose tx is a real buy (receipts, one batch per chain)."""
    by_chain = defaultdict(list)
    for k, tx in pairs.items():
        if tx:
            by_chain[k[0]].append((k, tx))
    ok, unread = set(), set()

    async def one(ch, items):
        try:
            hashes = sorted({tx for _, tx in items})
            rs = await asyncio.wait_for(RPC(ch).batch([("eth_getTransactionReceipt", [h]) for h in hashes]), 30)
            rc = dict(zip(hashes, rs))
        except Exception:
            unread.update(k for k, _ in items)
            return
        for k, tx in items:
            r = rc.get(tx)
            if r is None:
                unread.add(k)
            elif _is_buy(r, k[1], k[2]):
                ok.add(k)
    await asyncio.gather(*[one(ch, it) for ch, it in by_chain.items()])
    return ok, unread


async def shared_coins(d, targets):
    """Other coins that SEVERAL top holders traded (home chain + their other chains). Many top holders in the same
    other coin = a team bundle reusing its wallets, or one degen group / copy-trade circle. Bought within minutes of
    each other = coordinated. A similar name = possibly the team's earlier coin or a copy."""
    hs = [h for h in targets if "holder" in h["roles"] and h.get("profile")][:20]
    rank = {h["addr"]: i for i, h in enumerate(hs)}
    by, meta, txof = defaultdict(dict), {}, {}
    for h in hs:
        a, p = h["addr"], h["profile"]
        for x in p.get("coin_list") or []:
            k = (d["chain"], x["token"])
            by[k][a] = x["first"]; meta[k] = x; txof[k + (a,)] = x.get("first_tx")
        for ch, v in (p.get("chain_hist") or {}).items():
            for x in (v or {}).get("all") or []:
                k = (ch, x["token"])
                if a not in by[k]:
                    by[k][a] = x["first"]; txof[k + (a,)] = x.get("first_tx")
                meta.setdefault(k, x)
    # fake coins sprayed by disperse bots land in many wallets within seconds and look 'bought together': keep only
    # wallets whose first tx of that coin was a real buy (the tx shows the wallet paying)
    cand = {ka: tx for ka, tx in txof.items() if len(by[ka[:2]]) >= 2}
    ok, unread = await _real_buys(cand)
    spam = Counter()
    for (ch, tok, a) in cand:
        if (ch, tok, a) not in ok and (ch, tok, a) not in unread:
            by[(ch, tok)].pop(a, None); spam[(ch, tok)] += 1
    from lib.xchain import BORING
    out = []
    for k, w in by.items():
        m = meta[k]
        if (m.get("symbol") or "").upper() in BORING:
            continue
        sim = _similar(m.get("symbol"), m.get("name"), d)
        if len(w) < (2 if sim else 3):
            continue
        ts = sorted(t for t in w.values() if t)
        tight = min(ts[i + 2] - ts[i] for i in range(len(ts) - 2)) if len(ts) >= 3 else (ts[1] - ts[0] if len(ts) == 2 else None)
        out.append({"chain": k[0], "token": k[1], "symbol": m.get("symbol") or "?", "state": m.get("state"), "mcap": m.get("mcap"),
                    "n10": sum(1 for a in w if rank[a] < 10), "n20": len(w), "who": sorted(w, key=lambda a: rank[a]),
                    "ranks": sorted(rank[a] + 1 for a in w), "tight": tight, "similar": sim,
                    "first": ts[0] if ts else None})
    for x in out:
        x["popular"] = (x["mcap"] or 0) >= 20e6 and x["state"] == "alive"
        x["together"] = x["tight"] is not None and x["tight"] <= 600
    out.sort(key=lambda x: (-x["similar"], x["popular"], -x["together"], -x["n10"], -x["n20"], x["tight"] if x["tight"] is not None else 9e9))
    # wallet pairs that keep buying the same SMALL coins (big popular coins say nothing), often within minutes
    shared, tog, tight = Counter(), Counter(), Counter()
    # this token's own first buys count as one more shared event (two wallets buying it seconds apart is evidence too)
    me = {h["addr"]: (h.get("first") or {}).get("ts") for h in hs if (h.get("first") or {}).get("kind") == "bought"}
    by[(d["chain"], d["token"])] = {a: t for a, t in me.items() if t}
    meta[(d["chain"], d["token"])] = {"symbol": d.get("symbol"), "state": "this token", "mcap": None}
    # coins each wallet traded on EVERY chain read (home + other chains): home-only counts read 0 on rh/arc (home history
    # not readable) and made every busy base/bsc degen look 'quiet'
    ncoins = Counter(a for k, w in by.items() if k != (d["chain"], d["token"]) for a in w)
    for k, w in by.items():
        m = meta[k]
        if (m.get("symbol") or "").upper() in BORING or ((m.get("mcap") or 0) >= 20e6 and m.get("state") == "alive"):
            continue
        ws = sorted(w)
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                a, b = ws[i], ws[j]
                shared[a, b] += 1
                # this token: launches pack buyers into minutes, so only buys <= 60 s apart count as 'together'
                if w[a] and w[b] and abs(w[a] - w[b]) <= (60 if k == (d["chain"], d["token"]) else 600):
                    tog[a, b] += 1
                if w[a] and w[b] and abs(w[a] - w[b]) <= 60:
                    tight[a, b] += 1
    # sharing many trending coins is normal for busy degens (it chained 13 of 20 wallets on a BSC meme), so a pair needs
    # >= 2 small coins bought within 10 min of each other
    # quiet wallets (<= 10 real coins in the window) sharing 2 rare picks bought together count too
    # Same rule as the all-holder scan (lib/holdernet): overlap must be big RELATIVE to how much the less active wallet
    # trades (m). Absolute counts ('>= 2 within 10 min, >= 3 shared') chained busy bots into a '9 wallets 17.7%' cluster
    # on GREM (rh): 5 of 86 coins together is chance for wallets that buy every trending coin.
    def is_strong(a, b):
        m = max(1, min(ncoins.get(a, 0), ncoins.get(b, 0)))
        return (tight[a, b] >= 3 and tight[a, b] >= 0.1 * m) or (tog[a, b] >= 3 and tog[a, b] >= 0.25 * m) \
            or (tog[a, b] >= 2 and m <= 10)
    strong = [(a, b) for (a, b) in shared if is_strong(a, b)]
    crowd = sum(1 for n in shared.values() if n >= 5)
    groups = tight_groups(strong, lambda p: (tight[p], tog[p], shared[p]))
    pct = {h["addr"]: h.get("pct") or 0 for h in hs}
    clusters = []
    for g in groups:
        g.sort(key=lambda a: rank[a])
        prs = [(a, b) for a, b in strong if a in g and b in g]
        clusters.append({"who": g, "ranks": [rank[a] + 1 for a in g], "pct": sum(pct[a] for a in g),
                         "pairs": [(rank[a] + 1, rank[b] + 1, shared[a, b], tog[a, b]) for a, b in sorted(prs, key=lambda p: (-tog[p], -shared[p]))]})
    clusters.sort(key=lambda c: -c["pct"])
    return {"n_top": len(hs), "n10": min(10, len(hs)), "coins": out[:15], "clusters": clusters,
            "spam_dropped": sum(spam.values()), "spam_coins": len(spam), "buys_checked": len(cand), "buys_unread": len(unread),
            "crowd": crowd, "n_pairs": len(hs) * (len(hs) - 1) // 2}


def render_shared(d):
    sh = d.get("shared") or {}
    if not sh.get("coins"):
        return ["SHARED COINS  no other coin traded by 3+ of the top holders (" + str(sh.get("n_top", 0)) + " profiled)"] if sh else []
    L = [f"SHARED COINS  other coins several top holders traded (team bundle reusing wallets, or one degen / copy-trade group)"]
    now = d["now"]
    for x in sh["coins"]:
        win = x["tight"]
        wtxt = "" if win is None or win > 3600 else (f" · 3 of them bought within {win / 60:.0f} min" if x["n20"] >= 3 else f" · bought {win / 60:.0f} min apart")
        if x["together"]:
            wtxt += "  ** TOGETHER **"
        if x["popular"]:
            wtxt += " (big popular coin: weak signal)"
        st = (f"${x['mcap'] / 1e6:,.1f}M" if (x["mcap"] or 0) >= 1e6 else f"${(x['mcap'] or 0) / 1e3:,.0f}k") if x["state"] == "alive" and x["mcap"] else (x["state"] or "?")
        L.append(f"   {x['symbol']} ({x['chain']}, {st}, {x['token']}): {x['n10']} of top {sh['n10']} · {x['n20']} of top {sh['n_top']}"
                 + f" (holders #{', #'.join(map(str, x['ranks']))})" + wtxt
                 + (f" · first bought {_ago(now, x['first'])} ago" if x["first"] else "")
                 + ("  ** SIMILAR NAME to this token **" if x["similar"] else ""))
    if sh.get("buys_checked"):
        L.append(f"   real-buy check: {sh['buys_checked']} wallet+coin first txs checked, {sh['spam_dropped']} were NOT buys (sprayed/airdropped coins,"
                 f" {sh['spam_coins']} coins) and are ignored" + (f"; {sh['buys_unread']} receipts not read (kept)" if sh.get("buys_unread") else ""))
    if sh.get("n_pairs"):
        L.append(f"   overlap: {sh['crowd']} of {sh['n_pairs']} holder pairs share 5+ small coins"
                 + (" (high: this holder base is one trending-coin crowd)" if sh["crowd"] >= sh["n_pairs"] * 0.15 else ""))
    for c in sh.get("clusters") or []:
        L.append(f"   CLUSTER holders #{', #'.join(map(str, c['ranks']))} (hold {c['pct']:.2f}% now): bought the same small coins within minutes, repeatedly — "
                 + "; ".join(f"#{a}+#{b}: {n} shared, {t} within 10 min" for a, b, n, t in c["pairs"][:6])
                 + "  -> one person, a copy-trader following another, or a group calling the same coins (not proof)")
        L.append("     " + " ".join(c["who"]))
    return L
