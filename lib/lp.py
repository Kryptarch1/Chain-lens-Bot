"""LP ownership analysis for V2 / V3 / V4 / custom pools.
Answers: who owns the liquidity, can they pull it, is it really locked.
"""
import asyncio, time
from lib.rpc import RPC, RPCError, ZERO, DEAD, topic_addr, pad_addr
from lib.evm import encode_call, decode1, decode_out, topic, addr as to_addr
from lib import sigs, explorer, sim, args as argsmod

KNOWN_LOCKERS = {  # lower-case address -> name (add as you confirm them)
    "0x663a5c229c09b049e36dcc11a9b0d4a8eb9db214": "UNCX (ETH v2)",
    "0x7f5c649856f900d15c83741f45ae46f5c6858234": "UNCX (ETH v3)",
    "0xe2fe530c047f2d85298b07d9333c05737f1435fb": "Team Finance (ETH)",
    "0xc765bddb93b0d1c1a88282ba0fa6b2d00e3e0c83": "UNCX (Base v2)",
    "0x231278edd38b00b07fbd52120cef685b9baebcc1": "UNCX (Base v3)",
    "0xc4e637d37113192f4f1f060daebd7758de7f4131": "UNCX (BSC v2)",
    "0x0c89c0407775dd89b12918b9c0aa42bf96518820": "PinkLock (BSC)",
}
NOT_REMOVAL_PREFIX = ("add", "increase", "deposit", "lock", "bind", "stake", "mint", "create", "init", "set", "fund", "credit", "fee", "donate", "provide")
REMOVAL_WORDS = ["remove", "withdraw", "unlock", "rescue", "collect", "decrease", "burn", "exit", "claim",
                 "emergency", "sweep", "release", "pull", "transfer", "migrate", "close", "redeem"]

V2_MINT = topic("Mint(address,uint256,uint256)")
V3_MINT = topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
V3_MINT_RAMSES = topic("Mint(address,address,uint256,int24,int24,uint128,uint256,uint256)")  # Ramses/Shadow v3: + indexed position index
V3_INC = topic("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
V4_MODIFY = topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
V4_INIT = topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
ERC721_TRANSFER = topic("Transfer(address,address,uint256)")


async def estimate_block(rpc, ts, latest=None):
    """Block number at unix time ts (minus a 2000-block margin): 16-way search on block timestamps, one batch per
    round. Average block time is NOT used — it changes over a chain's life (rh, 2026-10) and an overshoot made the
    v4 Initialize search miss and rescan all history."""
    latest = latest or await rpc.block_number()
    lo, hi = 0, latest  # invariant: block lo is at/before ts (or genesis), block hi after it
    cache_key = f"blk@{rpc.name}:{int(ts) // 600}"
    from lib import cache
    c = cache.get(cache_key)
    if c is not None:
        return c, latest
    for _ in range(5):
        if hi - lo <= 2000:
            break
        pts = sorted({lo + (hi - lo) * i // 16 for i in range(1, 16)})
        bs = await rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in pts])
        if not all(bs):
            break  # node can't serve some headers: keep the bracket found so far
        before = [b for b, x in zip(pts, bs) if int(x["timestamp"], 16) <= ts]
        lo = before[-1] if before else lo
        hi = next((b for b in pts if b > lo), hi)
    est = max(0, lo - 2000)
    if hi < latest:  # ts is safely in the past: the answer never changes
        cache.set(cache_key, est, ttl=None)
    return est, latest


async def contract_kind(rpc, ex, a):
    """-> dict(kind='eoa'|'dead'|'locker'|'contract', name, owner, controller, verified)"""
    al = a.lower()
    if al in (ZERO, DEAD) or al.endswith("dead"):
        return {"kind": "dead", "name": "burn address"}
    if al in KNOWN_LOCKERS:
        return {"kind": "locker", "name": KNOWN_LOCKERS[al]}
    code = await rpc.code(a)
    if len(code) <= 2:
        return {"kind": "eoa", "name": "plain wallet (EOA)"}
    fns = ("owner()", "controller()", "admin()", "getOwner()")
    src, rs = await asyncio.gather(ex.source(a), rpc.batch([("eth_call", [{"to": a, "data": encode_call(fn)}, "latest"]) for fn in fns]))
    owner = controller = None
    for fn, r in zip(fns, rs):
        if r and r != "0x" and len(r) >= 66:
            v = to_addr(r)
            if v != ZERO:
                if fn == "controller()":
                    controller = v
                elif not owner:
                    owner = v
    return {"kind": "contract", "name": src.get("name") or "unverified contract", "verified": src["verified"],
            "owner": owner, "controller": controller, "code": code, "source": src.get("source", "")}


# selectors through which a contract could take liquidity out or hand the LP to someone (v2 LP token, v3/v4 NFT, routers, PoolManager)
LP_EXIT_SELS = {"0c49ccbe": "decreaseLiquidity", "42966c68": "burn(uint256)", "42842e0e": "safeTransferFrom", "b88d4fde": "safeTransferFrom",
                "23b872dd": "transferFrom", "a9059cbb": "transfer", "a22cb465": "setApprovalForAll", "ac9650d8": "multicall",
                "dd46508f": "modifyLiquidities", "4afe393c": "modifyLiquiditiesWithoutUnlock", "5a6bcfda": "modifyLiquidity",
                "48c89491": "unlock", "89afcb44": "burn(address)", "baa2abde": "removeLiquidity", "02751cec": "removeLiquidityETH",
                "af2979eb": "removeLiquidityETHSupportingFeeOnTransferTokens", "2195995c": "removeLiquidityWithPermit",
                "ded9382a": "removeLiquidityETHWithPermit", "5b0d5984": "removeLiquidityETHWithPermitSupportingFeeOnTransferTokens"}


V2_ONLY_EXITS = {"a9059cbb", "89afcb44", "baa2abde", "02751cec", "af2979eb", "2195995c", "ded9382a", "5b0d5984"}


def code_exits(code, rows, nft=False):
    """Bytecode proof for an LP-holding contract: every way out it COULD use. [] = it has no code path that removes
    liquidity or moves/approves the LP (no exit selector pushed, no delegatecall/callcode, no generic call-anything fn).
    approve(x, 0) followed by approve(x, n) is the ERC-20 forceApprove helper (fee swaps) and is listed separately."""
    b, i, found, approve = bytes.fromhex((code or "0x")[2:]), 0, [], False
    while i < len(b):
        op = b[i]
        if op in (0xF4, 0xF2):
            found.append("delegatecall" if op == 0xF4 else "callcode")
        if op == 0x63 and i + 5 <= len(b):
            sel = b[i + 1:i + 5].hex()
            if sel in LP_EXIT_SELS and not (nft and sel in V2_ONLY_EXITS):  # an NFT position can't leave via ERC-20 transfer
                found.append(LP_EXIT_SELS[sel])
            elif sel == "095ea7b3":
                approve = True
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    if any("bytes" in (args or "") and mut not in ("view", "pure") for _, args, mut in rows):
        found.append("a function taking raw bytes (could call anything)")
    return sorted(set(found)), approve


async def removal_powers(rpc, info, a, ctx=(), nft=False):
    """For a custom LP-owning contract: which non-view functions look like removal, and can the owner call them."""
    rows = sigs.selectors_from_bytecode(info["code"])
    names = await sigs.names_for([s for s, _, _ in rows])
    info["exits"], info["approve_helper"] = code_exits(info["code"], rows, nft)
    cands = []
    for sel, args, mut in rows:
        if mut in ("view", "pure"):
            continue
        nm = names.get(sel) or ""
        low = nm.lower()
        if not nm:
            cands.append((sel, f"unnamed {sel}", args))
        elif any(w in low for w in REMOVAL_WORDS) and not low.startswith(NOT_REMOVAL_PREFIX):
            cands.append((sel, nm, args))
    who = info.get("controller") or info.get("owner")
    results = []
    cands = [c for c in cands if not c[1].startswith(CALLBACKS)]  # PoolManager/pool callbacks: not callable by people
    senders = [(l, s_) for l, s_ in (("owner", who), ("stranger", STRANGER)) if s_]

    async def one(sel, nm, args):
        callable_by, traced = [], None
        if args == "":
            for label, sender in senders:
                ok, _ = await sim.override_call(rpc, a, sel, sender)
                if ok:
                    callable_by.append(label)
        # trace it (no guessing from names): does the call actually take liquidity out of the pool?
        from lib import tracer

        async def run(sets):
            runs = [(label, nz, tracer.trace_or_sim(rpc, sender, a, sel + data, val)) for label, sender in senders for data, nz, val in sets]
            res = await asyncio.gather(*[r for _, _, r in runs], return_exceptions=True)
            ran = [(label, nz, tr) for (label, nz, _), tr in zip(runs, res) if isinstance(tr, dict) and not tr.get("error")]
            # handing the LP position NFT out of the holder contract is as good as pulling it (the receiver can then remove it)
            pulls = sorted({label for label, _, tr in ran if tr.get("liq_removed") or
                            (nft and any(t["from"].lower() == a.lower() for t in tr.get("nft_out", [])))})
            if pulls:
                return "pulls liquidity", pulls
            if any(nz for _, nz, _ in ran):
                return "runs, removes no liquidity", []  # ran with real (nonzero) values, moved no liquidity: fee/claim fn
            if res and all(tr is None for tr in res):
                return None, []  # neither tracing nor simulation available
            return ("unresolved (reverts with test args)" if res else None), []

        invented = [(d, nz, 0) for d, nz in argsmod.traced_sets(args, [x for _, x in senders] + [c for c in ctx if c and c != ZERO], STRANGER)]
        traced, pulls = await run(invented) if invented else (None, [])
        if traced not in ("pulls liquidity", "runs, removes no liquidity"):
            # invented args can't reach it (strings, structs, or args unknown): replay REAL past calls (exact calldata + value)
            real = [(t["input"][10:], True, int(t.get("value") or 0)) for t in await past_calls(rpc, a, sel)]
            if real:
                t2, p2 = await run(real)
                if t2:
                    traced, pulls = (t2 + " (replayed real past calls)" if t2 != "pulls liquidity" else t2), p2
        callable_by = sorted(set(callable_by) | set(pulls))
        results.append({"fn": nm, "args": args, "callable_by": callable_by, "traced": traced})

    await asyncio.gather(*[one(*c) for c in cands[:12]])
    return results


async def past_calls(rpc, a, sel, n=2):
    """Up to n successful past txs that called `sel` on contract a (from the explorer), newest first."""
    try:
        rows = await explorer.Explorer(rpc.name).txlist(a, 200)
    except Exception:
        return []
    sel = sel.lower().removeprefix("0x")
    return [t for t in rows if (t.get("input") or "").lower().startswith("0x" + sel) and str(t.get("isError", "0")) == "0"
            and (t.get("to") or "").lower() == a.lower()][:n]


STRANGER = "0x000000000000000000000000000000000000bEEF"
CALLBACKS = ("unlockCallback", "lockAcquired", "uniswapV3MintCallback", "uniswapV3SwapCallback", "pancakeV3", "before", "after")


async def pool_tokens(rpc, pool):
    """token0/token1 of a v2/v3 pool -> test arguments for removal-like fns (collect(token), withdraw(token)...)."""
    r = await asyncio.gather(rpc.try_call(pool, encode_call("token0()")), rpc.try_call(pool, encode_call("token1()")))
    return tuple(to_addr(x[1]) for x in r if x[0] and x[1] and len(x[1]) >= 66)


async def v2_lp(rpc, ex, pair, creator=None):
    sup = decode1("uint256", await rpc.eth_call(pair, encode_call("totalSupply()")))
    checks = {"dead": DEAD, "zero": ZERO}
    if creator:
        checks["creator"] = creator
    bals = await rpc.batch([("eth_call", [{"to": pair, "data": encode_call("balanceOf(address)", [a])}, "latest"]) for a in checks.values()])
    out = {"type": "v2", "pair": pair, "lp_supply": sup, "holders": []}
    for (k, a), b in zip(checks.items(), bals):
        v = decode1("uint256", b) if b else 0
        if v:
            out["holders"].append({"who": k, "address": a, "pct": 100 * v / sup if sup else 0})
    hl = await ex.holders(pair, limit=10)
    for h in hl:
        try:
            v = int(h["value"])
        except Exception:
            continue
        if sup and v / sup > 0.01 and h["address"].lower() not in [x["address"].lower() for x in out["holders"]]:
            out["holders"].append({"who": "holder", "address": h["address"], "pct": 100 * v / sup})
    out["holders"].sort(key=lambda x: -x["pct"])
    burned = sum(h["pct"] for h in out["holders"] if h["who"] in ("dead", "zero"))
    out["burned_pct"] = burned
    toks = await pool_tokens(rpc, pair)

    async def inspect(h):
        info = await contract_kind(rpc, ex, h["address"])
        h["info"] = info
        if info["kind"] == "contract":
            info["removal"] = await removal_powers(rpc, info, h["address"], toks)
    await asyncio.gather(*[inspect(h) for h in out["holders"][:4] if h["who"] not in ("dead", "zero")])
    return out


async def v3_lp(rpc, ex, pool, from_block, to_block, nfpm_hint=None):
    """Positions = the LARGEST mints (the Mint event carries the liquidity amount), so a busy pool's history is never
    replayed: receipts of the top 40 mint txs in one batch, ownerOf/positions for every tokenId in one batch, then the
    top 8 live positions are inspected in parallel."""
    logs, pl = await asyncio.gather(rpc.get_logs(pool, [[V3_MINT, V3_MINT_RAMSES]], from_block, to_block), rpc.try_call(pool, encode_call("liquidity()")))
    out = {"type": "v3", "pool": pool, "positions": [], "n_mints": len(logs),
           "pool_liquidity": int(pl[1], 16) if pl[0] and pl[1] and pl[1] != "0x" else None}
    def minted(l):  # liquidity word: standard (sender, amount, ..) vs Ramses (sender, tickUpper, amount, ..)
        w = 2 if l["topics"][0] == V3_MINT_RAMSES else 1
        return int(l["data"][2:][64 * w:64 * (w + 1)] or "0", 16)
    logs = sorted(logs, key=lambda l: -minted(l))  # by minted liquidity, biggest first
    nfpms = {}
    for l in logs:
        nfpms.setdefault(topic_addr(l["topics"][1]), []).append(l["transactionHash"])
    codes = dict(zip(nfpms, await asyncio.gather(*[rpc.code(o) for o in nfpms])))
    for owner, txs in nfpms.items():
        if len(codes[owner]) <= 2:
            out["positions"].append({"owner": owner, "kind": "eoa (direct position, no NFT)", "liq": None})
            continue
        txs = list(dict.fromkeys(txs))[:40]
        ids = set()
        for r in await rpc.batch([("eth_getTransactionReceipt", [h]) for h in txs]):
            for lg in (r or {}).get("logs", []):
                if lg["address"].lower() == owner.lower() and lg["topics"] and lg["topics"][0] == V3_INC:
                    ids.add(int(lg["topics"][1], 16))
        ids = sorted(ids)
        reads = await rpc.batch([("eth_call", [{"to": owner, "data": encode_call(fn, [tid])}, "latest"]) for tid in ids for fn in ("ownerOf(uint256)", "positions(uint256)")])
        for i, tid in enumerate(ids):
            o, pos = reads[2 * i], reads[2 * i + 1]
            liq = None
            if pos and len(pos) > 2:
                try:
                    if len(pos) == 2 + 64 * 10:  # Ramses-style NFPM: (token0, token1, tickSpacing, tickLower, tickUpper, liquidity, ..)
                        liq = int(pos[2 + 64 * 5:2 + 64 * 6], 16)
                    else:
                        liq = decode_out("uint96,address,address,address,uint24,int24,int24,uint128,uint256,uint256,uint128,uint128", pos)[7]
                except Exception:
                    pass
            holder = to_addr(o) if o and len(o) >= 66 else None
            out["positions"].append({"nft": owner, "id": tid, "owner": holder or "BURNED (token does not exist)", "liq": liq})
    out["positions"].sort(key=lambda p: -(p.get("liq") or 0))
    live = [p for p in out["positions"] if p.get("owner") and p["owner"].startswith("0x") and p.get("liq")][:8]

    toks = await pool_tokens(rpc, pool)

    async def inspect(p):
        p["info"] = await contract_kind(rpc, ex, p["owner"])
        if p["info"]["kind"] == "contract":
            p["info"]["removal"] = await removal_powers(rpc, p["info"], p["owner"], toks, nft=True)
    await asyncio.gather(*[inspect(p) for p in live])
    return out


def hook_permissions(hook):
    bits = int(hook, 16) & 0x3FFF
    names = ["beforeInitialize", "afterInitialize", "beforeAddLiquidity", "afterAddLiquidity", "beforeRemoveLiquidity",
             "afterRemoveLiquidity", "beforeSwap", "afterSwap", "beforeDonate", "afterDonate", "beforeSwapReturnDelta",
             "afterSwapReturnDelta", "afterAddLiquidityReturnDelta", "afterRemoveLiquidityReturnDelta"]
    return [n for i, n in enumerate(names) if bits & (1 << (13 - i))]


from eth_utils import keccak as _keccak


def _v4_position_slot(pool_id, owner, tl, tu, salt_hex):
    pool_slot = int.from_bytes(_keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")
    positions_slot = (pool_slot + 6).to_bytes(32, "big")
    pk = _keccak(bytes.fromhex(owner[2:].rjust(40, "0")) + (tl & 0xFFFFFF).to_bytes(3, "big") + (tu & 0xFFFFFF).to_bytes(3, "big") + bytes.fromhex(salt_hex.rjust(64, "0")))
    return "0x" + _keccak(pk + positions_slot).hex()


async def v4_current_liquidity(rpc, pm, pool_id, owner, tl, tu, salt_hex):
    """Current liquidity of a v4 position read straight from PoolManager storage (no logs, any chain)."""
    slot = _v4_position_slot(pool_id, owner, tl, tu, salt_hex)
    ok, r = await rpc.try_call(pm, encode_call("extsload(bytes32)", [bytes.fromhex(slot[2:])]))
    if ok and r and len(r) >= 66:
        return int(r[-32:], 16)
    return None


def position_amounts(sqrtp, tl, tu, liq):
    """(amount0, amount1) raw units a v3/v4 position of `liq` between ticks tl..tu holds at pool price sqrtp (X96)."""
    import math
    if not sqrtp or not liq:
        return 0.0, 0.0
    sp, sa, sb = sqrtp / 2 ** 96, math.pow(1.0001, tl / 2), math.pow(1.0001, tu / 2)
    if sp <= sa:
        return liq * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, liq * (sb - sa)
    return liq * (sb - sp) / (sp * sb), liq * (sp - sa)


def position_value(sqrtp, tl, tu, liq):
    """Value of a position in token1 units at the current price: comparable across ranges (raw liquidity is not:
    a full-range position holding most of the pool has FAR fewer liquidity units than a narrow one)."""
    a0, a1 = position_amounts(sqrtp, tl, tu, liq)
    return a0 * (sqrtp / 2 ** 96) ** 2 + a1, a0, a1


async def v4_pool_liquidity(rpc, pm, pool_id):
    """Active (in-range) liquidity of a v4 pool, straight from PoolManager storage: pools[id].liquidity (slot +3)."""
    base = int.from_bytes(_keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")
    ok, r = await rpc.try_call(pm, encode_call("extsload(bytes32)", [(base + 3).to_bytes(32, "big")]))
    return int(r[-32:], 16) & ((1 << 128) - 1) if ok and r and len(r) >= 66 else None


V4_BEFORE_REMOVE = 1 << 9  # hook permission bit in the hook address


async def hook_blocks_removal(rpc, pm, key, sender, tl, tu, liq, salt):
    """Simulate the pool's hook gate `beforeRemoveLiquidity` exactly as the PoolManager would call it for this position
    (msg.sender = PoolManager, sender = the position's owner), now and 10 years from now. -> revert reason if the hook
    refuses removal at both times and can't be upgraded (no DELEGATECALL/CALLCODE), else None."""
    from eth_abi import encode
    hook = key[4] if key and len(key) > 4 else None
    if not hook or not int(hook, 16) & V4_BEFORE_REMOVE:
        return None
    code = bytes.fromhex((await rpc.code(hook))[2:])
    i = 0
    while i < len(code):
        op = code[i]
        if op in (0xF4, 0xF2):
            return None  # upgradeable / delegating hook: today's rule can change
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    data = "0x" + (_keccak(text="beforeRemoveLiquidity(address,(address,address,uint24,int24,address),(int24,int24,int256,bytes32),bytes)")[:4] +
                   encode(["address", "(address,address,uint24,int24,address)", "(int24,int24,int256,bytes32)", "bytes"],
                          [sender, tuple(key[:5]), (tl, tu, -liq, bytes.fromhex(salt[-64:].rjust(64, "0"))), b""])).hex()
    tx = {"from": pm, "to": hook, "data": data}
    out = []
    for extra in ([], [{}, {"time": hex(int(time.time()) + 10 * 365 * 86400)}]):
        try:
            await rpc.call("eth_call", [tx, "latest"] + extra)
            return None  # the hook lets this removal through
        except RPCError as e:
            m = str(e)
            if "revert" not in m.lower():
                if extra:
                    break  # RPC can't override the clock: judge on today's result
                return None
            out.append(m)
    err = out[0]
    if "0x8f4e75b2" in err:
        return "LiquidityLocked()"
    return err[:80]


async def v4_lp(rpc, ex, cfg, pool_id, from_block, to_block, seed_txs=(), init_hint=None):
    pm = cfg["v4"]["pool_manager"]
    posm = cfg["v4"].get("posm")
    out = {"type": "v4", "pool_id": pool_id, "positions": [], "hook": None}
    async def find_init():
        if init_hint:  # pool creation time known: look in a small window around it first
            r = await rpc.get_logs(pm, [V4_INIT, pool_id], max(0, init_hint - 300_000), min(to_block, init_hint + 600_000))
            if r:
                return r
        r = await rpc.get_logs(pm, [V4_INIT, pool_id], from_block, to_block)
        if not r and from_block > 0:  # start block was a guess and may be too late: search all history
            try:
                r = await rpc.get_logs(pm, [V4_INIT, pool_id], 0, from_block)
            except RPCError:
                r = []
        return r

    async def find_modify():
        try:
            return await rpc.get_logs(pm, [V4_MODIFY, pool_id], from_block, to_block)
        except RPCError:
            return []
    init, pre_logs = await asyncio.gather(find_init(), find_modify())
    if init and int(init[0]["blockNumber"], 16) < from_block:
        # the start block was estimated from average block time and overshot: fetch the missed liquidity events
        try:
            pre_logs = await rpc.get_logs(pm, [V4_MODIFY, pool_id], int(init[0]["blockNumber"], 16), from_block - 1) + list(pre_logs)
        except RPCError:
            out["scan_gap"] = True
    out["pool_liquidity"] = await v4_pool_liquidity(rpc, pm, pool_id)
    if init:
        d = init[0]["data"][2:]
        fee = int(d[0:64], 16); hook = to_addr(d[128:192])
        ts = int(d[64:128], 16); ts = ts - (1 << 256) if ts >> 255 else ts
        out["fee"] = fee; out["dynamic_fee"] = fee == 0x800000
        out["key"] = (topic_addr(init[0]["topics"][2]), topic_addr(init[0]["topics"][3]), fee, ts, hook)
        if hook != ZERO:
            hinfo = await contract_kind(rpc, ex, hook)
            out["hook"] = {"address": hook, "permissions": hook_permissions(hook), "name": hinfo.get("name"),
                           "verified": hinfo.get("verified"), "owner": hinfo.get("owner")}
    if "key" not in out:
        for h in [x for x in seed_txs if x]:
            r = await rpc.receipt(h)
            for l in (r or {}).get("logs", []):
                if l["topics"] and l["topics"][0] == V4_INIT and l["topics"][1].lower() == pool_id.lower():
                    d = l["data"][2:]
                    ts = int(d[64:128], 16); ts = ts - (1 << 256) if ts >> 255 else ts
                    out["key"] = (topic_addr(l["topics"][2]), topic_addr(l["topics"][3]), int(d[0:64], 16), ts, to_addr(d[128:192]))
    logs = list(pre_logs)
    for h in [x for x in seed_txs if x]:
        r = await rpc.receipt(h)
        for l in (r or {}).get("logs", []):
            if l["address"].lower() == pm.lower() and l["topics"] and l["topics"][0] == V4_MODIFY and l["topics"][1].lower() == pool_id.lower():
                if l not in logs:
                    logs.append(l)
    out["n_modify"] = len(logs)
    out["removals"] = []
    net = {}
    for l in logs:
        d = l["data"][2:]
        tl = int(d[0:64], 16); tl = tl - (1 << 256) if tl >> 255 else tl
        tu = int(d[64:128], 16); tu = tu - (1 << 256) if tu >> 255 else tu
        dl = int(d[128:192], 16); dl = dl - (1 << 256) if dl >> 255 else dl
        k = (topic_addr(l["topics"][2]), tl, tu, d[192:256])
        net[k] = net.get(k, 0) + dl
        if dl < 0:
            out["removals"].append({"by": k[0], "salt": k[3], "liq": -dl, "tx": l["transactionHash"], "block": int(l["blockNumber"], 16)})
    # only the biggest net positions matter for "who controls the liquidity"; verify them against live storage
    top = sorted([k for k, v in net.items() if v > 0], key=lambda k: -net[k])[:20]
    if not top:  # logs may be incomplete: still verify every key we saw (bounded)
        top = list(net)[:20]
    out["n_positions_seen"] = len(net)
    cur = await asyncio.gather(*[v4_current_liquidity(rpc, pm, pool_id, s_, a, b, sa) for (s_, a, b, sa) in top])
    from lib import v4hook
    try:
        sqrtp = (await v4hook.pool_state(rpc, pm, pool_id))["sqrtp"]
    except Exception:
        sqrtp = 0
    vals = {k: position_value(sqrtp, k[1], k[2], c) for k, c in zip(top, cur) if c}
    # rank by what each position HOLDS now (raw liquidity units rank a full-range position far too low)
    live = sorted([(k, c) for k, c in zip(top, cur) if c], key=lambda x: -(vals[x[0]][0] if sqrtp else x[1]))[:12]

    async def one(k, liq):
        sender, tl, tu, salt = k
        p = {"sender": sender, "liq": liq, "ticks": (tl, tu), "salt": salt}
        if sqrtp and k in vals:
            p["value"], p["amt0"], p["amt1"] = vals[k]
        nft_owner = None
        if not posm and int(salt, 16):
            # no position manager configured (chains with many v4 copies): a sender that answers ownerOf(salt) is one
            ok, o = await rpc.try_call(sender, encode_call("ownerOf(uint256)", [int(salt, 16)]))
            nft_owner = to_addr(o) if ok and o and len(o) >= 66 else None
        if nft_owner:
            p["nft"], p["owner"], p["posm"] = int(salt, 16), nft_owner, sender
        elif posm and sender.lower() == posm.lower():
            tid = int(salt, 16)
            ok, o = await rpc.try_call(posm, encode_call("ownerOf(uint256)", [tid]))
            p["nft"] = tid
            p["owner"] = to_addr(o) if ok and o and len(o) >= 66 else "BURNED (token does not exist)"
        else:
            p["owner"] = sender
        if out.get("key"):
            p["hook_lock"] = await hook_blocks_removal(rpc, pm, out["key"], sender, tl, tu, liq, salt)
        if p["owner"].startswith("0x"):
            p["info"] = await contract_kind(rpc, ex, p["owner"])
            if p["info"]["kind"] == "contract":
                p["info"]["removal"] = await removal_powers(rpc, p["info"], p["owner"], (out.get("key") or ())[:2], nft=True)
        return p
    out["positions"] = list(await asyncio.gather(*[one(k, liq) for k, liq in live]))
    out["positions"].sort(key=lambda x: -(x.get("value") if x.get("value") is not None else x["liq"]))
    return out


def _one_sided(lp, p):
    """'holds only <token>, no <quote>' when a position holds just the launched token: pulling it takes no quote out of the pool."""
    key = lp.get("key") or ()
    q = (lp.get("quote_addr") or "").lower()
    if p.get("value") is None or len(key) < 2 or not q:
        return None
    qa, ta = (p["amt0"], p["amt1"]) if key[0].lower() == q else (p["amt1"], p["amt0"])
    if ta > 0 and qa <= ta * 1e-9:
        return f"holds only the token, no {lp.get('quote') or 'quote'} — pulling it takes no {lp.get('quote') or 'quote'} out of the pool"
    return None


def judge(lp):
    """-> (status, reasons). status: LOCKED | UNLOCKED | PARTIAL | UNCLEAR"""
    reasons = []
    if lp["type"] == "v2":
        if lp.get("burned_pct", 0) >= 95:
            return "LOCKED", [f"{lp['burned_pct']:.1f}% of LP tokens burned"]
        held = [h for h in lp["holders"] if h["who"] not in ("dead", "zero")]
        for h in held[:3]:
            info = h.get("info", {})
            if info.get("kind") == "eoa":
                reasons.append(f"{h['pct']:.1f}% of LP in plain wallet {h['address']} — can be removed any time")
            elif info.get("kind") == "locker":
                reasons.append(f"{h['pct']:.1f}% in {info['name']} (check unlock date)")
            elif info.get("kind") == "contract":
                rem = [r for r in info.get("removal", []) if r["callable_by"]]
                if rem:
                    reasons.append(f"{h['pct']:.1f}% in {info['name']} {h['address']} — removal callable by {','.join(rem[0]['callable_by'])} ({rem[0]['fn']})")
                else:
                    reasons.append(f"{h['pct']:.1f}% in {info['name']} {h['address']} — no callable removal found")
        if any("plain wallet" in r or "callable by owner" in r for r in reasons):
            return "UNLOCKED", reasons
        if lp.get("burned_pct", 0) >= 50:
            return "PARTIAL", [f"{lp['burned_pct']:.1f}% burned"] + reasons
        return ("UNCLEAR" if not reasons else "PARTIAL"), reasons or ["could not identify LP holders"]
    pos = lp.get("positions", [])
    rem = lp.get("removals") or []
    if lp["type"] == "v3" and pos and all((p.get("liq") == 0) for p in pos if isinstance(p.get("id"), int)) and any(isinstance(p.get("id"), int) for p in pos):
        ids = ", ".join(f"#{p['id']}" for p in pos[:6] if isinstance(p.get("id"), int))
        if (lp.get("pool_liquidity") or 0) > 1000:  # positions we found are empty but the pool is live: someone else holds it
            return "UNCLEAR", [f"the {len(pos)} positions found hold 0 liquidity but the pool still has live liquidity — holder not identified"]
        return "PULLED", [f"ALL liquidity removed — position NFT {ids} now holds 0 liquidity (owner {pos[0].get('owner')})"]
    if not pos and lp.get("pool_liquidity"):
        return "UNCLEAR", ["pool still holds live liquidity but its owner position wasn't found (liquidity-event scan incomplete)"]
    if not pos and rem:
        big = max(rem, key=lambda r: r["liq"])
        return "PULLED", [f"ALL liquidity removed — {len(rem)} removal(s), largest by {big['by']} in tx {big['tx']} (block {big['block']})"]
    if not pos:
        return "UNCLEAR", ["no liquidity positions found in scanned range"]
    owners = {((p.get("sender") or "").lower(), p.get("salt")) for p in pos}
    own_rem = [r for r in rem if (r["by"].lower(), r.get("salt")) in owners]
    if own_rem:
        reasons.append(f"{len(own_rem)} past removal(s) by current LP holders, latest tx {own_rem[-1]['tx']}")
    by_value = all(p.get("value") is not None for p in pos) and sum(p["value"] for p in pos) > 0
    w = (lambda p: p["value"]) if by_value else (lambda p: p.get("liq") or 0)
    total = sum(w(p) for p in pos) or 1
    unlocked = locked = 0
    for p in pos:
        share = 100 * w(p) / total
        o = p.get("owner") or ""
        info = p.get("info", {})
        if p.get("hook_lock"):
            locked += share; reasons.append(f"{share:.0f}% locked by the pool's hook — it reverts every removal of this position ({p['hook_lock']}), "
                                            f"simulated as the holder now and 10 years ahead; the hook has no delegatecall so this can't change"); continue
        if o.startswith("BURNED") or o.lower() in (ZERO, DEAD):
            locked += share; reasons.append(f"{share:.0f}% burned (position NFT sent to burn address)" if not o.startswith("BURNED") else f"{share:.0f}% burned (NFT destroyed)"); continue
        if info.get("kind") == "eoa":
            unlocked += share
            one_side = _one_sided(lp, p)
            reasons.append(f"{share:.0f}% owned by plain wallet {o} — can remove any time" + (f" ({one_side})" if one_side else "")); continue
        if info.get("kind") == "locker":
            locked += share; reasons.append(f"{share:.0f}% in {info['name']}"); continue
        if info.get("kind") == "contract":
            rem = [r for r in info.get("removal", []) if r["callable_by"] and not (r.get("traced") or "").startswith("runs, removes no liquidity")]
            listed = [r for r in info.get("removal", []) if not (r.get("traced") or "").startswith("runs, removes no liquidity")]
            cleared = [r for r in info.get("removal", []) if (r.get("traced") or "").startswith("runs, removes no liquidity")]
            if rem:
                unlocked += share
                reasons.append(f"{share:.0f}% in {info['name']} {o} — removal callable by {','.join(rem[0]['callable_by'])} via {rem[0]['fn']}" +
                               (" (traced: pulls liquidity)" if rem[0].get("traced") == "pulls liquidity" else ""))
            elif not listed and cleared:
                locked += share
                reasons.append(f"{share:.0f}% in {info['name']} {o} — no removal path: {', '.join(r['fn'] for r in cleared[:3])} run but remove no liquidity (traced)")
            elif listed and info.get("exits") == [] and not info.get("proxy"):
                locked += share
                reasons.append(f"{share:.0f}% in {info['name']} {o} — no removal path in its code: it never calls decreaseLiquidity/burn/"
                               f"transfer/setApprovalForAll on the LP and has no delegatecall (bytecode proof)" +
                               (" — only an ERC-20 approve helper (fee swaps)" if info.get("approve_helper") else ""))
            elif listed:
                reasons.append(f"{share:.0f}% in {info['name']} {o} — has removal-like fns ({', '.join(r['fn'] for r in listed[:3])}) needing args; owner {info.get('controller') or info.get('owner')}" +
                               (f"; {', '.join(r['fn'] for r in cleared[:2])} traced: remove no liquidity" if cleared else ""))
            else:
                locked += share
                reasons.append(f"{share:.0f}% in {info['name']} {o} — no removal function found" + (" (verified)" if info.get("verified") else " (UNVERIFIED)"))
    # every pullable position holds only the launched token (side "limit-sell" ranges, launchpad LP slots): the quote side
    # (ETH etc.) all sits in locked/burned positions, so pulling them can't take quote out of the pool
    side = [p for p in pos if (p.get("info") or {}).get("kind") == "eoa"]
    if by_value and side and locked >= 50 and all(_one_sided(lp, p) for p in side):
        q = lp.get("quote") or "quote"
        return "LOCKED", [f"all {q} liquidity is locked ({locked:.0f}% of the pool's value) — the {len(side)} plain-wallet position(s) "
                          f"({unlocked:.0f}%) hold only the token: pulling them takes no {q} out, but they can withdraw and sell those tokens"] + reasons
    if unlocked >= 50:
        return "UNLOCKED", reasons
    if locked >= 95:
        return "LOCKED", reasons
    if locked >= 50:
        return "PARTIAL", reasons
    return "UNCLEAR", reasons


async def analyze_pair(rpc, ex, cfg, pair, creator=None, created_ts=None):
    """pair: dexscreener pair dict -> lp dict with judgement."""
    pid = pair["pairAddress"]
    labels = pair.get("labels") or []
    dex = pair.get("dexId", "")
    latest = await rpc.block_number()
    from lib import hypersync
    cap = 10 ** 12 if hypersync.available(rpc.cfg.get("id")) else rpc.logs_range * (800 if rpc.private else 60)  # HyperSync: whole history
    init_hint = None
    if pair.get("created_block"):
        from_block = max(pair["created_block"] - 1, latest - cap)
    elif created_ts:
        from_block, _ = await estimate_block(rpc, created_ts / 1000, latest)
        init_hint = from_block
        from_block = max(from_block, latest - cap)
    else:
        from_block = max(0, latest - cap)
    if "v4" in labels or len(pid) == 66:
        if "v4" not in cfg:
            return {"type": "v4", "status": "UNCLEAR", "reasons": ["no v4 PoolManager configured for this chain"]}
        if pair.get("pm") and cfg["v4"].get("any_manager"):
            cfg = {**cfg, "v4": {**cfg["v4"], "pool_manager": pair["pm"]}}
        lp = await v4_lp(rpc, ex, cfg, pid, from_block, latest, pair.get("seed_txs") or (), init_hint)
    elif "v3" in labels or any(x in dex for x in ("v3", "algebra", "aerodrome-slipstream")):
        lp = await v3_lp(rpc, ex, pid, from_block, latest)
    else:
        code = await rpc.code(pid)
        ok, _ = await rpc.try_call(pid, encode_call("totalSupply()"))
        if ok:
            lp = await v2_lp(rpc, ex, pid, creator)
        else:
            lp = await v3_lp(rpc, ex, pid, from_block, latest)
    lp["dex"] = dex; lp["labels"] = labels; lp["liq_usd"] = (pair.get("liquidity") or {}).get("usd")
    lp["quote_balance"] = pair.get("quote_balance")
    lp["quote"] = (pair.get("quoteToken") or {}).get("symbol")
    qa = ((pair.get("quoteToken") or {}).get("address") or "").lower()
    lp["quote_addr"] = ZERO if qa in ("0x0000000000000000000000000000000000000000", "") and lp.get("type") == "v4" and (lp.get("key") or ("",))[0] == ZERO else qa
    lp["status"], lp["reasons"] = judge(lp)
    return lp


def render(lp, ex):
    liq = f"liq ${lp['liq_usd']:,.0f}" if lp.get("liq_usd") else (f"quote side {lp['quote_balance']:,.4f} {lp.get('quote')}" if lp.get("quote_balance") is not None else "liq ?")
    lines = [f"LP  {lp.get('dex','?')} {lp['type']}  quote {lp.get('quote')}  {liq}  → {lp['status']}"]
    for r in lp.get("reasons", []):
        lines.append(f"   - {r}")
    if lp.get("hook"):
        h = lp["hook"]
        lines.append(f"   hook {h['address']} ({h.get('name')}, {'verified' if h.get('verified') else 'UNVERIFIED'}) perms: {', '.join(h['permissions']) or 'none'}" +
                     (f"  dynamic fee" if lp.get("dynamic_fee") else f"  fee {lp.get('fee',0)/10000:.2f}%") + (f"  owner {h['owner']}" if h.get("owner") else ""))
    for p in lp.get("positions", [])[:4]:
        extra = f" nft#{p['id']}" if p.get("id") is not None else (f" nft#{p['nft']}" if isinstance(p.get("nft"), int) else "")
        lines.append(f"   pos{extra} liq {p.get('liq')} owner {p.get('owner')} [{(p.get('info') or {}).get('name','')}]")
    return "\n".join(lines)
