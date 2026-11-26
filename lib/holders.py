"""Holder data collector for `dd holders`: who holds a token, how each wallet got it, and how the wallets are linked.

Facts only (the written analysis is done on top of this output):
  1. every Transfer of the token since launch is replayed -> balances, and per wallet: bought / sold / received / sent
  2. top holders are confirmed with balanceOf and split into pools, burn, lockers, contracts and plain wallets
  3. for the top wallets + the dev: first funder (2 hops, bridges followed), tx count, native balance, who signed the buy
  4. links: same funder, funder is another holder, same bridge origin, same buy signer, tokens passed between holders
     ('proven' = on-chain fact); same exchange within 30 min and same launch block are listed as 'possible'
"""
import asyncio, re, time
from collections import defaultdict, Counter
from datetime import datetime
from lib.rpc import RPC, ZERO, DEAD, TRANSFER, topic_addr
from lib.evm import encode_call, decode1, delegation_7702
from lib import explorer
from lib.lp import KNOWN_LOCKERS
from lib.trace import EXCHANGE_NONCE, CHAIN_BY_ID, SOLANA_ID, bridge_fill


def _ts(x):
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        try:
            return int(datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp())
        except Exception:
            return None


async def transfers(rpc, token, from_block, to_block):
    logs = await rpc.get_logs(token, [TRANSFER], from_block=from_block, to_block=to_block)
    out = []
    for l in logs:
        if len(l.get("topics") or []) < 3:
            continue  # ERC-721 style / malformed
        d = l.get("data") or "0x"
        out.append({"block": int(l["blockNumber"], 16), "tx": l["transactionHash"], "i": int(l.get("logIndex", "0x0"), 16),
                    "from": topic_addr(l["topics"][1]).lower(), "to": topic_addr(l["topics"][2]).lower(),
                    "v": int(d, 16) if len(d) > 2 else 0})
    return out


def replay(rows, pools):
    """-> (balances, per-wallet activity, first trading block). A tx is a BUY when the pools' net token balance drops
    in it and a SELL when it rises; anything else is a plain transfer. Tokens sent to a pool before the first buy are
    the liquidity being seeded, not a sale."""
    bal = defaultdict(int)
    act = defaultdict(lambda: {"bought": 0, "sold": 0, "received": 0, "sent": 0, "minted": 0, "seeded": 0, "buys": 0, "sells": 0,
                               "lp_added": 0, "lp_removed": 0, "sell_txs": [], "buy_txs": [],
                               "first": None, "first_buy": None, "last_block": 0, "recv_from": Counter(), "sent_to": Counter()})
    txs, order = defaultdict(list), []
    for r in rows:
        if r["tx"] not in txs:
            order.append(r["tx"])
        txs[r["tx"]].append(r)
    start = None
    for tx in order:
        items = txs[tx]
        delta = defaultdict(int)
        for r in items:
            delta[r["from"]] -= r["v"]; delta[r["to"]] += r["v"]
            bal[r["from"]] -= r["v"]; bal[r["to"]] += r["v"]
        pool_d = sum(d for a, d in delta.items() if a in pools)
        blk = items[0]["block"]
        if pool_d < 0 and start is None:
            start = blk
        for a, d in delta.items():
            if a in pools or a == ZERO or d == 0:
                continue
            x = act[a]
            x["last_block"] = blk
            if d > 0:
                srcs = Counter()
                for r in items:
                    if r["to"] == a:
                        srcs[r["from"]] += r["v"]
                if pool_d < 0:
                    kind = "bought"; x["bought"] += d; x["buys"] += 1; x["buy_txs"].append((tx, d))
                    if not x["first_buy"]:
                        x["first_buy"] = {"block": blk, "tx": tx, "amount": d}
                elif set(srcs) == {ZERO}:
                    kind = "minted"; x["minted"] += d
                else:
                    kind = "received"; x["received"] += d
                    for s, v in srcs.items():
                        if s != ZERO and s not in pools:
                            x["recv_from"][s] += v
                if not x["first"]:
                    x["first"] = {"block": blk, "tx": tx, "kind": kind, "amount": d,
                                  "src": [s for s, _ in srcs.most_common(3)]}
            else:
                if pool_d > 0 and start is None:
                    x["seeded"] += -d
                elif pool_d > 0:
                    x["sold"] += -d; x["sells"] += 1; x["sell_txs"].append((tx, -d))
                else:
                    x["sent"] += -d
                    for r in items:
                        if r["from"] == a and r["to"] not in pools:
                            x["sent_to"][r["to"]] += r["v"]
    return bal, act, start


LP_ADD = {"0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f",   # V2 Mint
          "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"}   # V3 / PancakeV3 Mint
LP_REMOVE = {"0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496",  # V2 Burn
             "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"}  # V3 / PancakeV3 Burn
V4_MODIFY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"   # V4 ModifyLiquidity (delta sign = add/remove)


async def lp_moves(rpc, act, cap=600):
    """The replay calls tokens going INTO a pool a sell and tokens coming OUT a buy. Adding liquidity / pulling it
    looks the same, so a dev who opens a second pool at a made-up price reads as 'sold 11%', and an LP pull as a buy.
    Re-read those txs' receipts: a Mint / Burn / V4 ModifyLiquidity in the tx -> liquidity added / removed."""
    want = []
    for a, x in act.items():
        want += [t for t, _ in x["sell_txs"]] + [t for t, _ in x["buy_txs"]]
    want = sorted(set(want))[:cap]
    if not want:
        return 0
    try:
        rc = await rpc.batch([("eth_getTransactionReceipt", [t]) for t in want])
    except Exception:
        return 0
    add, rem = set(), set()
    for t, r in zip(want, rc):
        for lg in (r or {}).get("logs") or []:
            t0 = (lg.get("topics") or [None])[0]
            if t0 in LP_ADD:
                add.add(t)
            elif t0 in LP_REMOVE:
                rem.add(t)
            elif t0 == V4_MODIFY:
                d = lg.get("data") or "0x"
                w = d[2 + 128:2 + 192]  # 3rd word: liquidityDelta (int256)
                if w:
                    (rem if int(w[0], 16) >= 8 else add).add(t)
    n = 0
    for a, x in act.items():
        for t, v in x["sell_txs"]:
            if t in add:
                x["sold"] -= v; x["sells"] -= 1; x["lp_added"] += v; n += 1
        for t, v in x["buy_txs"]:
            if t in rem:
                x["bought"] -= v; x["buys"] -= 1; x["lp_removed"] += v; n += 1
                if x["first"] and x["first"]["tx"] == t:
                    x["first"]["kind"] = "removed liquidity"
                if x["first_buy"] and x["first_buy"]["tx"] == t:
                    x["first_buy"] = None
    return n


async def find_markets(rpc, rows, pools, n=20):
    """Contracts that SELL the token to many wallets and BUY it back from many (a launchpad bonding curve, or a market a
    DEX pair list doesn't know). They are treated as pools, so curve buys count as buys instead of 'received from'.
    Routers only pass tokens through in the same tx (net zero), so adding them changes nothing."""
    to_n, from_n = defaultdict(set), defaultdict(set)
    for r in rows:
        to_n[r["from"]].add(r["to"]); from_n[r["to"]].add(r["from"])
    cand = [a for a in to_n if a not in pools and a != ZERO and len(to_n[a]) >= n and len(from_n.get(a, ())) >= n]
    if not cand:
        return []
    codes = await rpc.batch([("eth_getCode", [a, "latest"]) for a in cand])
    out = []
    for a, c in zip(cand, codes):
        if c and c != "0x" and delegation_7702(c) is None:
            out.append(a)
    return out


def _reach(reason):
    m = re.search(r"for (\d+) holders|to (\d+) holders|^(\d+) wallets sent", reason)
    return int(next(g for g in m.groups() if g)) if m else 0


class Links:
    """Two union-finds over wallets: PROVEN links (on-chain facts) and ALL links (proven + likely), with the evidence."""
    def __init__(self):
        self.p, self.q, self.why = {}, {}, defaultdict(list)

    @staticmethod
    def _find(p, a):
        p.setdefault(a, a)
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def join(self, a, b, reason, proven=True):
        for p, use in ((self.p, proven), (self.q, True)):
            if use:
                ra, rb = self._find(p, a), self._find(p, b)
                if ra != rb:
                    p[ra] = rb
        self.why[frozenset((a, b))].append(reason if proven else "LIKELY: " + reason)

    def groups(self, members, proven=True):
        p = self.p if proven else self.q
        g = defaultdict(list)
        for m in members:
            g[self._find(p, m)].append(m)
        return [v for v in g.values() if len(v) > 1]


TAIL_MIN = 1e-4  # full pass: every wallet holding at least 0.01% of supply (~99% of what wallets hold); smaller = dust
TAIL_CAP = 700
CLUSTER_BUCKET = "same-coin clusters (copy-trade / one trader, not proof)"


async def _kinds(rpc, addrs):
    """nonce + code for each address -> {addr: {'nonce', 'kind'}}. A lookup that comes back empty is retried once and
    then marked kind 'unknown' (never 'wallet' with 0 txs): under load a failed nonce read turned an exchange hot wallet
    (268k txs) into a '0 txs' private funder and created a false PROVEN 'same first funder' link (SpaceXCoin, gake)."""
    out = {}
    todo = list(addrs)
    for attempt in range(2):
        if not todo:
            break
        r = await rpc.batch([("eth_getTransactionCount", [f, "latest"]) for f in todo] + [("eth_getCode", [f, "latest"]) for f in todo])
        miss = []
        for i, f in enumerate(todo):
            n, c = r[i], r[len(todo) + i]
            if n is None or c is None:
                miss.append(f)
                continue
            n = int(n, 16)
            out[f] = {"nonce": n, "kind": "contract" if (len(c) > 2 and not delegation_7702(c)) else "exchange/service" if n >= EXCHANGE_NONCE else "wallet"}
        todo = miss
    for f in todo:
        out[f] = {"nonce": None, "kind": "unknown"}
    return out


async def collect(chain, token, top=25, profile=20, full=False, deadline=None, after=None):
    """full=True: also profile EVERY wallet holding >= 0.01% of supply (role 'small holder'), so the links and groups
    cover the whole holder base, not just the top wallets. Their funders come from our archive node (lib/nodefund)."""
    t0 = time.time()
    rpc, ex = RPC(chain), explorer.Explorer(chain)
    token = token.lower()
    notes, timing = [], {}

    def budget(cap, reserve=0):
        """seconds a step may take: its own cap, clamped to the run's deadline minus what later steps need"""
        return max(2.0, min(cap, deadline - time.time() - reserve)) if deadline else cap
    from cmds.check import phase_a
    info = await phase_a(rpc, ex, chain, token)
    timing["token"] = time.time() - t0
    if not info.get("created_block"):
        return {"error": "could not find the token's creation block (no transfer history to replay)", "info": info}
    latest = await rpc.block_number()
    t = time.time()
    rows = await transfers(rpc, token, info["created_block"], latest)
    timing["transfers"] = time.time() - t
    pairs = info.get("pairs") or []
    pools = {p["pairAddress"].lower() for p in pairs if len(p.get("pairAddress") or "") == 42}
    v4 = (rpc.cfg.get("v4") or {}).get("pool_manager")
    for p in pairs:
        if len(p.get("pairAddress") or "") == 66 and (p.get("pm") or v4):
            pools.add((p.get("pm") or v4).lower())
    markets = await find_markets(rpc, rows, pools)
    pools |= set(markets)
    bal, act, start = replay(rows, pools)
    t_lp = time.time()
    if await lp_moves(rpc, act):
        notes.append("liquidity adds / pulls by wallets are shown as such, not as sells / buys")
    timing["markets+lp moves"] = time.time() - t_lp
    sym = rpc.cfg.get("symbol", "ETH")
    dec = info.get("decimals") or 18

    # --- devs: the creator and whoever signed the creation tx (a launcher contract's creator is not the human)
    devs = set()
    if info.get("creator"):
        devs.add(info["creator"].lower())
    if info.get("created_tx"):
        try:
            ct = await rpc.tx(info["created_tx"])
            if ct and ct.get("from"):
                devs.add(ct["from"].lower())
        except Exception:
            pass

    # --- candidates: biggest balances, biggest amounts EVER acquired (wallets that already left), collectors
    #     (a wallet many others sent their tokens to), distributors, and the wallets that fed a collector
    acq = {a: x["bought"] + x["minted"] + x["received"] for a, x in act.items()}
    skip = pools | {ZERO, token}
    by_bal = [a for a, v in sorted(bal.items(), key=lambda kv: -kv[1]) if v > 0 and a != ZERO][:top * 3 + 30]
    by_acq = [a for a, v in sorted(acq.items(), key=lambda kv: -kv[1]) if a not in skip][:top * 2 + 20]
    coll = [a for a, x in act.items() if a not in skip and len([s for s in x["recv_from"] if s not in skip]) >= 3]
    dist = [a for a, x in act.items() if a not in skip and len([s for s in x["sent_to"] if s not in skip]) >= 3]
    feeders = sorted({s for c in coll for s in act[c]["recv_from"] if s not in skip}, key=lambda a: -acq.get(a, 0))
    supply0 = info.get("supply") or sum(v for v in bal.values() if v > 0) or 1
    tail = [a for a, v in sorted(bal.items(), key=lambda kv: -kv[1]) if v >= supply0 * TAIL_MIN and a not in skip][:TAIL_CAP] if full else []
    from lib import tracked
    trk = tracked.load()
    trk_in = sorted(a for a in trk if a in act and a not in skip)  # tracked wallets that ever held this token
    cand = list(dict.fromkeys(by_bal + by_acq + coll + dist + feeders[:120] + sorted(devs) + tail + trk_in))
    t_c = time.time()
    res = await rpc.batch([("eth_getCode", [a, "latest"]) for a in cand] +
                          [("eth_call", [{"to": token, "data": encode_call("balanceOf(address)", [a])}, "latest"]) for a in cand])
    timing["balances"] = time.time() - t_c
    codes, bals = res[:len(cand)], res[len(cand):]
    supply = info.get("supply") or sum(v for v in bal.values() if v > 0) or 1
    hold, drift = {}, 0
    for a, c, b in zip(cand, codes, bals):
        try:
            real = decode1("uint256", b) if b and b != "0x" else bal[a]
        except Exception:
            real = bal[a]
        if bal[a] > 0 and abs(real - bal[a]) > bal[a] * 0.02:
            drift += 1
        c = c or "0x"
        if a in pools:
            kind = "pool"
        elif a in (ZERO, DEAD) or a.endswith("dead"):
            kind = "burn"
        elif a in KNOWN_LOCKERS:
            kind = "locker"
        elif a == token:
            kind = "token contract"
        elif len(c) > 2 and not delegation_7702(c):
            kind = "contract"
        else:
            kind = "wallet"
        hold[a] = {"addr": a, "bal": real, "pct": 100 * real / supply, "got_pct": 100 * acq.get(a, 0) / supply, "kind": kind,
                   "dev": a in devs, "name": KNOWN_LOCKERS.get(a), "roles": []}
    if drift > 3:
        notes.append(f"{drift} balances differ from the replayed transfers (reflection/rebase or fee-on-transfer token): amounts bought/sold are approximate")
    isw = lambda a: a in hold and hold[a]["kind"] == "wallet"
    top_now = [a for a in by_bal if isw(a) and hold[a]["bal"] > 0]
    top_now = sorted(top_now, key=lambda a: -hold[a]["bal"])[:top]
    exited = [a for a in by_acq if isw(a) and a not in top_now and a not in devs and acq[a] >= supply * 0.003
              and hold[a]["bal"] < acq[a] * 0.1][:40 if full else 25]
    coll_w = [a for a in coll if isw(a)]
    dist_w = [a for a in dist if isw(a) and a not in devs]
    feed_w = [a for a in feeders if isw(a)]
    if len(feed_w) > 40:
        notes.append(f"{len(feed_w)} wallets fed a collector; only the 40 biggest are profiled (all are counted in the group totals)")
    for role, lst in (("holder", top_now), ("exited", exited), ("collector", coll_w), ("distributor", dist_w), ("fed a collector", feed_w[:40]),
                      ("dev", [d for d in devs if isw(d)])):
        for a in lst:
            hold[a]["roles"].append(role)
    for a in trk_in:
        if a in hold:
            hold[a]["roles"].append("tracked")
    W0 = list(dict.fromkeys(top_now + exited + coll_w + dist_w + feed_w[:40] + [d for d in devs if isw(d)] + [a for a in trk_in if isw(a)]))
    tail_w = [a for a in tail if isw(a) and hold[a]["bal"] > 0 and a not in W0]
    for a in tail_w:
        hold[a]["roles"].append("small holder")
    W = W0 + tail_w
    tail_set = set(tail_w)
    wallets = [hold[a] for a in W]
    hist_task = None
    from lib import alchemy as _al0
    if full and (_al0.available(chain) or rpc.private):
        # holder histories start NOW, alongside the funder lookups (they only need the wallet list)
        from lib import holdernet
        try:
            cb = await rpc.call("eth_getBlockByNumber", [hex(info["created_block"]), False])
            age_d = (time.time() - int(cb["timestamp"], 16)) / 86400
        except Exception:
            age_d = 30
        # history window: coins < 1 day old run on a 60 s budget -> 3 days (snipers / bundles / copy-traders buy
        # dozens of coins a day, 3 days shows them); < 3 days -> 7; older coins (120 s budget) -> 14
        net_days = 3 if age_d <= 1 else 7 if age_d <= 3 else 14
        hist_task = asyncio.ensure_future(holdernet.load(chain, W, {a: hold[a]["pct"] for a in W}, sorted(devs),
                                                         core=W0, days=net_days, deadline=deadline))
    if after is not None:
        # the all-holder pass runs alongside the quick pass: its heavy part (hundreds of funder searches, receipts)
        # waits until the quick pass is printed, so the user's first answer is not slowed (8 s alone vs 18-26 s when
        # both hit the node at once); its history load above is light and already running
        try:
            await asyncio.wait_for(after.wait(), budget(30, 30))
        except Exception:
            pass

    # --- per wallet: the tx that first gave it tokens, block times, tx count, native balance, first funder
    first_txs = sorted({act[a]["first"]["tx"] for a in W if act[a]["first"]})
    blocks = sorted({act[a]["first"]["block"] for a in W if act[a]["first"]} | ({start} if start else set()) | {info["created_block"], latest})

    async def funded(a):
        try:
            return await asyncio.wait_for(ex.funded_by(a), 150)
        except Exception as e:
            return {"error": type(e).__name__}

    async def funded_all():
        a, b = await asyncio.gather(funded_many(W0), funded_many(tail_w, node=True))
        return a + b

    async def funded_many(W, node=False):
        # the explorer can't list wallets on this chain (BSC/Base/RH free key) and HyperSync's free plan runs out:
        # binary-search our own archive node instead (lib/nodefund), explorer path only for what that misses.
        # node=True (hundreds of small holders): always the node when we have a private one
        if not W:
            return []
        if not (rpc.private and (node or ex.es_id in explorer.FREE_UNSUPPORTED)):
            return await asyncio.gather(*[funded(a) for a in W])
        from lib import cache, nodefund, alchemy
        res, miss = {}, []
        for a in W:
            c = cache.get(f"funded:{chain}:{a}")
            if c is not None:
                res[a] = c
            else:
                miss.append(a)
        if miss and alchemy.available(chain) and not node:
            # (hundreds of small holders stay on our node: Alchemy's free plan does ~7 calls/s and is busy with histories)
            # one call per wallet (its first inbound native transfers, oldest first) instead of the ~54-call node search
            al = await asyncio.gather(*[alchemy.first_funding(chain, a) for a in miss])
            left = []
            for a, x in zip(miss, al):
                if x.get("from"):
                    res[a] = {k: x[k] for k in ("from", "tx", "time", "internal", "value") if k in x}
                    cache.set(f"funded:{chain}:{a}", res[a], ttl=None)
                elif x.get("none"):
                    res[a] = {"sponsored": True}
                    cache.set(f"funded:{chain}:{a}", res[a], ttl=None)
                else:
                    left.append(a)
            miss = left
        if miss:
            r0 = await rpc.batch([("eth_getTransactionCount", [a, hex(latest)]) for a in miss] + [("eth_getBalance", [a, hex(latest)]) for a in miss])
            n = len(miss)
            nn = {a: int(r0[i], 16) if r0[i] else 0 for i, a in enumerate(miss)}
            bb = {a: int(r0[n + i], 16) if r0[n + i] else 0 for i, a in enumerate(miss)}
            try:
                nf = await asyncio.wait_for(nodefund.first_funding_many(chain, miss, nn, bb, latest), budget(240 if node else 90, 20))
            except Exception as e:
                nf = {a: {"error": type(e).__name__} for a in miss}
            for a in miss:
                x = nf.get(a) or {"error": "missing"}
                if x.get("from"):
                    res[a] = {k: x[k] for k in ("from", "tx", "time", "internal", "value") if k in x}
                    cache.set(f"funded:{chain}:{a}", res[a], ttl=None)
                elif x.get("none"):
                    res[a] = {"sponsored": True} if x.get("sponsored") else {}
                    cache.set(f"funded:{chain}:{a}", res[a], ttl=None if x.get("sponsored") else 600)
                else:
                    res[a] = {"error": "node: " + str(x.get("error"))}
        return [res[a] for a in W]

    t = time.time()
    txr, blr, nb, fund = await asyncio.gather(
        rpc.batch([("eth_getTransactionByHash", [h]) for h in first_txs]),
        rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in blocks]),
        rpc.batch([("eth_getTransactionCount", [a, "latest"]) for a in W] + [("eth_getBalance", [a, "latest"]) for a in W]),
        funded_all())
    timing["wallets"] = time.time() - t
    txd = {h: x for h, x in zip(first_txs, txr) if x}
    bts = {b: int(x["timestamp"], 16) for b, x in zip(blocks, blr) if x}
    now = bts.get(latest) or int(time.time())
    for i, h in enumerate(wallets):
        a = h["addr"]
        x = act[a]
        h["nonce"] = int(nb[i], 16) if nb[i] else None
        h["native"] = int(nb[len(W) + i], 16) / 1e18 if nb[len(W) + i] else None
        h.update({k: x[k] for k in ("bought", "sold", "received", "sent", "minted", "seeded", "buys", "sells", "lp_added", "lp_removed")})
        h["recv_from"] = dict(x["recv_from"].most_common(4)); h["sent_to"] = dict(x["sent_to"].most_common(4))
        h["n_recv_from"], h["n_sent_to"] = len(x["recv_from"]), len(x["sent_to"])
        f = x["first"]
        if f:
            tx = txd.get(f["tx"]) or {}
            h["first"] = {**f, "ts": bts.get(f["block"]), "signer": (tx.get("from") or "").lower() or None,
                          "to": (tx.get("to") or "").lower() or None, "value": int(tx.get("value") or "0x0", 16) / 1e18 if tx else None,
                          "index": int(tx["transactionIndex"], 16) if tx.get("transactionIndex") else None,
                          "after_start": (f["block"] - start) if start is not None else None}
        fb = fund[i] or {}
        h["funder"] = (fb.get("from") or "").lower() or None
        h["fund_tx"], h["fund_ts"] = fb.get("tx"), _ts(fb.get("time"))
        h["fund_internal"], h["fund_value"] = bool(fb.get("internal")), fb.get("value")
        if fb.get("sponsored"):
            h["sponsored"] = True  # never held native coin before its first tx: an app's gas-sponsored / smart-wallet account
        elif fb.get("error") or not fb:
            notes.append(f"funder of {a} not found ({fb.get('error') or 'no inbound native transfer seen'})")

    # --- funders: exchange / contract / plain wallet; one more hop for plain wallets; bridge payouts matched to their origin
    t = time.time()
    F = sorted({h["funder"] for h in wallets if h["funder"]})
    fk = await _kinds(rpc, F) if F else {}
    # a busy funder (>= 1,000 txs) or one shared by several holders may be a payout service (an exchange hot wallet,
    # a bridge solver, a trading app's withdrawal wallet) that pays out to many people: then sharing it proves nothing
    shared = Counter(h["funder"] for h in wallets if h["funder"])
    svc_check = [f for f in F if fk[f]["kind"] == "wallet" and (fk[f]["nonce"] >= 1000 or shared[f] >= 2)]

    from lib import alchemy as _al
    node_only = bool(rpc.private and ex.es_id in explorer.FREE_UNSUPPORTED and not _al.available(chain))

    async def payout_service(f):
        if node_only:
            # can't list its txs here without HyperSync: a busy funder is treated as a service (sharing it is not proof)
            return f"busy wallet ({fk[f]['nonce']:,} txs; its payouts can't be listed on this chain's free explorer), treated as a service" \
                if fk[f]["nonce"] >= 1000 else None
        try:
            tl = await asyncio.wait_for(ex.txlist(f, limit=200), 40)
        except Exception:
            return None
        outs = [x for x in tl if (x.get("from") or "").lower() == f and int(x.get("value") or 0) > 0 and (x.get("input") or "0x") == "0x"]
        rec = {(x.get("to") or "").lower() for x in outs}
        if len(rec) >= 20:
            span = (int(tl[0].get("timeStamp") or 0) - int(tl[-1].get("timeStamp") or 0)) / 3600 if tl else 0
            return f"pays out to many wallets: {len(rec)} recipients in its last {len(tl)} txs ({span:.0f} h)"
        return None
    fund_txs = sorted({h["fund_tx"] for h in wallets if h.get("fund_tx")})
    async def payout_service_t(f):
        # capped: one slow explorer read held the quick pass for 26 s (SpaceXCoin); a busy funder that is not read in
        # time is treated like the node-only case (>= 1,000 txs = a service, sharing it proves nothing)
        try:
            return await asyncio.wait_for(payout_service(f), 8)
        except Exception:
            return f"busy wallet ({fk[f]['nonce']:,} txs; payouts not read in time), treated as a service" if fk[f]["nonce"] >= 1000 else None
    async def all_services():
        # one 5 s cap for ALL of them (they share the explorer / Alchemy pacer; per-funder caps still added up to 20 s)
        ts = [asyncio.ensure_future(payout_service_t(f)) for f in svc_check]
        if ts:
            await asyncio.wait(ts, timeout=5)
        out = []
        for f, t in zip(svc_check, ts):
            if t.done() and not t.exception():
                out.append(t.result())
            else:
                t.cancel()
                out.append(f"busy wallet ({fk[f]['nonce']:,} txs; payouts not read in time), treated as a service" if fk[f]["nonce"] >= 1000 else None)
        return out
    svc_res, ftx = await asyncio.gather(all_services(),
                                        rpc.batch([("eth_getTransactionByHash", [x]) for x in fund_txs]) if fund_txs else asyncio.sleep(0, []))
    for f, why in zip(svc_check, svc_res):
        if why:
            fk[f]["kind"], fk[f]["why"] = "payout service", why
    ftx = {x: t for x, t in zip(fund_txs, ftx or []) if t}
    for h in wallets:
        fx = ftx.get(h.get("fund_tx"))
        # what THIS wallet got: a contract payout (internal) is not the tx's value (one tx can fund 40 wallets)
        h["fund_amt"] = h["fund_value"] if h.get("fund_value") is not None else \
            (int(fx["value"], 16) / 1e18 if fx and not h.get("fund_internal") else None)
        h["fund_block"] = int(fx["blockNumber"], 16) if fx else None
        if h["fund_amt"] is not None and h["fund_amt"] < 1e-6:
            # dust 'activation' sends (1e-18 BNB from an app's service wallet) are markers, not funding or wallet age
            h["fund_marker"] = h["funder"]
            h["funder"] = h["fund_tx"] = h["fund_ts"] = h["fund_block"] = None
    hop2 = [f for f in F if fk[f]["kind"] == "wallet" and f not in W]
    hop2_node = bool(tail_w)
    sem = asyncio.Semaphore(6)

    async def bridge(h):
        async with sem:
            try:
                return await asyncio.wait_for(bridge_fill(h["fund_tx"], h["addr"]), 12)
            except Exception:
                return None

    maybe_bridge = [h for h in wallets if h["funder"] and h["fund_tx"] and fk[h["funder"]]["kind"] != "wallet"
                    and (h["addr"] not in tail_set or fk[h["funder"]]["kind"] == "contract")]
    timing["funders: services+amounts"] = time.time() - t
    t2 = time.time()

    async def _tm(k, co):
        r = await co
        timing[k] = time.time() - t2
        return r
    h2, br = await asyncio.gather(_tm("funders: 2nd hop", funded_many(hop2, node=hop2_node)), _tm("funders: bridges", asyncio.gather(*[bridge(h) for h in maybe_bridge])))
    for f, r in zip(hop2, h2):
        fk[f]["funder"] = ((r or {}).get("from") or "").lower() or None
    G = sorted({v["funder"] for v in fk.values() if v.get("funder")} - set(fk))
    if G:
        fk.update(await _kinds(rpc, G))
    for h, b in zip(maybe_bridge, br):
        if b:
            src = "solana" if b["chain_id"] == SOLANA_ID else CHAIN_BY_ID.get(b["chain_id"], str(b["chain_id"]))
            h["bridge"] = {"name": b.get("name"), "from": b["sender"], "chain": src, "amount": b.get("amount"), "symbol": b.get("symbol"), "usd": b.get("usd")}
    timing["funders"] = time.time() - t

    # --- who signed each wallet's first tx: a signer with thousands of txs is a relayer / trading-bot service that signs
    #     for many unrelated users, so it proves nothing; a small signer buying for several wallets is one operator
    signers = sorted({(h.get("first") or {}).get("signer") for h in wallets} - {None} - set(W))
    sn = await rpc.batch([("eth_getTransactionCount", [x, "latest"]) for x in signers]) if signers else []
    sk = {x: (int(n, 16) if n else 0) for x, n in zip(signers, sn)}
    relayer = {x for x in signers if sk[x] >= EXCHANGE_NONCE}

    async def app_relayer(x):
        """A trading app signs for its users from relayer wallets that send (almost) every tx to the app's router."""
        try:
            tl = await asyncio.wait_for(ex.txlist(x, limit=100), 40)
        except Exception:
            return False
        tos = Counter((t.get("to") or "").lower() for t in tl if (t.get("from") or "").lower() == x)
        n = sum(tos.values())
        return n >= 50 and tos.most_common(1)[0][1] >= 0.9 * n
    n_signed = Counter((h.get("first") or {}).get("signer") for h in wallets)
    top_signers = {(h.get("first") or {}).get("signer") for h in wallets if h["addr"] not in tail_set}
    chk = [x for x in signers if x not in relayer and sk[x] >= 50 and (x in top_signers or n_signed[x] >= 2)]
    t_sg = time.time()
    tasks = [asyncio.ensure_future(app_relayer(x)) for x in chk]
    if tasks:
        await asyncio.wait(tasks, timeout=5)  # one cap for all: per-signer txlists queue behind the shared pacer
    for x, tk in zip(chk, tasks):
        r = tk.result() if tk.done() and not tk.exception() else (tk.cancel() and False) or sk[x] >= 1000
        if r:
            relayer.add(x)
    timing["signers"] = time.time() - t_sg
    by_relayer = defaultdict(list)
    for h in wallets:
        f = h.get("first") or {}
        if f.get("signer") and f["signer"] != h["addr"]:
            f["signer_txs"] = sk.get(f["signer"])
            f["signer_relayer"] = f["signer"] in relayer

    # --- links
    t_lk = time.time()
    L = Links()
    Wset = set(W)
    by_f, by_g, by_origin, by_signer = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for h in wallets:
        a, f = h["addr"], h["funder"]
        if f and f in Wset:  # funded by another holder: a fact whatever that holder's tx count
            L.join(a, f, f"{a} was first funded by holder {f} ({h.get('fund_amt') or 0:.4g} {sym})")
        if h.get("bridge"):
            by_origin[(h["bridge"]["chain"], h["bridge"]["from"].lower())].append(a)
        elif f and fk[f]["kind"] == "wallet":
            by_f[f].append(a)
            g = fk[f].get("funder")
            if g and fk.get(g, {}).get("kind") == "wallet":
                if g in Wset:
                    L.join(a, g, f"{a}'s funder {f} was funded by holder {g}")
                by_g[g].append(a)
        s = (h.get("first") or {}).get("signer")
        if s and s != a:
            if s in Wset:
                L.join(a, s, f"{a}'s first tokens came in a tx signed by holder {s}")
            (by_relayer if s in relayer else by_signer)[s].append(a)
        for src in act[a]["recv_from"]:
            if src in Wset:
                L.join(a, src, f"{src} sent its tokens to {a}" if len(act[a]["recv_from"]) < 3 else
                       f"{len(act[a]['recv_from'])} wallets sent their tokens to one wallet, {a}")
    for f, ws in by_f.items():
        for w in ws[1:]:
            L.join(ws[0], w, f"same first funder {f} ({fk[f]['nonce']} txs)")
    # one tx paid the first native coin to several holders (a disperse / one-shot-contract funding: each wallet gets a
    # payout from a different throwaway contract, so 'same funder' misses it). An exchange batch withdrawal also pays many
    # strangers in one tx -> only when the tx's signer is not a busy service.
    by_ftx = defaultdict(list)
    for h in wallets:
        if h.get("fund_tx") and h.get("funder"):
            by_ftx[h["fund_tx"]].append(h["addr"])
    multi = {t: ws for t, ws in by_ftx.items() if len(ws) >= 2}
    if multi:
        fs = await rpc.batch([("eth_getTransactionByHash", [t]) for t in multi])
        signer = {t: (x or {}).get("from", "").lower() for t, x in zip(multi, fs)}
        sg = sorted({v for v in signer.values() if v})
        sn = await rpc.batch([("eth_getTransactionCount", [v, "latest"]) for v in sg]) if sg else []
        sn = {v: int(n, 16) for v, n in zip(sg, sn) if n}  # a failed read is unknown, never '0 txs' (false PROVEN link)
        for t, ws in multi.items():
            if signer.get(t) and signer[t] in sn and sn[signer[t]] < 1000:
                for w in ws[1:]:
                    L.join(ws[0], w, f"first {sym} for {len(ws)} holders came in ONE tx {t} signed by {signer[t]} "
                                     f"({sn[signer[t]]} txs): one wallet funded them all")
    # one BUY tx delivered tokens to several holders = one wallet paid for all of them (a bundle buy). An airdrop
    # (no buy) to many wallets is not proof: disperse bots and promo drops hit strangers.
    by_tx = defaultdict(list)
    for h in wallets:
        f = h.get("first") or {}
        if f.get("kind") == "bought" and f.get("tx"):
            by_tx[f["tx"]].append(h["addr"])
    for tx, ws in by_tx.items():
        # a fee / referral wallet also gets a small cut in the buy tx: only wallets that got a real share count
        big = max((act[a]["first"]["amount"] for a in ws), default=0)
        ws = [a for a in ws if act[a]["first"]["amount"] >= big * 0.1]
        if 2 <= len(ws) <= 30:
            for w in ws[1:]:
                L.join(ws[0], w, f"first tokens bought in ONE tx {tx} that delivered to {len(ws)} holders (bundle buy)")
    for g, ws in by_g.items():
        for w in ws[1:]:
            L.join(ws[0], w, f"funders share one funder {g} (2 hops)")
        for f, ws2 in by_f.items():  # a wallet funded directly by g joins wallets funded through g
            if f == g:
                L.join(ws[0], ws2[0], f"{g} funded one directly and the other's funder")
    for (ch, o), ws in by_origin.items():
        for w in ws[1:]:
            L.join(ws[0], w, f"both bridged in from {o} on {ch}")
    for s, ws in by_signer.items():
        for w in ws[1:]:
            L.join(ws[0], w, f"first tokens arrived in txs signed by the same wallet {s} ({sk.get(s, 0):,} txs, not a relayer)")
    # LIKELY: a payout service / exchange paid several holders the SAME amount within a few blocks (one batch, one person)
    by_svc = defaultdict(list)
    for h in wallets:
        f = h["funder"]
        if f and f not in Wset and fk[f]["kind"] in ("payout service", "exchange/service") and h.get("fund_block") and h.get("fund_amt"):
            by_svc[f].append(h)
    for f, hs in by_svc.items():
        hs.sort(key=lambda h: h["fund_block"])
        for i, x in enumerate(hs):
            for y in hs[i + 1:]:
                if y["fund_block"] - x["fund_block"] > 10:
                    break
                if abs(x["fund_amt"] - y["fund_amt"]) <= 0.01 * max(x["fund_amt"], y["fund_amt"]):
                    L.join(x["addr"], y["addr"], f"{fk[f]['kind']} {f} paid both {x['fund_amt']:.5g} {sym} in "
                           + ("the same block" if y['fund_block'] == x['fund_block'] else f"{y['fund_block'] - x['fund_block']} blocks") + f" (block {x['fund_block']})", proven=False)
    net = None
    from lib import alchemy as _al
    # chains without Alchemy but with our own node (rh, arc): node logs instead (holdernet._hist_logs)
    if hist_task is not None:
        # every checked holder's history: sends between holders, shared exchange deposit addresses (proof) and
        # same-rare-coin clusters (pattern) across the whole holder base
        from lib import holdernet
        t = time.time()
        try:
            loaded = await asyncio.wait_for(hist_task, budget(150, 6))
            net = await asyncio.wait_for(holdernet.scan(chain, {"token": token, "native": sym, "devs": sorted(devs)}, wallets, timing,
                                                         days=net_days, deadline=deadline, core=W0, loaded=loaded), budget(120, 1))
            for a, b, why in net["links"]:
                if a in Wset and b in Wset:
                    L.join(a, b, why)
            notes.extend(net["notes"])
        except Exception as e:
            notes.append(f"all-holder history scan failed ({type(e).__name__}: {str(e)[:60]})")
        timing["all-holder scan"] = time.time() - t

    def group(g, conf):
        gs = set(g)
        # reasons that tie MANY wallets together first (one funding tx for 43 holders, a bundle buy, a collector):
        # alphabetical order buried the one line that explained a 68-wallet group under '47 more links'
        why = sorted({r for pair, rs in L.why.items() if pair <= gs for r in rs}, key=lambda r: (-_reach(r), r))
        extra = {s for a in g for s in act[a]["recv_from"] if s not in gs and s not in skip and s in feeders and "collector" in hold[a]["roles"]}
        inside = gs | extra
        # what the group took in from outside itself: buys, mints, and tokens received from non-members
        got = sum(act[a]["bought"] + act[a]["minted"] + sum(v for s, v in act[a]["recv_from"].items() if s not in inside) for a in inside)
        return {"members": sorted(g, key=lambda a: (-hold[a]["bal"], -hold[a]["got_pct"])), "unprofiled": len(extra), "conf": conf,
                "pct": sum(hold[a]["pct"] for a in g), "got_pct": 100 * got / supply,
                "sold_pct": 100 * sum(act[a]["sold"] for a in inside) / supply,
                "dev": any(a in devs for a in g), "why": why}
    groups = [group(g, "proven") for g in L.groups(W)]
    proven_sets = [set(g["members"]) for g in groups]
    for g in L.groups(W, proven=False):
        if not any(set(g) <= p for p in proven_sets):
            groups.append(group(g, "likely"))
    groups.sort(key=lambda g: (g["conf"] != "proven", -max(g["pct"], g["got_pct"])))
    breakdown = None
    if full:
        # where 100% of the supply sits now: every token is in exactly one bucket
        B = defaultdict(float)
        for h in hold.values():
            if h["bal"] > 0 and h["kind"] != "wallet":
                B[{"pool": "pools / markets", "token contract": "contracts"}.get(h["kind"], h["kind"] + "s")] += h["pct"]
        gof = {}
        for g in groups:
            for a in g["members"]:
                gof.setdefault(a, g)
        incl = {a for c in (net or {}).get("clusters", []) for a in c["who"]}
        nfresh = napp = nind = 0
        for h in wallets:
            if h["bal"] <= 0:
                continue
            g = gof.get(h["addr"])
            f = h.get("first") or {}
            if g and g["dev"]:
                k = "dev-linked groups"
            elif g:
                k = f"{g['conf']} groups (one owner per group)"
            elif h["addr"] in incl:
                k = CLUSTER_BUCKET
            elif h.get("sponsored"):
                k = "app accounts (gas-sponsored, never funded), unlinked"; napp += 1
            elif h.get("fund_ts") and f.get("ts") and 0 <= f["ts"] - h["fund_ts"] <= 3600:
                k = "funded under 1 h before their first token, unlinked"; nfresh += 1
            else:
                k = "independent wallets (no link found)"; nind += 1
            B[k] += h["pct"]
            h["bucket"] = k
        known = sum(B.values())
        n_dust = sum(1 for a, v in bal.items() if 0 < v < supply0 * TAIL_MIN and a not in skip)
        B[f"dust wallets under 0.01% each ({n_dust}, not checked)"] += max(0.0, 100 - known)
        breakdown = {"buckets": sorted(B.items(), key=lambda kv: -kv[1]), "n_checked": len([h for h in wallets if h["bal"] > 0]),
                     "n_tail": len(tail_w), "n_dust": n_dust}

    # --- possible links (not merged): same exchange within 30 min, same block at launch
    possible = []
    d_kind = lambda f: fk.get(f, {}).get("kind", "?")
    ex_f = defaultdict(list)
    for h in wallets:
        if h["funder"] and fk[h["funder"]]["kind"] not in ("wallet", "payout service") and not h.get("bridge") and h["fund_ts"] \
                and h["addr"] not in tail_set:
            ex_f[h["funder"]].append(h)
    for f, hs in ex_f.items():
        hs.sort(key=lambda h: h["fund_ts"])
        run = [hs[0]]
        for h in hs[1:] + [None]:
            if h and h["fund_ts"] - run[-1]["fund_ts"] <= 1800:
                run.append(h); continue
            if len(run) > 1:
                possible.append({"kind": "same exchange/service wallet, funded within 30 min of each other", "via": f,
                                 "members": [x["addr"] for x in run], "pct": sum(x["pct"] for x in run), "got_pct": sum(x["got_pct"] for x in run)})
            run = [h] if h else []
    # the dev's funder also paid other holders shortly before launch (a service: not proof, but worth a look)
    for dv in devs:
        hd = hold.get(dv)
        dh = next((h for h in wallets if h["addr"] == dv), None)
        if not dh or not dh.get("funder") or not dh.get("fund_block"):
            continue
        near = [h for h in wallets if h["addr"] != dv and h.get("funder") == dh["funder"] and h.get("fund_block")
                and abs(h["fund_block"] - dh["fund_block"]) <= 300]
        if near:
            possible.append({"kind": f"funded by the dev's own funder {dh['funder']} ({d_kind(dh['funder'])}) within "
                             f"{max(abs(h['fund_block'] - dh['fund_block']) for h in near) * 12 // 60} min of the dev",
                             "via": None, "members": [dv] + [h["addr"] for h in near],
                             "pct": sum(hold[a]["pct"] for a in [dv] + [h["addr"] for h in near]),
                             "got_pct": sum(hold[a]["got_pct"] for a in [dv] + [h["addr"] for h in near])})
    for r, ws in by_relayer.items():
        ws = [w for w in ws if w not in tail_set]
        if len(ws) > 1:
            possible.append({"kind": f"first buy sent through the same relayer / trading-bot service ({sk[r]:,} txs): users of one service, not proof of one owner",
                             "via": r, "members": ws, "pct": sum(hold[a]["pct"] for a in ws), "got_pct": sum(hold[a]["got_pct"] for a in ws)})
    blk = defaultdict(list)
    for h in wallets:
        f = h.get("first") or {}
        if f.get("kind") == "bought" and f.get("after_start") is not None and f["after_start"] <= 2:
            blk[f["block"]].append(h)
    for b, hs in blk.items():
        if len(hs) >= 2:
            possible.append({"kind": f"first buy in the same block, {b - start} block(s) after trading started", "via": None,
                             "members": [x["addr"] for x in hs], "pct": sum(x["pct"] for x in hs), "got_pct": sum(x["got_pct"] for x in hs)})

    # --- the user's tracked wallets: in this token, funding a holder, or inside a group / same-coin cluster
    tr_rows = []
    by_w = {h["addr"]: h for h in wallets}
    # rank among EVERY holding wallet (the quick pass checks only the top + movers, so ranking inside `wallets` said #26
    # for a wallet that is really #50); pools, contracts and burns don't take a rank
    not_w = {a for a, x in hold.items() if x["kind"] != "wallet"} | pools | {ZERO, DEAD}
    allbal = sorted((v for a, v in bal.items() if v > 0 and a not in not_w), reverse=True)
    import bisect
    neg = [-v for v in allbal]
    rank_now = {h["addr"]: bisect.bisect_left(neg, -h["bal"]) + 1 for h in wallets if h["bal"] > 0}
    gidx = {a: (i, g) for i, g in enumerate(groups, 1) for a in g["members"]}
    cidx = {a: (i, c) for i, c in enumerate(((net or {}).get("clusters") or []), 1) for a in c["who"]}
    for a, lab in trk.items():
        h = by_w.get(a)
        funded = [x["addr"] for x in wallets if x["addr"] != a and (x.get("funder") == a or fk.get(x.get("funder") or "", {}).get("funder") == a)]
        if not h and not funded:
            continue
        r = {"addr": a, "label": lab, "funded": funded}
        if h:
            got = h["bought"] + h["received"] + h["minted"]
            f = h.get("first") or {}
            r.update({"pct": h["pct"], "got_pct": h["got_pct"], "rank": rank_now.get(a), "sold_share": (h["sold"] + h["sent"]) / got if got else 0,
                      "first_kind": f.get("kind"), "first_ts": f.get("ts"), "after_start": f.get("after_start"), "paid": f.get("value")})
            if a in gidx:
                i, g = gidx[a]
                r["group"] = (i, g["conf"], [m for m in g["members"] if m != a])
            if a in cidx:
                i, c = cidx[a]
                r["cluster"] = (i, [m for m in c["who"] if m != a])
        tr_rows.append(r)
    tr_rows.sort(key=lambda r: (-(r.get("pct") or 0), -(r.get("got_pct") or 0)))

    timing["links+groups"] = time.time() - t_lk
    pair0 = pairs[0] if pairs else {}
    out = {"chain": chain, "token": token, "name": info.get("name"), "symbol": info.get("symbol"), "decimals": dec, "supply": supply,
            "native": sym, "devs": sorted(devs), "created_block": info["created_block"], "created_ts": bts.get(info["created_block"]),
            "start_block": start, "start_ts": bts.get(start) if start else None, "now": now, "latest": latest,
            "mcap": pair0.get("marketCap") or pair0.get("fdv"), "liq": (pair0.get("liquidity") or {}).get("usd"),
            "n_transfers": len(rows), "n_holders": sum(1 for a, v in bal.items() if v > 0 and a not in skip), "n_ever": sum(1 for a in act if a not in skip), "pools": sorted(pools), "markets": markets,
            "hold": sorted(hold.values(), key=lambda h: -h["bal"]), "wallets": wallets, "funders": fk, "groups": groups,
            "possible": possible, "notes": notes, "timing": timing, "full": full, "breakdown": breakdown,
            "net_clusters": (net or {}).get("clusters"), "dev_coins": (net or {}).get("dev_coins"), "leaders": (net or {}).get("leaders"), "net_days": net_days if hist_task is not None else None, "tracked": tr_rows, "n_tracked_list": len(trk)}
    if profile:
        t = time.time()
        from lib import wprofile
        try:
            notes += await asyncio.wait_for(wprofile.profile_all(chain, out, n=profile), 240)
        except Exception as e:
            notes.append(f"wallet profiles failed ({type(e).__name__}: {str(e)[:80]})")
        timing["profiles"] = time.time() - t
    out["timing"] = {k: round(v, 1) for k, v in timing.items()}
    out["secs"] = round(time.time() - t0, 1)
    return out


def _age(s):
    if s is None:
        return "?"
    s = max(0, s)
    return f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.0f} d" if s < 86400 * 400 else f"{s / 86400 / 365:.1f} y"


def _wallet_lines(d, h, n=None):
    q = lambda v: v / 10 ** d["decimals"]
    f = h.get("first") or {}
    got = h["bought"] + h["received"] + h["minted"]
    how = f.get("kind", "?")
    when = f"block +{f['after_start']}" if f.get("after_start") is not None and f["after_start"] >= 0 else "before trading"
    mins = f" ({_age(f['ts'] - d['start_ts'])} after start)" if f.get("ts") and d.get("start_ts") and f["ts"] >= d["start_ts"] else ""
    roles = [r for r in h["roles"] if r != "holder"]
    from lib.tracked import tag
    L = [f" {('#%d ' % n) if n else ''}{h['addr']}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}% in total" + (f"  [{', '.join(roles).upper()}]" if roles else "") + tag(h["addr"])]
    line = f"     tokens: first {how} {when}{mins}"
    if how == "bought" and f.get("value"):
        line += f", tx value {f['value']:.4g} {d['native']}"
    if how != "bought" and f.get("src"):
        line += f" from {', '.join(f['src'])}"
    L.append(line)
    parts = [f"bought {q(h['bought']):,.0f} in {h['buys']} buy(s)" if h["buys"] else None,
             f"received {q(h['received']):,.0f} from {h['n_recv_from']} wallet(s)" if h["received"] else None,
             f"minted {q(h['minted']):,.0f}" if h["minted"] else None,
             f"sold {q(h['sold']):,.0f} in {h['sells']} sell(s) = {100 * h['sold'] / got:.0f}% of what it got" if h["sells"] and got else "sold nothing",
             f"sent out {q(h['sent']):,.0f} to {h['n_sent_to']} address(es)" if h["sent"] else None,
             f"seeded liquidity {q(h['seeded']):,.0f}" if h["seeded"] else None,
             f"ADDED LIQUIDITY {q(h['lp_added']):,.0f} (its own pool position, not a sell)" if h.get("lp_added") else None,
             f"PULLED LIQUIDITY {q(h['lp_removed']):,.0f}" if h.get("lp_removed") else None]
    L.append("     " + " · ".join(p for p in parts if p))
    if f.get("signer") and f["signer"] != h["addr"]:
        n = f.get("signer_txs")
        L.append(f"     first tx was signed by another wallet: {f['signer']}" + (f" ({n:,} txs" + (", trading-app relayer)" if f.get("signer_relayer") else ")") if n is not None else ""))
    if f.get("to"):
        L.append(f"     first tx sent to {f['to']} (position {f.get('index')} in its block)  tx {f['tx']}")
    if h["recv_from"]:
        L.append("     received from: " + ", ".join(f"{a} ({q(v):,.0f})" for a, v in h["recv_from"].items()) + (" …" if h["n_recv_from"] > 4 else ""))
    if h["sent_to"]:
        L.append("     sent to: " + ", ".join(f"{a} ({q(v):,.0f})" for a, v in h["sent_to"].items()) + (" …" if h["n_sent_to"] > 4 else ""))
    w = f"     wallet: {h['nonce'] if h['nonce'] is not None else '?'} txs · {h['native']:.4g} {d['native']}" if h["native"] is not None else "     wallet: ?"
    if h.get("fund_ts"):
        w += f" · first funded {_age(d['now'] - h['fund_ts'])} ago"
    L.append(w)
    L.append("     " + _funder_text(d, h))
    from lib import wprofile
    L += wprofile.render(d, h)
    return L


def _funder_text(d, h):
    if h.get("bridge"):
        b = h["bridge"]
        return f"funded via {b['name']} bridge from {b['from']} on {b['chain']} ({b.get('amount')} {b.get('symbol')})"
    fu = h.get("funder")
    if not fu:
        return "funder: not found"
    k = d["funders"].get(fu, {})
    amt = f"{h['fund_amt']:.4g} {d['native']} " if h.get("fund_amt") else ""
    n = lambda x: f"{x:,}" if isinstance(x, int) else "?"
    line = f"funded {amt}by {fu} ({k.get('kind')}, {n(k.get('nonce'))} txs" + (f"; {k['why']}" if k.get("why") else "") + ")"
    if k.get("funder"):
        k2 = d["funders"].get(k["funder"], {})
        line += f" ← {k['funder']} ({k2.get('kind')}, {n(k2.get('nonce'))} txs)"
    return line


def render_tracked(d):
    """YOUR TRACKED WALLETS block: who from the user's list is in this coin, and how."""
    rows = d.get("tracked")
    if rows is None:
        return []
    from lib.tracked import label
    if not rows:
        return [f"YOUR TRACKED WALLETS  none of your {d.get('n_tracked_list', 0)} tracked wallets touched this coin or funded a holder"]
    L = [f"YOUR TRACKED WALLETS  {len(rows)} of your {d.get('n_tracked_list', 0)} are in this coin"]
    start = d.get("start_ts")
    for r in rows:
        if "pct" in r:
            when = ""
            if r.get("first_ts") and start:
                when = f"{'bought' if r['first_kind'] == 'bought' else r['first_kind']} {_age(r['first_ts'] - start)} after start"
            pos = (f"holds {r['pct']:.2f}% (#{r['rank']})" if r["pct"] >= 0.0005 else "holds nothing now") + f" · got {r['got_pct']:.2f}%"
            if r["sold_share"] >= 0.01:
                pos += f" · sold/sent {100 * min(1, r['sold_share']):.0f}%"
            L.append(f" ★ {r['label']}  {r['addr']}  {pos}" + (f" · {when}" if when else "") + (f" · paid {r['paid']:.4g} {d['native']}" if r.get("paid") else ""))
        else:
            L.append(f" ★ {r['label']}  {r['addr']}  does not hold it")
        if r.get("group"):
            i, conf, ms = r["group"]
            L.append(f"     in linked group ({conf.upper()}) with " + ", ".join(m + (f" ({label(m)})" if label(m) else "") for m in ms[:5]) + (" …" if len(ms) > 5 else ""))
        if r.get("cluster"):
            i, ms = r["cluster"]
            L.append(f"     same-coin cluster C{i} with " + ", ".join(m + (f" ({label(m)})" if label(m) else "") for m in ms[:5]) + (" …" if len(ms) > 5 else ""))
        if r["funded"]:
            L.append(f"     funded {len(r['funded'])} wallet(s) in this coin: " + ", ".join(r["funded"][:5]) + (" …" if len(r["funded"]) > 5 else ""))
    return L


def render(d):
    if d.get("error"):
        return f"== HOLDERS: {d['error']}"
    from lib.tracked import tag
    q = lambda v: v / 10 ** d["decimals"]
    L = [f"== HOLDERS {d['symbol']} ({d['name']}) on {d['chain']}  {d['token']}",
         f"   age {_age(d['now'] - d['created_ts']) if d['created_ts'] else '?'} · mcap {('$%s' % format(int(d['mcap']), ',')) if d['mcap'] else '?'}"
         f" · liquidity {('$%s' % format(int(d['liq']), ',')) if d['liq'] else '?'} · {d['n_holders']} holders now · {d['n_ever']} wallets ever held"
         f" · {d['n_transfers']} transfers replayed · trading started block {d['start_block']}",
         f"   dev: {', '.join(d['devs']) or 'unknown'}"]
    kinds = defaultdict(float)
    for h in d["hold"]:
        if h["bal"] > 0:
            kinds[h["kind"]] += h["pct"]
    now_w = [h for h in d["wallets"] if "holder" in h["roles"]]
    if d.get("markets"):
        L.append(f"   {len(d['markets'])} market contract(s) found in the transfers (bonding curve / router / pool not in the DEX pair list; buys from them count as buys): "
                 + ", ".join(d["markets"][:4]) + (" …" if len(d["markets"]) > 4 else ""))
    L.append("SUPPLY NOW  " + " · ".join(f"{k} {v:.1f}%" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]) if k != "wallet" and v >= 0.05)
             + f" · top {len(now_w)} wallets {sum(h['pct'] for h in now_w):.1f}%")
    L += render_tracked(d)
    for h in d["hold"]:
        if h["kind"] in ("contract", "locker") and h["pct"] >= 0.5:
            L.append(f"   {h['kind']} {h['addr']} holds {h['pct']:.2f}%" + (f" ({h['name']})" if h.get("name") else ""))
    by = {h["addr"]: h for h in d["wallets"]}
    L.append(f"LINKED GROUPS  {len(d['groups'])} group(s): hold {sum(g['pct'] for g in d['groups']):.1f}% now,"
             f" took in {sum(g['got_pct'] for g in d['groups']):.1f}% of supply in total")
    for i, g in enumerate(d["groups"], 1):
        n = len(g["members"]) + g["unprofiled"]
        L.append(f" G{i} [{g['conf'].upper()}]  {n} wallets · holds {g['pct']:.2f}% now · took in {g['got_pct']:.2f}% · sold {g['sold_pct']:.2f}%"
                 f"{'  ** INCLUDES THE DEV **' if g['dev'] else ''}")
        for a in g["members"][:8]:
            h = by[a]
            L.append(f"     {a}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}%  [{', '.join(h['roles'])}]" + tag(a))
        if n > 8:
            L.append(f"     … and {n - 8} more")
        for w in g["why"][:8]:
            L.append(f"     - {w}")
        if len(g["why"]) > 8:
            L.append(f"     - … {len(g['why']) - 8} more links")
    if d["possible"]:
        L.append("POSSIBLE LINKS (pattern, not proof)")
        for p in d["possible"]:
            L.append(f"   {len(p['members'])} wallets, {p['pct']:.2f}% now / {p['got_pct']:.2f}% taken in: {p['kind']}" + (f" ({p['via']})" if p["via"] else ""))
            L.append("     " + " ".join(a[:10] + "…" for a in p["members"]))
    if d.get("shared"):
        from lib.wprofile import render_shared
        L += render_shared(d)
    L.append(f"TOP HOLDERS NOW ({len(now_w)})")
    for i, h in enumerate(now_w, 1):
        L += _wallet_lines(d, h, i)
    ex = [h for h in d["wallets"] if "exited" in h["roles"]]
    if ex:
        L.append(f"BIG BUYERS WHO LEFT ({len(ex)}): got at least 0.3% of supply, now hold under a tenth of it")
        for h in ex:
            L += _wallet_lines(d, h)
    cd = [h for h in d["wallets"] if ("collector" in h["roles"] or "distributor" in h["roles"]) and "holder" not in h["roles"] and "exited" not in h["roles"]]
    if cd:
        L.append(f"COLLECTORS / DISTRIBUTORS ({len(cd)}): wallets that tokens were gathered into or handed out from")
        for h in cd:
            L += _wallet_lines(d, h)
    fd = [h for h in d["wallets"] if h["roles"] == ["fed a collector"]]
    if fd:
        L.append(f"WALLETS THAT FED A COLLECTOR ({len(fd)} profiled)")
        for h in fd:
            f = h.get("first") or {}
            L.append(f"   {h['addr']}  got {h['got_pct']:.2f}% ({f.get('kind', '?')} block +{f.get('after_start')}"
                     + (f", signed by {f['signer'][:10]}…" if f.get("signer") and f["signer"] != h["addr"] else "") + f") · {h['nonce']} txs · "
                     + (f"funded {_age(d['now'] - h['fund_ts'])} ago · " if h.get("fund_ts") else "") + _funder_text(d, h))
    dv = [h for h in d["wallets"] if h["roles"] == ["dev"]]
    if dv:
        L.append("DEV")
        for h in dv:
            L += _wallet_lines(d, h)
    if d["notes"]:
        L.append("NOT CHECKED / NOTES")
        miss = [n for n in d["notes"] if n.startswith("funder of ") and " not found" in n]
        rest = [n for n in d["notes"] if n not in miss]
        L += [f"   - {n}" for n in rest[:25]]
        if miss:
            why = Counter(n.split(" not found", 1)[1].strip(" ()") or "no funding tx found" for n in miss)
            L.append(f"   - funder not found for {len(miss)} wallet(s): " + "; ".join(f"{k} ×{v}" for k, v in why.most_common())
                     + " — " + ", ".join(n.split()[2] for n in miss[:40]))
    L.append(f"   ({d['secs']}s: " + ", ".join(f"{k} {v}s" for k, v in d["timing"].items()) + ")")
    return "\n".join(L)


def render_deep(d, notes, secs):
    """Second part of `dd holders` (after the quick pass): shared coins/clusters + each profiled wallet's deep profile."""
    from lib import wprofile
    L = ["", "== DEEP PROFILES (history, cross-chain, shared coins)"]
    L += wprofile.render_shared(d)
    n = 0
    for h in d["wallets"]:
        if not h.get("profile"):
            continue
        n += 1
        tag = f"#{[w for w in d['wallets'] if 'holder' in w['roles']].index(h) + 1} " if "holder" in h["roles"] else "[DEV] "
        L.append(f" {tag}{h['addr']}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}% in total")
        L += wprofile.render(d, h)
    if notes:
        L.append("NOT CHECKED / NOTES (profiles)")
        L += [f"   - {x}" for x in notes[:25]]
    L.append(f"   (deep profiles: {n} wallets, {secs:.1f}s)")
    return "\n".join(L)


def render_full(d, deep=None):
    """The ALL HOLDERS section: every wallet >= 0.01% of supply checked, linked groups across the whole holder base.
    deep = the quick-pass result after wprofile ran: its same-coin clusters (top 20 only) move wallets out of 'independent'."""
    if d.get("error"):
        return f"== ALL HOLDERS: {d['error']}"
    b = dict(d.get("breakdown") or {})
    cl = [set(c["who"]) for c in ((deep or {}).get("shared") or {}).get("clusters") or []]
    if cl and b.get("buckets"):
        ing = {a for g in d["groups"] for a in g["members"]}
        move = {a for c in cl for a in c} - ing
        byw = {h["addr"]: h for h in d["wallets"]}
        buckets = dict(b["buckets"])
        moved = 0.0
        for a in move:
            h = byw.get(a)
            if h and h["bal"] > 0 and h.get("bucket") in buckets and h.get("bucket") != CLUSTER_BUCKET:
                buckets[h["bucket"]] = max(0.0, buckets[h["bucket"]] - h["pct"])
                moved += h["pct"]
        if moved:
            buckets[CLUSTER_BUCKET] = buckets.get(CLUSTER_BUCKET, 0) + moved
            b["buckets"] = sorted(buckets.items(), key=lambda kv: -kv[1])
    hw = sorted([h for h in d["wallets"] if h["bal"] > 0], key=lambda h: -h["bal"])
    rank = {h["addr"]: i for i, h in enumerate(hw, 1)}
    by = {h["addr"]: h for h in d["wallets"]}
    from lib.tracked import tag
    L = ["", f"== ALL HOLDERS: {b.get('n_checked', 0)} wallets checked (every wallet with >= 0.01% of supply), "
             f"{b.get('n_dust', 0)} dust wallets below that"]
    L.append("SUPPLY 100%  " + " · ".join(f"{k} {v:.1f}%" for k, v in b.get("buckets", []) if v >= 0.05))
    L += render_tracked(d)
    G = sorted([g for g in d["groups"] if g["pct"] >= 0.1 or g["got_pct"] >= 0.5], key=lambda g: (g["pct"] < 0.05, g["conf"] != "proven", -g["pct"], -g["got_pct"]))
    rest = [g for g in d["groups"] if g not in G]
    L.append(f"LINKED GROUPS ACROSS ALL HOLDERS  {len(d['groups'])} group(s) hold {sum(g['pct'] for g in d['groups']):.1f}% now"
             + (f" ({len(rest)} tiny ones under 0.1% not listed, {sum(g['pct'] for g in rest):.2f}%)" if rest else ""))
    for i, g in enumerate(G, 1):
        m = [a for a in g["members"] if a in by]
        top = [a for a in m if rank.get(a, 999) <= 25]
        small = [a for a in m if a not in top]
        L.append(f" A{i} [{g['conf'].upper()}]  {len(m) + g['unprofiled']} wallets · holds {g['pct']:.2f}% now · took in {g['got_pct']:.2f}%"
                 f" · sold {g['sold_pct']:.2f}%" + ("  ** INCLUDES THE DEV **" if g["dev"] else "")
                 + (f"  includes top holder(s) {', '.join('#%d' % rank[a] for a in top)}" if top else "  (no top-25 wallet: hidden in the small holders)"))
        for a in sorted(m, key=lambda a: -by[a]["bal"])[:6]:
            h = by[a]
            L.append(f"     {('#%d' % rank[a]) if a in rank else '  -'} {a}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}%"
                     + (" · sold %.0f%%" % (100 * h["sold"] / max(1, h["bought"] + h["received"] + h["minted"])) if h["sold"] else "") + tag(a))
        if len(m) > 6:
            L.append(f"     … +{len(m) - 6} more wallets holding {sum(by[a]['pct'] for a in sorted(m, key=lambda a: -by[a]['bal'])[6:]):.2f}%")
        for w in g["why"][:4]:
            L.append(f"     - {w}")
        if len(g["why"]) > 4:
            L.append(f"     - … {len(g['why']) - 4} more links")
    nc = [c for c in d.get("net_clusters") or [] if c["pct"] >= 0.1]
    if d.get("net_clusters") is not None:
        L.append(f"SAME-COIN CLUSTERS ACROSS ALL HOLDERS (pattern, not proof)  {len(nc)} cluster(s) hold {sum(c['pct'] for c in nc):.1f}% now"
                 " — wallets that keep buying the same rare coins within 10 min of each other: one trader, a copy-trader or a calling group")
        for i, c in enumerate(nc, 1):
            top = [a for a in c["who"] if rank.get(a, 999) <= 25]
            L.append(f" C{i}  {len(c['who'])} wallets · holds {c['pct']:.2f}% now"
                     + (f"  includes top holder(s) {', '.join('#%d' % rank[a] for a in top)}" if top else "  (hidden in the small holders)"))
            for a in c["who"][:8]:
                h = by.get(a)
                if h:
                    L.append(f"     {('#%d' % rank[a]) if a in rank else '  -'} {a}  now {h['pct']:.2f}%"
                             + (" · sold %.0f%%" % (100 * h["sold"] / max(1, h["bought"] + h["received"] + h["minted"])) if h["sold"] else "") + tag(a))
            if len(c["who"]) > 8:
                L.append(f"     … +{len(c['who']) - 8} more")
            for a, b, n, t, t1, m, *det in c["pairs"][:3]:
                x = det[0] if det else {}
                how = (f" · {x['same']} in the SAME BLOCK (one operator's script)" if x.get("kind") == "same block" else
                       f" · {'first' if x['lead'][0] > x['lead'][1] else 'second'} on {max(x['lead'])} of {sum(x['lead'])}" if x.get("kind") == "leader" else "")
                L.append(f"     - {('#%d' % rank[a]) if a in rank else a[:10]} + {('#%d' % rank[b]) if b in rank else b[:10]}: "
                         f"{n} rare coins in common, {t} bought within 10 min of each other ({t1} within 60 s); the less active one traded {m} coins{how}")
    lead = d.get("leaders") or []
    if lead:
        L.append(f"LEADERS / FOLLOWERS (key wallets, last {d.get('net_days', 7)} d; pattern, not proof): one wallet buys, the other follows seconds-minutes later, coin after coin")
        for x in lead[:10]:
            fa, fo = x["leader"], x["follower"]
            L.append(f"   {('#%d' % rank[fa]) if fa in rank else fa} -> {('#%d' % rank[fo]) if fo in rank else fo}: followed on {x['led']} of {x['of']} coins, "
                     f"median {x['median_dt']} s later" + (f", {x['this_dt']} s later on this coin" if x.get("this_dt") is not None else ""))
    dcs = d.get("dev_coins") or []
    for dc in dcs:
        early = [(a, dt) for a, dt in dc.get("early") or [] if a != dc.get("dev")]
        L.append(f"DEV'S OTHER COIN {dc['symbol']} {dc['ca']} ({dc.get('state')}): "
                 + (("in within 60 s of its launch: " + ", ".join(f"{('#%d' % rank[a]) if a in rank else a} +{dt}s" for a, dt in early[:10])) if early else "none of these wallets bought it in its first minute"))
    bl = [p for p in d["possible"] if p["kind"].startswith("first buy in the same block") or p["kind"].startswith("funded by the dev")]
    if bl:
        L.append("POSSIBLE (all holders)")
        for p in bl:
            L.append(f"   {len(p['members'])} wallets, {p['pct']:.2f}% now / {p['got_pct']:.2f}% taken in: {p['kind']}")
    miss = [n for n in d["notes"] if n.startswith("funder of ") and " not found" in n]
    if miss:
        L.append(f"   funder not found for {len(miss)} wallet(s) (they can't be linked by funding)")
    for n in d["notes"]:  # the all-holder history notes (what was read, what failed) belong in the output
        if n.startswith(("holder histories", "holder token-transfer history", "all-holder history", "history not read", "all-holder history scan failed")):
            L.append(f"   {n}")
    L.append(f"   ({d['secs']}s: " + ", ".join(f"{k} {v}s" for k, v in d["timing"].items()) + ")")
    return "\n".join(L)
