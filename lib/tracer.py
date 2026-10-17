"""Execution evidence: what a call ACTUALLY did (not what its name says).

trace_call / trace_tx run debug_traceCall / debug_traceTransaction (callTracer with logs + prestateTracer
diff) on a Chainstack node or an anvil fork and reduce the output to:
  calls      every call frame: from → to, function, value, delegatecall/create/selfdestruct, error
  transfers  ERC20 Transfer events (token, from, to, amount) — where money moved
  native     value-carrying calls (ETH/BNB…)
  storage    changed slots per contract (from the state diff)
  ops        dangerous opcodes seen: delegatecall, selfdestruct, create
Everything degrades to None when the RPC has no debug namespace; callers must treat None as "not traced".
"""
import asyncio
from lib.rpc import RPCError
from lib.evm import addr as to_addr, to_int, short, fmt
from lib import sigs

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_MODIFY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"   # ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)
V3_DECREASE = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"  # DecreaseLiquidity(uint256,uint128,uint256,uint256)
V2_BURN = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"      # Burn(address,uint256,uint256,address)
_UNSUPPORTED = ("does not exist", "not available", "not supported", "method not found", "unsupported", "not allowed")
_CALL_CFG = {"tracer": "callTracer", "tracerConfig": {"withLog": True}}
_DIFF_CFG = {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}}


async def _trace(rpc, method, params_a, cfg, extra=None):
    cfg = dict(cfg)
    if extra:
        cfg.update(extra)
    try:
        return await asyncio.wait_for(rpc.call(method, params_a + [cfg]), 8)
    except RPCError as e:
        if any(w in str(e).lower() for w in _UNSUPPORTED):
            rpc.no_debug = True
        return None
    except Exception:
        return None


async def trace_call(rpc, frm, to, data, value=0, overrides=None, block="latest", diff=True):
    """Trace a hypothetical call at `block`. -> summary dict or None."""
    if getattr(rpc, "no_debug", False):
        return None
    tx = {"from": frm, "to": to, "data": data, "value": hex(value), "gas": hex(3_000_000)}
    extra = {"stateOverrides": overrides} if overrides else None
    cr, dr = await asyncio.gather(_trace(rpc, "debug_traceCall", [tx, block], _CALL_CFG, extra),
                                  _trace(rpc, "debug_traceCall", [tx, block], _DIFF_CFG, extra) if diff else asyncio.sleep(0))
    if cr is None:
        return None
    return summarize(cr, dr)


async def trace_tx(rpc, txhash, diff=True):
    """Trace a mined tx (fork or chain). -> summary dict or None."""
    if getattr(rpc, "no_debug", False) or not txhash:
        return None
    cr, dr = await asyncio.gather(_trace(rpc, "debug_traceTransaction", [txhash], _CALL_CFG),
                                  _trace(rpc, "debug_traceTransaction", [txhash], _DIFF_CFG) if diff else asyncio.sleep(0))
    if cr is None:
        return None
    return summarize(cr, dr)


def summarize(call_tree, diff=None):
    out = {"calls": [], "transfers": [], "native": [], "storage": {}, "balances": {}, "ops": set(), "error": call_tree.get("error"),
           "revert": call_tree.get("revertReason")}

    def walk(fr, depth):
        typ = (fr.get("type") or "CALL").upper()
        frm, to = (fr.get("from") or "").lower(), (fr.get("to") or "").lower()
        inp = fr.get("input") or "0x"
        val = to_int(fr.get("value") or "0x0")
        entry = {"depth": depth, "type": typ, "from": frm, "to": to, "sel": inp[:10] if len(inp) >= 10 else None,
                 "value": val, "error": fr.get("error"), "revert": fr.get("revertReason")}
        out["calls"].append(entry)
        if typ == "DELEGATECALL" and depth > 0:
            out.setdefault("delegates", []).append(to)
        elif typ in ("CREATE", "CREATE2"):
            out["ops"].add("create")
        elif typ == "SELFDESTRUCT":
            out["ops"].add("selfdestruct")
        if val and typ in ("CALL", "SELFDESTRUCT") and not fr.get("error"):
            out["native"].append({"from": frm, "to": to, "value": val})
        for lg in fr.get("logs") or []:
            tp = lg.get("topics") or []
            t0 = tp[0].lower() if tp else ""
            if t0 == V4_MODIFY and len(lg.get("data") or "") >= 2 + 64 * 3:
                d = int(lg["data"][2 + 128:2 + 192], 16)
                if d >> 255:
                    out.setdefault("liq_removed", []).append({"kind": "v4", "pool": tp[1], "by": to_addr(tp[2]), "delta": d - (1 << 256)})
            elif t0 in (V3_DECREASE, V2_BURN):
                out.setdefault("liq_removed", []).append({"kind": "v3" if t0 == V3_DECREASE else "v2", "at": (lg.get("address") or "").lower()})
            if len(tp) == 3 and tp[0].lower() == TRANSFER:
                out["transfers"].append({"token": (lg.get("address") or "").lower(), "from": to_addr(tp[1]), "to": to_addr(tp[2]),
                                         "amount": to_int(lg.get("data") or "0x0")})
        for c in fr.get("calls") or []:
            walk(c, depth + 1)

    walk(call_tree, 0)
    if diff:
        pre, post = diff.get("pre") or {}, diff.get("post") or {}
        for a in set(pre) | set(post):
            slots = set((post.get(a) or {}).get("storage") or {}) | set((pre.get(a) or {}).get("storage") or {})
            if slots:
                out["storage"][a.lower()] = sorted(slots)
            b0, b1 = (pre.get(a) or {}).get("balance"), (post.get(a) or {}).get("balance")
            if b0 is not None and b1 is not None and b0 != b1:
                out["balances"][a.lower()] = to_int(b1) - to_int(b0)
            if (post.get(a) or {}).get("code") and not (pre.get(a) or {}).get("code"):
                out["ops"].add("create")
    out["ops"] = sorted(out["ops"])
    return out


async def annotate(tr):
    """Resolve function names for the selectors in the call list (cached, one batched lookup)."""
    if not tr:
        return tr
    sels = {c["sel"] for c in tr["calls"] if c.get("sel")}
    names = await sigs.names_for(sorted(sels)) if sels else {}
    for c in tr["calls"]:
        c["fn"] = (names.get(c["sel"]) or c["sel"]) if c.get("sel") else ("(plain value)" if c["value"] else "(fallback)")
    return tr


def flows(tr, token, sender, receiver=None):
    """Net movement of `token` in this trace: who gained, besides `receiver`. -> (sent, received, others[(addr, amount)])
    `sent` = what `sender` gave up, `received` = what `receiver` got; others = everyone else that ended up with tokens
    (fee wallets, treasuries, the token contract itself)."""
    net = {}
    for t in tr["transfers"]:
        if t["token"] != token.lower():
            continue
        net[t["from"]] = net.get(t["from"], 0) - t["amount"]
        net[t["to"]] = net.get(t["to"], 0) + t["amount"]
    sent = -net.get(sender.lower(), 0)
    recv = net.get(receiver.lower(), 0) if receiver else 0
    cut = {}
    for t in tr["transfers"]:  # the cut = tokens taken out of THIS trade (from the payer), not old fees the contract dumps
        if t["token"] == token.lower() and t["from"] == sender.lower() and t["to"] not in (sender.lower(), (receiver or "").lower()):
            cut[t["to"]] = cut.get(t["to"], 0) + t["amount"]
    return sent, recv, sorted(cut.items(), key=lambda x: -x[1])


def danger_ops(tr, ignore=()):
    """delegatecall to anything but the token's own implementation, selfdestruct, create."""
    ign = {a.lower() for a in ignore if a}
    ops = list(tr.get("ops") or [])
    if any(d not in ign for d in tr.get("delegates") or []):
        ops.append("delegatecall")
    return sorted(set(ops))


def external_calls(tr, token, ignore=()):
    """Contracts the token (or its implementation) called, other than itself/its impl. -> [(to, fn)] unique."""
    ign = {a.lower() for a in ignore if a} | {token.lower()}
    seen, out = set(), []
    for c in tr["calls"]:
        if c["depth"] == 0 or c["type"] in ("STATICCALL", "DELEGATECALL") or c["from"] != token.lower() or c["to"] in ign:
            continue
        key = (c["to"], c.get("fn") or c.get("sel"))
        if key not in seen:
            seen.add(key); out.append(key)
    return out


def describe(tr, token, labels=None, dec=18, sym="tokens", ignore=()):
    """One-line human summary of a traced privileged call: storage written, external calls, money moved."""
    if not tr:
        return None
    labels = {k.lower(): v for k, v in (labels or {}).items()}
    lab = lambda a: labels.get(a, short(a))
    parts = []
    slots = tr["storage"].get(token.lower())
    if slots is not None:
        parts.append(f"writes {len(slots)} slot(s) on token")
    others = [a for a in tr["storage"] if a != token.lower() and tr["storage"][a]]
    if others:
        parts.append("writes storage on " + ", ".join(lab(a) for a in others[:3]))
    ext = external_calls(tr, token, ignore)
    if ext:
        parts.append("calls " + ", ".join(f"{lab(a)}.{fn}" for a, fn in ext[:3]))
    mv = [t for t in tr["transfers"]]
    for t in mv[:3]:
        parts.append(f"moves {fmt(t['amount'], dec, 0)} {sym if t['token'] == token.lower() else short(t['token'])} {lab(t['from'])}→{lab(t['to'])}")
    if len(mv) > 3:
        parts.append(f"+{len(mv) - 3} more transfers")
    for n in tr["native"][:2]:
        parts.append(f"sends {fmt(n['value'], 18, 4)} native {lab(n['from'])}→{lab(n['to'])}")
    for op in danger_ops(tr, ignore):
        parts.append(f"!{op}")
    if tr.get("error"):
        parts.append(f"reverted: {tr.get('revert') or tr['error']}")
    return "; ".join(parts) if parts else "no state change, no transfers"
