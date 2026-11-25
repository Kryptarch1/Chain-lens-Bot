"""Explorer data behind one interface.
Primary: Etherscan API V2 (needs key in keys/etherscan). Fallbacks: Blockscout API v2, Routescan.
All functions return [] / {} on failure instead of raising, and say which source answered.
"""
import asyncio, json, os, time
import aiohttp
from lib import cache, hypersync, alchemy
from lib.rpc import CHAINS, ROOT

KEYFILE = os.path.join(ROOT, "keys", "etherscan")
ETHERSCAN_KEY = open(KEYFILE).read().strip() if os.path.exists(KEYFILE) else ""
ES_RPS = float(os.environ.get("DD_ETHERSCAN_RPS", "2.8"))  # free tier: 3 calls/sec shared across all chains
_es_lock = None
_es_last = []
FREE_UNSUPPORTED = set()  # chain ids where the free key gets "not supported" (learned at runtime)


async def _es_throttle():
    """At most 3 Etherscan calls in any 1.1 s window (free tier: 3/s). A sliding window instead of fixed 0.36 s
    spacing: the first calls of a check go out at once instead of queueing behind each other."""
    global _es_lock
    if _es_lock is None:
        _es_lock = asyncio.Lock()
    async with _es_lock:
        n = max(1, int(ES_RPS + 0.5))
        while len(_es_last) >= n:
            wait = _es_last[-n] + 1.1 - time.time()
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        _es_last.append(time.time())
        del _es_last[:-n]


UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36", "accept": "application/json"}


async def _get(url, params=None, timeout=25):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=UA) as s:
            async with s.get(url, params=params) as r:
                txt = await r.text()
                return json.loads(txt)
    except Exception:
        return None


class Explorer:
    def __init__(self, chain):
        self.chain = chain
        self.cfg = CHAINS.get(chain, {}).get("explorer", {})
        self.es_id = self.cfg.get("etherscan")
        self.bs = self.cfg.get("blockscout")
        self.rs = self.cfg.get("routescan")
        self.web = self.cfg.get("web", "")

    # ---------- Etherscan V2 ----------
    async def es(self, module, action, **kw):
        if not (ETHERSCAN_KEY and self.es_id):
            return None
        if self.es_id in FREE_UNSUPPORTED and not (module == "contract" and action == "getsourcecode"):
            return None
        p = {"chainid": self.es_id, "module": module, "action": action, "apikey": ETHERSCAN_KEY, **kw}
        d = None
        for attempt in range(3):
            await _es_throttle()
            d = await _get("https://api.etherscan.io/v2/api", p)
            msg = str((d or {}).get("result") or "")
            if "rate limit" in msg.lower():
                await asyncio.sleep(1.0 + attempt)
                continue
            if "not supported for this chain" in msg:
                FREE_UNSUPPORTED.add(self.es_id)
                return None
            break
        if d and d.get("status") == "1":
            return d["result"]
        if d and isinstance(d.get("result"), (dict, list)) and d.get("message") != "NOTOK":
            return d["result"]
        return None

    async def rs_api(self, module, action, **kw):
        if not self.rs:
            return None
        d = await _get(self.rs, {"module": module, "action": action, **kw})
        return d.get("result") if d and d.get("status") == "1" else None

    # ---------- Blockscout v2 ----------
    async def bs_pages(self, path, max_pages=10):
        if not self.bs:
            return []
        out, q = [], None
        for _ in range(max_pages):
            d = await _get(self.bs + path + (("?" + q) if q else ""))
            if not d or "items" not in d:
                break
            out += d["items"]
            nxt = d.get("next_page_params")
            if not nxt:
                break
            q = "&".join(f"{k}={v}" for k, v in nxt.items())
        return out

    def alch(self):
        """The free Etherscan key can't list wallets on this chain (learned on the first call) and Alchemy can."""
        return self.es_id in FREE_UNSUPPORTED and alchemy.available(self.chain)

    # ---------- unified ----------
    async def source_full(self, addr):
        """Verified source + compiler settings, for compiling (Slither).
        -> {'name','compiler','optimizer','runs','evm','files':{path: code},'settings':{...}|None} or {}"""
        key = f"srcfull:{self.chain}:{addr.lower()}"
        c = cache.get(key)
        if c:
            return c
        out = {}
        r = await self.es("contract", "getsourcecode", address=addr) or await self.rs_api("contract", "getsourcecode", address=addr)
        if r and isinstance(r, list) and r[0].get("SourceCode"):
            x = r[0]
            files, settings, raw = {}, None, x["SourceCode"].strip()
            if raw.startswith("{{"):  # standard JSON input wrapped in an extra brace
                j = json.loads(raw[1:-1]); files = {k: v["content"] for k, v in j.get("sources", {}).items()}; settings = j.get("settings")
            elif raw.startswith("{"):
                try:
                    j = json.loads(raw)
                    files = {k: v["content"] for k, v in (j.get("sources") or j).items() if isinstance(v, dict) and "content" in v}
                    settings = j.get("settings")
                except json.JSONDecodeError:
                    files = {f"{x.get('ContractName') or 'Main'}.sol": raw}
            else:
                files = {f"{x.get('ContractName') or 'Main'}.sol": raw}
            out = {"name": x.get("ContractName"), "compiler": x.get("CompilerVersion"), "optimizer": x.get("OptimizationUsed") == "1",
                   "runs": int(x.get("Runs") or 200), "evm": (x.get("EVMVersion") or "").lower(), "files": files, "settings": settings}
        elif self.bs:
            d = await _get(f"{self.bs}/api/v2/smart-contracts/{addr}")
            if d and d.get("source_code"):
                files = {d.get("file_path") or f"{d.get('name') or 'Main'}.sol": d["source_code"]}
                for a in d.get("additional_sources") or []:
                    files[a.get("file_path")] = a.get("source_code")
                cs = d.get("compiler_settings") or {}
                out = {"name": d.get("name"), "compiler": d.get("compiler_version"), "optimizer": bool(d.get("optimization_enabled")),
                       "runs": int(d.get("optimization_runs") or 200), "evm": (d.get("evm_version") or "").lower(), "files": files,
                       "settings": cs or None}
        if out:
            cache.set(key, out)
        return out

    async def source(self, addr):
        """-> {'name','verified','source','abi','proxy_impl'} (cached forever once verified)."""
        key = f"src:{self.chain}:{addr.lower()}"
        c = cache.get(key)
        if c:  # verified: forever; unverified: 1 h (TTL set below) — saves a throttled explorer call per phase
            return c
        res = {"name": None, "verified": False, "source": "", "abi": None, "proxy_impl": None, "via": None}
        r = await self.es("contract", "getsourcecode", address=addr) or await self.rs_api("contract", "getsourcecode", address=addr)
        if r and isinstance(r, list) and r[0].get("SourceCode"):
            x = r[0]
            res.update(name=x.get("ContractName"), verified=True, source=x.get("SourceCode", ""),
                       abi=x.get("ABI") if x.get("ABI", "").startswith("[") else None,
                       proxy_impl=x.get("Implementation") or None, via="etherscan" if ETHERSCAN_KEY else "routescan")
        elif self.bs:
            d = await _get(f"{self.bs}/api/v2/smart-contracts/{addr}")
            if d and d.get("source_code"):
                res.update(name=d.get("name"), verified=True, source=d.get("source_code", ""),
                           abi=json.dumps(d["abi"]) if d.get("abi") else None,
                           proxy_impl=(d.get("implementations") or [{}])[0].get("address") if d.get("implementations") else None, via="blockscout")
        cache.set(key, res, ttl=None if res["verified"] else 3600)
        return res

    async def creator(self, addr):
        """-> {'creator','tx'} or {}"""
        key = f"creator:{self.chain}:{addr.lower()}"
        c = cache.get(key)
        if c:
            return c
        out = {}
        r = await self.es("contract", "getcontractcreation", contractaddresses=addr) or await self.rs_api("contract", "getcontractcreation", contractaddresses=addr)
        if r and isinstance(r, list):
            out = {"creator": r[0].get("contractCreator"), "tx": r[0].get("txHash")}
        elif self.bs:
            d = await _get(f"{self.bs}/api/v2/addresses/{addr}")
            if d and d.get("creator_address_hash"):
                out = {"creator": d["creator_address_hash"], "tx": d.get("creation_transaction_hash") or d.get("creation_tx_hash")}
        if out and out.get("tx"):
            cache.set(key, out)
        return out

    async def txlist(self, addr, limit=200):
        """Normal transactions of an address, newest first. -> list of dicts with hash,from,to,value,input,timeStamp,isError"""
        return await cache.once(("txlist", self.chain, addr.lower(), limit), lambda: self._txlist(addr, limit))

    async def _txlist(self, addr, limit):
        r = await self.es("account", "txlist", address=addr, sort="desc", page=1, offset=limit) or \
            await self.rs_api("account", "txlist", address=addr, sort="desc", page=1, offset=limit)
        if r and isinstance(r, list):
            return [{**t, "via": "etherscan"} for t in r]
        if self.alch():
            try:
                return await alchemy.txlist(self.chain, addr, limit)
            except Exception:
                pass
        cid = CHAINS.get(self.chain, {}).get("id")
        if hypersync.available(cid):
            try:
                rows = await hypersync.txlist(cid, addr)
                return list(reversed(rows))[:limit]
            except Exception:
                pass
        if self.bs:
            items = await self.bs_pages(f"/api/v2/addresses/{addr}/transactions", max_pages=min(10, max(1, limit // 50)))
            return [{"hash": i.get("hash"), "from": (i.get("from") or {}).get("hash"), "to": (i.get("to") or {}).get("hash"),
                     "value": i.get("value"), "input": i.get("raw_input", ""), "timeStamp": i.get("timestamp"),
                     "isError": "0" if i.get("status") == "ok" else "1", "functionName": (i.get("method") or ""),
                     "contractAddress": (i.get("created_contract") or {}).get("hash", ""), "via": "blockscout"} for i in items]
        return []

    async def tokentx(self, addr, limit=500, contract=None):
        kw = {"address": addr, "sort": "desc", "page": 1, "offset": limit}
        if contract:
            kw["contractaddress"] = contract
        r = await self.es("account", "tokentx", **kw) or await self.rs_api("account", "tokentx", **kw)
        if r and isinstance(r, list):
            return r
        if self.alch():
            try:
                rows = await alchemy.tokentx(self.chain, addr, limit)
                return [x for x in rows if not contract or x["contractAddress"] == contract.lower()]
            except Exception:
                pass
        if self.bs:
            items = await self.bs_pages(f"/api/v2/addresses/{addr}/token-transfers", max_pages=max(1, limit // 50))
            return [{"hash": i.get("transaction_hash") or i.get("tx_hash"), "from": (i.get("from") or {}).get("hash"),
                     "to": (i.get("to") or {}).get("hash"), "contractAddress": (i.get("token") or {}).get("address"),
                     "tokenSymbol": (i.get("token") or {}).get("symbol"), "tokenDecimal": (i.get("token") or {}).get("decimals"),
                     "value": (i.get("total") or {}).get("value"), "timeStamp": i.get("timestamp")} for i in items
                    if not contract or ((i.get("token") or {}).get("address", "").lower() == contract.lower())]
        return []

    async def token_transfers(self, token, limit=100, sort="asc"):
        """Transfers of one token (no address filter) — first transfers reveal the pool."""
        r = await self.es("account", "tokentx", contractaddress=token, sort=sort, page=1, offset=limit) or \
            await self.rs_api("account", "tokentx", contractaddress=token, sort=sort, page=1, offset=limit)
        if r and isinstance(r, list):
            return r
        if self.bs:
            items = await self.bs_pages(f"/api/v2/tokens/{token}/transfers", max_pages=2)
            items = list(reversed(items)) if sort == "asc" else items
            return [{"hash": i.get("transaction_hash") or i.get("tx_hash"), "from": (i.get("from") or {}).get("hash"),
                     "to": (i.get("to") or {}).get("hash"), "blockNumber": i.get("block_number"), "timeStamp": i.get("timestamp")} for i in items]
        return []

    async def internal(self, addr, limit=200):
        r = await self.es("account", "txlistinternal", address=addr, sort="desc", page=1, offset=limit)
        if r and isinstance(r, list):
            return r
        if self.bs:
            items = await self.bs_pages(f"/api/v2/addresses/{addr}/internal-transactions", max_pages=max(1, limit // 50))
            return [{"hash": i.get("transaction_hash"), "from": (i.get("from") or {}).get("hash"), "to": (i.get("to") or {}).get("hash"),
                     "value": i.get("value"), "timeStamp": i.get("timestamp")} for i in items]
        return []

    async def holders(self, token, limit=100):
        """-> list of {'address','value'} (raw units) newest-rank first, or []"""
        r = await self.es("token", "topholders", contractaddress=token, offset=limit)
        if r and isinstance(r, list):
            return [{"address": h.get("TokenHolderAddress"), "value": h.get("TokenHolderQuantity")} for h in r]
        if self.bs:
            items = await self.bs_pages(f"/api/v2/tokens/{token}/holders", max_pages=max(1, limit // 50))
            return [{"address": (i.get("address") or {}).get("hash"), "value": i.get("value"),
                     "is_contract": (i.get("address") or {}).get("is_contract")} for i in items[:limit]]
        return []

    async def funded_by(self, addr):
        """First inbound native transfer -> {'from','tx','time'} using txlist (asc). Cached: a wallet's first
        funding never changes (a miss is retried after 10 min — the wallet may be brand new)."""
        key = f"funded:{self.chain}:{addr.lower()}"
        c = cache.get(key)
        if c is not None:
            return c
        out = await cache.once(key, lambda: self._funded_by(addr))
        if not out.get("error"):  # a failed lookup is not "never funded": don't remember it
            cache.set(key, out, ttl=None if out.get("from") else 600)
        return out

    async def _funded_by(self, addr):
        hs_err = None
        r = await self.es("account", "txlist", address=addr, sort="asc", page=1, offset=20) or \
            await self.rs_api("account", "txlist", address=addr, sort="asc", page=1, offset=20)
        rows = r if (r and isinstance(r, list)) else []
        cid = CHAINS.get(self.chain, {}).get("id")
        if not rows and self.alch():
            ff = await alchemy.first_funding(self.chain, addr)
            if ff.get("from"):
                return ff
            if ff.get("none"):
                return {}
        if not rows and hypersync.available(cid):
            try:
                ff = await hypersync.first_funding(cid, addr)
                if ff:
                    return ff
            except Exception as e:
                hs_err = str(e)[:80]
        if not rows and self.bs:
            items = await self.bs_pages(f"/api/v2/addresses/{addr}/transactions", max_pages=4)
            rows = [{"from": (i.get("from") or {}).get("hash"), "to": (i.get("to") or {}).get("hash"), "value": i.get("value"),
                     "hash": i.get("hash"), "timeStamp": i.get("timestamp")} for i in reversed(items)]
        for t in rows:
            if (t.get("to") or "").lower() == addr.lower() and int(t.get("value") or 0) > 0:
                return {"from": t["from"], "tx": t["hash"], "time": t.get("timeStamp")}
        ir = await self.es("account", "txlistinternal", address=addr, sort="asc", page=1, offset=20)
        for t in (ir or []) if isinstance(ir, list) else []:
            if (t.get("to") or "").lower() == addr.lower() and int(t.get("value") or 0) > 0:
                return {"from": t["from"], "tx": t["hash"], "time": t.get("timeStamp"), "internal": True}
        return {"error": hs_err} if hs_err else {}

    def link(self, kind, x):
        w = self.web
        if not w:
            return x
        return f"{w}/{'address' if kind == 'a' else 'tx' if kind == 'tx' else 'token'}/{x}"


async def dexscreener_token(chain, token):
    d = await _get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")
    pairs = (d or {}).get("pairs") or []
    ds = CHAINS.get(chain, {}).get("dexscreener")
    return [p for p in pairs if not ds or p.get("chainId") == ds]


async def goplus(chain_id, token):
    d = await _get(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}", {"contract_addresses": token}, timeout=15)
    try:
        return list(d["result"].values())[0]
    except Exception:
        return {}
