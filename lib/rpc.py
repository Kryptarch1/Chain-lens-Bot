"""Async JSON-RPC client with pooled keep-alive sessions, batching, automatic
endpoint fallback, and chunked log scans that respect each chain's block-range limit.

Usage:
    from lib.rpc import RPC, run
    rpc = RPC("bsc")
    code = run(rpc.code(addr))
"""
import asyncio, json, os, time
import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAINS = json.load(open(os.path.join(ROOT, "chains.json")))
CHAINS.pop("_doc", None)

ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def resolve_chain(name):
    """Accept a short name ('bsc'), a chain id ('56'), or a raw RPC URL."""
    name = str(name)
    if name in CHAINS:
        return name, CHAINS[name]
    for k, v in CHAINS.items():
        if str(v["id"]) == name:
            return k, v
    if name.startswith("http"):
        return name, {"id": None, "symbol": "?", "rpc": [name], "logs_range": 1000, "explorer": {}}
    raise SystemExit(f"unknown chain {name}; known: {', '.join(CHAINS)}")



# A private endpoint (Chainstack) that answers "services are paused / unpaid invoice" is skipped by every check for
# PRIVATE_DOWN_TTL seconds: without this each call failed instead of moving on, forks never started, and the
# Chainstack-sized log ranges / concurrency were used against public RPCs.
PRIVATE_DOWN_TTL = 900
BILLING_WORDS = ("paused", "unpaid", "invoice", "suspended")


def _down_file(chain):
    return os.path.join(ROOT, "data", f"private_down_{chain}")


def private_down(chain):
    try:
        return time.time() - os.path.getmtime(_down_file(chain)) < PRIVATE_DOWN_TTL
    except OSError:
        return False


def mark_private_down(chain):
    try:
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        open(_down_file(chain), "w").close()
    except OSError:
        pass

def _batch_cap_err(msg):
    m = (msg or "").lower()
    return "batch" in m and any(w in m for w in ("too many", "too large", "exceed", "limit", "more than", "not allowed"))


class RPCError(Exception):
    pass


class RPC:
    _sessions = {}

    def __init__(self, chain):
        self.name, self.cfg = resolve_chain(chain)
        self.urls = list(self.cfg["rpc"])
        self.log_urls = list(self.cfg.get("logs_rpc") or self.cfg["rpc"])
        self.private = None
        # private endpoints (keys/rpc_<chain>, first line = https URL) go first; never printed
        kf = os.path.join(ROOT, "keys", f"rpc_{self.name}")
        if os.path.exists(kf) and not private_down(self.name):
            priv = [l.strip() for l in open(kf) if l.strip().startswith("http")]
            if priv:
                self.urls = priv[:1] + [u for u in self.urls if u != priv[0]]
                self.log_urls = priv[:1] + [u for u in self.log_urls if u != priv[0]]
                self.private = priv[0]
        self.headers = {"content-type": "application/json", **self.cfg.get("headers", {})}
        self.logs_range = self.cfg.get("logs_range", 1000)
        if self.private:  # Chainstack Growth: 10,000 blocks per eth_getLogs
            self.logs_range = int(self.cfg.get("private_logs_range", 10000))
        self.sem = asyncio.Semaphore(self.cfg.get("rps", 12))
        self.batch_max = int(self.cfg.get("batch_max", 50))  # learned downwards when a node says "too many calls in batch"
        if self.private:  # Chainstack Growth: 250 req/s plan-wide
            self.sem = asyncio.Semaphore(int(self.cfg.get("private_concurrency", 24)))
        self._id = 0
        self._bad = {}  # url -> time it failed

    def session(self):
        loop = asyncio.get_event_loop()
        key = id(loop)
        s = RPC._sessions.get(key)
        if s is None or s.closed:
            s = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=64, ttl_dns_cache=300, keepalive_timeout=60),
                                      timeout=aiohttp.ClientTimeout(total=40))
            RPC._sessions[key] = s
        return s

    def _billing_check(self, url, status, body):
        """Private endpoint paused for billing (Chainstack: 403 'services are paused due to unpaid invoices')."""
        if url == self.private and any(w in (body or "").lower() for w in BILLING_WORDS):
            mark_private_down(self.name)
            self._bad[url] = time.time()

    def _ordered_urls(self, urls=None):
        urls = urls or self.urls
        now = time.time()
        good = [u for u in urls if now - self._bad.get(u, 0) > 60]
        return good or urls

    async def _post(self, payload):
        last = None
        for url in self._ordered_urls():
            for attempt in range(2):
                try:
                    async with self.sem:
                        async with self.session().post(url, data=json.dumps(payload), headers=self.headers) as r:
                            if r.status == 429 or r.status >= 500:
                                raise RPCError(f"http {r.status}")
                            txt = await r.text()
                            if r.status == 403:
                                self._billing_check(url, r.status, txt)
                                raise RPCError("http 403")
                    d = json.loads(txt)
                    return d
                except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RPCError) as e:
                    last = e
                    await asyncio.sleep(0.3 * (attempt + 1))
            self._bad[url] = time.time()
        raise RPCError(f"all endpoints failed for {self.name}: {last}")

    SWITCH_WORDS = ("current plan", "upgrade", "archive", "personal token", "pruned", "missing trie node", "allnodes", "specify an address", "free plan", "not supported", "unsupported", "forbidden", "rate", "too many",
                    "quota", "capacity", "limit exceeded", "internal error", "timeout", "unavailable", "disabled", "not allowed", "paused", "unpaid", "invoice", "suspended")
    RANGE_WORDS = ("block range", "range", "exceeds max", "too large", "more than", "max blocks", "narrow")

    def _is_range_err(self, msg):
        m = msg.lower()
        return any(w in m for w in self.RANGE_WORDS) and "revert" not in m

    # immutable reads memoized for the life of the process: the same receipt / code / block is asked for by
    # several phases of one check (creation receipt 2-3x, token code 5x). Pending results (None) are not stored.
    _memo = {}
    MEMO_METHODS = ("eth_getCode", "eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_getBlockByNumber")

    async def call(self, method, params, urls=None):
        """JSON-RPC call. Reverts/range errors raise immediately; provider-limit errors move to the next endpoint."""
        mk = None
        # only real chains: forks reuse ports and addresses, so their code/receipts must never be memoized
        if method in self.MEMO_METHODS and self.name in CHAINS and not (method == "eth_getBlockByNumber" and not str(params[0]).startswith("0x")):
            mk = (self.name, method, json.dumps(params))
            if mk in RPC._memo:
                return RPC._memo[mk]
        res = await self._call(method, params, urls)
        if mk is not None and res is not None:
            if len(RPC._memo) > 4000:
                RPC._memo.clear()
            RPC._memo[mk] = res
        return res

    async def _call(self, method, params, urls=None):
        self._id += 1
        last = None
        if urls is None and method == "eth_getLogs":
            urls = self.log_urls
        pool = self._ordered_urls(urls)
        for url in pool:
            d = await self._post_to(url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            if d is None:
                continue
            if "error" in d:
                err = d["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if isinstance(err, dict) and err.get("data"):
                    msg += f' data: {err["data"]}'
                last = RPCError(msg)
                low = msg.lower()
                if "revert" in low or (self._is_range_err(msg) and url == pool[-1]) or len(pool) == 1:
                    raise last
                if self._is_range_err(msg):
                    continue  # "narrow the range / too many logs" is about the query, not the endpoint: never mark it bad
                if any(w in low for w in self.SWITCH_WORDS):
                    self._bad[url] = time.time()
                    if url == self.private and any(w in low for w in BILLING_WORDS):
                        mark_private_down(self.name)
                    continue
                raise last
            return d["result"]
        raise last or RPCError(f"all endpoints failed for {self.name}")

    async def _post_to(self, url, payload):
        """POST to one endpoint; returns parsed JSON or None (endpoint marked bad) on transport failure."""
        for attempt in range(4):
            try:
                async with self.sem:
                    async with self.session().post(url, data=json.dumps(payload), headers=self.headers) as r:
                        if r.status == 429:
                            raise RPCError("http 429")
                        if r.status >= 500 or r.status == 403:
                            self._billing_check(url, r.status, await r.text())
                            raise RPCError(f"http {r.status}")
                        txt = await r.text()
                d = json.loads(txt)
                if isinstance(d, dict) and isinstance(d.get("error"), dict) and any(w in str(d["error"].get("message", "")).lower() for w in ("rate limit", "too many requests", "exceeded the rate")):
                    raise RPCError("rate limited")
                return d
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RPCError) as e:
                limited = "429" in str(e) or "rate" in str(e)
                if attempt == 3 or (not limited and attempt >= 1):
                    break
                await asyncio.sleep((1.0 * 2 ** attempt) if limited else 0.3 * (attempt + 1))
        self._bad[url] = time.time()
        return None

    async def batch(self, calls, chunk=50):
        """calls: list of (method, params). Returns list of results (None on per-item error)."""
        out = [None] * len(calls)
        chunk = min(chunk, self.batch_max)

        async def one(start, size):
            items = calls[start:start + size]
            payload = [{"jsonrpc": "2.0", "id": start + i, "method": m, "params": p} for i, (m, p) in enumerate(items)]
            try:
                d = await self._post(payload)
                if isinstance(d, list) and size > 1:
                    # some nodes answer a too-big batch per item (dRPC free: "Batch of more than 3 requests are not allowed")
                    errs = [str((x.get("error") or {}).get("message", "")) for x in d if isinstance(x, dict) and isinstance(x.get("error"), dict)]
                    if any(_batch_cap_err(e) for e in errs):
                        d = {"error": {"message": next(e for e in errs if _batch_cap_err(e))}}
                if isinstance(d, dict):  # batch unsupported -> fall back
                    msg = str((d.get("error") or {}).get("message", "")).lower() if isinstance(d.get("error"), dict) else ""
                    if size > 1 and _batch_cap_err(msg):
                        # the node caps batch size (X Layer: 10): remember a smaller size and resend in pieces
                        half = max(1, size // 2)
                        self.batch_max = min(self.batch_max, half)
                        await asyncio.gather(*[one(s, half) for s in range(start, start + len(items), half)])
                        return
                    raise RPCError("batch unsupported")
                for item in d:
                    if "result" in item:
                        out[item["id"]] = item["result"]
            except RPCError:
                res = await asyncio.gather(*[self._safe(m, p) for m, p in items])
                for i, r in enumerate(res):
                    out[start + i] = r

        await asyncio.gather(*[one(s, chunk) for s in range(0, len(calls), chunk)])
        return out

    async def _safe(self, method, params):
        try:
            return await self.call(method, params)
        except RPCError:
            return None

    # ---- helpers ----
    async def block_number(self):
        return int(await self.call("eth_blockNumber", []), 16)

    async def code(self, addr):
        return await self.call("eth_getCode", [addr, "latest"])

    async def balance(self, addr, block="latest"):
        return int(await self.call("eth_getBalance", [addr, block]), 16)

    async def nonce(self, addr):
        return int(await self.call("eth_getTransactionCount", [addr, "latest"]), 16)

    async def storage(self, addr, slot):
        return await self.call("eth_getStorageAt", [addr, slot, "latest"])

    async def eth_call(self, to, data, sender=None, value=0, block="latest", overrides=None):
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        if value:
            tx["value"] = hex(value)
        params = [tx, block]
        if overrides:
            params.append(overrides)
        return await self.call("eth_call", params)

    async def try_call(self, to, data, sender=None, value=0, overrides=None):
        """Returns (ok, result_or_error_string)."""
        try:
            return True, await self.eth_call(to, data, sender, value, overrides=overrides)
        except RPCError as e:
            return False, str(e)

    async def tx(self, h):
        return await self.call("eth_getTransactionByHash", [h])

    async def receipt(self, h):
        return await self.call("eth_getTransactionReceipt", [h])

    async def block(self, n, full=False):
        return await self.call("eth_getBlockByNumber", [hex(n) if isinstance(n, int) else n, full])

    async def get_logs(self, address=None, topics=None, from_block=0, to_block=None, max_blocks=None, concurrency=None):
        """eth_getLogs over any range. Big ranges go to HyperSync (whole chain in seconds) and the last few
        blocks it hasn't indexed yet come from the RPC; small ranges use chunked, parallel RPC calls."""
        if to_block is None:
            to_block = await self.block_number()
        if max_blocks:
            from_block = max(from_block, to_block - max_blocks)
        from lib import hypersync
        span = to_block - from_block
        # RPC chunks up to ~1-2 s of work (Chainstack: 40 chunks in 0.25-0.55 s), then HyperSync — which is fast but
        # allows only 15 requests/min, so cheap scans stay on the RPC. Slow-log chains set hs_after_chunks lower
        # (monad: 40 chunks 2.7 s; 368 chunks took 45 s and timed out the LP scan).
        use_hs = span > self.logs_range * (self.cfg.get("hs_after_chunks") or (160 if self.private else 3))
        if hypersync.available(self.cfg.get("id")) and use_hs:
            try:
                hs_logs, covered = await self._hs_logs(address, topics, from_block, to_block)
                tail = await self._rpc_logs(address, topics, covered + 1, to_block, concurrency) if covered < to_block else []
                out = hs_logs + tail
                out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
                return out
            except Exception:
                # HyperSync down or rate-limited: scanning years of history over RPC would take minutes, so cover a
                # recent window only (callers treat a partial scan as partial: pool-liquidity guards, UNCLEAR)
                from_block = max(from_block, to_block - self.logs_range * (100 if self.private else 30))
        return await self._rpc_logs(address, topics, from_block, to_block, concurrency)

    async def _hs_logs(self, address, topics, from_block, to_block):
        """HyperSync logs with (1) a persistent cache of what was already scanned — logs older than 64 blocks never
        change, so a re-check only scans the new blocks — and (2) the remaining span split into 4 parallel queries
        (one 17M-block PoolManager scan: 6.1 s alone, 2.9 s as 4; HyperSync time grows with the span)."""
        from lib import hypersync, cache
        key = f"hslogs:{self.cfg['id']}:{json.dumps([address, topics], sort_keys=True).lower()}:{from_block}"
        c = cache.get(key) or {}
        logs, start = (c["logs"], c["to"] + 1) if c.get("to", -1) >= from_block else ([], from_block)
        covered = start - 1
        if start <= to_block:
            n = max(1, min(4, (to_block - start) // 5_000_000))  # parallel parts; each costs 1 of 15 requests/min
            step = (to_block - start) // n + 1
            parts = await asyncio.gather(*[hypersync.get_logs(self.cfg["id"], address, topics, s, min(to_block, s + step - 1))
                                           for s in range(start, to_block + 1, step)])
            for i, (lg, cov) in enumerate(parts):
                logs = logs + lg
                covered = cov
                end = min(to_block, start + (i + 1) * step - 1)
                if cov < end and i < len(parts) - 1:
                    if end - cov > 2000:
                        break  # part not indexed yet: stop here, the RPC tail covers the rest
                    logs = logs + await self._rpc_logs(address, topics, cov + 1, end)  # a few blocks HyperSync left out
                    covered = end
            logs = [l for l in logs if int(l["blockNumber"], 16) <= covered]
        safe = covered - 64  # keep only reorg-safe blocks in the cache
        if safe > c.get("to", -1) and safe >= from_block:
            cache.set(key, {"to": safe, "logs": [l for l in logs if int(l["blockNumber"], 16) <= safe]}, ttl=7 * 86400)
        covered = min(covered, to_block)
        return [l for l in logs if int(l["blockNumber"], 16) <= covered], covered

    async def _rpc_logs(self, address, topics, from_block, to_block, concurrency=None):
        rng = self.logs_range
        chunks = [(s, min(s + rng - 1, to_block)) for s in range(from_block, to_block + 1, rng)]
        sem = asyncio.Semaphore(concurrency or (20 if self.private else 6))
        out = []

        async def one(s, e):
            f = {"fromBlock": hex(s), "toBlock": hex(e)}
            if address:
                f["address"] = address
            if topics:
                f["topics"] = topics
            try:
                async with sem:  # hold a slot only for the request itself, never across the split below
                    return await self.call("eth_getLogs", [f])
            except RPCError as err:
                if self._is_range_err(str(err)) or "limit" in str(err).lower():
                    if e - s < 10:
                        return []
                    mid = (s + e) // 2
                    a, b = await asyncio.gather(one(s, mid), one(mid + 1, e))
                    return a + b
                raise

        for res in await asyncio.gather(*[one(s, e) for s, e in chunks]):
            out.extend(res)
        out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
        return out


def run(coro):
    """Run a coroutine to completion and close pooled sessions cleanly."""
    async def _wrap():
        try:
            return await coro
        finally:
            for s in list(RPC._sessions.values()):
                if not s.closed:
                    await s.close()
            RPC._sessions.clear()
            try:
                from lib import hypersync
                await hypersync.close()
            except Exception:
                pass
    return asyncio.run(_wrap())


def topic_addr(t):
    return "0x" + t[-40:]


def pad_addr(a):
    return "0x" + a[2:].lower().rjust(64, "0")
