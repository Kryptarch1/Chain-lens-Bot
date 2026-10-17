"""dd code <chain> <address> <function> [--from 0x..] [--args a,b,..] [--json]

Show what ONE function really does:
  verified    the exact Solidity of the function + its modifiers + every internal function it reaches (Slither)
  unverified  Heimdall pseudocode for that function, with selectors/custom errors decoded and plain storage variables
              renamed from the contract's own getters (decompiler map names are flagged: they can be wrong)
  STORAGE     the variables it uses, with the slot each lives in and its LIVE value — proven by RPC (getter access lists /
              compiler layout), never guessed from decompiled text
  TRACE       one simulated call (nothing is broadcast): every storage slot read / written (named, old → new), outside
              calls and events. This is the ground truth when the pseudocode looks mangled.
<function> = name (lowerFee), full signature (lowerFee(uint16)) or selector (0x117ef036). Works on proxies (code from the
implementation, storage from the proxy).
"""
import asyncio, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eth_abi import encode
from lib.rpc import RPC, RPCError, run, ZERO
from lib.evm import encode_call, addr as to_addr, short, hardcoded_addresses
from lib import explorer, sigs, decomp, static, slots, sim, cache, args as argsmod

STRANGER = "0x000000000000000000000000000000000000bEEF"
SLOT_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SLOT_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
ZOS_IMPL = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
CALLBACK = ("before", "after", "unlockCallback", "uniswapV3", "pancakeV3", "lockAcquired")
MAX_LINES = 160


async def implementation(rpc, addr, code):
    """Where the code lives: EIP-1967 / beacon / ZeppelinOS proxy or EIP-1167 clone -> (impl address or None, impl code)."""
    w = await rpc.batch([("eth_getStorageAt", [addr, s, "latest"]) for s in (SLOT_IMPL, SLOT_BEACON, ZOS_IMPL)])
    impl = None
    if w[0] and int(w[0], 16):
        impl = to_addr(w[0])
    elif w[1] and int(w[1], 16):
        ok, r = await rpc.try_call(to_addr(w[1]), encode_call("implementation()"))
        impl = to_addr(r) if ok and r and len(r) >= 66 else None
    elif w[2] and int(w[2], 16):
        impl = to_addr(w[2])
    if not impl:
        m = re.fullmatch(r"0x363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3", code.lower())
        impl = "0x" + m.group(1) if m else None
    return (impl, await rpc.code(impl)) if impl else (None, code)


def pick(fns, want):
    w = want.lower()
    if re.fullmatch(r"0x[0-9a-f]{8}", w):
        return [f for f in fns if f["sel"] == w]
    if "(" in w:
        return [f for f in fns if (f["name"] or "").lower() == w]
    return [f for f in fns if (f["name"] or "").split("(")[0].lower() == w]


def parse_args(types, raw):
    """--args a,b,c for the function's ABI types (address/uint/int/bool/bytes32 only)."""
    ts = [t.strip() for t in types.split(",") if t.strip()]
    vals = [v.strip() for v in raw.split(",")] if raw else []
    if len(ts) != len(vals):
        raise SystemExit(f"--args needs {len(ts)} value(s) for ({types})")
    out = []
    for t, v in zip(ts, vals):
        if t == "address":
            out.append(v)
        elif t.startswith(("uint", "int")):
            out.append(int(v, 0))
        elif t == "bool":
            out.append(v.lower() in ("1", "true", "yes"))
        elif t == "bytes32":
            out.append(bytes.fromhex(v[2:].rjust(64, "0")))
        else:
            raise SystemExit(f"--args: type {t} not supported")
    return encode(ts, out).hex()


async def choose_call(rpc, addr, f, cands, frm=None, raw_args=None):
    """Who calls it and with what: the first (caller, args) the contract accepts. -> (caller, data, accepted?)"""
    if raw_args is not None:
        data = f["sel"] + (parse_args(f["args"], raw_args) if f["args"] else "")
        return frm or cands[0], data, None
    callers = [frm] if frm else [STRANGER] + [c for c in cands if c and c.lower() != STRANGER.lower()]
    tries = []
    for c in callers[:8]:
        for data in list(argsmod.variants(f["sel"], f["name"] or "", f["args"], c)) or ([f["sel"]] if not f["args"] else []):
            tries.append((c, data))
    if not tries:
        return callers[0], None, None
    res = await asyncio.gather(*[sim.override_call(rpc, addr, d, c) for c, d in tries])
    for (c, d), (ok, _) in zip(tries, res):
        if ok:
            return c, d, True
    return tries[0][0], tries[0][1], False


async def trace(rpc, frm, to, data):
    """callTracer (calls + logs) and prestate before/after for one simulated call. -> (calls, pre, diff) or None"""
    tx = {"from": frm, "to": to, "data": data, "gas": hex(5_000_000)}
    ov = {"stateOverrides": {frm: {"balance": hex(10 ** 21)}}}
    cfgs = [{"tracer": "callTracer", "tracerConfig": {"withLog": True}, **ov}, {"tracer": "prestateTracer", **ov},
            {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}, **ov}]
    try:
        return await asyncio.gather(*[asyncio.wait_for(rpc.call("debug_traceCall", [tx, "latest", c]), 15) for c in cfgs])
    except Exception:
        return None


class LazyFork:
    """One private fork, started only if the node can't answer (no access lists / no debug_* on public RPCs)."""
    def __init__(self, chain):
        self.chain, self.f = chain, None

    async def rpc(self):
        if self.f is None:
            self.f = await sim.start(self.chain) or False
        return self.f.rpc if self.f else None

    def close(self):
        if self.f:
            self.f.close()


async def trace_any(rpc, fork, frm, to, data):
    """Trace on the node; if it has no debug_* (public RPCs), on the private fork (anvil traces locally)."""
    r = await trace(rpc, frm, to, data)
    if r is not None:
        return r, "node"
    frpc = await fork.rpc()
    return (await trace(frpc, frm, to, data), "fork") if frpc else (None, None)


def getter_renames(sol):
    """store_x -> getter name, when a getter's whole body is `return store_x;` (unique links only)."""
    found = {}
    for sel, sig, body in decomp._split(sol):
        m = re.match(r"function\s+([A-Za-z_]\w*)\(\)", sig.strip())
        if not m or m.group(1).startswith("Unresolved_"):
            continue
        lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith(("require(", "//", "}"))]
        if len(lines) == 1 and lines[0].startswith("return "):
            names = set(re.findall(r"\bstore_[a-z]+\b", lines[0]))
            if len(names) == 1:
                found.setdefault(names.pop(), set()).add(m.group(1))
    return {k: v.pop() for k, v in found.items() if len(v) == 1}


async def decode_hex_names(text):
    """Unresolved_xxxxxxxx / CustomError_xxxxxxxx -> real names from the signature database."""
    hx = sorted(set(re.findall(r"(?:Unresolved|CustomError)_([0-9a-f]{8})", text)))
    if not hx:
        return text
    names = await sigs.names_for(["0x" + h for h in hx])
    for h in hx:
        n = names.get("0x" + h)
        if n:
            text = re.sub(rf"(?:Unresolved|CustomError)_{h}\b", n.split("(")[0], text)
    return text


async def main(chain, address, want, frm=None, raw_args=None, as_json=False):
    rpc = RPC(chain)
    out, L = await report(rpc, explorer.Explorer(rpc.name), rpc.name, address, want, frm, raw_args)
    print(json.dumps(out, default=str, indent=1) if as_json else "\n".join(L))


async def report(rpc, ex, chain, address, want, frm=None, raw_args=None):
    """-> (dict, lines). rpc may be a fork; `chain` names the chain for a fallback fork."""
    t0 = time.time()
    code = await rpc.code(address)
    if len(code) <= 2:
        return {}, [f"{address} has no code on {chain}"]
    impl, icode = await implementation(rpc, address, code)
    code_addr = impl or address
    rows = sigs.selectors_from_bytecode(icode)
    names = await sigs.names_for([r[0] for r in rows])
    fns = [{"sel": s, "name": names.get(s), "args": a, "mut": m} for s, a, m in rows]
    hits = pick(fns, want)
    if not hits:
        custom = [f["name"] or f["sel"] for f in fns if f.get("mut") not in ("view", "pure")]
        return {}, [f"no function '{want}' in {short(code_addr)}. State-changing functions: {', '.join(custom[:40])}"]
    f = hits[0]
    src_task = asyncio.create_task(ex.source(code_addr))
    sm_task = asyncio.create_task(slots.resolve(rpc, address, fns))
    src = await src_task
    verified = bool(src.get("verified"))
    # candidate callers: owner-like getters, addresses held in storage, deployer, hardcoded addresses, PoolManager
    sm = await sm_task
    fork = LazyFork(chain)
    if not sm.scalars and not sm.maps and not verified:  # node can't say which slots getters read: ask a fork
        frpc = await fork.rpc()
        if frpc:
            sm = await slots.resolve(frpc, address, fns)
    cands = [slots.fmt_value(r["value"]) for rows_ in sm.scalars.values() for r in rows_ if 2 ** 150 <= (r["value"] or 0) < 2 ** 160]
    cr = await ex.creator(address)
    cands += [(cr or {}).get("creator")] + hardcoded_addresses(icode)[:6]
    pm = ((rpc.cfg or {}).get("v4") or {}).get("pool_manager")
    is_callback = (f["name"] or "").startswith(CALLBACK)
    if is_callback and pm:
        cands = [pm] + cands
    cands = [c for c in dict.fromkeys(c.lower() for c in cands if c) if c != ZERO]

    # the code
    out = {"chain": rpc.name, "address": address, "implementation": impl, "function": f["name"] or f["sel"], "selector": f["sel"],
           "verified": verified, "others": [h["name"] for h in hits[1:]]}
    L = []
    if verified:
        st = await static.code(ex, code_addr, f["sel"])
        if st and not st.get("error") and st.get("source"):
            out["source"] = st["source"]
            sm.add_layout(st.get("layout"))  # trace names from the compiler's own layout
            L.append(f"== CODE {st['sig']} — verified source {st['contract']} ({short(code_addr)}{', implementation of ' + short(address) if impl else ''})")
            for part in st["source"]:
                tag = {"function": "", "modifier": "modifier ", "internal": "internal "}[part["role"]]
                L.append(f"   // ---- {tag}{part['where']}.{part['name']}")
                L.extend("   " + l for l in part["code"].splitlines())
            # compiler layout = ground truth for slots; add live values
            lay = st.get("layout") or {}
            plain = {n: v for n, v in lay.items() if "slot" in v and not v["type"].startswith(("mapping", "string", "bytes ", "bytes)")) and "[" not in v["type"]}
            words = await rpc.batch([("eth_getStorageAt", [address, hex(v["slot"]), "latest"]) for v in plain.values()]) if plain else []
            L.append("== STORAGE it uses (compiler layout; live values read now)")
            for (n, v), w in zip(plain.items(), words):
                size = 20 if v["type"] == "address" else (1 if v["type"] in ("bool",) or v["type"].startswith("enum") else
                                                           (int(re.sub(r"\D", "", v["type"]) or 256) // 8 if v["type"].startswith(("uint", "int")) else 32))
                val = (int(w or "0x0", 16) >> (8 * v["offset"])) & ((1 << (8 * size)) - 1)
                rw = ("writes" if n in st["writes"] else "reads ")
                L.append(f"   {rw} {n} ({v['type']}) slot {v['slot']}{' @' + str(v['offset']) if v['offset'] else ''} = {slots.fmt_value(val, size) if v['type'] != 'bool' else ('true' if val else 'false')}")
            for n, v in lay.items():
                if v.get("constant"):
                    L.append(f"   uses   {n} (constant/immutable, in the code)")
                elif v["type"].startswith("mapping") or "[" in v["type"]:
                    L.append(f"   {'writes' if n in st['writes'] else 'reads '} {n} ({v['type']}) at slot {v['slot']}")
        else:
            verified = False
            L.append(f"== (verified, but Slither could not compile it: {(st or {}).get('error')}) — falling back to decompiled code")
    if not verified:
        key = f"decompsol:{decomp.VERSION}:" + decomp._key(icode).split(":")[-1]
        sol = cache.get(key)
        if not sol:
            sol = await decomp._run(icode, 40)
            if sol:
                cache.set(key, sol)
        chunk = next((("    " + sig + body) for sel, sig, body in decomp._split(sol or "") if sel == f["sel"]), None)
        if not chunk:
            L.append(f"== CODE {f['name'] or f['sel']}: decompiler could not recover this function")
        else:
            ren = getter_renames(sol)
            for old, new in ren.items():
                chunk = re.sub(rf"\b{old}\b", new, chunk)
            chunk = await decode_hex_names(chunk)
            lines = chunk.rstrip().rstrip("}").rstrip().splitlines()
            L.append(f"== CODE {f['name'] or f['sel']} — UNVERIFIED, Heimdall pseudocode ({short(code_addr)}{', implementation of ' + short(address) if impl else ''})")
            L.append("   (read from bytecode; may be imperfect — the TRACE below is ground truth)" +
                     ("; storage_map_* names come from the decompiler and can be WRONG: use the TRACE slots" if "storage_map_" in chunk else ""))
            L.extend("   " + l[4:] if l.startswith("    ") else "   " + l for l in lines[:MAX_LINES])
            if len(lines) > MAX_LINES:
                L.append(f"   … {len(lines) - MAX_LINES} more lines (dd code --json for all)")
            out["pseudocode"] = "\n".join(lines)
            used = {w for w in re.findall(r"\b[A-Za-z_]\w*\b", chunk)}
            touched = sorted(k for k, rows_ in sm.scalars.items() if any(r["name"] in used for r in rows_))
            if touched:
                L.append("== STORAGE it uses (proven on-chain via getters; live values)")
                for k in touched:
                    fields = sorted(sm.scalars[k], key=lambda r: r["offset"] or 0)
                    named = [r["name"] for r in fields if r["name"] in used]
                    L.append(f"   slot {k}: " + ", ".join(f"{r['name']} = {slots.fmt_value(r['value'], r['size'], r['name'])}" + (f" @byte {r['offset']}" if r["offset"] else "")
                                                       for r in fields) +
                             (f"   ← the code calls this slot '{named[0]}'; a cast like uint16(...) picks the field at that byte" if len(fields) > 1 else ""))

    # ground-truth trace
    if is_callback:
        L.append("== TRACE: v4/pool callback — it runs inside swaps/liquidity changes; see `dd check <chain> <token> --deep` (HOOK SIM)")
    else:
        caller, data, accepted = await choose_call(rpc, address, f, cands, frm, raw_args)
        if data is None:
            L.append(f"== TRACE: arguments ({f['args']}) can't be built automatically — pass --from and --args")
        else:
            tr, where = await trace_any(rpc, fork, caller, address, data)
            L.extend(await render_trace(tr, where, caller, data, f, address, sm, accepted, out))
    fork.close()
    L.append(f"   ({time.time()-t0:.1f}s)")
    return out, L


async def render_trace(tr, where, caller, data, f, address, sm, accepted, out):
    who = "anyone (a random wallet)" if caller.lower() == STRANGER.lower() else short(caller)
    argtxt = data[10:]
    head = f"== TRACE (simulated, nothing sent; as {who}" + (f", args 0x{argtxt[:24]}…" if argtxt else ", no args") + \
           (f", on a private fork" if where == "fork" else "") + ")"
    if tr is None:
        return [head, "   could not trace (no debug_* on this node and the fork did not start)"]
    calls, pre, diff = tr
    L = [head]
    addrs = [caller, address, slots.PROBE, STRANGER] + re.findall(r"0x[0-9a-fA-F]{40}", "0x" + argtxt) + \
            ["0x" + argtxt[i + 24:i + 64] for i in range(0, len(argtxt), 64) if argtxt[i:i + 24] == "0" * 24 and len(argtxt) >= i + 64]
    err = calls.get("error")
    reason = calls.get("revertReason")
    if err and not reason and (calls.get("output") or "0x")[2:10]:
        n = (await sigs.names_for(["0x" + calls["output"][2:10]])).get("0x" + calls["output"][2:10])
        reason = n or calls["output"][:10]
    L.append(f"   result: {'REVERTED — ' + str(reason or err) if err else 'ok'}" + (" (none of the tried callers/args were accepted; showing where it fails)" if accepted is False else ""))
    a = address.lower()
    pre_st = (pre.get(a) or {}).get("storage") or {}
    post_st = ((diff.get("post") or {}).get(a) or {}).get("storage") or {}
    old_st = ((diff.get("pre") or {}).get(a) or {}).get("storage") or {}
    # diffMode leaves a slot out of "post" when it is set to 0: a write is any key whose value differs pre vs post
    written = {k for k in set(post_st) | set(old_st) if int(post_st.get(k, "0x0"), 16) != int(old_st.get(k, "0x0"), 16)}
    post_st = {k: post_st.get(k, "0x0") for k in written}
    reads = [k for k in pre_st if k not in written and int(k, 16) not in slots.PROXY_SLOTS]
    if reads:
        L.append("   reads:  " + "; ".join(sm.value(int(k, 16), int(pre_st[k], 16), addrs) for k in reads[:8]) + (f" (+{len(reads)-8})" if len(reads) > 8 else ""))
    if written:
        L.append("   writes: " + "; ".join(sm.change(int(k, 16), int(old_st.get(k, "0x0"), 16), int(post_st[k], 16), addrs) for k in list(written)[:8]))
    others = [x for x in (diff.get("post") or {}) if x != a and ((diff["post"][x] or {}).get("storage"))]
    if others:
        L.append("   also writes storage of: " + ", ".join(short(x) for x in others[:4]))
    ext, logs = [], []

    def walk(fr, depth):
        if depth and fr.get("type") in ("CALL", "DELEGATECALL", "CALLCODE"):
            ext.append((fr.get("type"), fr.get("to"), (fr.get("input") or "0x")[:10], int(fr.get("value") or "0x0", 16)))
        for lg in fr.get("logs") or []:
            logs.append(lg)
        for c in fr.get("calls") or []:
            walk(c, depth + 1)
    walk(calls, 0)
    if ext:
        nm = await sigs.names_for(sorted({s for _, _, s, _ in ext if len(s) == 10}))
        L.append("   calls:  " + "; ".join(f"{'delegatecall ' if t == 'DELEGATECALL' else ''}{short(to)}.{(nm.get(s) or s).split('(')[0]}" + (f" +{v/1e18:.4g} native" if v else "") for t, to, s, v in ext[:6]))
    if logs:
        ev = await sigs.event_names(sorted({(lg.get("topics") or ["0x"])[0] for lg in logs}))
        L.append("   events: " + "; ".join(f"{(ev.get((lg.get('topics') or ['0x'])[0]) or (lg.get('topics') or ['?'])[0][:10]).split('(')[0]} @{short(lg.get('address'))}" for lg in logs[:6]))
    if not (reads or written or ext or logs) and not err:
        L.append("   touched no storage, made no calls, emitted no events")
    out["trace"] = {"caller": caller, "data": data, "where": where, "error": err, "reason": reason, "reads": reads, "writes": sorted(written), "calls": ext}
    return L


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    opts = {k: sys.argv[i + 1] for i, k in enumerate(sys.argv) if k in ("--from", "--args") and i + 1 < len(sys.argv)}
    a = [x for x in a if x not in opts.values()]
    if len(a) < 3:
        raise SystemExit(__doc__)
    run(main(a[0], a[1], a[2], opts.get("--from"), opts.get("--args"), "--json" in sys.argv))
