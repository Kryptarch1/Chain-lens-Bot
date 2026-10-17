"""dd trace <chain> <wallet> — follow a wallet's funding back through bridges to its origin."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import trace


async def main(chain, wallet):
    t0 = time.time()
    hops = await trace.trace(chain, wallet)
    print(f"== TRACE {wallet} on {chain}")
    print("\n".join(trace.render(hops)))
    print(f"   summary: {trace.one_line(hops)}")
    print(f"   ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit("usage: dd trace <chain> <wallet>")
    run(main(a[0], a[1]))
