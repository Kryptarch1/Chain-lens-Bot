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
import asyncio, statistics, time
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
}
WRAPPED = {"eth": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "base": "0x4200000000000000000000000000000000000006",
           "arb": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "bsc": "0xbb4cdb9cbd36b01bd1cbaead2de08d9173bc095c"}
WEALTH_CHAINS = ("eth", "base", "arb", "bsc")
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


async def _ds_tokens(chain, tokens):
    """DexScreener: token -> best pair (by liquidity) {'symbol','price','liq','mcap','created'}. Cached 10 min."""
    ds = explorer.CHAINS.get(chain, {}).get("dexscreener") or chain
    out, todo = {}, []
    for t in tokens:
        c = cache.get(f"ds:{chain}:{t}")
        if c is not None:
            out[t] = c
        else:
            todo.append(t)

    async def one(chunk):
        d = await explorer._get(f"https://api.dexscreener.com/tokens/v1/{ds}/{','.join(chunk)}", timeout=20)
        return d if isinstance(d, list) else []
    res = await asyncio.gather(*[one(todo[i:i + 30]) for i in range(0, len(todo), 30)])
    best = {}
    for pairs in res:
        for p in pairs:
            t = ((p.get("baseToken") or {}).get("address") or "").lower()
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if t and (t not in best or liq > best[t]["liq"]):
                best[t] = {"symbol": (p.get("baseToken") or {}).get("symbol"), "price": float(p.get("priceUsd") or 0), "liq": liq,
                           "mcap": p.get("marketCap") or p.get("fdv"), "created": (p.get("pairCreatedAt") or 0) // 1000 or None}
    for t in todo:
        v = best.get(t, {})
        out[t] = v
        cache.set(f"ds:{chain}:{t}", v, ttl=600)
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
    sem = asyncio.Semaphore(4)

    async def hist(a):
        async with sem:
            try:
                tl, tt = await asyncio.gather(asyncio.wait_for(ex.txlist(a, limit=300), 60),
                                              asyncio.wait_for(ex.tokentx(a, limit=1000), 60))
                return tl or [], tt or []
            except Exception as e:
                notes.append(f"history of {a} not read ({type(e).__name__})")
                return [], []
    prices_task = asyncio.create_task(_prices())
    hs = await asyncio.gather(*[hist(a) for a in addrs])
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
            k = coins.setdefault(c, {"symbol": x.get("tokenSymbol"), "first": None, "in_own": False, "out": False, "n_in": 0})
            ts = _ts(x.get("timeStamp"))
            if (x.get("to") or "").lower() == a:
                k["n_in"] += 1
                k["first"] = k["first"] or ts
                if x.get("hash") in own:
                    k["in_own"] = True
            elif (x.get("from") or "").lower() == a:
                k["out"] = True
        real = {c: k for c, k in coins.items() if k["first"] and (k["in_own"] or k["out"])}
        per[a] = {"coins": real, "seen": seen, "own": own, "tt_oldest": _ts(tt[-1].get("timeStamp")) if tt else None, "tt_n": len(tt)}
        all_tokens |= set(real) | seen
    all_tokens -= {token}
    info = await _ds_tokens(chain, sorted(all_tokens))

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
    wealth = await wealth_task

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
            coins.append({"token": c, "symbol": i.get("symbol") or k["symbol"], "first": k["first"], "state": state,
                          "mcap": i.get("mcap"), "liq": i.get("liq"), "early": early})
        coins.sort(key=lambda x: -(x["first"] or 0))
        now = d["now"]
        win = [x for x in coins if x["state"] == "alive" and (x["mcap"] or 0) >= 1_000_000 and (x["liq"] or 0) >= 50_000
               and (x["mcap"] or 0) <= (x["liq"] or 0) * 500]
        prot = Counter((x.get("to") or "").lower() for x in own_tx if (x.get("input") or "0x") != "0x" and x.get("to"))
        h["profile"] = {
            "txs_read": len(tl), "tokentx_read": p["tt_n"], "history_from": p["tt_oldest"],
            "last_own_before": last_own, "last_any_before": last_any, "txs_30d_before": n30,
            "dormant_days": round((buy_ts - last_any) / 86400, 1) if last_any else None,
            "app_trader": not own_tx and bool(tt),
            "paid": paid, "entry_mcap": entry_mcap, "typical_buy": typical, "n_swaps_seen": len(swaps),
            "size_vs_usual": round(paid / typical, 1) if paid and typical else None,
            "deployed": deployed[:5], "n_deployed": len(deployed),
            "biggest_native": (int(big.get("value") or 0) / 1e18, "in" if (big.get("to") or "").lower() == a else "out", _ts(big.get("timeStamp"))) if big and int(big.get("value") or 0) else None,
            "stable_moves": sorted(stab, reverse=True)[:5], "n_stable_moves": len(stab),
            "coins_total": len(coins), "coins_30d": sum(1 for x in coins if x["first"] and now - x["first"] <= 30 * 86400),
            "coins_90d": sum(1 for x in coins if x["first"] and now - x["first"] <= 90 * 86400),
            "alive": sum(1 for x in coins if x["state"] == "alive"), "dead": sum(1 for x in coins if x["state"] != "alive"),
            "early": sum(1 for x in coins if x["early"]), "early_winners": [x for x in win if x["early"]][:6], "winners": win[:6],
            "recent_coins": coins[:8],
            "holdings": sorted(held[a], reverse=True)[:8], "holdings_usd": sum(u for u, _, _ in held[a]),
            "wealth": wealth.get(a, {}), "eth_usd": eth_usd,
            "protocols": [(c, names.get(c) or None, k) for c, k in prot.most_common(5)],
        }
    notes.append(f"profiles: {len(targets)} wallets, history = newest 300 txs + 1000 token transfers each ({time.time() - t0:.1f}s)")
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
    if p["last_any_before"]:
        act += f" · last activity of any kind before it: {_ago(bt, p['last_any_before'])} earlier"
        if p["dormant_days"] and p["dormant_days"] >= 30:
            act += f"  ** DORMANT {p['dormant_days']:.0f} days, then bought this **"
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
    return L
