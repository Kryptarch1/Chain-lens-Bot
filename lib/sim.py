"""Simulation: state-override eth_call (no fork needed) and anvil forks with impersonation.

override_call: run a call as any sender, optionally with a fake balance — works on any RPC that
supports the 3rd eth_call param (most do). Used for "can the owner call X?" tests.

Fork: spawns anvil on a free port against the chain's primary RPC, returns an RPC bound to it.
Kill with fork.close(). Never pkill by pattern (that killed our own shell before).
"""
import asyncio, os, socket, subprocess, time
from lib.rpc import RPC, RPCError

ANVIL = os.path.expanduser("~/.foundry/bin/anvil")


async def override_call(rpc, to, data, sender, value=0, balance_wei=10 ** 21):
    """(ok, result|error). Gives sender a balance so value-calls don't fail on funds."""
    ov = {sender: {"balance": hex(balance_wei)}}
    ok, res = await rpc.try_call(to, data, sender, value, overrides=ov)
    if not ok and "override" in res.lower():  # RPC doesn't support overrides -> plain call
        ok, res = await rpc.try_call(to, data, sender, value)
    return ok, res


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Fork:
    def __init__(self, chain, block=None):
        self.chain = chain
        self.src = RPC(chain)
        self.port = _free_port()
        url = getattr(self.src, "private", None) or self.src.cfg.get("fork_rpc") or self.src.urls[0]
        # --prune-history: keep recent states in RAM only. Without it every fork left ~270 MB of state files in
        # ~/.foundry/anvil/tmp and filled the disk (28 GB on 2026-10-17).
        args = [ANVIL, "--fork-url", url, "--port", str(self.port), "--silent", "--hardfork", "cancun", "--prune-history", "64",
                "--compute-units-per-second", str(self.src.cfg.get("fork_cups", 100 if (self.src.cfg.get("rps") and not self.src.private) else 800)), "--retries", "8", "--fork-retry-backoff", "800"]
        for k, v in self.src.cfg.get("headers", {}).items():
            args += ["--fork-header", f"{k}: {v}"]
        if block:
            args += ["--fork-block-number", str(block)]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{self.port}"
        self.rpc = RPC(self.url)
        self.rpc.logs_range = 10 ** 9

    async def ready(self, timeout=45):
        t = time.time()
        while time.time() - t < timeout:
            try:
                await self.rpc.block_number(); return True
            except Exception:
                await asyncio.sleep(0.4)
        return False

    async def impersonate(self, addr, fund=True):
        """Idempotent per fork: impersonate once, fund once (funding again would distort balance measurements)."""
        imp = getattr(self, "_imp", None)
        if imp is None:
            imp = self._imp = set()
        key = addr.lower()
        if key not in imp:
            await self.rpc.call("anvil_impersonateAccount", [addr])
            imp.add(key)
        if fund and key not in getattr(self, "_funded", set()):
            if not hasattr(self, "_funded"):
                self._funded = set()
            await self.rpc.call("anvil_setBalance", [addr, hex(10 ** 21)])
            self._funded.add(key)

    async def send(self, sender, to, data, value=0, gas=3_000_000, fund=True):
        """Send an impersonated tx; returns receipt (status '0x1' = success). fund=False keeps the sender's real balance."""
        await self.impersonate(sender, fund=fund)
        try:
            h = await self.rpc.call("eth_sendTransaction", [{"from": sender, "to": to, "data": data, "value": hex(value), "gas": hex(gas)}])
        except RPCError as e:  # rejected before mining (e.g. no gas money) -> treat as a failed tx
            return {"status": "0x0", "rejected": str(e)[:120]}
        for i in range(50):
            r = await self.rpc.receipt(h)
            if r:
                return r
            await asyncio.sleep(0.01 if i < 10 else 0.1)
        return None

    async def mine(self, blocks=1, seconds=0):
        """Advance the fork: optionally jump the clock, then mine `blocks` empty blocks."""
        if seconds:
            await self.rpc.call("evm_increaseTime", [hex(seconds)])
        await self.rpc.call("anvil_mine", [hex(max(1, blocks))])

    async def context(self):
        b = await self.rpc.block("latest")
        return int(b["number"], 16), int(b["timestamp"], 16)

    async def deploy(self, sender, bytecode_hex, gas=6_000_000):
        """Deploy a contract from an impersonated sender; returns the new address or None."""
        await self.impersonate(sender, fund=True)
        try:
            h = await self.rpc.call("eth_sendTransaction", [{"from": sender, "data": bytecode_hex, "gas": hex(gas)}])
        except RPCError as e:
            return None
        for _ in range(50):
            r = await self.rpc.receipt(h)
            if r:
                return r.get("contractAddress") if r.get("status") == "0x1" else None
            await asyncio.sleep(0.1)
        return None

    async def snapshot(self):
        return await self.rpc.call("evm_snapshot", [])

    async def revert(self, snap):
        self._funded = set()  # balances set after the snapshot are rolled back too
        return await self.rpc.call("evm_revert", [snap])

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


async def start(chain, block=None, tries=2, timeout=45):
    """A ready fork, retrying once (a busy node can make anvil miss its first start-up window). -> Fork or None"""
    for _ in range(tries):
        f = Fork(chain, block=block)
        if await f.ready(timeout):
            return f
        f.close()
    return None
