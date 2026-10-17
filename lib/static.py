"""Verified source -> what each function actually does (Slither, compiled exactly like the explorer did; cached).

Per public function: protected? (onlyOwner / msg.sender check), storage written (incl. internal helpers), outside calls,
native sends, delegatecall/selfdestruct. Plus the map that matters most for holders:
  "owner switches"  = state variables every transfer READS that a protected function can WRITE (fees, blacklists,
                      trading flags, limits, the pair, external checker contracts) — and which function sets each.
Findings are READ FROM CODE (inferred); simulations prove behaviour.
"""
import asyncio, json, os, re, sys, tempfile, shutil
from lib import cache

VERSION = 6
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
SOLC_SELECT = os.path.join(ROOT, ".venv", "bin", "solc-select")
ART = os.path.expanduser("~/.solc-select/artifacts")
TRANSFER_SIGS = ("transfer(address,uint256)", "transferFrom(address,address,uint256)")
STANDARD = set(TRANSFER_SIGS) | {"approve(address,uint256)", "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
                                 "increaseAllowance(address,uint256)", "decreaseAllowance(address,uint256)",
                                 "renounceOwnership()", "transferOwnership(address)", "acceptOwnership()"}


async def _solc(version):
    path = os.path.join(ART, f"solc-{version}", f"solc-{version}")
    if os.path.exists(path):
        return path
    p = await asyncio.create_subprocess_exec(SOLC_SELECT, "install", version, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(p.wait(), 90)
    except asyncio.TimeoutError:
        p.kill()
    return path if os.path.exists(path) else None


async def analyze(ex, addr, timeout=90):
    """-> summary dict, {'error': ...}, or None if not verified. Cached per chain+address (verified source never changes)."""
    key = f"static:v{VERSION}:{ex.chain}:{addr.lower()}"
    c = cache.get(key)
    if c:
        return json.loads(c) if isinstance(c, str) else c
    sf = await ex.source_full(addr)
    if not sf or not sf.get("files"):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", sf.get("compiler") or "")
    if not m:
        return {"error": f"unknown compiler {sf.get('compiler')}"}
    if not (sf.get("compiler") or "").lstrip("v").startswith(("0.4", "0.5", "0.6", "0.7", "0.8")):
        return {"error": f"not Solidity ({sf.get('compiler')})"}
    solc = await _solc(m.group(1))
    if not solc:
        return {"error": f"could not install solc {m.group(1)}"}
    settings = sf.get("settings") or {"optimizer": {"enabled": sf.get("optimizer"), "runs": sf.get("runs", 200)}}
    if sf.get("evm") and sf["evm"] not in ("default", "") and "evmVersion" not in settings:
        settings["evmVersion"] = sf["evm"]
    out = await _work(sf["files"], settings, solc, sf.get("name"), timeout)
    if out.get("ok"):
        cache.set(key, json.dumps(out))
    return out


async def code(ex, addr, selector, timeout=90):
    """Exact source of one function (+ modifiers and internal functions it reaches) of a verified contract. Cached."""
    key = f"code:v{VERSION}:{ex.chain}:{addr.lower()}:{selector.lower()}"
    c = cache.get(key)
    if c:
        return json.loads(c) if isinstance(c, str) else c
    sf = await ex.source_full(addr)
    if not sf or not sf.get("files"):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", sf.get("compiler") or "")
    solc = await _solc(m.group(1)) if m else None
    if not solc:
        return {"error": f"no compiler for {sf.get('compiler')}"}
    settings = sf.get("settings") or {"optimizer": {"enabled": sf.get("optimizer"), "runs": sf.get("runs", 200)}}
    out = await _work(sf["files"], settings, solc, sf.get("name"), timeout, {"mode": "code", "selector": selector})
    if out.get("ok") and not out.get("error"):
        cache.set(key, json.dumps(out))
    return out


async def analyze_local(files, version, name, settings=None, timeout=90):
    """Same analysis for source we have locally (our test contracts). files: {path: code}."""
    solc = await _solc(version)
    if not solc:
        return {"error": f"could not install solc {version}"}
    return await _work(files, settings or {"optimizer": {"enabled": True, "runs": 200}}, solc, name, timeout)


async def _work(files, settings, solc, name, timeout, extra=None):
    d = tempfile.mkdtemp(prefix="sl-")
    try:
        inp, outp = os.path.join(d, "in.json"), os.path.join(d, "out.json")
        json.dump({"files": files, "settings": settings, "solc": solc, "name": name, "workdir": d, **(extra or {})}, open(inp, "w"))
        p = await asyncio.create_subprocess_exec(PY, os.path.join(ROOT, "lib", "static_worker.py"), inp, outp, cwd=d,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return {"error": f"slither timed out after {timeout}s"}
        if not os.path.exists(outp):
            last = (err or b"").decode(errors="ignore").strip().splitlines()[-1:] or ["?"]
            return {"error": f"slither failed: {last[0][:120]}"}
        out = json.load(open(outp))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    out["ok"] = True
    return out


def cached(ex, addr):
    c = cache.get(f"static:v{VERSION}:{ex.chain}:{addr.lower()}")
    return (json.loads(c) if isinstance(c, str) else c) if c else None


CALLBACK = ("before", "after", "unlockCallback", "uniswapV3", "pancakeV3", "lockAcquired", "onERC721Received", "supportsInterface")


def _skip(f):
    return f["sig"] in STANDARD or f["name"].startswith(("initialize", "__") + CALLBACK)


def switches(s):
    """state var read by transfers -> protected functions that can write it"""
    if not s or not s.get("ok"):
        return {}
    tr_reads = set()
    for f in s["fns"]:
        if f["sig"] in TRANSFER_SIGS:
            tr_reads |= set(f["reads"])
    out = {}
    for f in s["fns"]:
        if not f["protected"] or _skip(f):
            continue
        for v in f["writes"]:
            if v in tr_reads and v not in (s.get("balance_vars") or []) + (s.get("supply_vars") or []) and v not in ("_allowances", "allowance", "_owner", "owner"):
                out.setdefault(v, []).append(f["name"])
    return out


def findings(s):
    if not s or not s.get("ok"):
        return []
    out = []
    sup, bal = set(s.get("supply_vars") or []), set(s.get("balance_vars") or [])
    sw = switches(s)
    for f in s["fns"]:
        if _skip(f):
            continue
        who = "owner-only" if f["protected"] else "ANYONE"
        if f["protected"] and sup & set(f["writes"]):
            out.append(("bad", f"{f['sig']} ({who}) changes totalSupply — mint/burn power"))
        elif f["protected"] and bal & set(f["writes"]) and "address" in f["params"]:
            out.append(("bad", f"{f['sig']} ({who}) edits balances of an address it is given"))
        for op in f["ops"]:
            if op.startswith("delegatecall") or op == "selfdestruct":
                out.append(("bad", f"{f['sig']} ({who}) does {op}"))
    # transfers calling a contract the owner can replace
    for f in s["fns"]:
        if f["sig"] not in TRANSFER_SIGS:
            continue
        for call in f["calls"]:
            tgt = call.split(".")[0]
            if tgt in sw:
                out.append(("warn", f"transfers call {call} — owner can replace `{tgt}` via {', '.join(sw[tgt][:2])}()"))
    return list(dict.fromkeys(out))


def render(s):
    if not s:
        return []
    if s.get("error"):
        return [f"== CODE (Slither): not analysed — {s['error']}"]
    L = [f"== CODE (Slither on verified source {s['contract']} — read from code, not executed)"]
    for lvl, msg in findings(s):
        L.append(f"   {'✗' if lvl == 'bad' else '!'} {msg}")
    sw = switches(s)
    if sw:
        L.append("   owner switches every transfer reads: " + "; ".join(f"{v} ← {', '.join(fs[:2])}()" for v, fs in list(sw.items())[:8]))
    priv = [f for f in s["fns"] if f["protected"] and not _skip(f)]
    for f in priv[:12]:
        bits = []
        if f["writes"]:
            bits.append("writes " + ", ".join(f["writes"][:4]))
        if f["calls"]:
            bits.append("calls " + ", ".join(f["calls"][:2]))
        if f["ops"]:
            bits.append(", ".join(f["ops"][:2]))
        gate = ", ".join(f["modifiers"]) or f.get("gate") or "msg.sender check"
        L.append(f"   · {f['sig']} [{gate}]: {'; '.join(bits) or 'no storage writes'}")
    open_fns = [f for f in s["fns"] if not f["protected"] and not _skip(f) and (set(f["writes"]) & (set(s.get('supply_vars') or []) | set(s.get('balance_vars') or [])))]
    for f in open_fns[:4]:
        L.append(f"   · {f['sig']} [anyone]: writes {', '.join(f['writes'][:4])}")
    return L
