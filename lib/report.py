"""The holder REPORT `dd holders` ends with (2026-11-26, user: "the tool writes the report"): the answer in the user's
format, built from the quick pass + deep profiles + all-holder pass, so a check needs no hand-written profiles.

  1. SUMMARY OF FINDINGS: numbered, each = evidence + "Meaning:" (what it means for the trade). Only non-obvious
     points: tracked wallets, hidden combined holders (proven groups + leader/follower pairs + same-block operators
     among CURRENT holders, merged into one entity each), the dev (fresh, sold, paid whom, its other coins and who
     sniped them), what the exited buyers were and what of them still holds, the narrative crowd, alpha, fresh/dormant.
  2. EVERY TOP HOLDER: % owned (+ got / sold), entry, who the wallet is, history, before-buy numbers, links, traits.
  3. EXITED BIG BUYERS, grouped by operator.
  4. NOT CHECKED.
Every wallet is a clickable explorer link. Written to data/reports/ (never pushed) and printed."""
import os, time
from collections import defaultdict
from lib.rpc import ROOT, RPC


def _web(chain):
    from lib import explorer
    return (explorer.CHAINS.get(chain, {}).get("explorer") or {}).get("web") or ""


class _Ctx:
    def __init__(self, d, fd):
        self.d, self.fd = d, (fd if fd and not fd.get("error") else {})
        self.web = _web(d["chain"])
        W = list(d.get("wallets") or [])
        self.by = {h["addr"]: h for h in (self.fd.get("wallets") or [])}
        self.by.update({h["addr"]: h for h in W})  # quick-pass dicts carry the profiles
        self.top = [h for h in W if "holder" in h["roles"]]
        self.rank = {h["addr"]: i + 1 for i, h in enumerate(self.top)}
        self.exited = [h for h in W if "exited" in h["roles"]]
        for h in self.fd.get("wallets") or []:
            if "exited" in h["roles"] and h["addr"] not in {x["addr"] for x in self.exited}:
                self.exited.append(h)
        self.exited.sort(key=lambda h: -(h.get("got_pct") or 0))
        self.devs = set(d.get("devs") or [])
        self.track = {r["addr"]: r.get("label") for r in d.get("tracked") or []}

    def link(self, a):
        s = f"{a[:6]}…{a[-4:]}"
        return f"[{s}]({self.web}/address/{a})" if self.web else s

    def name(self, a):
        """'#3 [0x12…ab](url)' / 'exited [..]' / 'DEV [..]' + tracked star"""
        h = self.by.get(a) or {}
        tag = f"#{self.rank[a]}" if a in self.rank else "DEV" if a in self.devs else \
            "exited" if "exited" in (h.get("roles") or []) else "holder" if (h.get("pct") or 0) > 0 else "wallet"
        star = f" ★ {self.track[a]}" if a in self.track else ""
        return f"{tag} {self.link(a)}{star}"

    def pct(self, a):
        return (self.by.get(a) or {}).get("pct") or 0


def _mins(d, h):
    f = h.get("first") or {}
    if f.get("ts") and d.get("start_ts"):
        m = (f["ts"] - d["start_ts"]) / 60
        return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"
    return None


def _usd(x):
    if x is None:
        return "?"
    return f"${x / 1e6:,.1f}M" if x >= 1e6 else f"${x / 1e3:,.0f}k" if x >= 1e3 else f"${x:,.0f}"


def _entities(c):
    """Current holders tied together (proven groups, leader/follower pairs, same-block clusters) -> merged entities."""
    d, fd = c.d, c.fd
    parent = {}

    def f(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    why = defaultdict(list)

    def join(a, b, reason):
        parent[f(a)] = f(b)
        why[frozenset((a, b))].append(reason)
    for g in (fd.get("groups") or d.get("groups") or []):
        if g.get("conf") != "proven":
            continue
        ms = [m for m in g["members"] if c.pct(m) > 0]
        if len(ms) >= 2:
            r = (g.get("why") or ["proven link"])[0]
            for m in ms[1:]:
                join(ms[0], m, "PROVEN: " + _short(r, c))
    for x in ((d.get("shared") or {}).get("leaders") or []) + (fd.get("leaders") or []):
        a, b = x["leader"], x["follower"]
        if c.pct(a) > 0 and c.pct(b) > 0:
            join(a, b, f"{c.name(b)} follows {c.name(a)}: bought after it on {x['led']} of {x['of']} coins, median {x['median_dt']} s later"
                       + (f", {x['this_dt']} s later on this coin" if x.get("this_dt") else ""))
    for cl in fd.get("net_clusters") or []:
        for a, b, sh, tog, tight, m, det in cl["pairs"]:
            if c.pct(a) > 0 and c.pct(b) > 0 and det and det.get("kind") == "same block":
                join(a, b, f"same block on {det['same']} of {tog} shared small coins: one operator's script")
    groups = defaultdict(set)
    for a in list(parent):
        groups[f(a)].add(a)
    out = []
    for ms in groups.values():
        if len(ms) < 2:
            continue
        reasons = [r for p, rs in why.items() if p <= ms for r in rs]
        out.append({"members": sorted(ms, key=lambda a: -c.pct(a)), "pct": sum(c.pct(a) for a in ms), "why": reasons})
    out.sort(key=lambda e: -e["pct"])
    return out


def _short(r, c, n=220):
    """shorten a group reason, then turn addresses into labelled links (cut BEFORE linking: a cut through a markdown
    link broke the page)"""
    import re
    r = re.sub(r" \(tx 0x[0-9a-f]{64}\)", "", r).replace("the DEV ", "").replace("straight to wallet ", "to ").replace("straight to the DEV ", "to ")
    r = re.sub(r"\b(?:by|from|to) holder (0x)", lambda m: m.group(0).replace("holder ", ""), r).replace("signed by holder ", "signed by ")
    if len(r) > n:
        r = r[:n].rsplit(" ", 1)[0] + "…"
    return re.sub(r"0x[0-9a-f]{40}", lambda m: c.name(m.group(0)) if m.group(0) in c.by else c.link(m.group(0)), r)


def _findings(c):
    d, fd = c.d, c.fd
    F = []
    # 1. tracked wallets
    tr = [r for r in d.get("tracked") or [] if (r.get("got_pct") or 0) > 0]
    if tr:
        rows = []
        for r in tr:
            s = f"★ **{r['label']}** {c.link(r['addr'])}: holds {r.get('pct') or 0:.2f}%" + (f" (#{r['rank']})" if r.get("rank") else "")
            h = c.by.get(r["addr"]) or {}
            m = _mins(d, h)
            if m:
                s += f", bought {m} after launch"
            if r.get("paid"):
                s += f", paid {r['paid']:.3g} {d['native']}"
            s += f", sold {100 * (r.get('sold_share') or 0):.0f}%"
            rows.append(s)
        held = [r for r in tr if (r.get("sold_share") or 0) < 0.1 and (r.get("pct") or 0) > 0]
        F.append(("Your tracked wallets: " + "; ".join(rows) + ".",
                  ("still fully in: " + ", ".join(r["label"] for r in held) if held else "none of them is still fully in")
                  + "; watch them for exits."))
    # 2. hidden combined holders among CURRENT holders
    for e in _entities(c)[:4]:
        if e["pct"] < 0.3:
            continue
        ms = ", ".join(f"{c.name(a)} {c.pct(a):.2f}%" for a in e["members"][:6])
        lead = [r for r in e["why"] if "follows" in r]
        F.append((f"**One combined holder of {e['pct']:.2f}%**: {ms}. " + " · ".join(list(dict.fromkeys(e["why"]))[:4]) + ".",
                  ("when the leader sells, the followers are likely to sell with it" if lead else
                   "treat them as ONE holder") + f": {e['pct']:.2f}% can move at once."))
    # 3. the dev
    for dv in sorted(c.devs):
        h = c.by.get(dv)
        if not h:
            continue
        got = h.get("got_pct") or 0
        sold = 100 * ((h.get("sold") or 0) + (h.get("sent") or 0)) / max(1e-9, (h.get("bought") or 0) + (h.get("received") or 0) + (h.get("minted") or 0))
        pb = (h.get("profile") or {}).get("pre_buy") or {}
        bits = [f"dev {c.link(dv)} got {got:.2f}%, holds {h.get('pct') or 0:.2f}% (sold/sent {min(100, sold):.0f}%)"]
        if pb.get("age_days") is not None and pb["age_days"] <= 3:
            bits.append(f"fresh wallet ({pb['age_days']:.1f} d old, {pb['nonce']} txs)")
        paid = []
        for g in (fd.get("groups") or []):
            for r in g.get("why") or []:
                if r.startswith("the DEV ") and "sent" in r:
                    paid.append(_short(r, c))
        if paid:
            bits.append("**" + "; ".join(dict.fromkeys(paid)) + "**")
        meaning = []
        for dc in fd.get("dev_coins") or []:
            if dc.get("dev") != dv:
                continue
            early = [(a, dt) for a, dt in dc.get("early") or [] if a != dv][:8]
            s = f"its other coin {dc['symbol']} ({c.link(dc['ca'])}, {dc.get('state', '?')})"
            if early:
                s += ": in within 60 s of its launch: " + ", ".join(f"{c.name(a)} (+{dt} s)" for a, dt in early)
            bits.append(s)
            # an early buyer of the dev's other coin is an INSIDER only with a second tie to the dev (paid by it, or in a
            # proven group with it); launch snipers buy every new coin in its first seconds and prove nothing alone
            tied = set()
            for g in (fd.get("groups") or []):
                if dv in g["members"]:
                    tied |= set(g["members"])
            ins = [a for a, _ in early if a in tied and (c.by.get(a, {}).get("got_pct") or 0) > 0]
            if ins:
                meaning.append("insider / the dev's own sniper (paid by or linked to the dev AND in its other coin at launch): "
                               + ", ".join(c.name(a) for a in ins[:5]) + "; the dev farms launches with it")
            elif early:
                meaning.append("the wallets in at its launch are launch snipers that buy every new coin (no tie to the dev found)")
        if len(bits) > 1 or got >= 1:
            F.append(("; ".join(bits) + ".", "; ".join(meaning) or ("dev is out: no dev supply left to dump" if (h.get("pct") or 0) < 0.05 else
                                                                   f"dev still holds {h.get('pct'):.2f}%: watch it")))
    # 4. exited buyers: who they were, and what of them still holds
    ex = c.exited
    if ex:
        tot = sum(h.get("got_pct") or 0 for h in ex)
        ops = []
        exset = {h["addr"] for h in ex}
        for g in fd.get("groups") or []:
            ms = [m for m in g["members"] if m in exset or c.pct(m) > 0]
            if g.get("conf") == "proven" and len(ms) >= 2 and sum(m in exset for m in ms) >= len(ms) / 2 and g.get("got_pct", 0) >= 1 \
                    and g.get("pct", 0) < 0.25 * g.get("got_pct", 0):
                ops.append((g.get("got_pct", 0), " + ".join(c.name(m) for m in ms[:4]) + f" took {g.get('got_pct', 0):.2f}% ({_short((g.get('why') or [''])[0], c, 140)})"))
        still = []
        for cl in fd.get("net_clusters") or []:
            for a, b, sh, tog, tight, m, det in cl["pairs"]:
                if det and det.get("kind") == "same block" and (a in exset or b in exset):
                    ga = (c.by.get(a) or {}).get("got_pct", 0) + (c.by.get(b) or {}).get("got_pct", 0)
                    ops.append((ga, f"{c.name(a)} + {c.name(b)}: {det['same']} of {tog} shared coins bought in the same block (one bot) — took {ga:.2f}%"))
                    for x in (a, b):
                        if c.pct(x) > 0:
                            still.append(x)
        seen_ops, ops2 = [], []
        for o in sorted(ops, key=lambda x: -x[0]):
            import re as _re
            ms = set(_re.findall(r"\[(0x[0-9a-f]{4})…", o[1]))
            if any(ms <= m or m <= ms for m in seen_ops):
                continue
            seen_ops.append(ms)
            ops2.append(o)
        ops = ops2
        s = f"{len(ex)} big early buyers already sold out (took {tot:.0f}% of supply in total, counting repeat buys)"
        if ops:
            s += ". Operators among them: " + "; ".join(o for _, o in ops[:5])
        F.append((s + ".", ("still holding from those operators: " + ", ".join(f"{c.name(a)} {c.pct(a):.2f}%" for a in dict.fromkeys(still)) + " — expect it to be sold")
                  if still else "the early sniper supply is already dumped; what remains is later buyers"))
    # 5. entries vs now
    em = sorted(h["profile"]["entry_mcap"] for h in c.top if (h.get("profile") or {}).get("entry_mcap"))
    if len(em) >= 4 and d.get("mcap"):
        med = em[len(em) // 2]
        F.append((f"Top holders who bought entered at {_usd(em[0])}–{_usd(em[-1])} mcap (median {_usd(med)}); now {_usd(d['mcap'])}.",
                  f"the typical top holder is {'up' if d['mcap'] >= med else 'down'} {abs(d['mcap'] / med - 1) * 100:.0f}%"
                  + (": profit-taking pressure" if d["mcap"] >= 2 * med else ": little profit to take yet" if d["mcap"] >= med else ": many are under water")))
    # 6. narrative crowd
    sim = [x for x in (d.get("shared") or {}).get("coins") or [] if x.get("similar") and not x.get("popular")]
    if sim:
        x = sim[0]
        F.append((f"{x['n20']} of the top 20 also bought {x['symbol']} ({_usd(x.get('mcap')) if x.get('mcap') else x.get('state')}), a similar name"
                  + (", several within minutes of each other" if x.get("together") else "") + ".",
                  "the holder base rotates between look-alike tickers; that one is now " + (_usd(x.get("mcap")) if x.get("mcap") else str(x.get("state")))))
    # 7. alpha
    alpha = [(h, p["early_winners"]) for h in c.top for p in [h.get("profile") or {}] if p.get("early_winners")]
    if alpha:
        F.append(("Early on coins that went big: " + "; ".join(f"{c.name(h['addr'])}: " + ", ".join(f"{w['symbol']} ({_usd(w['mcap'])})" for w in ws[:3])
                                                              for h, ws in alpha[:6]) + ".", "these are the wallets with a real track record here"))
    else:
        F.append(("No top-20 wallet was early (< 1 h) on any coin now ≥ $1M (tokenized stocks excluded).", "no proven alpha in the holder base"))
    # 8. fresh / dormant
    fr, dm = [], []
    for h in c.top + [c.by[x] for x in c.devs if x in c.by]:
        pb = (h.get("profile") or {}).get("pre_buy") or {}
        t = _trait_fresh_dormant(pb)
        if t == "FRESH":
            fr.append(f"{c.name(h['addr'])} ({pb['age_days']:.1f} d old, {pb['nonce']} txs)")
        elif t == "DORMANT":
            dm.append(f"{c.name(h['addr'])}")
    if fr or dm:
        F.append(((("Fresh: " + ", ".join(fr) + ". ") if fr else "") + (("Dormant 30+ d then bought this: " + ", ".join(dm) + ".") if dm else ""),
                  "fresh wallets with size are often insiders' side wallets; dormant ones waking up for this coin are worth watching"))
    return F


def _trait_fresh_dormant(pb):
    if not pb:
        return None
    c30 = pb.get("coins30") or 0
    if pb.get("app"):
        return "DORMANT" if c30 == 0 and pb.get("coins_window_ok") else None
    if pb.get("age_days") is not None and pb["age_days"] <= 3:
        return "FRESH"
    if pb.get("d30") == 0 and pb.get("nonce", 0) >= 10 and c30 == 0:
        return "DORMANT"
    return None


def _profile(c, h):
    d = c.d
    a = h["addr"]
    p = h.get("profile") or {}
    got = h.get("got_pct") or 0
    inn = (h.get("bought") or 0) + (h.get("received") or 0) + (h.get("minted") or 0)
    sold = 100 * ((h.get("sold") or 0) + (h.get("sent") or 0)) / inn if inn else 0
    tags = []
    pb = p.get("pre_buy") or {}
    t = _trait_fresh_dormant(pb)
    if t:
        tags.append(t)
    if (h.get("nonce") or 0) >= 5000 or (h.get("buys") or 0) >= 200:
        tags.append("BOT")
    if p.get("early_winners"):
        tags.append("GEM HISTORY")
    if p and p.get("coins_90d") is not None and p["coins_90d"] <= 12 and not p.get("app_trader") and (h.get("nonce") or 0) < 2000 \
            and ((p.get("wealth") or {}).get("usd", 0) >= 5000 or (p.get("paid") or 0) * (p.get("eth_usd") or 0) >= 1000):
        tags.append("SELECTIVE")
    head = f"**{c.name(a)} · {h.get('pct') or 0:.2f}%**" + (f" (got {got:.2f}%, sold {sold:.0f}%)" if got - (h.get('pct') or 0) > 0.05 else "")
    if tags:
        head += " · " + " · ".join(tags)
    L = [head]
    f = h.get("first") or {}
    m = _mins(d, h)
    if f.get("kind") == "bought":
        e = f"bought {m} after launch" if m else "bought"
        e += f" in {h.get('buys') or 1} buy(s)"
        if p.get("paid"):
            e += f", first {p['paid']:.3g} {d['native']}"
            if p.get("entry_mcap"):
                e += f" at ~{_usd(p['entry_mcap'])} mcap"
            if p.get("size_vs_usual"):
                e += f" ({p['size_vs_usual']}x its usual size)"
    else:
        src = list((h.get("recv_from") or {}).keys())[:2]
        e = f"received its tokens{(' ' + m + ' after launch') if m else ''}" + (" from " + ", ".join(c.name(x) for x in src) if src else "")
    L.append("- Entry: " + e)
    who = []
    if p.get("app_trader") or pb.get("app"):
        who.append("trades through an app")
    if h.get("nonce") is not None and not (p.get("app_trader") or pb.get("app")):
        who.append(f"{h['nonce']:,} own txs")
    if pb.get("age_days") is not None:
        who.append(f"wallet {pb['age_days']:.0f} d old at the buy" if pb["age_days"] >= 2 else f"wallet {pb['age_days']:.1f} d old at the buy")
    if h.get("native") and h["native"] >= 0.05:
        who.append(f"{h['native']:.3g} {d['native']} on hand")
    w = (p.get("wealth") or {}).get("usd")
    if w and w >= 1000:
        who.append(f"~{_usd(w)} in native + stables across chains")
    if p.get("holdings_usd") and p["holdings_usd"] >= 1000:
        who.append(f"other memes {_usd(p['holdings_usd'])}")
    if who:
        L.append("- Wallet: " + ", ".join(who))
    if p:
        hs = f"- History: {p.get('coins_total', 0)} coins" + (f" ({p.get('coins_30d', 0)} in the last 30 d)" if p.get("coins_total") else "") \
             + f", {p.get('early', 0)} bought < 1 h after launch"
        if p.get("early_winners"):
            hs += "; **early on " + ", ".join(f"{x['symbol']} ({_usd(x['mcap'])})" for x in p["early_winners"][:3]) + "**"
        elif p.get("winners"):
            hs += "; held (not early) " + ", ".join(f"{x['symbol']}" for x in p["winners"][:3])
        rc = [x for x in p.get("recent_coins") or [] if x.get("token") != d["token"]][:3]
        if rc:
            hs += "; recent: " + ", ".join(f"{x['symbol']} {_usd(x['mcap']) if x.get('mcap') else x.get('state')}" for x in rc)
        L.append(hs)
        if pb:
            if pb.get("app"):
                L.append(f"- Before this buy: bought {pb.get('coins30', 0)} coins in the 30 d and {pb.get('coins7', 0)} in the 7 d before")
            else:
                L.append(f"- Before this buy: {pb['d7']} own txs in the 7 d, {pb['d30']} in the 30 d; {pb.get('coins30', 0)} coins bought in those 30 d")
    links = []
    for g in (c.fd.get("groups") or c.d.get("groups") or []):
        if a in g["members"] and g.get("conf") == "proven":
            others = [x for x in g["members"] if x != a][:3]
            links.append("PROVEN with " + ", ".join(c.name(x) for x in others) + f" ({_short((g.get('why') or [''])[0], c, 150)})")
    for x in ((d.get("shared") or {}).get("leaders") or []) + (c.fd.get("leaders") or []):
        if a == x["leader"]:
            links.append(f"leads {c.name(x['follower'])} ({x['led']}/{x['of']} coins, ~{x['median_dt']} s ahead)")
        elif a == x["follower"]:
            links.append(f"follows {c.name(x['leader'])} ({x['led']}/{x['of']} coins, ~{x['median_dt']} s behind)")
    for cl in c.fd.get("net_clusters") or []:
        for x, y, sh, tog, tight, mm, det in cl["pairs"]:
            if a in (x, y):
                o = y if a == x else x
                kind = "same block (one operator)" if det and det.get("kind") == "same block" else "within 10 min"
                links.append(f"{tog} small coins {kind} with {c.name(o)}")
    links = list(dict.fromkeys(links))
    L.append("- Links: " + ("; ".join(links[:4]) if links else "none found"))
    return L


def build(d, fd, secs, budget):
    c = _Ctx(d, fd)
    L = []
    age = (d["now"] - d["created_ts"]) / 3600 if d.get("created_ts") else None
    L.append(f"# {d.get('name')} ({d.get('symbol')}) · {d['chain']} · holder report")
    L.append(f"{c.link(d['token'])} · age {age:.1f} h · mcap {_usd(d.get('mcap'))} · liq {_usd(d.get('liq'))} · "
             f"{d.get('n_holders', '?')} holders · top {len(c.top)} hold {sum(h.get('pct') or 0 for h in c.top):.1f}% · built in {secs:.0f} s (budget {budget:.0f} s)"
             if age is not None else f"{c.link(d['token'])}")
    L.append("\n## Summary of findings")
    for i, (ev, mean) in enumerate(_findings(c), 1):
        L.append(f"{i}. {ev}\n   Meaning: {mean.rstrip('.')}.")
    L.append("\n## Every top holder")
    for h in c.top:
        L += _profile(c, h)
        L.append("")
    L.append("## Exited big buyers")
    for h in c.exited[:30]:
        m = _mins(d, h)
        bits = [f"got {h.get('got_pct') or 0:.2f}%"]
        if m:
            bits.append(f"bought {m} after launch")
        if (h.get("nonce") or 0) >= 5000:
            bits.append(f"bot ({h['nonce']:,} txs)")
        ln = []
        for g in (c.fd.get("groups") or []):
            if h["addr"] in g["members"] and g.get("conf") == "proven":
                ln.append("PROVEN with " + ", ".join(c.name(x) for x in g["members"] if x != h["addr"])[:200])
        for cl in c.fd.get("net_clusters") or []:
            for x, y, sh, tog, tight, mm, det in cl["pairs"]:
                if h["addr"] in (x, y) and det and det.get("kind") == "same block":
                    ln.append(f"same block as {c.name(y if h['addr'] == x else x)} on {det['same']} coins")
        for dc in c.fd.get("dev_coins") or []:
            if any(a == h["addr"] for a, _ in dc.get("early") or []):
                ln.append(f"sniped the dev's {dc['symbol']}")
        L.append(f"- {c.name(h['addr'])}: " + ", ".join(bits) + (" · " + "; ".join(dict.fromkeys(ln)) if ln else ""))
    nc = [n for n in (d.get("notes") or []) + (c.fd.get("notes") or []) + ([fd["error"]] if fd and fd.get("error") else [])
          if any(w in n.lower() for w in ("not read", "not finished", "failed", "timed out", "budget", "not seen", "not checked", "not readable"))]
    if nc:
        L.append("\n## Not checked")
        for n in list(dict.fromkeys(nc))[:8]:
            L.append(f"- {n}")
    return "\n".join(L)


def write(d, fd, secs, budget):
    text = build(d, fd, secs, budget)
    out = os.path.join(ROOT, "data", "reports")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"{d['chain']}-{(d.get('symbol') or 'token')[:20]}-{d['token'][:10]}-{time.strftime('%Y%m%d-%H%M')}.md")
    with open(path, "w") as f:
        f.write(text + "\n")
    return path, text
