"""Follow the money back from a wallet: first funding tx -> funder; through bridges (Relay, Across, deBridge) to the source chain;
stop at exchange-type wallets, Solana, unknown bridges, or max depth.
Uses: explorer/HyperSync for wallet history, Chainstack/RPC for nonce/balance/code, Relay / Across / deBridge APIs for
bridge payouts (all public, no key): a funding tx that is a bridge payout is matched to its deposit on the source chain.
"""
import asyncio, json
import aiohttp
from lib.rpc import RPC, CHAINS
from lib import explorer

RELAY = "https://api.relay.link/requests/v2"
EXCHANGE_NONCE = 10_000           # wallets with this many txs are exchanges / services / bots
CHAIN_BY_ID = {v["id"]: k for k, v in CHAINS.items()}
SOLANA_ID = 792703809


async def relay_fill(tx_hash):
    """If tx_hash is a Relay payout, return the origin {chain_id, sender, amount, symbol, usd, in_tx}."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(RELAY, params={"hash": tx_hash, "limit": 1}) as r:
                d = await r.json(content_type=None)
    except Exception:
        return None
    rs = d.get("requests") or []
    if not rs:
        return None
    r = rs[0]
    m = (r.get("data") or {}).get("metadata") or {}
    ci = m.get("currencyIn") or {}
    ins = (r.get("data") or {}).get("inTxs") or []
    return {"chain_id": (ci.get("currency") or {}).get("chainId"), "sender": r.get("user"),
            "amount": ci.get("amountFormatted"), "symbol": (ci.get("currency") or {}).get("symbol"),
            "usd": ci.get("amountUsd"), "in_tx": ins[0].get("hash") if ins else None, "time": r.get("createdAt")}


ACROSS = "https://app.across.to/api/deposits"
DLN = "https://stats-api.dln.trade/api"
DLN_SOLANA = 7565164  # deBridge's id for Solana


async def _get(url, params=None, method="GET", body=None):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": "dd-bot/1.0"}) as s:
            async with s.request(method, url, params=params, json=body) as r:
                return await r.json(content_type=None) if r.status == 200 else None
    except Exception:
        return None


async def _decimals(chain_id, token):
    ch = CHAIN_BY_ID.get(chain_id)
    if not ch or not token or int(token, 16) == 0 or token.lower().startswith("0xeeeeeeee"):
        return 18
    try:
        r = await RPC(ch).eth_call(token, "0x313ce567")
        return int(r, 16)
    except Exception:
        return 18


async def across_fill(tx_hash, wallet):
    """If tx_hash is an Across payout to `wallet`, return the deposit it filled (origin chain, depositor, amount)."""
    d = await _get(ACROSS, {"recipient": wallet, "limit": 50})
    for x in d or []:
        if (x.get("fillTx") or "").lower() == tx_hash.lower():
            dec = await _decimals(x.get("originChainId"), x.get("inputToken"))
            amt = int(x.get("inputAmount") or 0) / 10 ** dec
            usd = amt * float(x.get("inputPriceUsd") or 0)
            return {"chain_id": x.get("originChainId"), "sender": x.get("depositor"), "amount": f"{amt:.6g}", "symbol": "",
                    "usd": usd or None, "in_tx": x.get("depositTxHash"), "time": x.get("depositBlockTimestamp")}
    return None


async def debridge_fill(tx_hash):
    """If tx_hash fulfilled a deBridge (DLN) order, return the order's origin (chain, maker, amount)."""
    d = await _get(f"{DLN}/Orders/filteredList", method="POST", body={"skip": 0, "take": 1, "filter": tx_hash})
    orders = (d or {}).get("orders") or []
    if not orders:
        return None
    o = await _get(f"{DLN}/Orders/{orders[0]['orderId']['stringValue']}")
    if not o:
        return None
    ful = ((o.get("fulfilledDstEventMetadata") or {}).get("transactionHash") or {}).get("stringValue") or ""
    if ful.lower() != tx_hash.lower():
        return None
    give = o.get("giveOfferWithMetadata") or {}
    cid = int(((give.get("chainId") or {}).get("bigIntegerValue")) or 0)
    meta = give.get("metadata") or {}
    dec = int(meta.get("decimals") or 18)
    amt = int(((give.get("amount") or {}).get("bigIntegerValue")) or 0) / 10 ** dec
    return {"chain_id": SOLANA_ID if cid == DLN_SOLANA else cid, "sender": (o.get("makerSrc") or {}).get("stringValue"),
            "amount": f"{amt:.6g}", "symbol": meta.get("symbol") or "", "usd": None,
            "in_tx": ((o.get("createdSrcEventMetadata") or {}).get("transactionHash") or {}).get("stringValue"), "time": o.get("creationTimestamp")}


async def bridge_fill(tx_hash, wallet):
    """Which bridge paid `wallet` in tx_hash (Relay, Across or deBridge), checked in parallel. -> {name, ...} or None"""
    res = await asyncio.gather(relay_fill(tx_hash), across_fill(tx_hash, wallet), debridge_fill(tx_hash), return_exceptions=True)
    for name, r in zip(("Relay", "Across", "deBridge"), res):
        if isinstance(r, dict) and r.get("sender"):
            return {"name": name, **r}
    return None


async def step(chain, wallet):
    """One hop: who first funded `wallet` on `chain`."""
    ex = explorer.Explorer(chain)
    fb = await ex.funded_by(wallet)
    if not fb.get("from"):
        return None
    rpc = RPC(chain)
    funder = fb["from"]
    nonce, bal, code = await asyncio.gather(rpc.nonce(funder), rpc.balance(funder), rpc.code(funder))
    return {"chain": chain, "wallet": wallet, "funder": funder, "tx": fb.get("tx"), "time": fb.get("time"),
            "funder_nonce": nonce, "funder_balance": bal, "funder_is_contract": len(code) > 2, "symbol": rpc.cfg.get("symbol", "ETH")}


async def trace(chain, wallet, max_hops=4):
    """-> list of hops (dicts) + final 'end' reason."""
    hops, seen = [], set()
    cur_chain, cur = chain, wallet
    for _ in range(max_hops):
        key = (cur_chain, cur.lower())
        if key in seen:
            break
        seen.add(key)
        h = await step(cur_chain, cur)
        if not h:
            hops.append({"chain": cur_chain, "wallet": cur, "end": "no funding tx found"})
            break
        # bridge payout? (Relay / Across / deBridge solvers pay out on the destination chain)
        rf = await bridge_fill(h["tx"], cur) if h.get("tx") else None
        if rf:
            h["bridge"] = rf
            hops.append(h)
            src = CHAIN_BY_ID.get(rf["chain_id"])
            if rf["chain_id"] == SOLANA_ID:
                hops.append({"chain": "solana", "wallet": rf["sender"], "end": "origin on Solana (not traced further)"})
                break
            if not src:
                hops.append({"chain": str(rf["chain_id"]), "wallet": rf["sender"], "end": f"origin chain {rf['chain_id']} not configured"})
                break
            cur_chain, cur = src, rf["sender"]
            continue
        hops.append(h)
        if h["funder_nonce"] >= EXCHANGE_NONCE:
            hops.append({"chain": cur_chain, "wallet": h["funder"], "end": f"exchange/service wallet ({h['funder_nonce']:,} txs) — trail ends"})
            break
        if h["funder_is_contract"]:
            hops.append({"chain": cur_chain, "wallet": h["funder"], "end": "funded by a contract (bridge/router/multisig) — not followed"})
            break
        cur = h["funder"]
    return hops


def render(hops):
    L = []
    for h in hops:
        if h.get("end"):
            L.append(f"   ← {h['chain']} {h['wallet']}: {h['end']}")
            continue
        amt = ""
        if h.get("bridge"):
            b = h["bridge"]
            usd = f", ${float(b['usd']):,.0f}" if b.get("usd") else ""
            src = "Solana" if b["chain_id"] == SOLANA_ID else CHAIN_BY_ID.get(b["chain_id"], b["chain_id"])
            L.append(f"   ← {h['chain']} {h['wallet']} received via {b.get('name', 'Relay')} from {b['sender']} on {src}"
                     f" ({b['amount']} {b['symbol']}{usd}) in tx {b.get('in_tx')}".replace(" )", ")"))
        else:
            L.append(f"   ← {h['chain']} {h['wallet']} funded by {h['funder']}"
                     f" ({'contract' if h['funder_is_contract'] else 'wallet'}, {h['funder_nonce']:,} txs, {h['funder_balance']/1e18:,.4g} {h['symbol']}) tx {h['tx']}")
    return L


def one_line(hops):
    """Compact summary for dd check: 'via Relay from base ← exchange wallet (6.6M txs)'."""
    parts = []
    for i, h in enumerate(hops):
        nxt = hops[i + 1] if i + 1 < len(hops) else {}
        if not h.get("end") and not h.get("bridge") and nxt.get("end") and nxt.get("wallet", "").lower() == h["funder"].lower():
            continue  # the next 'end' line already describes this funder
        if h.get("end"):
            parts.append(h["end"].split(" — ")[0])
        elif h.get("bridge"):
            b = h["bridge"]
            src = "Solana" if b["chain_id"] == SOLANA_ID else CHAIN_BY_ID.get(b["chain_id"], b["chain_id"])
            parts.append(f"bridged via {b.get('name', 'Relay')} from {src}" + (f" (${float(b['usd']):,.0f})" if b.get("usd") else ""))
        else:
            parts.append(f"{h['funder'][:10]}… ({h['funder_nonce']:,} txs)")
    return " ← ".join(parts)
