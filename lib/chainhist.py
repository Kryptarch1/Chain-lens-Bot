"""Full trading history of a wallet on each OTHER chain it is active on (for `dd holders` profiles).

For every (wallet, chain) pair: the wallet's token transfers (up to 1000) and txs (up to 300) from that chain's explorer
(Etherscan V2 -> Routescan -> Blockscout; HyperSync is not used here, too slow for this), then the same facts as the
home-chain profile: coins it really traded (signed the tx or later sent the token out), their state today, how many it
bought within 1 h of the pool opening, EARLY on coins now >= $1M, its usual swap size, last activity, and what it holds
there now (balances of coins it traded, priced, capped at pool liquidity).
Chains the Etherscan free key does not serve (Base, BSC, Robinhood, ...) come back as 'not readable' instead of empty.
"""
import asyncio, statistics, time
from collections import defaultdict
from lib.rpc import RPC
from lib.evm import encode_call, decode1
from lib import explorer


def _ts(x):
    try:
        return int(x)
    except Exception:
        return None


async def _one(chain, addr, sem):
    ex = explorer.Explorer(chain)
    async with sem:
        try:
            tt = await asyncio.wait_for(ex.tokentx(addr, limit=1000), 45) or []
        except Exception as e:
            return {"error": type(e).__name__}
        from lib import alchemy
        if not tt and ex.es_id in explorer.FREE_UNSUPPORTED and not alchemy.available(chain):
            return {"error": "not readable on the free Etherscan key"}
        try:
            tl = await asyncio.wait_for(ex.txlist(addr, limit=300), 45) or []
        except Exception:
            tl = []
    return {"tt": tt, "tl": tl}


async def collect(pairs, now, prices):
    """pairs: list of (addr, chain). -> {addr: {chain: summary}}"""
    from lib.wprofile import _ds_tokens, STABLES, WRAPPED
    from lib.xchain import BORING
    sem = asyncio.Semaphore(4)
    raw = await asyncio.gather(*[_one(ch, a, sem) for a, ch in pairs])
    by_chain = defaultdict(list)
    for (a, ch), r in zip(pairs, raw):
        by_chain[ch].append((a, r))
    out = defaultdict(dict)

    async def chain_part(ch, items):
        base = set(STABLES.get(ch, {})) | {WRAPPED.get(ch, "")}
        per = {}
        tokens = set()
        for a, r in items:
            if r.get("error"):
                out[a][ch] = {"error": r["error"]}
                continue
            tl, tt = r["tl"], r["tt"]
            own = {x.get("hash") for x in tl if (x.get("from") or "").lower() == a}
            coins = {}
            for x in reversed(tt):
                c = (x.get("contractAddress") or "").lower()
                if not c or c in base or (x.get("tokenSymbol") or "").upper().lstrip("$") in {b.lstrip("$") for b in BORING}:
                    continue
                k = coins.setdefault(c, {"symbol": x.get("tokenSymbol"), "first": None, "own": False, "out": False})
                if (x.get("to") or "").lower() == a:
                    if not k["first"]:
                        k["first"], k["first_tx"] = _ts(x.get("timeStamp")), x.get("hash")
                    k["own"] = k["own"] or x.get("hash") in own
                elif (x.get("from") or "").lower() == a:
                    k["out"] = True
            real = {c: k for c, k in coins.items() if k["first"] and (k["own"] or k["out"])}
            per[a] = (tl, tt, own, real)
            tokens |= set(real)
        if not per:
            return
        info = await _ds_tokens(ch, sorted(tokens))
        rpc = RPC(ch)
        keys = [(a, c) for a in per for c in per[a][3]]
        res = []
        if keys:
            try:
                res = await asyncio.wait_for(rpc.batch(
                    [("eth_call", [{"to": c, "data": encode_call("balanceOf(address)", [a])}, "latest"]) for a, c in keys] +
                    [("eth_call", [{"to": c, "data": encode_call("decimals()")}, "latest"]) for _, c in keys]), 40)
            except Exception:
                res = []
        held = defaultdict(list)
        if res:
            n = len(keys)
            for i, (a, c) in enumerate(keys):
                try:
                    dec = decode1("uint8", res[n + i]) if res[n + i] and res[n + i] != "0x" else 18
                    amt = decode1("uint256", res[i]) / 10 ** dec if res[i] and res[i] != "0x" else 0
                except Exception:
                    continue
                i_ = info.get(c) or {}
                usd = min(amt * (i_.get("price") or 0), i_.get("liq") or 0)
                if usd >= 300:
                    held[a].append((usd, i_.get("symbol") or per[a][3][c]["symbol"] or "?"))
        sym = rpc.cfg.get("symbol", "ETH")
        for a, (tl, tt, own, real) in per.items():
            coins = []
            for c, k in real.items():
                i_ = info.get(c) or {}
                state = "no pool" if not i_ else ("dead" if (i_.get("liq") or 0) < 1000 else "alive")
                early = bool(i_.get("created") and k["first"] - i_["created"] <= 3600)
                coins.append({"token": c, "first_tx": k.get("first_tx"), "name": i_.get("name"), "symbol": i_.get("symbol") or k["symbol"] or "?", "first": k["first"], "state": state, "early": early,
                              "mcap": i_.get("mcap"), "liq": i_.get("liq"), "sold": k["out"]})
            coins.sort(key=lambda x: -(x["first"] or 0))
            win = [x for x in coins if x["early"] and x["state"] == "alive" and (x["mcap"] or 0) >= 1_000_000
                   and (x["liq"] or 0) >= 50_000 and (x["mcap"] or 0) <= (x["liq"] or 0) * 500]
            swaps = [int(x.get("value") or 0) / 1e18 for x in tl if (x.get("from") or "").lower() == a
                     and int(x.get("value") or 0) > 0 and (x.get("input") or "0x") != "0x"]
            times = [_ts(x.get("timeStamp")) for x in tl + tt if _ts(x.get("timeStamp"))]
            out[a][ch] = {
                "coins": len(coins), "alive": sum(1 for x in coins if x["state"] == "alive"),
                "early": sum(1 for x in coins if x["early"]), "early_winners": win[:6],
                "d30": sum(1 for x in coins if x["first"] and now - x["first"] <= 30 * 86400),
                "recent": coins[:6], "all": coins, "from": min(times) if times else None, "last": max(times) if times else None,
                "typical": statistics.median(swaps) if swaps else None, "n_swaps": len(swaps), "symbol": sym,
                "usd_per_native": prices.get(ch) or None,
                "holdings": sorted(held[a], reverse=True)[:6], "holdings_usd": sum(u for u, _ in held[a]),
                "tt_n": len(tt), "capped": len(tt) >= 1000}
    await asyncio.gather(*[chain_part(ch, items) for ch, items in by_chain.items()])
    return out


def _ago(now, ts):
    if not ts:
        return "?"
    s = now - ts
    return f"{s / 3600:.0f} h" if s < 172800 else f"{s / 86400:.0f} d" if s < 86400 * 730 else f"{s / 86400 / 365:.1f} y"


def _usd(v):
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}k" if v >= 1e3 else f"${v:,.0f}"


def render(hist, now):
    L = []
    for ch, x in (hist or {}).items():
        if x.get("error"):
            L.append(f"     {ch.upper()} history: {x['error']}")
            continue
        if not x["coins"]:
            L.append(f"     {ch.upper()} history: no coins traded in the {x['tt_n']} token transfers read")
            continue
        span = f"back to {_ago(now, x['from'])} ago" + (" (newest 1000 transfers only)" if x["capped"] else "")
        line = (f"     {ch.upper()} history ({span}): {x['coins']} coins · {x['d30']} in last 30 d · alive {x['alive']} / dead {x['coins'] - x['alive']}"
                f" · bought < 1 h after launch: {x['early']}")
        if x["typical"]:
            line += f" · usual swap {x['typical']:.3g} {x['symbol']}" + (f" (~${x['typical'] * x['usd_per_native']:,.0f})" if x["usd_per_native"] else "")
        line += f" · last active {_ago(now, x['last'])} ago"
        L.append(line)
        if x["early_winners"]:
            L.append(f"       ** EARLY on {ch} coins now >= $1M: " + ", ".join(f"{w['symbol']} ({_usd(w['mcap'])})" for w in x["early_winners"]) + " **")
        L.append("       recent: " + " · ".join(
            f"{c['symbol']} {_ago(now, c['first'])} ago " + (_usd(c["mcap"] or 0) if c["state"] == "alive" and c["mcap"] else c["state"])
            + (" (early)" if c["early"] else "") + (" sold" if c["sold"] else "") for c in x["recent"]))
        if x["holdings"]:
            L.append(f"       holds on {ch}: {_usd(x['holdings_usd'])} — " + ", ".join(f"{s} {_usd(u)}" for u, s in x["holdings"]))
    return L


async def home_logs(chain, addrs, now, days=30):
    """Fallback when the home chain's explorer can't list a wallet's history (BSC/Base/Robinhood on the free Etherscan
    key): the wallets' token transfers of the last `days` days from one multi-wallet Transfer log scan on our own node
    (no HyperSync: its free plan is the scarce resource). -> {addr: tokentx-shaped rows, newest first}; timestamps are
    estimated from block numbers (steady block times)."""
    from lib.lp import estimate_block
    from lib.rpc import TRANSFER, topic_addr, pad_addr
    rpc = RPC(chain)
    latest = await rpc.block_number()
    b0, _ = await estimate_block(rpc, now - days * 86400, latest)
    b0 += 2000
    spb = days * 86400 / max(1, latest - b0)
    pads = [pad_addr(a) for a in addrs]
    ins, outs = await asyncio.gather(rpc._rpc_logs(None, [TRANSFER, None, pads], b0, latest, None),
                                     rpc._rpc_logs(None, [TRANSFER, pads], b0, latest, None))
    aset, out = set(addrs), defaultdict(list)
    for l in ins + outs:
        t = l.get("topics") or []
        if len(t) != 3:
            continue
        fr, to = topic_addr(t[1]).lower(), topic_addr(t[2]).lower()
        blk = int(l["blockNumber"], 16)
        d = l.get("data") or "0x"
        row = {"hash": l.get("transactionHash"), "from": fr, "to": to, "contractAddress": l["address"].lower(),
               "tokenSymbol": None, "value": str(int(d, 16) if len(d) > 2 else 0), "blockNumber": str(blk),
               "timeStamp": str(int(now - (latest - blk) * spb)), "via": "logs"}
        for a in {fr, to} & aset:
            out[a].append(row)
    for a in out:
        out[a].sort(key=lambda r: -int(r["blockNumber"]))
    return dict(out)
