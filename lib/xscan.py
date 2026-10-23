"""xscan.cc (unofficial, no key): recently deployed contracts on eth / base / bsc.
Used for "same code deployed elsewhere" (scam-kit spraying) and later the deploy radar.
Only knows contracts it indexed at deploy time; anything else returns an error -> treated as 'no data'."""
import aiohttp

BASE = "https://xscan.cc/api/v1/contracts"
CHAIN = {"eth": "eth", "base": "base", "bsc": "bsc"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}


async def _get(path, timeout=6):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=UA) as s:
            async with s.get(f"{BASE}/{path}") as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception:
        return None


async def lookup(chain, address):
    """-> {'indexed', 'name', 'deployer', 'similar': [...], 'deployers': n, 'chains': {...}} or {} if unsupported/unknown.
    Optional data: time-boxed to 2.5 s (the site is often slow or down) and cached (1 h; a failure for 10 min)."""
    c = CHAIN.get(chain)
    if not c:
        return {}
    from lib import cache
    key = f"xscan:{chain}:{address.lower()}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    out = await _lookup(c, address)
    cache.set(key, out, ttl=3600 if out.get("indexed") else 600)
    return out


async def _lookup(c, address):
    import asyncio
    info, sim = await asyncio.gather(_get(f"{c}/{address}", timeout=2.5), _get(f"{c}/{address}/similar", timeout=2.5))
    if not info or "error" in info:
        return {"indexed": False}
    sims = [x for x in ((sim or {}).get("results") or []) if x.get("address", "").lower() != address.lower() and x.get("chain") != "sepolia"]
    chains = {}
    for x in sims:
        chains[x.get("chain")] = chains.get(x.get("chain"), 0) + 1
    return {"indexed": True, "name": info.get("name"), "deployer": info.get("deployer"), "verified_sources": info.get("sources"),
            "similar": sims, "deployers": len({x.get("deployer") for x in sims if x.get("deployer")}), "chains": chains}


def render(x):
    if not x or not x.get("indexed"):
        return []
    sims = x.get("similar") or []
    if not sims:
        return ["   xscan: no other contracts with this code seen recently"]
    ch = ", ".join(f"{n} on {c}" for c, n in sorted(x["chains"].items(), key=lambda kv: -kv[1]))
    first = sorted((s.get("blockTimestamp") or "") for s in sims)[0][:16].replace("T", " ")
    return [f"   ! same code deployed {len(sims)}x recently ({ch}) by {x['deployers']} different wallet(s), earliest {first} — template/kit; check what the copies did (xscan.cc)"]
