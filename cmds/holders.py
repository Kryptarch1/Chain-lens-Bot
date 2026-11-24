"""dd holders <chain> <token> [--top N] [--fast = no wallet profiles] [--json] — who holds the token, how they got it, how the wallets are linked."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rpc import run
from lib import holders


async def main(chain, token, top, as_json):
    d = await holders.collect(chain, token, top=top, profile=0 if '--fast' in sys.argv else 20)
    print(json.dumps(d, indent=1, default=str) if as_json else holders.render(d))


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit("usage: dd holders <chain> <token> [--top N] [--fast] [--json]")
    top = 25
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
        a = [x for x in a if x != str(top)]
    run(main(a[0], a[1], top, "--json" in sys.argv))
