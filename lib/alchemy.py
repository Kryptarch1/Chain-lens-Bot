"""Alchemy (free key in keys/alchemy): full wallet history where the free Etherscan key can't list wallets (BSC, Base),
and one-call first funding. alchemy_getAssetTransfers lists a wallet's top-level txs (zero-value contract calls too),
contract payouts to it (internal) and its token transfers, newest or oldest first, 1,000 per page.

Rows are returned in Etherscan's shapes (txlist / tokentx) so every caller works unchanged. The key is never printed.
"""
import asyncio, os
from datetime import datetime
import aiohttp
from lib import cache
from lib.rpc import ROOT

KEYFILE = os.path.join(ROOT, "keys", "alchemy")
KEY = open(KEYFILE).read().strip() if os.path.exists(KEYFILE) else ""
NETS = {"eth": "eth-mainnet", "base": "base-mainnet", "bsc": "bnb-mainnet"}
DUST = 1e-6  # native; smaller 'fundings' are app activation markers
RPS = float(os.environ.get("DD_ALCHEMY_RPS", "8"))  # free plan: ~8 history calls/s sustained (measured), 10/s gets 429s
_sem = None
_pace = None
_next = [0.0]
_down = set()


_high = [0]
_lowsem = None


async def _throttle(low=False):
    """One request every 1/RPS s, shared by every caller in the process. Low-priority callers (the background
    all-holder scan) wait while any normal caller (profiles, explorer reads) is queued."""
    global _pace
    if _pace is None:
        _pace = asyncio.Lock()
    global _lowsem
    if low:
        if _lowsem is None:
            _lowsem = asyncio.Semaphore(1)
        async with _lowsem:  # at most one low request waits at the pacer, so normal ones never queue behind hundreds
            while _high[0] > 0:
                await asyncio.sleep(0.05)
            await _locked()
        return
    _high[0] += 1
    try:
        await _locked()
    finally:
        _high[0] -= 1


async def _locked():
    async with _pace:
        now = asyncio.get_event_loop().time()
        wait = _next[0] - now
        if wait > 0:
            await asyncio.sleep(wait)
        _next[0] = max(now, _next[0]) + 1 / RPS


def available(chain):
    return bool(KEY) and chain in NETS and chain not in _down


def _clean(s):
    return str(s).replace(KEY, "<key>") if KEY else str(s)


async def _post(chain, method, params, timeout=40, low=False):
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(16)
    url = f"https://{NETS[chain]}.g.alchemy.com/v2/{KEY}"
    err = None
    for attempt in range(7):
        await _throttle(low)
        async with _sem:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                    async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                        if r.status == 429:
                            err = "rate limited"
                        else:
                            j = await r.json(content_type=None)
                            if "error" in j:
                                msg = _clean((j["error"] or {}).get("message"))
                                if "not enabled" in msg.lower() or "unauthorized" in msg.lower() or r.status in (401, 403):
                                    _down.add(chain)  # network not enabled for this key: stop trying
                                    raise RuntimeError(f"alchemy {chain}: {msg[:80]}")
                                err = msg[:80]
                                if "rate" not in msg.lower() and "capacity" not in msg.lower():
                                    raise RuntimeError(f"alchemy {chain}: {err}")
                            else:
                                return j.get("result")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = type(e).__name__
        await asyncio.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"alchemy {chain}: {err}")


async def transfers(chain, addr, direction, cats, order="desc", pages=1, max_count=1000, low=False):
    """-> raw transfer dicts (newest first for 'desc'). direction: 'from' | 'to'."""
    out, key = [], None
    for _ in range(pages):
        p = {"fromBlock": "0x0", "toBlock": "latest", f"{direction}Address": addr, "category": cats, "order": order,
             "withMetadata": True, "excludeZeroValue": False, "maxCount": hex(max_count)}
        if key:
            p["pageKey"] = key
        r = await _post(chain, "alchemy_getAssetTransfers", [p], low=low) or {}
        out += r.get("transfers") or []
        key = r.get("pageKey")
        if not key:
            break
    return out


def _ts(x):
    try:
        return int(datetime.fromisoformat(x["metadata"]["blockTimestamp"].replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _raw(x):
    try:
        return int((x.get("rawContract") or {}).get("value") or "0x0", 16)
    except Exception:
        return 0


def _merge(a, b, limit):
    seen, out = set(), []
    for x in sorted(a + b, key=lambda x: -(int(x.get("blockNum") or "0x0", 16))):
        if x.get("uniqueId") in seen:
            continue
        seen.add(x.get("uniqueId"))
        out.append(x)
    return out[:limit]


async def tokentx(chain, addr, limit=1000):
    """Token transfers in and out, newest first, Etherscan tokentx shape."""
    addr = addr.lower()
    key = f"alch:tt:{chain}:{addr}:{limit}"
    c = cache.get(key)
    if c is not None:
        return c
    a, b = await asyncio.gather(transfers(chain, addr, "from", ["erc20"], max_count=min(limit, 1000)),
                                transfers(chain, addr, "to", ["erc20"], max_count=min(limit, 1000)))
    out = []
    for x in _merge(a, b, limit):
        rc = x.get("rawContract") or {}
        try:
            dec = int(rc.get("decimal") or "0x12", 16)
        except Exception:
            dec = 18
        out.append({"hash": x.get("hash"), "from": (x.get("from") or "").lower(), "to": (x.get("to") or "").lower(),
                    "contractAddress": (rc.get("address") or "").lower(), "value": str(_raw(x)), "tokenSymbol": x.get("asset") or "",
                    "tokenDecimal": str(dec), "timeStamp": str(_ts(x) or 0), "blockNumber": str(int(x.get("blockNum") or "0x0", 16)), "via": "alchemy"})
    cache.set(key, out, ttl=600)
    return out


async def txlist(chain, addr, limit=300, rpc=None):
    """Top-level txs sent by or to the wallet, newest first, Etherscan txlist shape. `input` (needed to tell a contract
    call from a plain send) is read from our node for the wallet's own txs."""
    addr = addr.lower()
    key = f"alch:tl:{chain}:{addr}:{limit}"
    c = cache.get(key)
    if c is not None:
        return c
    a, b = await asyncio.gather(transfers(chain, addr, "from", ["external"], max_count=min(limit, 1000)),
                                transfers(chain, addr, "to", ["external"], max_count=min(limit, 1000)))
    rows = _merge(a, b, limit)
    inp = {}
    own = [x["hash"] for x in rows if (x.get("from") or "").lower() == addr]
    if own:
        from lib.rpc import RPC
        rpc = rpc or RPC(chain)
        try:
            r = await asyncio.wait_for(rpc.batch([("eth_getTransactionByHash", [h]) for h in own]), 60)
            inp = {h: t for h, t in zip(own, r) if t}
        except Exception:
            pass
    out = []
    for x in rows:
        t = inp.get(x.get("hash")) or {}
        out.append({"hash": x.get("hash"), "from": (x.get("from") or "").lower(), "to": (x.get("to") or "").lower(),
                    "value": str(_raw(x)), "input": t.get("input") or "0x", "timeStamp": str(_ts(x) or 0),
                    "blockNumber": str(int(x.get("blockNum") or "0x0", 16)), "isError": "0", "functionName": "",
                    "contractAddress": "", "via": "alchemy"})
    cache.set(key, out, ttl=600)
    return out


async def first_funding(chain, addr):
    """First native coin received above dust (plain send or contract payout) -> {'from','tx','time'[,'internal']},
    {'none': True, 'sponsored': True} if it never received any (an app account), or {'error': ...}."""
    addr = addr.lower()
    try:
        rows = await transfers(chain, addr, "to", ["external", "internal"], order="asc", max_count=25)
    except Exception as e:
        return {"error": _clean(e)[:80]}
    for x in rows:
        if (x.get("asset") in (None, "ETH", "BNB") or x.get("category") in ("external", "internal")) and _raw(x) / 1e18 >= DUST:
            r = {"from": (x.get("from") or "").lower(), "tx": x.get("hash"), "time": str(_ts(x) or 0), "value": _raw(x) / 1e18}
            if x.get("category") == "internal":
                r["internal"] = True
            return r
    if len(rows) >= 25:
        return {"error": "only dust in its first 25 inbound transfers"}
    return {"none": True, "sponsored": True}
