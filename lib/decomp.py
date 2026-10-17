"""Unverified bytecode -> what each function actually does (Heimdall-rs decompiler, run locally, cached by code hash).

Per function we report structure, not names:
  access    who it checks: owner-like storage address / HARDCODED address / allowlist map / tx.origin / nobody
  writes    storage it changes: totalSupply, a per-wallet entry for ANY address passed in, plain variables
  calls     outside contracts it calls (and with value), delegatecall, selfdestruct
  reverts   the revert messages it can throw (e.g. transfer: "cooldown", "max sell", "paused")
Heimdall output is pseudocode: treat findings as READ FROM CODE (inferred), not executed. Simulations prove behaviour.
"""
import asyncio, json, os, re, shutil, tempfile
from eth_utils import keccak
from lib import cache, sigs

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "heimdall", "heimdall")
VERSION = 2  # bump to invalidate cached parses
STD = {"0xa9059cbb", "0x23b872dd", "0x095ea7b3", "0x70a08231", "0x18160ddd", "0xdd62ed3e", "0x06fdde03", "0x95d89b41", "0x313ce567"}
TRANSFER_SELS = {"0xa9059cbb", "0x23b872dd"}


def available():
    return os.path.exists(BIN)


async def _run(code_hex, timeout):
    d = tempfile.mkdtemp(prefix="hd-")
    try:
        p = await asyncio.create_subprocess_exec(BIN, "decompile", code_hex, "--include-sol", "--output", d, "-q", "--timeout", "2000",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=d)
        try:
            await asyncio.wait_for(p.wait(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return None
        f = os.path.join(d, "decompiled.sol")
        return open(f).read() if os.path.exists(f) else None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _split(sol):
    """-> [(selector, signature line, body)]"""
    parts = re.split(r"/// @custom:selector\s+(0x[0-9a-fA-F]{8})", sol)
    out = []
    for i in range(1, len(parts) - 1, 2):
        sel, chunk = parts[i].lower(), parts[i + 1]
        m = re.search(r"function\s+[^\n]*\{", chunk)
        if not m:
            continue
        out.append((sel, m.group(0), chunk[m.end():]))
    return out


ACCESS_RE = re.compile(r"require\(([^;]*msg\.sender[^;]*)\)")


def _features(sel, sig, body):
    view = " view " in sig or " pure " in sig
    f = {"sel": sel, "view": view, "access": None, "writes": [], "calls": [], "ops": [], "reverts": [], "events": []}
    # local variables that hold an argument (Heimdall: `address var_a = address(arg0);`)
    arg_vars = set(re.findall(r"(var_\w+)\s*=\s*(?:address\()?\(?arg\d", body))
    for m in ACCESS_RE.finditer(body):
        cond = m.group(1)
        hard = re.search(r"(?:\b(0x[0-9a-fA-F]{1,40}|0)\s*(?:==|-)\s*\(?(?:address\()?msg\.sender)|(?:msg\.sender\)?\s*(?:==|-)\s*\(?(?:address\()?(0x[0-9a-fA-F]{1,40})\b)", cond)
        if hard:
            v = hard.group(1) or hard.group(2)
            f["access"] = "fixed address " + ("0x" + v[2:].rjust(40, "0").lower() if v.startswith("0x") else "(immutable)"); break
        st = re.search(r"address\((\w+)\)", cond.replace("address(msg.sender)", ""))
        if st and not st.group(1).startswith(("arg", "var_", "msg")):
            f["access"] = f"only {st.group(1)}"; break
        if re.search(r"storage_map_\w+\[[^\]]*msg\.sender", cond):
            f["access"] = "allowlist/role map"; break
    if not f["access"] and "tx.origin" in body:
        f["access"] = "checks tx.origin"
    for line in body.splitlines():
        s = line.strip()
        w = re.match(r"([A-Za-z_]\w*)(\[[^\]]*\])?\s*=(?!=)", s)
        if w and not w.group(1).startswith(("var_", "uint", "int", "address", "bool", "bytes", "string")):
            name, key = w.group(1), (w.group(2) or "")[1:-1]
            if key:
                k = "any address (argument)" if (key.startswith("arg") or key in arg_vars or "arg" in key) else ("caller" if "msg.sender" in key else "computed key")
                tag = f"{name}[{k}]"
            else:
                tag = name
            if tag not in f["writes"]:
                f["writes"].append(tag)
        for cm in re.finditer(r"address\(([^)]*)\)\.(\w+)\(", s):
            tgt, fn = cm.group(1), cm.group(2)
            if fn in ("transfer", "send"):
                f["ops"].append(f"sends native to {tgt}")
            else:
                f["calls"].append(f"{tgt}.{fn}")
        if "delegatecall" in s:
            f["ops"].append("delegatecall")
        if "selfdestruct" in s:
            f["ops"].append("selfdestruct")
        if ".call{value" in s or "call.value" in s:
            f["ops"].append("sends native (call with value)")
    f["reverts"] = list(dict.fromkeys(re.findall(r'"([^"]{1,48})"\)', body)))[:8]
    f["events"] = list(dict.fromkeys(re.findall(r"emit (\w+)\(", body)))[:5]
    f["calls"] = list(dict.fromkeys(f["calls"]))[:6]
    f["ops"] = list(dict.fromkeys(f["ops"]))
    return f


def _key(code_hex):
    return f"decomp:v{VERSION}:0x" + keccak(bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)).hex()


def cached(code_hex):
    """Previously decompiled result for this exact code, or None (no work done)."""
    if not code_hex or len(code_hex) < 10:
        return None
    c = cache.get(_key(code_hex))
    return (json.loads(c) if isinstance(c, str) else c) if c else None


async def analyze(code_hex, timeout=25):
    """-> {'fns': {sel: features}, 'ok': bool} (cached per code hash) or None if Heimdall is missing/failed."""
    if not available() or not code_hex or len(code_hex) < 10:
        return None
    key = _key(code_hex)
    h = key.split(":")[-1]
    c = cache.get(key)
    if c:
        return json.loads(c) if isinstance(c, str) else c
    sol = await _run(code_hex, timeout)
    if not sol:
        return None
    fns = {sel: _features(sel, sig, body) for sel, sig, body in _split(sol)}
    names = await sigs.names_for(list(fns))
    for sel, f in fns.items():
        f["name"] = names.get(sel) or f"unnamed {sel}"
    out = {"ok": True, "code_hash": h, "fns": fns}
    cache.set(key, json.dumps(out))
    return out


def findings(d):
    """High-signal facts read from code (not executed). -> [(level, text)]"""
    if not d:
        return []
    out = []
    for sel, f in d["fns"].items():
        if f["view"] or sel in STD - TRANSFER_SELS:
            continue
        nm = f["name"]
        gated = f["access"]
        if gated and gated.startswith("fixed address"):
            out.append(("warn", f"{nm}: only callable by {gated} baked into the code — not visible in owner(), can't be renounced"))
        if sel in TRANSFER_SELS:
            # only contracts held in storage/hardcoded can be swapped by the dev; calls on the recipient/sender (arg) can't
            ext = [c for c in f["calls"] if not c.endswith(("balanceOf", "decimals")) and not c.startswith(("arg", "var_", "msg.sender"))]
            if ext:
                out.append(("warn", f"{nm} calls outside contract(s) during transfers: {', '.join(ext[:3])} — they can be swapped to block sells"))
            continue
        if not gated:
            continue
        if "totalSupply" in f["writes"]:
            out.append(("bad", f"{nm} ({gated}) changes totalSupply — mint/burn power"))
        per_wallet = [w for w in f["writes"] if "[any address (argument)]" in w]
        if per_wallet and nm.startswith("unnamed"):  # named setters (setTaxExempt…) are listed in the CODE table only
            out.append(("warn", f"{nm} ({gated}) sets a per-wallet entry for ANY address — balance edit / blacklist / limit"))
        for op in f["ops"]:
            if op in ("delegatecall", "selfdestruct"):
                out.append(("bad", f"{nm} ({gated}) does {op}"))
    return out


def transfer_reverts(d):
    if not d:
        return []
    msgs = []
    for sel in TRANSFER_SELS:
        f = d["fns"].get(sel)
        if f:
            msgs += [m for m in f["reverts"] if m not in msgs]
    return msgs


def render(d, only_privileged=True):
    if not d:
        return []
    L = [f"== CODE (decompiled with Heimdall — read from bytecode, not executed)"]
    for lvl, msg in findings(d):
        L.append(f"   {'✗' if lvl == 'bad' else '!'} {msg}")
    tr = transfer_reverts(d)
    if tr:
        L.append(f"   transfer can revert with: {', '.join(repr(m) for m in tr[:8])}")
    rows = [f for s, f in d["fns"].items() if not f["view"] and s not in STD and (f["access"] or not only_privileged)]
    for f in rows[:10]:
        bits = []
        if f["writes"]:
            bits.append("writes " + ", ".join(f["writes"][:3]))
        if f["calls"]:
            bits.append("calls " + ", ".join(f["calls"][:2]))
        if f["ops"]:
            bits.append(", ".join(f["ops"][:2]))
        L.append(f"   · {f['name']} [{f['access']}]: {'; '.join(bits) or 'no storage writes or calls'}")
    return L
