"""Function selectors from bytecode + names from the local DB (openchain as fallback).
Also classifies selectors into standard / risky / unknown for the token check.
"""
import asyncio, json, re, subprocess, os
import aiohttp
from lib import cache

CAST = os.path.expanduser("~/.foundry/bin/cast")

# Standard, harmless selectors (ERC20/721/1155, permit, metadata, Ownable views)
STANDARD = {
    "0x06fdde03": "name()", "0x95d89b41": "symbol()", "0x313ce567": "decimals()", "0x18160ddd": "totalSupply()",
    "0x70a08231": "balanceOf(address)", "0xa9059cbb": "transfer(address,uint256)", "0xdd62ed3e": "allowance(address,address)",
    "0x095ea7b3": "approve(address,uint256)", "0x23b872dd": "transferFrom(address,address,uint256)",
    "0xd505accf": "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)", "0x7ecebe00": "nonces(address)",
    "0x3644e515": "DOMAIN_SEPARATOR()", "0x84b0196e": "eip712Domain()", "0x8da5cb5b": "owner()",
    "0x01ffc9a7": "supportsInterface(bytes4)", "0x081812fc": "getApproved(uint256)", "0x6352211e": "ownerOf(uint256)",
    "0x42842e0e": "safeTransferFrom(address,address,uint256)", "0xb88d4fde": "safeTransferFrom(address,address,uint256,bytes)",
    "0xa22cb465": "setApprovalForAll(address,bool)", "0xe985e9c5": "isApprovedForAll(address,address)",
    "0xc87b56dd": "tokenURI(uint256)", "0x4f6ccce7": "tokenByIndex(uint256)", "0x2f745c59": "tokenOfOwnerByIndex(address,uint256)",
    "0x32cb6b0c": "MAX_SUPPLY()", "0xe8a3d485": "contractURI()", "0x39509351": "increaseAllowance(address,uint256)",
    "0xa457c2d7": "decreaseAllowance(address,uint256)", "0x42966c68": "burn(uint256)", "0x79cc6790": "burnFrom(address,uint256)",
    "0x715018a6": "renounceOwnership()", "0xf2fde38b": "transferOwnership(address)", "0x79ba5097": "acceptOwnership()",
    "0xe30c3978": "pendingOwner()", "0x9dc29fac": "burn(address,uint256)",
}

# Words in a function name that mean "privileged power" (used for flagging, case-insensitive)
RISKY_WORDS = ["mint", "blacklist", "blocklist", "whitelist", "pause", "unpause", "freeze", "setfee", "settax", "fee", "tax",
               "setmax", "maxtx", "maxwallet", "limit", "rescue", "withdraw", "sweep", "recover", "claim", "upgrade", "setimpl",
               "setrouter", "setpair", "exclude", "include", "bot", "sniper", "cooldown", "trading", "enable", "disable",
               "setowner", "admin", "role", "grant", "revoke", "seize", "confiscate", "burnfrom", "destroy", "kill", "selfdestruct",
               "settreasury", "setmarketing", "setwallet", "swapback", "manualswap", "airdrop", "multisend", "update", "config", "set"]


def selectors_from_bytecode(code_hex):
    """Use `cast selectors` (evmole) -> list of (selector, args, mutability). Falls back to PUSH4 scan."""
    try:
        out = subprocess.run([CAST, "selectors", code_hex], capture_output=True, text=True, timeout=30).stdout
        rows = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if parts and re.fullmatch(r"0x[0-9a-f]{8}", parts[0]):
                rows.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
        if rows:
            return rows
    except Exception:
        pass
    sels = sorted(set("0x" + m for m in re.findall(r"63([0-9a-f]{8})14", code_hex.lower())))
    return [(s, "", "") for s in sels]


async def names_for(selectors):
    """selector -> function signature (or None). Local cache first, then one batched openchain call."""
    sels = [s.lower() for s in selectors]
    out = {s: STANDARD[s] for s in sels if s in STANDARD}
    missing = [s for s in sels if s not in out]
    local = cache.sig_get_many(missing)
    out.update(local)
    missing = [s for s in missing if s not in out]
    if missing:
        found = {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for i in range(0, len(missing), 80):
                    q = ",".join(missing[i:i + 80])
                    async with s.get(f"https://api.openchain.xyz/signature-database/v1/lookup?function={q}&filter=true") as r:
                        d = await r.json()
                    for sel, lst in (d.get("result", {}).get("function") or {}).items():
                        if lst:
                            found[sel.lower()] = lst[0]["name"]
        except Exception:
            pass
        cache.sig_set_many(found)
        cache.sig_set_many({s: "" for s in missing if s not in found}, src="miss")
        out.update(found)
    return {s: (out.get(s) or None) for s in sels}


async def event_names(topics):
    topics = [t.lower() for t in topics]
    out = cache.event_get_many(topics)
    missing = [t for t in topics if t not in out]
    if missing:
        found = {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for i in range(0, len(missing), 50):
                    q = ",".join(missing[i:i + 50])
                    async with s.get(f"https://api.openchain.xyz/signature-database/v1/lookup?event={q}&filter=true") as r:
                        d = await r.json()
                    for t, lst in (d.get("result", {}).get("event") or {}).items():
                        if lst:
                            found[t.lower()] = lst[0]["name"]
        except Exception:
            pass
        cache.event_set_many(found)
        out.update(found)
    return {t: out.get(t) for t in topics}


def classify(sel, name, mutability):
    """-> 'standard' | 'risky' | 'view' | 'unknown' | 'other'"""
    if sel in STANDARD:
        return "standard"
    if not name:
        return "unknown" if mutability != "view" and mutability != "pure" else "view"
    if mutability in ("view", "pure"):
        return "view"
    low = name.lower()
    if any(w in low for w in RISKY_WORDS):
        return "risky"
    return "other"
