"""Ground-truth storage map: which raw slot (and byte offset) each getter reads, and its live value — proven by RPC,
never guessed from decompiler text.

For every no-argument getter: ask the node which storage keys the call reads (eth_createAccessList, else a prestate
trace), read those slots, and find where the returned value sits inside the slot word (packed variables).
For every 1-argument address getter (balanceOf, isExcluded, …): call it for a probe address, take the key it reads and
find the mapping's base slot by matching keccak(pad(address) . pad(base)) (Solidity) or keccak(base . address) (Vyper).
-> SlotMap: name a slot ("slot 5 @20 (1 byte) = _lock = true") or a mapping key ("balanceOf[0x12…]").
"""
import asyncio
from eth_utils import keccak
from lib.evm import encode_call, short

PROBE = "0x00000000000000000000000000000000000Bb1ee"
# slots every proxy reads on each call: not the contract's own variables
PROXY_SLOTS = {int(x, 16) for x in (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",  # EIP-1967 implementation
    "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",  # EIP-1967 admin
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50",  # EIP-1967 beacon
    "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3")}  # ZeppelinOS implementation
MAX_BASE = 400


async def _keys(rpc, storage_addr, call):
    """Storage keys of `storage_addr` that one eth_call reads. -> list[int] or None if the node can't tell."""
    try:
        al = await rpc.call("eth_createAccessList", [dict(call, **{"from": PROBE, "gasPrice": "0x0"}), "latest"])
        return [int(k, 16) for e in al.get("accessList", []) if e["address"].lower() == storage_addr.lower() for k in e.get("storageKeys", [])]
    except Exception:
        pass
    try:
        tr = await rpc.call("debug_traceCall", [call, "latest", {"tracer": "prestateTracer"}])
        return [int(k, 16) for k in ((tr or {}).get(storage_addr.lower()) or {}).get("storage", {})]
    except Exception:
        return None


def _place(word, value):
    """Byte offset and size at which `value` sits inside a 32-byte storage word, or (None, None)."""
    if value == word:
        return 0, 32
    for size in (1, 2, 4, 8, 16, 20):
        if value >> (8 * size):
            continue
        for off in range(0, 33 - size):
            if (word >> (8 * off)) & ((1 << (8 * size)) - 1) == value:
                return off, size
    return None, None


def base_of(key, addrs):
    """Mapping base slot behind a storage key: key = mapping[addr] (+ field index for a struct value, 0..7).
    -> (base, addr, field) or None"""
    for a in dict.fromkeys(x.lower() for x in addrs if x):
        pa = bytes.fromhex(a[2:].rjust(64, "0"))
        for b in range(MAX_BASE):
            bb = b.to_bytes(32, "big")
            for h in (int.from_bytes(keccak(pa + bb), "big"), int.from_bytes(keccak(bb + pa), "big")):
                if 0 <= key - h < 8:
                    return b, a, key - h
    return None


class SlotMap:
    def __init__(self):
        self.scalars = {}   # slot -> [{"name", "offset", "size", "value"}]
        self.maps = {}      # base slot -> name
        self.words = {}     # slot -> raw word (int)
        self.computed = {}  # getter -> {"slots": [...], "value"}: computed from several slots

    def name_slot(self, slot):
        rows = self.scalars.get(slot) or []
        return ", ".join(r["name"] for r in rows) if rows else None

    def add_layout(self, layout):
        """Compiler storage layout (verified contracts): exact names, slots and offsets for every variable."""
        for n, v in (layout or {}).items():
            if "slot" not in v:
                continue
            t = v["type"]
            if t.startswith("mapping"):
                self.maps[v["slot"]] = n
                continue
            if "[" in t or t.startswith(("string", "bytes ")) or t == "bytes":
                continue
            size = 20 if t == "address" else 1 if t == "bool" or t.startswith("enum") else \
                int(_re.sub(r"\D", "", t) or 256) // 8 if t.startswith(("uint", "int")) else 32
            rows = self.scalars.setdefault(v["slot"], [])
            if not any(r["name"] == n for r in rows):
                rows.append({"name": n, "offset": v["offset"], "size": size, "value": None, "bool": t == "bool"})

    def fields(self, key, word):
        """Packed fields of a known slot, decoded from a raw word: [(name, value)]"""
        out = []
        for r in sorted(self.scalars.get(key) or [], key=lambda r: r["offset"] or 0):
            if r["offset"] is None:
                continue
            size = r["size"] or 32
            out.append((r["name"] if not r.get("bool") else r["name"] + "?", (word >> (8 * r["offset"])) & ((1 << (8 * size)) - 1), size))
        return out

    def change(self, key, old, new, addrs=()):
        """'feeBps: 150 → 1' for the packed fields that changed, else 'name: old → new' on the whole word."""
        rows = self.scalars.get(key) or []
        if len(rows) > 1:
            parts = [f"{n.rstrip('?')}: {fmt_value(a, sz, n)} → {fmt_value(b, sz, n)}" for (n, a, sz), (_, b, _) in zip(self.fields(key, old), self.fields(key, new)) if a != b]
            if parts:
                return "; ".join(dict.fromkeys(parts))
        nm = self.name_slot(key)
        return f"{self.describe(key, addrs)}: {fmt_value(old, None, nm)} → {fmt_value(new, None, nm)}"

    def value(self, key, word, addrs=()):
        """'name = value' for a read; packed slots list each field."""
        rows = self.scalars.get(key) or []
        if len(rows) > 1:
            return ", ".join(dict.fromkeys(f"{n.rstrip('?')} = {fmt_value(v, sz, n)}" for n, v, sz in self.fields(key, word)))
        return f"{self.describe(key, addrs)} = {fmt_value(word, None, self.name_slot(key))}"

    def describe(self, key, addrs=()):
        """Human name for a raw storage key: getter name, or mapping[address], else the raw slot."""
        if key in PROXY_SLOTS:
            return "proxy implementation/admin slot"
        if key in self.scalars:
            return self.name_slot(key)
        if key < MAX_BASE:
            return f"slot {key} (no getter)"
        hit = base_of(key, list(addrs) + [PROBE])
        if hit:
            b, a, fld = hit
            label = TEST_WALLETS.get(int(a, 16))
            return f"{self.maps.get(b, f'mapping@slot{b}')}[{label or short(a)}]" + (f".field{fld}" if fld else "")
        h = hex(key)
        return f"slot {h[:10]}…{h[-4:]} (hashed: unnamed mapping/array)"


async def resolve(rpc, storage_addr, fns, extra_addrs=()):
    """fns: [{sel, name, args, mut}] -> SlotMap with every getter we could prove. Getters are called on the real
    contract (storage_addr; for proxies the proxy holds the storage)."""
    sm = SlotMap()
    getters0 = [f for f in fns if f.get("args") == "" and f.get("mut") in ("view", "pure", "") and f.get("name")]
    getters1 = [f for f in fns if f.get("args") == "address" and f.get("mut") in ("view", "pure", "") and f.get("name")]

    async def g0(f):
        call = {"to": storage_addr, "data": f["sel"]}
        ok, ret = await rpc.try_call(storage_addr, f["sel"])
        if not ok or not ret or len(ret) != 66:  # only single-word values (strings/arrays are not switches)
            return None
        keys = await _keys(rpc, storage_addr, call)
        return f, int(ret, 16), [k for k in (keys or []) if k not in PROXY_SLOTS]

    async def g1(f):
        call = {"to": storage_addr, "data": f["sel"] + encode_call("x(address)", [PROBE])[10:]}
        keys = await _keys(rpc, storage_addr, call)
        return f, [k for k in (keys or []) if k not in PROXY_SLOTS]

    r0, r1 = await asyncio.gather(asyncio.gather(*[g0(f) for f in getters0[:40]]), asyncio.gather(*[g1(f) for f in getters1[:20]]))
    want = sorted({k for x in r0 if x for k in x[2] if k < 2 ** 64})
    words = await rpc.batch([("eth_getStorageAt", [storage_addr, hex(k), "latest"]) for k in want]) if want else []
    sm.words = {k: int(w or "0x0", 16) for k, w in zip(want, words)}
    for x in r0:
        if not x:
            continue
        f, val, keys = x
        nm = f["name"].split("(")[0]
        small = [k for k in keys if k in sm.words]
        if len(small) == 1:  # one plain slot: we can say exactly where the value lives
            k = small[0]
            off, size = _place(sm.words[k], val)
            sm.scalars.setdefault(k, []).append({"name": nm, "offset": off, "size": size, "value": val})
        elif len(small) > 1:  # value computed from several slots (e.g. a fee that decays with time)
            sm.computed[nm] = {"slots": small, "value": val}
    for x in r1:
        f, keys = x
        for k in keys:
            hit = base_of(k, [PROBE])
            if hit and hit[2] == 0:
                sm.maps.setdefault(hit[0], f["name"].split("(")[0])
                break
    return sm


import re as _re
BOOLISH = _re.compile(r"^(is|has|can|should|was)[A-Z_]|(enabled|open|opened|paused|initialized|blocked|locked|active|launched|started|lifted|trading|frozen|renounced)$", _re.I)
ADDRISH = _re.compile(r"(owner|admin|pair|pool|router|oracle|controller|treasury|wallet|recipient|hook|manager|factory|system|operator|minter|signer|feeto|marketing|dev|receiver|vault|locker|beneficiary)$", _re.I)
TEST_WALLETS = {int(PROBE, 16): "test-wallet", int("0x000000000000000000000000000000000000bEEF", 16): "random-wallet"}


def fmt_value(v, size=None, name=None):
    """Addresses are random 160-bit numbers (almost always >= 2^150); token amounts are far smaller.
    Booleans only when the variable's name says so (a 1-byte fee of 1 is a number)."""
    if v is None:
        return "?"
    if name and name.endswith("?"):
        return "true" if v else "false"
    if v in TEST_WALLETS:
        return f"{TEST_WALLETS[v]} ({hex(v)})"
    if 2 ** 150 <= v < 2 ** 160 or (name and 0 < v < 2 ** 160 and ADDRISH.search(name.rstrip("?"))):
        return "0x" + hex(v)[2:].rjust(40, "0")
    if name and v in (0, 1) and BOOLISH.search(name):
        return "true" if v else "false"
    return str(v)
