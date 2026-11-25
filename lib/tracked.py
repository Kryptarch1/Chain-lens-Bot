"""The user's tracked wallets (KOLs, hunters, known insiders): data/tracked.json = {"wallets": {address: label}}.
data/ is never pushed: the list is private. `dd tracked list|add|rm` edits it; `dd holders` flags every tracked wallet
that holds / held the token, funded a holder, or sits in a linked group or same-coin cluster."""
import json, os
from lib.rpc import ROOT

PATH = os.path.join(ROOT, "data", "tracked.json")
_c = {"mtime": None, "w": {}}


def load():
    try:
        m = os.path.getmtime(PATH)
    except OSError:
        return {}
    if m != _c["mtime"]:
        try:
            _c["w"] = {a.lower(): l for a, l in (json.load(open(PATH)).get("wallets") or {}).items()}
        except Exception:
            _c["w"] = {}
        _c["mtime"] = m
    return _c["w"]


def label(a):
    return load().get((a or "").lower())


def tag(a):
    l = label(a)
    return f"  ★ {l}" if l else ""


def _save(w):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump({"_doc": "tracked wallets: address -> label (user's private list; data/ is never pushed)", "wallets": w},
              open(PATH, "w"), indent=1, ensure_ascii=False)
    _c["mtime"] = None


def add(a, lab):
    w = dict(load())
    w[a.lower()] = lab
    _save(w)


def remove(a):
    w = dict(load())
    r = w.pop(a.lower(), None)
    _save(w)
    return r
