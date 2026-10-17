"""One place that builds test arguments for any function (used by probes, quick sims, fork sims, LP and hook tests).

modes:
  probe  access check ("can X call this?"): addresses = the caller, numbers big (1e24)
  small  same, numbers = 1 (setters that only allow lowering / small values still pass the access check)
  worst  worst-case effect for holders: block/blacklist-style fns target the test buyer, "give me" fns the caller,
         fee setters 9900 (99% in bps), amounts 1e24
variants() yields worst first, then fee fallbacks (5000/1000/500), then small — so a capped setter still runs.
"""
from lib.evm import encode_call

BUYER = "0x00000000000000000000000000000000000Bb1ee"
FEE_WORDS = ("fee", "tax", "bps", "rate")
TARGET_WORDS = ("blacklist", "blocklist", "bot", "block", "freeze", "exclude", "ban", "deny", "sniper")
SELF_WORDS = ("controller", "minter", "owner", "admin", "operator")


def values(args, who, mode="probe", fn_name=""):
    """-> list of python values for the comma-separated ABI types in `args`, or None if a type isn't supported."""
    low = (fn_name or "").lower()
    out = []
    for t in [a.strip() for a in args.split(",") if a.strip()]:
        if t == "address":
            if mode == "worst":
                out.append(BUYER if any(w in low for w in TARGET_WORDS) else (who if any(w in low for w in SELF_WORDS) else BUYER))
            else:
                out.append(who)
        elif t.startswith(("uint", "int")):
            signed = t.startswith("int")
            bits = int(t[3 if signed else 4:] or 256)
            top = 2 ** (bits - 1) - 1 if signed else 2 ** bits - 1
            if mode == "small":
                out.append(1)
            elif mode == "worst" and any(w in low for w in FEE_WORDS):
                out.append(min(9900, top))
            else:
                out.append(min(10 ** 24, top))
        elif t == "bool":
            out.append((not low.startswith(("enable", "open", "start")) if "trading" in low else True) if mode == "worst" else True)
        elif t == "address[]":
            out.append([BUYER if mode == "worst" else who])
        elif t.endswith("[]"):
            out.append([1])
        elif t == "bytes32":
            out.append(b"\x00" * 32)
        elif t.startswith("bytes"):
            out.append(b"")
        elif t == "string":
            out.append("x")
        else:
            return None
    return out


def calldata(sel, args, who, mode="probe", fn_name=""):
    v = values(args, who, mode, fn_name)
    if v is None:
        return None
    return sel + (encode_call(f"x({args})", v)[10:] if args else "")


def variants(sel, fn_name, args, who):
    """Calldata to try in order until one runs: worst case, fee fallbacks, then small values."""
    seen = []
    first = calldata(sel, args, who, "worst", fn_name)
    if first:
        seen.append(first); yield first
    ts = [a.strip() for a in args.split(",") if a.strip()]
    if ts and any(w in (fn_name or "").lower() for w in FEE_WORDS) and all(t.startswith("uint") for t in ts):
        for v in (5000, 1000, 500):
            d = sel + encode_call(f"x({args})", [v] * len(ts))[10:]
            if d not in seen:
                seen.append(d); yield d
    s = calldata(sel, args, who, "small", fn_name)
    if s and s not in seen:
        yield s


def traced_sets(args, addrs, stranger):
    """Argument sets for traced probes of LP-removal functions (address/uint/bool/bytes32 only).
    -> [(encoded args, nonzero?)]: every known address with 1, then zeros (proves nothing), then a big number."""
    ts = [t.strip() for t in args.split(",") if t.strip()]
    if any(t not in ("address", "bool", "bytes32") and not t.startswith(("uint", "int")) for t in ts):
        return []
    if not ts:
        return [("", True)]
    addrs = list(dict.fromkeys(addrs or [stranger]))[:5]
    out = []
    for ad, num in [(a, 1) for a in addrs] + [(addrs[0], 0), (addrs[0], 2 ** 100)]:
        vals = []
        for t in ts:
            if t == "address":
                vals.append(ad)
            elif t.startswith(("uint", "int")):
                signed = t.startswith("int")
                bits = int(t[3 if signed else 4:] or 256)
                vals.append(min(num, 2 ** (bits - 1) - 1 if signed else 2 ** bits - 1))  # never exceed the type's range
            elif t == "bool":
                vals.append(num != 0)
            else:
                vals.append(b"\x00" * 32)
        enc = encode_call(f"x({','.join(ts)})", vals)[10:]
        if enc not in [e for e, _ in out]:
            out.append((enc, num != 0))
    return out
