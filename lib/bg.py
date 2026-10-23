"""Detached finisher for an analysis the check stopped waiting for (the 15 s budget).

The Slither worker / Heimdall binary keep running after the check prints; this process waits for their output
file, stores the result in the cache exactly as the in-check path would, and cleans the temp dir. The next check
of the same contract then has it. One-shot, no locks: a second run only rewrites the same cache key.
    python -m lib.bg slither  <out.json>        <cache key>
    python -m lib.bg heimdall <decompiled.sol>  <cache key>
"""
import asyncio, json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def wait_file(path, limit=420):
    t = time.time()
    while not os.path.exists(path):
        if time.time() - t > limit:
            return False
        time.sleep(1)
    time.sleep(1)  # let the writer finish the file
    return True


def main():
    kind, path, key = sys.argv[1:4]
    from lib import cache
    try:
        if not wait_file(path):
            return
        if kind == "slither":
            for _ in range(5):
                try:
                    out = json.load(open(path)); break
                except json.JSONDecodeError:
                    time.sleep(1)
            else:
                return
            out["ok"] = True
            cache.set(key, json.dumps(out))
        elif kind == "heimdall":
            from lib import decomp
            asyncio.run(decomp.finish(open(path).read(), key))
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


if __name__ == "__main__":
    main()
