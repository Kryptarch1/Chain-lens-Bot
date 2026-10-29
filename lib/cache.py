"""SQLite cache: generic kv with TTL + function-signature table.
Everything that never changes (bytecode, source, deployer, selectors) is cached forever.
"""
import json, os, sqlite3, threading, time

DB = os.environ.get("DD_CACHE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache.db"))
_lock = threading.Lock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        _conn = sqlite3.connect(DB, check_same_thread=False, timeout=30)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, exp REAL)")
        _conn.execute("CREATE TABLE IF NOT EXISTS sigs (sel TEXT PRIMARY KEY, name TEXT, src TEXT)")
        _conn.execute("CREATE TABLE IF NOT EXISTS events (topic TEXT PRIMARY KEY, name TEXT)")
        _conn.commit()
    return _conn


def get(k):
    with _lock:
        row = conn().execute("SELECT v, exp FROM kv WHERE k=?", (k,)).fetchone()
    if not row:
        return None
    v, exp = row
    if exp and exp < time.time():
        return None
    return json.loads(v)


def set(k, v, ttl=None):
    exp = time.time() + ttl if ttl else None
    with _lock:
        conn().execute("INSERT OR REPLACE INTO kv (k, v, exp) VALUES (?,?,?)", (k, json.dumps(v), exp))
        conn().commit()


def sig_get_many(sels):
    sels = [s.lower() for s in sels]
    if not sels:
        return {}
    q = ",".join("?" * len(sels))
    with _lock:
        rows = conn().execute(f"SELECT sel, name FROM sigs WHERE sel IN ({q})", sels).fetchall()
    return dict(rows)


def sig_set_many(d, src="openchain"):
    if not d:
        return
    with _lock:
        conn().executemany("INSERT OR REPLACE INTO sigs (sel, name, src) VALUES (?,?,?)",
                           [(k.lower(), v, src) for k, v in d.items()])
        conn().commit()


def event_get_many(topics):
    topics = [t.lower() for t in topics]
    if not topics:
        return {}
    q = ",".join("?" * len(topics))
    with _lock:
        rows = conn().execute(f"SELECT topic, name FROM events WHERE topic IN ({q})", topics).fetchall()
    return dict(rows)


def event_set_many(d):
    if not d:
        return
    with _lock:
        conn().executemany("INSERT OR REPLACE INTO events (topic, name) VALUES (?,?)",
                           [(k.lower(), v) for k, v in d.items()])
        conn().commit()


_inflight = {}


async def once(k, make):
    """Single-flight: concurrent callers with the same key share ONE run of make() (a coroutine factory) in this
    process. Parallel phases often ask for the same wallet history / funding at the same moment — one request, not
    two, against rate-limited APIs (HyperSync: 15 requests/min on the free plan)."""
    import asyncio
    loop = asyncio.get_running_loop()
    key = (id(loop), k)
    fut = _inflight.get(key)
    if fut is None:
        fut = _inflight[key] = asyncio.ensure_future(make())
        fut.add_done_callback(lambda _f: _inflight.pop(key, None))
    return await asyncio.shield(fut)
