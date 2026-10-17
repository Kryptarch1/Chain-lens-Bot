"""Follow the money back from a wallet: first funding tx -> funder; through bridges (Relay) to the source chain;
stop at exchange-type wallets, Solana, unknown bridges, or max depth.
Uses: explorer/HyperSync for wallet history, Chainstack/RPC for nonce/balance/code, Relay API for bridge fills.
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
        # bridge payout? (Relay solver pays out on the destination chain)
        rf = await relay_fill(h["tx"]) if h.get("tx") else None
        if rf and rf.get("sender"):
            h["bridge"] = {"name": "Relay", **rf}
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
            L.append(f"   ← {h['chain']} {h['wallet']} received via Relay from {b['sender']} on {CHAIN_BY_ID.get(b['chain_id'], b['chain_id'])}"
                     f" ({b['amount']} {b['symbol']}, ${float(b['usd'] or 0):,.0f}) in tx {b.get('in_tx')}")
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
            parts.append(f"bridged via Relay from {CHAIN_BY_ID.get(h['bridge']['chain_id'], h['bridge']['chain_id'])} (${float(h['bridge']['usd'] or 0):,.0f})")
        else:
            parts.append(f"{h['funder'][:10]}… ({h['funder_nonce']:,} txs)")
    return " ← ".join(parts)
