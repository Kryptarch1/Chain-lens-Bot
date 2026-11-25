"""dd tracked list [text] | add <address> <label…> | rm <address> — the private tracked-wallet list (data/tracked.json)."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import tracked

a = sys.argv[1:]
cmd = a[0] if a else "list"
if cmd == "list":
    q = " ".join(a[1:]).lower()
    w = tracked.load()
    rows = [(l, x) for x, l in w.items() if not q or q in l.lower() or q in x]
    for l, x in sorted(rows, key=lambda r: r[0].lower()):
        print(f"{x}  {l}")
    print(f"({len(rows)} of {len(w)} tracked wallets)")
elif cmd == "add" and len(a) >= 3 and re.fullmatch(r"0x[0-9a-fA-F]{40}", a[1]):
    tracked.add(a[1], " ".join(a[2:]))
    print(f"added {a[1].lower()}  {' '.join(a[2:])}  ({len(tracked.load())} tracked)")
elif cmd == "rm" and len(a) == 2:
    r = tracked.remove(a[1])
    print(f"removed {a[1].lower()} ({r})" if r else "not in the list")
else:
    raise SystemExit("usage: dd tracked list [text] | add <0xaddress> <label> | rm <0xaddress>")
