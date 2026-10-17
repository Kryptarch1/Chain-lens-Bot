"""Testnet history of a wallet (usually the dev): did they rehearse? Same code tested before launch?

Per testnet: tx count (nonce) via public RPC; full tx list + deploys via HyperSync where available; then the
code of each testnet deploy is compared with the mainnet token's code (selector overlap) to spot rehearsals.
Only the given wallet is checked — devs often test from a different wallet, so 'no testnet history' is weak evidence.
"""
import asyncio, re, time
import aiohttp
from lib import hypersync, sigs

TESTNETS = {  # name: (chain_id, public RPC or None, hypersync?)
    "sepolia": (11155111, "https://ethereum-sepolia-rpc.publicnode.com", True),
    "hoodi": (560048, "https://ethereum-hoodi-rpc.publicnode.com", True),
    "base-sepolia": (84532, "https://sepolia.base.org", True),
    "bsc-testnet": (97, "https://bsc-testnet-rpc.publicnode.com", True),
    "arb-sepolia": (421614, "https://sepolia-rollup.arbitrum.io/rpc", True),
    "op-sepolia": (11155420, "https://sepolia.optimism.io", True),
    "avax-fuji": (43113, "https://api.avax-test.network/ext/bc/C/rpc", True),
    "sonic-testnet": (14601, "https://rpc.testnet.soniclabs.com", True),
    "monad-testnet": (10143, "https://testnet-rpc.monad.xyz", True),
    "arc-testnet": (5042002, None, True),
    "hyper-testnet": (998, "https://rpc.hyperliquid-testnet.xyz/evm", False),
    "ink-sepolia": (763373, "https://rpc-gel-sepolia.inkonchain.com", False),
    "megaeth-testnet": (6343, "https://carrot.megaeth.com/rpc", False),
    "rh-testnet": (46630, "https://rpc.testnet.chain.robinhood.com", False),
}
UA = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126", "content-type": "application/json"}


async def _rpc(s, url, method, params):
    try:
        async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
        return d.get("result")
    except Exception:
        return None


def _selectors(code):
    try:
        return {s for s, _, _ in sigs.selectors_from_bytecode(code)}
    except Exception:
        return set(re.findall(r"63([0-9a-f]{8})14", code.lower()))


async def scan(wallet, token_code=None, max_deploys=8):
    """-> {'active': [...], 'checked': n, 'rehearsals': [...], 'errors': [...]}"""
    out = {"active": [], "checked": 0, "rehearsals": [], "unreachable": []}
    target = _selectors(token_code) if token_code and len(token_code) > 200 else set()
    async with aiohttp.ClientSession(headers=UA) as s:
        async def one(name, cid, url, has_hs):
            n = None
            if url:
                r = await _rpc(s, url, "eth_getTransactionCount", [wallet, "latest"])
                n = int(r, 16) if r else None
            rows = []
            if has_hs and hypersync.TOKEN and (n is None or n > 0):
                try:
                    rows = await asyncio.wait_for(hypersync.txlist(cid, wallet), 12)
                except Exception:
                    rows = []
            if n is None and not rows and not has_hs:
                out["unreachable"].append(name)
                return None
            sent = [t for t in rows if (t.get("from") or "").lower() == wallet.lower()]
            if not (n or rows):
                return None
            deploys = [t for t in sent if t.get("contractAddress")]
            ts = [int(t["timeStamp"]) for t in rows if str(t.get("timeStamp") or "").isdigit()]
            entry = {"net": name, "txs": n if n is not None else len(sent), "deploys": len(deploys),
                     "first": min(ts) if ts else None, "last": max(ts) if ts else None, "matches": []}
            # compare testnet deploys with the mainnet token's code
            if target and deploys and url:
                for t in deploys[-max_deploys:]:
                    code = await _rpc(s, url, "eth_getCode", [t["contractAddress"], "latest"])
                    if not code or len(code) < 200:
                        continue
                    sel = _selectors(code)
                    if not sel:
                        continue
                    overlap = len(sel & target) / max(1, len(target | sel))
                    if code.lower() == (token_code or "").lower() or overlap >= 0.8:
                        entry["matches"].append({"address": t["contractAddress"], "similarity": round(overlap, 2),
                                                 "exact": code.lower() == (token_code or "").lower(), "time": int(t["timeStamp"]) if str(t.get("timeStamp") or "").isdigit() else None})
            return entry

        res = await asyncio.gather(*[one(n, c, u, h) for n, (c, u, h) in TESTNETS.items()], return_exceptions=True)
    out["checked"] = len(TESTNETS)
    for r in res:
        if isinstance(r, dict):
            out["active"].append(r)
            for m in r["matches"]:
                out["rehearsals"].append({"net": r["net"], **m})
    out["active"].sort(key=lambda e: -(e["txs"] or 0))
    return out


def render(t, launch_ts=None):
    if not t:
        return []
    if not t.get("active"):
        return [f"   testnets: no activity from this wallet on {t.get('checked', 0)} testnets (devs often test from another wallet)"]
    parts = []
    for e in t["active"][:5]:
        when = time.strftime("%m-%d", time.gmtime(e["last"])) if e.get("last") else "?"
        parts.append(f"{e['net']} {e['txs']} txs/{e['deploys']} deploys (last {when})")
    L = [f"   testnets: {'; '.join(parts)}"]
    for r in t.get("rehearsals", [])[:3]:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["time"])) if r.get("time") else "?"
        before = ""
        if launch_ts and r.get("time"):
            d = (launch_ts - r["time"]) / 3600
            before = f", {d:.0f}h before launch" if d > 0 else ""
        L.append(f"   ! rehearsal: {'IDENTICAL' if r['exact'] else f'{int(r['similarity']*100)}% same'} code deployed on {r['net']} at {when}{before} ({r['address']})")
    return L
