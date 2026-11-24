"""Holder data collector for `dd holders`: who holds a token, how each wallet got it, and how the wallets are linked.

Facts only (the written analysis is done on top of this output):
  1. every Transfer of the token since launch is replayed -> balances, and per wallet: bought / sold / received / sent
  2. top holders are confirmed with balanceOf and split into pools, burn, lockers, contracts and plain wallets
  3. for the top wallets + the dev: first funder (2 hops, bridges followed), tx count, native balance, who signed the buy
  4. links: same funder, funder is another holder, same bridge origin, same buy signer, tokens passed between holders
     ('proven' = on-chain fact); same exchange within 30 min and same launch block are listed as 'possible'
"""
import asyncio, time
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
                    kind = "bought"; x["bought"] += d; x["buys"] += 1
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
                    x["sold"] += -d; x["sells"] += 1
                else:
                    x["sent"] += -d
                    for r in items:
                        if r["from"] == a and r["to"] not in pools:
                            x["sent_to"][r["to"]] += r["v"]
    return bal, act, start


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


async def collect(chain, token, top=25, profile=20):
    t0 = time.time()
    rpc, ex = RPC(chain), explorer.Explorer(chain)
    token = token.lower()
    notes, timing = [], {}
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
    bal, act, start = replay(rows, pools)
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
    cand = list(dict.fromkeys(by_bal + by_acq + coll + dist + feeders[:120] + sorted(devs)))
    res = await rpc.batch([("eth_getCode", [a, "latest"]) for a in cand] +
                          [("eth_call", [{"to": token, "data": encode_call("balanceOf(address)", [a])}, "latest"]) for a in cand])
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
              and hold[a]["bal"] < acq[a] * 0.1][:25]
    coll_w = [a for a in coll if isw(a)]
    dist_w = [a for a in dist if isw(a) and a not in devs]
    feed_w = [a for a in feeders if isw(a)]
    if len(feed_w) > 40:
        notes.append(f"{len(feed_w)} wallets fed a collector; only the 40 biggest are profiled (all are counted in the group totals)")
    for role, lst in (("holder", top_now), ("exited", exited), ("collector", coll_w), ("distributor", dist_w), ("fed a collector", feed_w[:40]),
                      ("dev", [d for d in devs if isw(d)])):
        for a in lst:
            hold[a]["roles"].append(role)
    W = list(dict.fromkeys(top_now + exited + coll_w + dist_w + feed_w[:40] + [d for d in devs if isw(d)]))
    wallets = [hold[a] for a in W]

    # --- per wallet: the tx that first gave it tokens, block times, tx count, native balance, first funder
    first_txs = sorted({act[a]["first"]["tx"] for a in W if act[a]["first"]})
    blocks = sorted({act[a]["first"]["block"] for a in W if act[a]["first"]} | ({start} if start else set()) | {info["created_block"], latest})

    async def funded(a):
        try:
            return await asyncio.wait_for(ex.funded_by(a), 150)
        except Exception as e:
            return {"error": type(e).__name__}

    t = time.time()
    txr, blr, nb, fund = await asyncio.gather(
        rpc.batch([("eth_getTransactionByHash", [h]) for h in first_txs]),
        rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in blocks]),
        rpc.batch([("eth_getTransactionCount", [a, "latest"]) for a in W] + [("eth_getBalance", [a, "latest"]) for a in W]),
        asyncio.gather(*[funded(a) for a in W]))
    timing["wallets"] = time.time() - t
    txd = {h: x for h, x in zip(first_txs, txr) if x}
    bts = {b: int(x["timestamp"], 16) for b, x in zip(blocks, blr) if x}
    now = bts.get(latest) or int(time.time())
    for i, h in enumerate(wallets):
        a = h["addr"]
        x = act[a]
        h["nonce"] = int(nb[i], 16) if nb[i] else None
        h["native"] = int(nb[len(W) + i], 16) / 1e18 if nb[len(W) + i] else None
        h.update({k: x[k] for k in ("bought", "sold", "received", "sent", "minted", "seeded", "buys", "sells")})
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
        if fb.get("error") or not fb:
            notes.append(f"funder of {a} not found ({fb.get('error') or 'no inbound native transfer seen'})")

    # --- funders: exchange / contract / plain wallet; one more hop for plain wallets; bridge payouts matched to their origin
    t = time.time()
    F = sorted({h["funder"] for h in wallets if h["funder"]})
    fr = await rpc.batch([("eth_getTransactionCount", [f, "latest"]) for f in F] + [("eth_getCode", [f, "latest"]) for f in F]) if F else []
    fk = {}
    for i, f in enumerate(F):
        n = int(fr[i], 16) if fr[i] else 0
        c = fr[len(F) + i] or "0x"
        fk[f] = {"nonce": n, "kind": "contract" if (len(c) > 2 and not delegation_7702(c)) else "exchange/service" if n >= EXCHANGE_NONCE else "wallet"}
    # a busy funder (>= 1,000 txs) or one shared by several holders may be a payout service (an exchange hot wallet,
    # a bridge solver, a trading app's withdrawal wallet) that pays out to many people: then sharing it proves nothing
    shared = Counter(h["funder"] for h in wallets if h["funder"])
    svc_check = [f for f in F if fk[f]["kind"] == "wallet" and (fk[f]["nonce"] >= 1000 or shared[f] >= 2)]

    async def payout_service(f):
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
    svc_res, ftx = await asyncio.gather(asyncio.gather(*[payout_service(f) for f in svc_check]),
                                        rpc.batch([("eth_getTransactionByHash", [x]) for x in fund_txs]) if fund_txs else asyncio.sleep(0, []))
    for f, why in zip(svc_check, svc_res):
        if why:
            fk[f]["kind"], fk[f]["why"] = "payout service", why
    ftx = {x: t for x, t in zip(fund_txs, ftx or []) if t}
    for h in wallets:
        fx = ftx.get(h.get("fund_tx"))
        h["fund_amt"] = int(fx["value"], 16) / 1e18 if fx else None
        h["fund_block"] = int(fx["blockNumber"], 16) if fx else None
    hop2 = [f for f in F if fk[f]["kind"] == "wallet" and f not in W]
    sem = asyncio.Semaphore(6)

    async def bridge(h):
        async with sem:
            try:
                return await asyncio.wait_for(bridge_fill(h["fund_tx"], h["addr"]), 12)
            except Exception:
                return None

    maybe_bridge = [h for h in wallets if h["funder"] and h["fund_tx"] and fk[h["funder"]]["kind"] != "wallet"]
    h2, br = await asyncio.gather(asyncio.gather(*[funded(f) for f in hop2]), asyncio.gather(*[bridge(h) for h in maybe_bridge]))
    for f, r in zip(hop2, h2):
        fk[f]["funder"] = ((r or {}).get("from") or "").lower() or None
    G = sorted({v["funder"] for v in fk.values() if v.get("funder")} - set(fk))
    if G:
        gr = await rpc.batch([("eth_getTransactionCount", [g, "latest"]) for g in G] + [("eth_getCode", [g, "latest"]) for g in G])
        for i, g in enumerate(G):
            n = int(gr[i], 16) if gr[i] else 0
            c = gr[len(G) + i] or "0x"
            fk[g] = {"nonce": n, "kind": "contract" if (len(c) > 2 and not delegation_7702(c)) else "exchange/service" if n >= EXCHANGE_NONCE else "wallet"}
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
    chk = [x for x in signers if x not in relayer and sk[x] >= 50]
    for x, r in zip(chk, await asyncio.gather(*[app_relayer(x) for x in chk])):
        if r:
            relayer.add(x)
    by_relayer = defaultdict(list)
    for h in wallets:
        f = h.get("first") or {}
        if f.get("signer") and f["signer"] != h["addr"]:
            f["signer_txs"] = sk.get(f["signer"])
            f["signer_relayer"] = f["signer"] in relayer

    # --- links
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
    def group(g, conf):
        gs = set(g)
        why = sorted({r for pair, rs in L.why.items() if pair <= gs for r in rs})
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

    # --- possible links (not merged): same exchange within 30 min, same block at launch
    possible = []
    d_kind = lambda f: fk.get(f, {}).get("kind", "?")
    ex_f = defaultdict(list)
    for h in wallets:
        if h["funder"] and fk[h["funder"]]["kind"] not in ("wallet", "payout service") and not h.get("bridge") and h["fund_ts"]:
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

    pair0 = pairs[0] if pairs else {}
    out = {"chain": chain, "token": token, "name": info.get("name"), "symbol": info.get("symbol"), "decimals": dec, "supply": supply,
            "native": sym, "devs": sorted(devs), "created_block": info["created_block"], "created_ts": bts.get(info["created_block"]),
            "start_block": start, "start_ts": bts.get(start) if start else None, "now": now, "latest": latest,
            "mcap": pair0.get("marketCap") or pair0.get("fdv"), "liq": (pair0.get("liquidity") or {}).get("usd"),
            "n_transfers": len(rows), "n_holders": sum(1 for a, v in bal.items() if v > 0 and a not in skip), "n_ever": sum(1 for a in act if a not in skip), "pools": sorted(pools),
            "hold": sorted(hold.values(), key=lambda h: -h["bal"]), "wallets": wallets, "funders": fk, "groups": groups,
            "possible": possible, "notes": notes, "timing": timing}
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
    L = [f" {('#%d ' % n) if n else ''}{h['addr']}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}% in total" + (f"  [{', '.join(roles).upper()}]" if roles else "")]
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
             f"seeded liquidity {q(h['seeded']):,.0f}" if h["seeded"] else None]
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
    line = f"funded {amt}by {fu} ({k.get('kind')}, {k.get('nonce', 0):,} txs" + (f"; {k['why']}" if k.get("why") else "") + ")"
    if k.get("funder"):
        k2 = d["funders"].get(k["funder"], {})
        line += f" ← {k['funder']} ({k2.get('kind')}, {k2.get('nonce', 0):,} txs)"
    return line


def render(d):
    if d.get("error"):
        return f"== HOLDERS: {d['error']}"
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
    L.append("SUPPLY NOW  " + " · ".join(f"{k} {v:.1f}%" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]) if k != "wallet" and v >= 0.05)
             + f" · top {len(now_w)} wallets {sum(h['pct'] for h in now_w):.1f}%")
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
            L.append(f"     {a}  now {h['pct']:.2f}% · got {h['got_pct']:.2f}%  [{', '.join(h['roles'])}]")
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
        L += [f"   - {n}" for n in d["notes"][:25]]
    L.append(f"   ({d['secs']}s: " + ", ".join(f"{k} {v}s" for k, v in d["timing"].items()) + ")")
    return "\n".join(L)
