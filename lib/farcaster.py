"""Farcaster identity of a wallet: which Farcaster account VERIFIED this address (the user signed a message with the
wallet, so it's proof of control, not a guess) or uses it as its custody address.
Source: Neynar bulk-by-address (public docs key unless NEYNAR_API_KEY is set); web3.bio as fallback for one address.
Cached 1 day on a hit, 6 h on a miss; time-boxed so it never slows a check."""
import asyncio
import os

import aiohttp

NEYNAR = "https://api.neynar.com/v2/farcaster/user/bulk-by-address"
WEB3BIO = "https://api.web3.bio/profile/farcaster/"


async def _get(url, params=None, headers=None, timeout=3):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url, params=params, headers=headers or {}) as r:
                if r.status == 404 and "neynar" in url:
                    return {}  # Neynar's "No users found"
                return await r.json(content_type=None) if r.status == 200 else None
    except Exception:
        return None


def _user(u, addr):
    va = (u.get("verified_addresses") or {}).get("eth_addresses") or []
    x = [a.get("username") for a in (u.get("verified_accounts") or []) if a.get("platform") == "x" and a.get("username")]
    return {"fid": u.get("fid"), "username": u.get("username"), "name": u.get("display_name"),
            "bio": ((u.get("profile") or {}).get("bio") or {}).get("text") or "",
            "followers": u.get("follower_count"), "score": u.get("score"), "x": x[0] if x else None,
            "how": "custody" if (u.get("custody_address") or "").lower() == addr else
                   "verified" if addr in [a.lower() for a in va] else "linked",
            "other_wallets": [a for a in va if a.lower() != addr]}


async def _web3bio(addr):
    j = await _get(WEB3BIO + addr, timeout=3)
    if not isinstance(j, dict) or not j.get("identity"):
        return None
    return {"fid": None, "username": j["identity"], "name": j.get("displayName"), "bio": j.get("description") or "",
            "followers": None, "score": None, "x": None, "how": "verified", "other_wallets": []}


async def lookup(addrs):
    """-> {address(lower): user dict or None}. Missing key = lookup failed (unknown, not 'no account')."""
    from lib import cache
    addrs = list(dict.fromkeys(a.lower() for a in addrs if a))
    out, todo = {}, []
    for a in addrs:
        c = cache.get(f"fc:{a}")
        if c is not None:
            out[a] = c or None
        else:
            todo.append(a)
    if not todo:
        return out
    key = os.environ.get("NEYNAR_API_KEY", "NEYNAR_API_DOCS")
    j = await _get(NEYNAR, params={"addresses": ",".join(todo)}, headers={"x-api-key": key})
    if isinstance(j, dict) and "message" not in j:
        for a in todo:
            us = j.get(a) or []
            out[a] = _user(us[0], a) if us else None
    else:  # Neynar down / rate-limited -> web3.bio one by one
        res = await asyncio.gather(*[_web3bio(a) for a in todo[:5]])
        for a, u in zip(todo[:5], res):
            if u:
                out[a] = u
    for a in todo:
        if a in out:
            cache.set(f"fc:{a}", out[a] or {}, ttl=86400 if out[a] else 6 * 3600)
    return out


def describe(u):
    """'@benleventhal (Ben, fid 11707, 438 followers) "Founder and CEO of Blackbird.xyz"'"""
    bits = [b for b in (u.get("name"), f"fid {u['fid']}" if u.get("fid") else None,
                        f"{u['followers']:,} followers" if u.get("followers") is not None else None,
                        f"X @{u['x']}" if u.get("x") else None) if b]
    bio = (u.get("bio") or "").replace("\n", " ").strip()
    return f"@{u['username']} ({', '.join(bits)})" + (f' "{bio[:90]}"' if bio else "")


def render(found):
    """found: [(role, address, user)] -> WHO lines."""
    L = []
    for role, addr, u in found:
        how = {"custody": "owns the Farcaster account", "verified": "verified (signed) by"}.get(u["how"], "linked to")
        L.append(f"   farcaster: {role} {addr} {how} {describe(u)}")
        if u.get("other_wallets"):
            L.append(f"     same person's other verified wallets: {', '.join(u['other_wallets'][:4])}")
    return L
