"""Test the owner switches Slither found: set each state variable that transfers read directly in storage
(eth_simulateV1 state override, nothing is broadcast) and see whether a fresh holder can still sell.

Why: guessing a setter's arguments often fails (capped values, odd parameters); flipping the variable itself proves
what the switch DOES. Whether the owner can actually set that value is shown next to it (setter + who can call it).
  bool                      flipped
  mapping(address=>bool)    the holder's entry set true, then false (blacklist / whitelist / exempt)
  mapping(address=>uintN)   the holder's entry set to 0 and to a large value (per-wallet limits, cooldowns)
  uintN                     0 and 10000 (limits switched off / fee at 100% in bps)
  address                   a contract that always reverts (oracle / checker / router swapped)
"""
import asyncio
from eth_utils import keccak
from lib.evm import encode_call, short
from lib import quicksim

BUYER = quicksim.BUYER
REVERTER = "0x000000000000000000000000000000000000fE11"  # gets code 0xfe (INVALID) via override: every call to it reverts
import re as _re
# re-entry / in-progress guards: set and reset inside one call, so nobody can leave them switched on — not owner switches
GUARD = _re.compile(r"(^_?(updating|entered|locked|lock|inswap|swapping|inclaim|bypass|status|reentr\w*|lifecycle\w*|processing|busy|mutex)$)", _re.I)
TYPES_OK = ("bool", "address", "uint", "enum:", "mapping(address => bool)", "mapping(address => uint")


def _slot_of(entry, key=None):
    base = entry["slot"]
    if key is None:
        return base
    return int.from_bytes(keccak(bytes.fromhex(key[2:].rjust(64, "0")) + base.to_bytes(32, "big")), "big")


def _put(word, offset, size, value):
    """Write `value` (int) into a 32-byte storage word at byte `offset` (from the right), `size` bytes wide."""
    mask = ((1 << (8 * size)) - 1) << (8 * offset)
    return (word & ~mask) | ((value << (8 * offset)) & mask)


def _size(t):
    if t == "bool":
        return 1
    if t == "address":
        return 20
    if t.startswith("uint"):
        return int(t[4:] or 256) // 8
    return 32


def candidates(name, entry):
    """-> [(label, slot, offset, size, value)] storage edits to try for one switch variable."""
    t = entry["type"]
    out = []
    if t == "bool":
        out.append(("flipped", _slot_of(entry), entry["offset"], 1, None))  # None = flip current value
    elif t.startswith("uint") and "[" not in t:
        sz = _size(t)
        out += [("set to 0", _slot_of(entry), entry["offset"], sz, 0), ("set to 10000", _slot_of(entry), entry["offset"], sz, min(10000, (1 << (8 * sz)) - 1))]
    elif t.startswith("enum:"):
        out += [(f"set to state {i}", _slot_of(entry), entry["offset"], 1, i) for i in range(min(int(t[5:]), 6))]
    elif t == "address":
        out.append(("pointed at a reverting contract", _slot_of(entry), entry["offset"], 20, int(REVERTER, 16)))
    elif t == "mapping(address => bool)":
        out += [("holder's entry = true", _slot_of(entry, BUYER), 0, 1, 1), ("holder's entry = false", _slot_of(entry, BUYER), 0, 1, 0)]
    elif t.startswith("mapping(address => uint"):
        sz = _size(t.split("=>")[1].strip(" )"))
        out += [("holder's entry = 0", _slot_of(entry, BUYER), 0, sz, 0), ("holder's entry = large", _slot_of(entry, BUYER), 0, sz, 10 ** 24 if sz == 32 else (1 << (8 * sz)) - 1)]
    return out


async def run(rpc, token, pool, hp, static_res, probes=(), limit=10):
    """-> list of {var, setters, callers, edit, buy_ok?, sell_ok, why} or None if not testable."""
    from lib import static
    if not (hp and hp.get("buy_ok") and hp.get("got") and static_res and static_res.get("layout")):
        return None
    sw = static.switches(static_res)
    lay = static_res["layout"]
    callers = {}
    for p in probes:
        nm = (p.get("fn") or "").split("(")[0]
        if p.get("callers"):
            callers.setdefault(nm, p["callers"][0])
    jobs, skipped = [], []
    for var, setters in list(sw.items())[:limit]:
        if GUARD.match(var):
            continue
        e = lay.get(var)
        if not e or not e["type"].startswith(TYPES_OK) or "[" in e["type"]:
            skipped.append({"var": var, "skipped": "slot unknown (name used twice / packed struct)" if not e else f"type {e['type']} not testable", "setters": setters})
            continue
        for label, slot, off, size, val in candidates(var, e):
            jobs.append((var, setters, label, slot, off, size, val))
    if not jobs:
        return skipped
    cur = await rpc.batch([("eth_getStorageAt", [token, hex(slot), "latest"]) for _, _, _, slot, _, _, _ in jobs])

    async def one(job, word_hex):
        var, setters, label, slot, off, size, val = job
        word = int(word_hex or "0x0", 16)
        if val is None:  # flip a bool
            val = 0 if (word >> (8 * off)) & 0xFF else 1
        elif label.startswith("set to state") and ((word >> (8 * off)) & 0xFF) == val:
            return None  # that's the current state
        new = _put(word, off, size, val)
        ov = {token: {"stateDiff": {"0x" + slot.to_bytes(32, "big").hex(): "0x" + new.to_bytes(32, "big").hex()}}}
        if label.startswith("pointed"):
            ov[REVERTER] = {"code": "0xfe"}
        blocks = [{"calls": [{"from": pool, "to": token, "data": encode_call("transfer(address,uint256)", [BUYER, hp["amount"]])}]},
                  {"stateOverrides": ov, "calls": [{"from": BUYER, "to": token, "data": encode_call("transfer(address,uint256)", [pool, hp["got"]])}]}]
        r = await quicksim.simulate_blocks(rpc, blocks)
        if r is None:
            return None
        sell = r[1][0]
        who = next((callers[s] for s in setters if s in callers), None)
        return {"var": var, "type": lay[var]["type"], "setters": setters, "caller": who, "edit": label,
                "sell_ok": sell["ok"], "why": sell.get("err")}

    res = await asyncio.gather(*[one(j, w) for j, w in zip(jobs, cur)], return_exceptions=True)
    return [r for r in res if isinstance(r, dict)] + skipped


def findings(res):
    out = []
    for r in res or []:
        if r.get("skipped") or r["sell_ok"]:
            continue
        via = f"{', '.join(s + '()' for s in r['setters'][:2])}"
        if r.get("caller"):
            out.append(("bad", f"SELL BLOCKER (switch): `{r['var']}` {r['edit']} → sells revert; set by {via}, callable by {short(r['caller'])}"))
        else:
            out.append(("warn", f"switch `{r['var']}` {r['edit']} → sells revert; set by {via} (no current caller found)"))
    # one line per variable is enough
    seen, uniq = set(), []
    for lvl, m in out:
        k = m.split("`")[1] if "`" in m else m
        if k not in seen:
            seen.add(k); uniq.append((lvl, m))
    return uniq


def render(res):
    if res is None:
        return []
    if not res:
        return []
    L = ["== OWNER SWITCHES (each variable transfers read, set directly in storage, then a holder sells — simulated)"]
    byvar = {}
    for r in res:
        if r.get("skipped"):
            L.append(f"   ? {r['var']} ← {', '.join(r['setters'][:2])}(): not tested — {r['skipped']}")
            continue
        byvar.setdefault(r["var"], []).append(r)
    for var, rows in byvar.items():
        parts = [f"{r['edit']}: sell {'OK' if r['sell_ok'] else 'REVERTS' + (' (' + str(r['why'])[:40] + ')' if r.get('why') else '')}" for r in rows]
        who = rows[0].get("caller")
        L.append(f"   {'✗' if any(not r['sell_ok'] for r in rows) and who else ('!' if any(not r['sell_ok'] for r in rows) else '·')} {var} ({rows[0]['type']}) ← {', '.join(rows[0]['setters'][:2])}()" +
                 (f" by {short(who)}" if who else "") + ": " + " · ".join(parts))
    return L
