"""ABI helpers: selector, encode a call from a signature string, decode outputs."""
import re
from eth_abi import encode, decode
from eth_utils import keccak


def selector(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def topic(sig):
    return "0x" + keccak(text=sig).hex()


def _types(sig):
    inner = sig[sig.index("(") + 1:sig.rindex(")")]
    return _split(inner)


def _split(s):
    """Split 'a,(b,c),d' on top-level commas."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [x.strip() for x in out if x.strip()]


def encode_call(sig, args=()):
    """encode_call('balanceOf(address)', [addr]) -> calldata hex"""
    types = _types(sig)
    return selector(sig) + (encode(types, list(args)).hex() if types else "")


def decode_out(types, data):
    """decode_out(['uint256','address'], '0x..') -> tuple"""
    if not data or data == "0x":
        return None
    if isinstance(types, str):
        types = _split(types)
    return decode(types, bytes.fromhex(data[2:]))


def decode1(t, data):
    r = decode_out([t], data)
    return r[0] if r else None


def addr(x):
    return "0x" + x[-40:].lower() if x else None


def to_int(h):
    return int(h, 16) if h and h != "0x" else 0


def fmt(n, dec=18, digits=4):
    return f"{n / 10 ** dec:,.{digits}f}"


def short(a):
    return a[:6] + "…" + a[-4:] if a else "-"


def strings_in(code_hex, minlen=6):
    b = bytes.fromhex(code_hex[2:]) if code_hex.startswith("0x") else bytes.fromhex(code_hex)
    return [m.decode() for m in re.findall(rb"[\x20-\x7e]{%d,}" % minlen, b)]


def hardcoded_addresses(code_hex):
    h = code_hex[2:].lower()
    seen = set()
    for m in re.findall(r"73([0-9a-f]{40})", h):  # PUSH20
        if m not in ("0" * 40, "f" * 40) and not m.startswith("0000000000000000"):
            seen.add("0x" + m)
    return sorted(seen)


def delegation_7702(code):
    """EIP-7702: a plain wallet that set code shows `0xef0100 || delegate` (23 bytes). -> delegate address or None.
    Such an account is still controlled by its private key (it sends its own txs), so it must be treated as a wallet."""
    c = (code or "").lower()
    return "0x" + c[8:48] if c.startswith("0xef0100") and len(c) == 48 else None
