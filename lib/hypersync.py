"""Envio HyperSync client: whole-chain log and transaction history in seconds.
Token in keys/envio (never printed). Endpoint per chain: https://<chainId>.hypersync.xyz
Results are converted to eth_getLogs / explorer-txlist shapes so callers don't care where data came from.
"""
import asyncio, json, os
import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TF = os.path.join(ROOT, "keys", "envio")
TOKEN = open(_TF).read().strip() if os.path.exists(_TF) else ""
SUPPORTED = {1, 10, 56, 143, 146, 999, 4326, 4663, 5042, 8453, 42161, 43114, 57073}
_session = {}


def available(chain_id):
    return bool(TOKEN) and chain_id in SUPPORTED


def _sess():
    loop = asyncio.get_event_loop()
    s = _session.get(id(loop))
    if s is None or s.closed:
        s = aiohttp.ClientSession(headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                  timeout=aiohttp.ClientTimeout(total=60))
        _session[id(loop)] = s
    return s


async def close():
    for s in list(_session.values()):
        if not s.closed:
            await s.close()
    _session.clear()


async def _query(chain_id, q, max_pages=400):
    """Paginate a HyperSync query. -> (list of data items, archive_height)"""
    url = f"https://{chain_id}.hypersync.xyz/query"
    out, ah = [], None
    end = q.get("to_block")
    for _ in range(max_pages):
        for attempt in range(3):
            try:
                async with _sess().post(url, data=json.dumps(q)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"hypersync http {r.status}: {(await r.text())[:120]}")
                    d = await r.json()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        out += d.get("data", [])
        ah = d.get("archive_height", ah)
        nb = d.get("next_block")
        if not nb or (end and nb >= end) or (ah and nb >= ah):
            break
        q = dict(q, from_block=nb)
    return out, ah


def _hex(n):
    return hex(n) if isinstance(n, int) else n


async def get_logs(chain_id, address, topics, from_block, to_block):
    """eth_getLogs-shaped results for [from_block, to_block] (to_block inclusive) + highest block covered."""
    sel = {"topics": [([t] if isinstance(t, str) else (t or [])) for t in (topics or [])]}
    if address:
        sel["address"] = address if isinstance(address, list) else [address]
    q = {"from_block": from_block, "to_block": to_block + 1, "logs": [sel],
         "field_selection": {"log": ["block_number", "log_index", "transaction_hash", "address", "topic0", "topic1", "topic2", "topic3", "data"]}}
    data, ah = await _query(chain_id, q)
    logs = []
    for item in data:
        for l in item.get("logs", []):
            tps = [l.get(f"topic{i}") for i in range(4)]
            logs.append({"address": l.get("address"), "topics": [t for t in tps if t], "data": l.get("data") or "0x",
                         "blockNumber": _hex(l.get("block_number")), "logIndex": _hex(l.get("log_index")),
                         "transactionHash": l.get("transaction_hash")})
    covered = min(to_block, (ah - 1) if ah else to_block)
    return logs, covered


async def txlist(chain_id, addr, from_block=0, with_time=True):
    """All transactions sent from or to `addr` (ascending), explorer-txlist shaped."""
    a = addr.lower()
    q = {"from_block": from_block, "transactions": [{"from": [a]}, {"to": [a]}],
         "field_selection": {"transaction": ["block_number", "hash", "from", "to", "value", "input", "contract_address", "status"],
                             "block": ["number", "timestamp"] if with_time else []}}
    data, _ = await _query(chain_id, q)
    ts, rows = {}, []
    for item in data:
        for b in item.get("blocks", []) or []:
            ts[b.get("number")] = b.get("timestamp")
        rows += item.get("transactions", []) or []
    out = []
    for t in rows:
        bn = t.get("block_number")
        tsv = ts.get(bn)
        tsv = int(tsv, 16) if isinstance(tsv, str) and tsv.startswith("0x") else tsv
        val = t.get("value") or "0x0"
        out.append({"hash": t.get("hash"), "from": t.get("from"), "to": t.get("to"), "blockNumber": str(bn),
                    "value": str(int(val, 16) if isinstance(val, str) and val.startswith("0x") else int(val or 0)),
                    "input": t.get("input") or "0x", "contractAddress": t.get("contract_address") or "",
                    "timeStamp": str(tsv or ""), "isError": "0" if t.get("status") in (1, "0x1", None) else "1", "via": "hypersync"})
    return out


async def first_funding(chain_id, addr, max_pages=6):
    """Earliest inbound native transfer to `addr` (reads only the first pages of its history)."""
    a = addr.lower()
    q = {"from_block": 0, "transactions": [{"to": [a]}],
         "field_selection": {"transaction": ["block_number", "hash", "from", "to", "value"], "block": ["number", "timestamp"]}}
    url = f"https://{chain_id}.hypersync.xyz/query"
    for _ in range(max_pages):
        async with _sess().post(url, data=json.dumps(q)) as r:
            if r.status != 200:
                return {}
            d = await r.json()
        ts = {}
        for item in d.get("data", []):
            for b in item.get("blocks", []) or []:
                ts[b.get("number")] = b.get("timestamp")
            for t in item.get("transactions", []) or []:
                v = t.get("value") or "0x0"
                v = int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v or 0)
                if v > 0:
                    tsv = ts.get(t.get("block_number"))
                    tsv = int(tsv, 16) if isinstance(tsv, str) and tsv.startswith("0x") else tsv
                    return {"from": t.get("from"), "tx": t.get("hash"), "time": str(tsv or "")}
        nb, ah = d.get("next_block"), d.get("archive_height")
        if not nb or (ah and nb >= ah):
            break
        q = dict(q, from_block=nb)
    return {}
