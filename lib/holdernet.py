"""Links across EVERY checked holder from their full wallet histories (Alchemy), for the ALL HOLDERS pass of `dd holders`.

  1. sends between holders   a holder sent native coin or any other token straight to another holder      -> PROVEN
  2. exchange deposit address two holders sent funds to the same address, and that address forwards
                             (almost) everything to one exchange hot wallet: one exchange account         -> PROVEN
  3. same-coin clusters      pairs that bought the same RARE coins (2..RARE_MAX of the checked holders hold
                             them; a coin half the holders bought is the crowd) within 10 min, repeatedly.
                             Airdrops dropped (one tx reaching 3+ holders; no payment in the first tx).    -> not proof
Pair rule, relative to activity (m = coins the less active of the two traded; busy bots share hundreds by chance):
>= 3 rare coins bought within 60 s of each other and >= 10% of m, or >= 3 within 10 min and >= 25% of m, or >= 2 within
10 min when one wallet is quiet (m <= 10: two matches out of so few picks is not chance). This token's own first buys <= 60 s apart count
as one such match.
"""
import asyncio
from collections import defaultdict, Counter
from lib import alchemy
from lib.rpc import RPC
from lib.trace import EXCHANGE_NONCE

RARE_MAX = 10
QUIET = 10
TOG = 600
MIN_NATIVE = 0.01  # smaller native sends are the spam / address-poisoning range
MAX_RCPT = 4
OUT_TOP = 60  # holders whose outgoing transfers are read (exchange deposit addresses)
RECEIPT_CAP = 1500  # real-buy receipt checks per scan


async def _hist(chain, a, outgoing):
    """-> (outgoing, incoming) transfers. Incoming (native + tokens) for every holder: its coins, and sends from other
    holders seen from the receiving side. Outgoing only for the biggest holders (deposit addresses): Alchemy's free
    plan does ~8 calls/s, so one call per wallet is what keeps ~400 holders near a minute."""
    from lib import cache
    key = f"alch:net:{chain}:{a}:{int(outgoing)}"
    c = cache.get(key)
    if c is not None:
        return c
    try:
        inn = await alchemy.transfers(chain, a, "to", ["external", "erc20"], low=True)
        out = await alchemy.transfers(chain, a, "from", ["external", "erc20"], low=True) if outgoing else []
    except Exception:
        return None
    cache.set(key, [out, inn], ttl=1800)
    return out, inn


async def _hist_logs(chain, A, big, days=7):
    """Every checked holder's token transfers of the last `days` days from ONE multi-wallet Transfer log scan on our own
    node (all wallets in one topic filter), shaped like Alchemy rows so the same link / cluster code runs on them.
    The cost grows with the block range, not with the number of wallets: SpaceXCoin (bsc) 756 wallets x 7 d = 20 s in one
    filter; chunks of 100 wallets took 73 s and Alchemy one-wallet-at-a-time ran 6 min and timed out. Busy snipers stay
    in (their same-block pairs are the finding)."""
    import time
    from datetime import datetime, timezone
    from lib import chainhist
    now = time.time()
    _hist_logs.skipped = 0

    _hist_logs.errors = []

    async def part(chunk):
        for i in range(2):
            try:
                # incoming only: coins (first buys) and holder-to-holder sends are both seen from the receiving side;
                # native sends come from Alchemy. Halves the node calls (the scan shares Chainstack's rate with the
                # profiles' log scans on every chain and ran > 44 s under load).
                return await asyncio.wait_for(chainhist.home_logs(chain, chunk, now, days, outgoing=False), 75)
            except Exception as e:
                if i:
                    _hist_logs.errors.append(f"{type(e).__name__}: {str(e)[:80]}")
                    return {}
                await asyncio.sleep(1)
    parts = await asyncio.gather(*[part(A[i:i + 800]) for i in range(0, len(A), 800)])
    H = {}
    for rows in parts:
        for a, rs in rows.items():
            inn, out = [], []
            for x in rs:
                row = {"category": "erc20", "hash": x["hash"], "from": x["from"], "to": x["to"], "asset": None,
                       "blockNum": hex(int(x["blockNumber"])), "uniqueId": f"{x['hash']}:{x['from']}:{x['to']}:{x['contractAddress']}",
                       "rawContract": {"address": x["contractAddress"], "value": hex(int(x["value"] or 0))},
                       "metadata": {"blockTimestamp": datetime.fromtimestamp(int(x["timeStamp"]), timezone.utc).isoformat().replace("+00:00", "Z")}}
                if x["to"] == a:
                    inn.append(row)
                elif x["from"] == a and a in big:
                    out.append(row)
            H[a] = (out, inn)
    return H


async def _native(chain, A0, big, deadline):
    """Native-coin sends (BNB/ETH) for the wallets that matter most (top holders, big exited buyers, dev, tracked):
    Alchemy, incoming for all of them + outgoing for the biggest. Token logs can't show native sends. Whatever is not
    read by the deadline is skipped (counted in the notes)."""
    import time
    from lib import cache
    if not alchemy.available(chain):
        return {}, len(A0)

    async def one(a):
        key = f"alch:nat:{chain}:{a}:{int(a in big)}"
        c = cache.get(key)
        if c is not None:
            return c
        inn = await alchemy.transfers(chain, a, "to", ["external"], max_count=300, low=True)
        out = await alchemy.transfers(chain, a, "from", ["external"], max_count=300, low=True) if a in big else []
        cache.set(key, [out, inn], ttl=1800)
        return out, inn
    tasks = {a: asyncio.ensure_future(one(a)) for a in A0}
    left = max(1.0, deadline - time.time()) if deadline else 60
    await asyncio.wait(list(tasks.values()), timeout=left)
    res, miss = {}, 0
    for a, t in tasks.items():
        if t.done() and not t.exception():
            res[a] = t.result()
        else:
            miss += 1
            t.cancel()
    return res, miss


def _amt(x):
    return alchemy._raw(x)


async def load(chain, A, pct0, devs, core=None, days=7, deadline=None):
    """Holder histories, started as soon as the wallet list is known (runs alongside the funder lookups).
    -> (H {addr: (outgoing rows, incoming rows)}, big, notes)"""
    import time
    S = set(A)
    devs = [x.lower() for x in devs or []]
    big = set(sorted(A, key=lambda a: -(pct0.get(a) or 0))[:OUT_TOP]) | (set(devs) & S)
    A0 = [a for a in (core or A[:80]) if a in S]
    big |= set(A0[:OUT_TOP])
    sym = RPC(chain).cfg.get("symbol") or "ETH"
    notes = []
    if RPC(chain).private:
        (H, (nat, miss)) = await asyncio.gather(_hist_logs(chain, A, big, days),
                                                _native(chain, A0, big, (deadline - 8) if deadline else time.time() + 40))
        for a, (o, i) in nat.items():
            H[a] = (H[a][0] + o, H[a][1] + i) if a in H else (o, i)
        if getattr(_hist_logs, "errors", None):
            notes.append(f"holder token-transfer history NOT read ({'; '.join(_hist_logs.errors)[:160]}): same-coin clusters / leaders missing")
        notes.append(f"holder histories = last {days} days of token transfers from our node ({len(H)} of {len(A)} wallets had any)"
                     + (f"; native {sym} sends read for {len(nat)} key wallets" + (f" ({miss} not read in time)" if miss else "")
                        if alchemy.available(chain) else f"; native {sym} sends between holders not seen (no Alchemy on {chain})"))
    else:
        res = await asyncio.gather(*[_hist(chain, a, a in big) for a in A])
        H = {a: r for a, r in zip(A, res) if r}
        if len(H) < len(A):
            notes.append(f"history not read for {len(A) - len(H)} of {len(A)} holders (Alchemy)")
    return H, big, notes


async def scan(chain, d, wallets, timing=None, days=7, deadline=None, core=None, loaded=None):
    """wallets: holder dicts (addr, first, pct). core: the wallets that matter most (top holders, exited big buyers,
    dev, tracked) whose native sends are read too. loaded: a load() result started earlier.
    -> {'links': [(a, b, why)], 'clusters': [...], 'notes': [...], 'dev_coins': [...]}"""
    from lib.wprofile import STABLES, WRAPPED, _real_buys
    from lib.xchain import BORING
    import time
    t0 = time.time()
    token = d["token"]
    sym = d.get("native") or "ETH"
    devs = [x.lower() for x in d.get("devs") or []]
    A = [h["addr"] for h in wallets]
    S = set(A)
    if loaded is None:
        loaded = await load(chain, A, {h["addr"]: h.get("pct") for h in wallets}, devs, core, days, deadline)
    H, big, notes = loaded
    notes = list(notes)
    if timing is not None:
        timing["all-holder history"] = time.time() - t0
    base = set(STABLES.get(chain, {})) | {WRAPPED.get(chain, "")}

    # --- 1. direct sends between holders (seen from the receiver: every holder's incoming transfers are read).
    #     Native >= MIN_NATIVE counts as is (a top-level transfer is signed by its sender). A token transfer counts only if
    #     the sender SIGNED that tx and it reached <= MAX_RCPT addresses: spam tokens fake 'transfer from X' events
    #     (address poisoning) and disperse bots spray memecoins to hundreds of wallets in one tx.
    cand = defaultdict(list)  # (a, b) -> [(kind, what, tx)]
    for b, (_, inn) in H.items():
        for x in inn:
            a = (x.get("from") or "").lower()
            if a not in S or b == a:
                continue
            c = ((x.get("rawContract") or {}).get("address") or "").lower()
            if c == token:
                continue  # this token's own transfers are already linked from the replay
            if x.get("category") == "external":
                v = _amt(x) / 1e18
                if v >= MIN_NATIVE:
                    cand[(a, b)].insert(0, ("native", f"{v:.4g} {sym}", x.get("hash")))
            elif x.get("category") == "erc20":
                cand[(a, b)].append(("token", x.get("asset") or "tokens", x.get("hash")))
    for a, (out, _) in H.items():
        for x in out:
            b = (x.get("to") or "").lower()
            if b in S and b != a and x.get("category") == "external" and _amt(x) / 1e18 >= MIN_NATIVE \
                    and not any(k == "native" for k, _, _ in cand[(a, b)]):
                cand[(a, b)].insert(0, ("native", f"{_amt(x) / 1e18:.4g} {sym}", x.get("hash")))
    links = []
    check = sorted({t for v in cand.values() for k, _, t in v[:3] if k == "token" and not any(kk == "native" for kk, _, _ in v)})
    good = set()
    if check:
        rpc0 = RPC(chain)
        r = await rpc0.batch([("eth_getTransactionByHash", [t]) for t in check] + [("eth_getTransactionReceipt", [t]) for t in check])
        n = len(check)
        TR = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        for i, t in enumerate(check):
            tx, rc = r[i], r[n + i]
            if not tx or not rc:
                continue
            rcpts = {lg["topics"][2] for lg in rc.get("logs") or [] if lg.get("topics") and lg["topics"][0] == TR and len(lg["topics"]) > 2}
            if len(rcpts) <= MAX_RCPT:
                good.add((t, (tx.get("from") or "").lower()))
    for (a, b), v in cand.items():
        for kind, what, t in v[:3]:
            if kind == "native" or (t, a) in good:
                links.append((a, b, f"{'the DEV ' if a in devs else ''}{a} sent {what} straight to {'the DEV ' if b in devs else 'wallet '}{b} (tx {t})"))
                break

    # --- 2. shared exchange deposit addresses
    recv = defaultdict(set)
    for a, (out, _) in H.items():
        for x in out:
            to = (x.get("to") or "").lower()
            if to and to not in S and to != token:
                if x.get("category") == "external" and _amt(x) / 1e18 < MIN_NATIVE:
                    continue
                recv[to].add(a)
    cand = [x for x, ws in recv.items() if len(ws) >= 2]
    rpc = RPC(chain)
    dep_n = 0
    if cand:
        r = await rpc.batch([("eth_getCode", [x, "latest"]) for x in cand] + [("eth_getTransactionCount", [x, "latest"]) for x in cand])
        n = len(cand)
        eoa = [x for i, x in enumerate(cand) if (r[i] or "0x") == "0x" and 0 < int(r[n + i] or "0x0", 16) < EXCHANGE_NONCE][:60]
        outs = await asyncio.gather(*[alchemy.transfers(chain, x, "from", ["external", "erc20"], max_count=100, low=True) for x in eoa], return_exceptions=True)
        dest = {}
        for x, o in zip(eoa, outs):
            if isinstance(o, Exception) or len(o) < 2:
                continue
            c = Counter((t.get("to") or "").lower() for t in o)
            top, k = c.most_common(1)[0]
            if k >= 0.9 * len(o):
                dest[x] = top
        if dest:
            tops = sorted(set(dest.values()))
            tn = await rpc.batch([("eth_getTransactionCount", [t, "latest"]) for t in tops])
            hot = {t for t, v in zip(tops, tn) if v and int(v, 16) >= EXCHANGE_NONCE}
            for x, t in dest.items():
                if t in hot:
                    ws = sorted(recv[x])
                    dep_n += 1
                    for w in ws[1:]:
                        links.append((ws[0], w, f"both sent funds to the same exchange deposit address {x} (it forwards everything to "
                                                f"exchange wallet {t}): one exchange account"))
    if timing is not None:
        timing["all-holder links"] = time.time() - t0

    # --- 3. same-coin clusters
    first = {}  # (coin, wallet) -> (ts, tx)
    by_tx = defaultdict(set)
    ncoins = Counter()
    for a, (_, inn) in H.items():
        coins = set()
        for x in inn:  # newest first: keep overwriting -> earliest
            if x.get("category") != "erc20":
                continue
            c = ((x.get("rawContract") or {}).get("address") or "").lower()
            if not c or c == token or c in base or (x.get("asset") or "").upper() in BORING:
                continue
            coins.add(c)
            first[(c, a)] = (alchemy._ts(x) or 0, x.get("hash"), int(x.get("blockNum") or "0x0", 16))
            by_tx[(c, x.get("hash"))].add(a)
        ncoins[a] = len(coins)
    spray = {k for k, ws in by_tx.items() if len(ws) >= 3}
    holders_of = defaultdict(list)
    for (c, a), (ts, h, _b) in first.items():
        if (c, h) not in spray:
            holders_of[c].append(a)
    rare = {c: ws for c, ws in holders_of.items() if 2 <= len(ws) <= RARE_MAX}
    ncoins = Counter(a for c, ws in holders_of.items() for a in ws)  # coins it got, airdrop sprays left out

    def count(keep=None):
        shared, tog, tight = Counter(), Counter(), Counter()
        for c, ws in rare.items():
            if keep is not None:
                ws = [a for a in ws if (c, a) in keep]
            for i, a in enumerate(ws):
                for b in ws[i + 1:]:
                    p = (a, b) if a < b else (b, a)
                    shared[p] += 1
                    dt = abs(first[(c, a)][0] - first[(c, b)][0])
                    if dt <= TOG:
                        tog[p] += 1
                    if dt <= 60:
                        tight[p] += 1
        return shared, tog, tight
    shared, tog, tight = count()
    # receipts only for the coins behind pairs that could qualify (a real-buy check on every rare coin costs ~30 s)
    need = {(c, a) for c, ws in rare.items() for a in ws
            if any(tog[(a, b) if a < b else (b, a)] >= 2 for b in ws if b != a)}
    # receipts are the costly part (SpaceXCoin: 7,461 -> ~25 s of node time that starved the quick pass and the
    # profiles): key wallets' coins first, then the pairs with the most coins together; the rest stays 'unread' (kept)
    core0 = set(core or [])
    best = Counter()
    for c, ws in rare.items():
        for i, a in enumerate(ws):
            for b in ws[i + 1:]:
                p = (a, b) if a < b else (b, a)
                best[(c, a)] = max(best[(c, a)], tog[p]); best[(c, b)] = max(best[(c, b)], tog[p])
    need = sorted(need, key=lambda k: (k[1] not in core0, -best[k]))
    capped = need[RECEIPT_CAP:]
    need = need[:RECEIPT_CAP]
    ok, unread = await _real_buys({(chain, c, a): first[(c, a)][1] for c, a in need})
    unread = set(unread) | {(chain, c, a) for c, a in capped}
    keep = {(c, a) for (_, c, a) in ok | unread} | {(c, a) for c, ws in rare.items() for a in ws if (c, a) not in need}
    shared, tog, tight = count(keep)
    fb = {h["addr"]: (h.get("first") or {}).get("ts") for h in wallets if (h.get("first") or {}).get("kind") == "bought"}
    for p in list(shared):
        a, b = p
        if fb.get(a) and fb.get(b) and abs(fb[a] - fb[b]) <= 60:
            tog[p] += 1; tight[p] += 1; shared[p] += 1

    # LEADER pairs among the key wallets (top holders, big exited buyers, dev): one wallet buys first and the other
    # follows seconds to minutes later, coin after coin = copy-trade or one person's side wallet. Coins are 'rare' here
    # relative to the key wallets: across 756 holders a trending coin has > 10 buyers and the pattern vanished
    # (SpaceXCoin #1 -> #12: 6 of 6 coins, #1 first by 20-375 s, and 29 s first on this token).
    core_set = set(core or []) & set(H)
    rare_core = {}
    for c, ws in holders_of.items():
        k = [a for a in ws if a in core_set and (c, a) in keep]
        if 2 <= len(k) <= RARE_MAX:
            rare_core[c] = k

    from lib.wprofile import leader_pairs
    byc = {c: {a: first[(c, a)][0] for a in ws} for c, ws in rare_core.items()}
    leaders = leader_pairs(byc, {c: {} for c in byc}, {a: t for a, t in fb.items() if a in core_set}, BORING)

    def is_strong(p):
        # overlap must be big RELATIVE to how much the wallets trade: two bots that buy every trending coin share
        # hundreds by chance. m = the smaller wallet's number of coins.
        m = max(1, min(ncoins[p[0]], ncoins[p[1]]))
        return (tight[p] >= 3 and tight[p] >= 0.1 * m) or (tog[p] >= 3 and tog[p] >= 0.25 * m) \
            or (tog[p] >= 2 and m <= QUIET)
    strong = [p for p in shared if is_strong(p)]
    from lib.wprofile import tight_groups
    groups = tight_groups(strong, lambda p: (tight[p], tog[p], shared[p]))
    pct = {h["addr"]: h.get("pct") or 0 for h in wallets}
    def detail(p):
        """How the pair buys together: same block (one operator's script), and who goes first (a leader and a follower
        = copy-trade or one person's side wallet). SpaceXCoin: 0x19dd/0xde2c 127 of 127 coins in the same block (one bot);
        #1 first on 8 of 8 coins with #4 / #12 20-70 s behind (followers)."""
        a, b = p
        same = lead_a = lead_b = 0
        dts = []
        for c, ws in rare.items():
            if (c, a) not in keep or (c, b) not in keep or a not in ws or b not in ws:
                continue
            ta, tb = first[(c, a)], first[(c, b)]
            dt = tb[0] - ta[0]
            if abs(dt) > TOG:
                continue
            if ta[2] and ta[2] == tb[2]:
                same += 1
                continue
            dts.append(abs(dt))
            if dt > 0:
                lead_a += 1
            elif dt < 0:
                lead_b += 1
        fa, fb_ = fb.get(a), fb.get(b)
        this = (fb_ - fa) if fa and fb_ else None
        n = same + lead_a + lead_b
        kind = None
        if n >= 3 and same >= 0.6 * n:
            kind = "same block"
        elif lead_a + lead_b >= 3 and max(lead_a, lead_b) >= 0.8 * (lead_a + lead_b):
            kind = "leader"
        return {"same": same, "lead": (lead_a, lead_b), "median_dt": int(sorted(dts)[len(dts) // 2]) if dts else None,
                "this_dt": this, "kind": kind}
    clusters = []
    for g in groups:
        g.sort(key=lambda a: -pct.get(a, 0))
        prs = sorted([p for p in strong if p[0] in g and p[1] in g], key=lambda p: (-tight[p], -tog[p], -shared[p]))
        clusters.append({"who": g, "pct": sum(pct.get(a, 0) for a in g),
                         "pairs": [(a, b, shared[(a, b)], tog[(a, b)], tight[(a, b)], min(ncoins[a], ncoins[b]), detail((a, b))) for a, b in prs]})
    clusters.sort(key=lambda c: -c["pct"])

    # --- 4. the dev's OTHER coins: who was in within 60 s of their launch (the dev's own sniper / insider wallets
    #     follow it from coin to coin; SpaceXCoin: 0x6d3e bought 4 blocks in, sniped the dev's next coin 1 s after
    #     launch, then got 1.149 BNB from the dev)
    dev_coins = []
    if devs:
        from lib import explorer as exmod, who
        rpc1, ex = RPC(chain), exmod.Explorer(chain)
        for dv in devs[:2]:
            try:
                txs = await asyncio.wait_for(ex.txlist(dv, limit=300), 15)
                past = await asyncio.wait_for(who.past_coins(rpc1, ex, dv, token, txs or []), 15)
            except Exception:
                continue
            for pc in past:
                c = pc["ca"]
                t_launch = first[(c, dv)][0] if (c, dv) in first else pc.get("launched")
                if not t_launch:
                    continue
                inn = sorted(((first[(c, a)][0] - t_launch, a) for a in A if a != dv and (c, a) in first
                              and -5 <= first[(c, a)][0] - t_launch <= 60), key=lambda x: x[0])
                dev_coins.append({**pc, "dev": dv, "early": [(a, int(dt)) for dt, a in inn]})
    if timing is not None:
        timing["all-holder clusters"] = time.time() - t0
    notes.append(f"all-holder history: {len(H)} wallets; {len(rare)} rare coins shared by 2-{RARE_MAX} holders, "
                 f"{sum(1 for k in spray)} airdrop txs dropped, {len(ok)} real buys; {dep_n} shared deposit address(es)")
    return {"links": links, "clusters": clusters, "notes": notes, "dev_coins": dev_coins, "leaders": leaders}
