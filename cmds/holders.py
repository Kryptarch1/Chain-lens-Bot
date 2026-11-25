"""dd holders <chain> <token> [--top N] [--fast = quick pass only] [--json] — who holds the token, how they got it, how the
wallets are linked. Prints a QUICK PASS first (~10 s: holders, links, big buyers who left, dev), then the DEEP PROFILES
(top 20) and ALL HOLDERS (every wallet >= 0.01% of supply linked: the whole holder base, run in parallel)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import holders


async def main(chain, token, top, as_json):
    fast = "--fast" in sys.argv
    if as_json:
        d = await holders.collect(chain, token, top=top, profile=0 if fast else 20, full=not fast)
        print(json.dumps(d, indent=1, default=str))
        return
    d = await holders.collect(chain, token, top=top, profile=0)
    if d.get("error"):
        print(d["error"])
        return
    print("== QUICK PASS" + ("" if fast else " (deep profiles follow)"))
    print(holders.render(d), flush=True)
    if fast:
        return
    import asyncio
    from lib import wprofile
    t = time.time()

    async def full():
        try:
            return await asyncio.wait_for(holders.collect(chain, token, top=top, profile=0, full=True), 420)
        except Exception as e:
            return {"error": f"all-holder pass failed ({type(e).__name__}: {str(e)[:80]})"}
    full_task = asyncio.ensure_future(full())
    try:
        notes = await asyncio.wait_for(wprofile.profile_all(chain, d, n=20), 240)
    except Exception as e:
        notes = [f"wallet profiles failed ({type(e).__name__}: {str(e)[:80]})"]
    print(holders.render_deep(d, notes, time.time() - t), flush=True)
    print(holders.render_full(await full_task, d), flush=True)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit("usage: dd holders <chain> <token> [--top N] [--fast] [--json]")
    top = 25
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
        a = [x for x in a if x != str(top)]
    run(main(a[0], a[1], top, "--json" in sys.argv))
