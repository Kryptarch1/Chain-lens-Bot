"""dd x find <name> [context words…] [--days 90] [--max 50]

Project discovery on X, the way the user does it by hand:
  1. search recent posts for the name (+ context, e.g. "chimera ink") — retweets excluded
  2. collect every account that posts about it or is @mentioned with it; rank the ones whose handle / display name
     matches the name (the likely official account) and look them up: created, followers, bio, website
  3. flag RECENTLY CREATED accounts (default: under 90 days) among authors and mentioned accounts
  4. list the links being shared (sites, docs, dApps, other X accounts)
Read-only (app bearer token in keys/x). Costs: $0.005 per post, $0.01 per account looked up; results cached 6 h.
X does not allow searching ACCOUNTS by keyword with an app token, so accounts are discovered through posts.
"""
import asyncio, json, os, re, sys, time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aiohttp
from lib import cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.x.com/2"
POST_COST, USER_COST = 0.005, 0.01
UF = "created_at,public_metrics,description,url,entities,verified"


def token():
    p = os.path.join(ROOT, "keys", "x")
    if not os.path.exists(p):
        raise SystemExit("no X token: put the bearer token in keys/x")
    return open(p).read().strip()


async def get(s, path, params):
    key = "x:" + path + ":" + json.dumps(params, sort_keys=True)
    c = cache.get(key)
    if c:
        return (json.loads(c) if isinstance(c, str) else c), True
    async with s.get(API + path, params=params) as r:
        d = await r.json(content_type=None)
    if r.status == 200:
        cache.set(key, json.dumps(d), ttl=6 * 3600)
    elif r.status == 429:
        raise SystemExit("X rate limit hit — try again in a few minutes")
    elif r.status in (401, 403):
        raise SystemExit(f"X refused the request ({r.status}): {d.get('title') or d.get('detail')}")
    return d, False


def age_days(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    except Exception:
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def matches(name, text):
    """Handle/display name contains the project name, or its first letters (chimera -> 'chimerink', 'chimeras')."""
    n, t = norm(name), norm(text)
    return bool(n) and (n in t or (len(n) >= 5 and n[:max(5, len(n) - 1)] in t))


def site(u):
    try:
        h = urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


async def find(name, context, days=90, maxp=50):
    t0 = time.time()
    q = " ".join([name] + context) + " -is:retweet"
    cost = 0.0
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token()}"}, timeout=aiohttp.ClientTimeout(total=30)) as s:
        d, cached = await get(s, "/tweets/search/recent", {"query": q, "max_results": max(10, min(100, maxp)),
                                                          "tweet.fields": "created_at,entities,author_id,public_metrics",
                                                          "expansions": "author_id", "user.fields": UF})
        posts = d.get("data") or []
        users = {u["id"]: u for u in (d.get("includes") or {}).get("users", [])}
        if not cached:
            cost += len(posts) * POST_COST + len(users) * USER_COST
        authors = Counter(p["author_id"] for p in posts)
        mentions, links = Counter(), Counter()
        for p in posts:
            ent = p.get("entities") or {}
            for m in ent.get("mentions") or []:
                mentions[m["username"].lower()] += 1
            for u in ent.get("urls") or []:
                x = u.get("expanded_url") or u.get("url") or ""
                if not x or ("x.com/" in x or "twitter.com/" in x) and "/status/" in x:
                    continue  # skip links to other posts / photos
                links[x.split("?")[0].rstrip("/")] += 1
        known = {u["username"].lower(): u for u in users.values()}
        n = norm(name)
        # candidate project accounts: handle or display name contains the name; plus the most-mentioned accounts
        cands = [h for h in mentions if matches(name, h)] + [u["username"].lower() for u in users.values()
                                                               if matches(name, u["username"]) or matches(name, u.get("name"))]
        cands += [h for h, _ in mentions.most_common(8)]
        cands = list(dict.fromkeys(cands))[:15]
        need = [h for h in cands if h not in known]
        if need:
            d2, cached2 = await get(s, "/users/by", {"usernames": ",".join(need[:100]), "user.fields": UF})
            for u in d2.get("data") or []:
                known[u["username"].lower()] = u
            if not cached2:
                cost += len(d2.get("data") or []) * USER_COST
    return {"query": q, "posts": posts, "users": users, "authors": authors, "mentions": mentions, "links": links,
            "known": known, "cands": cands, "name": name, "days": days, "cost": cost, "elapsed": time.time() - t0}


def acct_line(u, mark=""):
    m = u.get("public_metrics") or {}
    a = age_days(u.get("created_at", ""))
    web = ""
    for x in ((u.get("entities") or {}).get("url") or {}).get("urls", []):
        web = x.get("expanded_url") or ""
    bio = (u.get("description") or "").replace("\n", " ")
    return (f"   {mark}@{u['username']} — {u.get('name')} · created {u.get('created_at', '')[:10]} ({a}d) · "
            f"{m.get('followers_count', 0):,} followers · {m.get('tweet_count', 0):,} posts" + (f" · {web}" if web else "") +
            (f"\n      {bio[:140]}" if bio else ""))


def render(r):
    L = [f"== X: \"{r['query'].replace(' -is:retweet', '')}\" — {len(r['posts'])} recent posts (last 7 days)"]
    if not r["posts"]:
        return L + ["   nothing found in the last 7 days"]
    n = norm(r["name"])
    match = [r["known"][h] for h in r["cands"] if h in r["known"] and (matches(r["name"], h) or matches(r["name"], r["known"][h].get("name")))]
    if match:
        L.append("== LIKELY PROJECT ACCOUNTS (handle or name matches)")
        for u in sorted(match, key=lambda u: -(r["mentions"].get(u["username"].lower(), 0) * 10 + (u.get("public_metrics") or {}).get("followers_count", 0) / 1000))[:6]:
            L.append(acct_line(u, "★ " if r["mentions"].get(u["username"].lower()) else ""))
            if r["mentions"].get(u["username"].lower()):
                L.append(f"      tagged in {r['mentions'][u['username'].lower()]} of these posts")
    top = [(h, c) for h, c in r["mentions"].most_common(8)]
    if top:
        L.append("== MOST TAGGED ACCOUNTS")
        L.append("   " + " · ".join(f"@{h} ×{c}" for h, c in top))
    fresh = [u for u in list(r["users"].values()) + [r["known"][h] for h in r["cands"] if h in r["known"]]
             if (age_days(u.get("created_at", "")) or 9999) <= r["days"]]
    fresh = list({u["username"]: u for u in fresh}.values())
    if fresh:
        L.append(f"== RECENTLY CREATED ACCOUNTS (under {r['days']} days) posting about it or tagged")
        for u in sorted(fresh, key=lambda u: age_days(u.get("created_at", "")) or 0)[:10]:
            L.append(acct_line(u, "! "))
    if r["links"]:
        L.append("== LINKS BEING SHARED")
        by_site = Counter()
        for u, c in r["links"].items():
            by_site[site(u)] += c
        for u, c in r["links"].most_common(8):
            L.append(f"   {c}× {u}")
    L.append("== RECENT POSTS")
    for p in sorted(r["posts"], key=lambda p: -(p.get("public_metrics") or {}).get("like_count", 0))[:6]:
        u = r["users"].get(p["author_id"], {})
        pm = p.get("public_metrics") or {}
        L.append(f"   {p['created_at'][:16].replace('T', ' ')} @{u.get('username', '?')} ({pm.get('like_count', 0)}♥): {p['text'][:150].replace(chr(10), ' ')}")
        L.append(f"      https://x.com/{u.get('username', 'i')}/status/{p['id']}")
    L.append(f"   (cost ≈ ${r['cost']:.2f}{' — cached, free' if r['cost'] == 0 else ''} · {r['elapsed']:.1f}s)")
    return L


async def main(argv):
    if len(argv) < 2 or argv[0] != "find":
        raise SystemExit(__doc__)
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 90
    maxp = int(argv[argv.index("--max") + 1]) if "--max" in argv else 50
    words = [a for i, a in enumerate(argv[1:], 1) if not a.startswith("--") and argv[i - 1] not in ("--days", "--max")]
    r = await find(words[0], words[1:], days, maxp)
    print("\n".join(render(r)))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
