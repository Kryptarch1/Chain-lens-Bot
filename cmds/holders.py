"""dd holders <chain> <token> [--top N] [--fast = quick pass only] [--json] [--budget S] — who holds the token, how they
got it, how the wallets are linked.

TIME BUDGET (2026-11-26, user: "new coins 1 min max, otherwise 2 min max"): everything starts at second 0 —
the QUICK PASS (~10 s), the ALL-HOLDER pass (every wallet >= 0.01%: funders + one node-log scan of all their token
transfers -> links, same-coin clusters, leaders/followers, the dev's other coins) and, as soon as the quick pass is in,
the DEEP PROFILES (top 20 + dev). Each section prints the moment it is ready. Budget: 60 s for coins < 1 day old,
120 s otherwise (DD_HOLDERS_BUDGET or --budget overrides). Safeguards: the quick pass is never cut; a later section
that misses the deadline prints what it has and lists what was not checked; then the REPORT (summary of findings +
every holder's profile + exited list) is written from whatever finished."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import holders


async def _budget(chain, token):
    if "--budget" in sys.argv:
        return float(sys.argv[sys.argv.index("--budget") + 1])
    if os.environ.get("DD_HOLDERS_BUDGET"):
        return float(os.environ["DD_HOLDERS_BUDGET"])
    # age = the TOKEN's creation block (~1 s), not DexScreener's pool time: a launchpad coin's pool is its graduation
    # (LENGO: pool 18 h old, token 34 h old)
    try:
        from lib.rpc import RPC
        from lib import explorer
        from cmds.check import phase_a
        rpc = RPC(chain)
        info = await asyncio.wait_for(phase_a(rpc, explorer.Explorer(chain), chain, token.lower()), 6)
        b = await rpc.call("eth_getBlockByNumber", [hex(info["created_block"]), False])
        if time.time() - int(b["timestamp"], 16) > 86400:
            return 120.0
    except Exception:
        pass
    return 60.0


async def main(chain, token, top, as_json):
    t0 = time.time()
    fast = "--fast" in sys.argv
    budget = await _budget(chain, token)
    deadline = t0 + budget
    if as_json:
        d = await holders.collect(chain, token, top=top, profile=0 if fast else 20, full=not fast, deadline=deadline)
        print(json.dumps(d, indent=1, default=str))
        return

    quick_done = asyncio.Event()

    async def full():
        try:
            return await asyncio.wait_for(holders.collect(chain, token, top=top, profile=0, full=True, deadline=deadline - 2, after=quick_done),
                                          max(5, deadline - time.time() + 3))
        except Exception as e:
            return {"error": f"all-holder pass not finished in the {budget:.0f} s budget ({type(e).__name__})"}
    full_task = None if fast else asyncio.ensure_future(full())
    d = await holders.collect(chain, token, top=top, profile=0)
    quick_done.set()
    if d.get("error"):
        print(d["error"])
        return
    print(f"== QUICK PASS ({time.time() - t0:.0f} s)" + ("" if fast else f" (deep profiles + all holders follow, budget {budget:.0f} s)"))
    print(holders.render(d), flush=True)
    if fast:
        return
    from lib import wprofile
    t = time.time()
    try:
        notes = await asyncio.wait_for(wprofile.profile_all(chain, d, n=20, deadline=deadline - 3), max(5, deadline - time.time() + 5))
    except Exception as e:
        notes = [f"wallet profiles not finished in the budget ({type(e).__name__}): profiles below are partial or missing"]
    print(holders.render_deep(d, notes, time.time() - t), flush=True)
    fd = await full_task
    print(holders.render_full(fd, d), flush=True)
    from lib import report
    path, text = report.write(d, fd, time.time() - t0, budget)
    print(f"\n== REPORT ({time.time() - t0:.0f} s of {budget:.0f} s budget) -> {path}\n")
    print(text, flush=True)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    for flag in ("--top", "--budget"):
        if flag in sys.argv:
            v = sys.argv[sys.argv.index(flag) + 1]
            a = [x for x in a if x != v]
    if len(a) < 2:
        raise SystemExit("usage: dd holders <chain> <token> [--top N] [--fast] [--json] [--budget S]")
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 25
    run(main(a[0], a[1], top, "--json" in sys.argv))
