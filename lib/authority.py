"""Who holds dangerous power NOW, and who can GAIN it later.

Covers (first release):
- AccessControl roles: role ids from RoleGranted/RoleRevoked history, current members confirmed with hasRole,
  role admins via getRoleAdmin, and "grantable" paths: a role with no holder that an admin can hand out.
- Ownable: owner, pendingOwner (a renounced owner with a pending handover is not renounced).
- Controller type of each privileged address: plain wallet, Safe (signers/threshold), TimelockController
  (min delay), proxy admin (can swap all code), other contract.
- Confirms grantable paths with a read-only simulation: admin grants the role, then the dangerous function runs.
Everything unknown stays unknown; nothing here turns "not found" into "safe".
"""
import asyncio
from eth_utils import keccak
from lib.evm import encode_call, decode1, decode_out, selector, topic, addr as to_addr
from lib.rpc import ZERO, topic_addr
from lib import quicksim

ROLE_GRANTED = topic("RoleGranted(bytes32,address,address)")
ROLE_REVOKED = topic("RoleRevoked(bytes32,address,address)")
ROLE_ADMIN_CHANGED = topic("RoleAdminChanged(bytes32,bytes32,bytes32)")
SEL = {"hasRole": selector("hasRole(bytes32,address)"), "getRoleAdmin": selector("getRoleAdmin(bytes32)"),
       "grantRole": selector("grantRole(bytes32,address)"), "pendingOwner": selector("pendingOwner()")}
KNOWN_ROLES = {("0x" + "0" * 64): "DEFAULT_ADMIN_ROLE"}
for n in ("MINTER_ROLE", "BURNER_ROLE", "PAUSER_ROLE", "ADMIN_ROLE", "OPERATOR_ROLE", "UPGRADER_ROLE", "BLACKLISTER_ROLE",
          "BLOCKLISTER_ROLE", "FEE_SETTER_ROLE", "GOVERNANCE_ROLE", "MANAGER_ROLE", "CONTROLLER_ROLE", "WHITELISTER_ROLE",
          "RESCUER_ROLE", "TREASURER_ROLE", "DEFAULT_ADMIN", "OWNER_ROLE", "DEPLOYER_ROLE", "BRIDGE_ROLE", "VAULT_ROLE"):
    KNOWN_ROLES["0x" + keccak(text=n).hex()] = n
DANGER_WORDS = ("mint", "blacklist", "blocklist", "pause", "freeze", "fee", "tax", "seize", "confiscate", "setoracle", "setcontroller",
                "upgrade", "burnfrom", "settrading", "enabletrading", "disable", "withdraw", "rescue", "sweep", "limit", "maxtx", "maxwallet")


def role_name(r):
    return KNOWN_ROLES.get(r.lower(), r[:10] + "…")


async def controller_kind(rpc, ex, a, depth=0):
    """-> dict(kind, label, detail) for an address that holds power."""
    if not a or a.lower() == ZERO:
        return {"kind": "none", "label": "nobody"}
    code = await rpc.code(a)
    if len(code) <= 2:
        return {"kind": "eoa", "label": "plain wallet (one person, no delay)"}
    out = {"kind": "contract"}
    ok, o = await rpc.try_call(a, encode_call("getOwners()"))
    ok2, t = await rpc.try_call(a, encode_call("getThreshold()"))
    if ok and ok2 and o and t and len(o) > 130:
        try:
            owners = decode1("address[]", o); thr = decode1("uint256", t)
            out.update(kind="safe", label=f"Safe multisig {thr}-of-{len(owners)} (no built-in delay)", signers=list(owners), threshold=thr)
            return out
        except Exception:
            pass
    ok, d = await rpc.try_call(a, encode_call("getMinDelay()"))
    if ok and d and d != "0x":
        delay = decode1("uint256", d)
        out.update(kind="timelock", label=f"timelock, min delay {delay/3600:.1f}h for NEW operations (already-queued ones may be ready)", delay=delay)
        return out
    src = await ex.source(a)
    name = src.get("name") or "(unverified)"
    ok, o = await rpc.try_call(a, encode_call("owner()"))
    if ok and o and len(o) >= 66 and depth < 2:  # e.g. ProxyAdmin: whoever owns it holds the power
        own = "0x" + o[-40:]
        sub = await controller_kind(rpc, ex, own, depth + 1)
        signers = f" (signers: {', '.join(x[:6] + '…' + x[-4:] for x in sub.get('signers', [])[:3])})" if sub.get("signers") else ""
        out.update(label=f"contract {name}, owned by {own[:6]}…{own[-4:]} = {sub['label']}{signers}", owner=own, owner_kind=sub)
        return out
    out["label"] = f"contract {name} — its own rules decide; not resolved"
    return out


async def roles(rpc, token, created_block=None):
    """-> {role: {'members': [...], 'admin': role_or_None}} from history + current reads."""
    latest = await rpc.block_number()
    start = created_block if created_block is not None else max(0, latest - min(rpc.logs_range * 400, 2_000_000))
    try:
        logs = await asyncio.wait_for(rpc.get_logs(token, [[ROLE_GRANTED, ROLE_REVOKED]], start, latest), 15)
    except Exception:
        logs = []
    hist = {}
    for l in logs:
        role = l["topics"][1]; acct = topic_addr(l["topics"][2])
        hist.setdefault(role, set())
        if l["topics"][0] == ROLE_GRANTED:
            hist[role].add(acct)
        else:
            hist[role].discard(acct)
    out = {}
    for role, cands in hist.items():
        members = []
        for acct in cands:
            ok, r = await rpc.try_call(token, SEL["hasRole"] + role[2:] + acct[2:].rjust(64, "0"))
            if ok and r and int(r, 16) == 1:
                members.append(acct)
        ok, ar = await rpc.try_call(token, SEL["getRoleAdmin"] + role[2:])
        out[role] = {"members": members, "admin": ar if ok and ar and len(ar) >= 66 else None, "complete": bool(created_block)}
    # admin roles may exist with members but no grants in our window (e.g. DEFAULT_ADMIN granted at construction) -> read the deployer
    return out


async def analyze(rpc, ex, token, info, fns, probes):
    """-> dict(roles, grantable=[...], owner=..., controllers={addr: kind}, notes=[...])"""
    out = {"roles": {}, "grantable": [], "notes": [], "controllers": {}, "owner": None}
    sels = {f["sel"] for f in fns}
    # ---- owner / pending owner ----
    owner = info.get("owner")
    ok, po = await rpc.try_call(token, SEL["pendingOwner"]) if selector("pendingOwner()") in sels else (False, None)
    pending = to_addr(po) if ok and po and len(po) >= 66 and to_addr(po) != ZERO else None
    out["owner"] = {"address": owner, "pending": pending}
    if pending:
        out["notes"].append(f"ownership handover pending to {pending} — 'renounced' owner is NOT final")
    # ---- controller types for every address that can call something ----
    holders = set()
    for p in probes:
        for c in p.get("callers", []):
            holders.add(c)
    if owner:
        holders.add(owner)
    if info.get("admin"):
        holders.add(info["admin"])
    kinds = await asyncio.gather(*[controller_kind(rpc, ex, a) for a in holders])
    out["controllers"] = dict(zip(holders, kinds))
    if info.get("admin"):
        out["notes"].append(f"proxy admin {info['admin']} can replace ALL code (upgrade) — {out['controllers'][info['admin']]['label']}")
    # ---- AccessControl ----
    if SEL["grantRole"] in sels and SEL["hasRole"] in sels:
        rl = await roles(rpc, token, info.get("created_block"))
        out["roles"] = rl
        danger = [f for f in fns if f["mut"] not in ("view", "pure") and any(w in (f["name"] or "").lower() for w in DANGER_WORDS)]
        # which role can call which dangerous fn: test from a member of each role with members
        role_can = {}
        for role, r in rl.items():
            if not r["members"]:
                continue
            m = r["members"][0]
            for f in danger:
                from cmds.sim import calldata as worst_case
                data = worst_case(f["sel"], f["name"], f["args"], m)
                if not data:
                    continue
                okc, _ = await rpc.try_call(token, data, m)
                if okc:
                    role_can.setdefault(role, []).append(f["name"])
        out["role_can"] = role_can
        # candidate roles = roles seen in events + well-known names + 32-byte constants embedded in the bytecode
        import re as _re
        code = info.get("impl_code") or info.get("code") or await rpc.code(token)
        consts = {"0x" + m for m in _re.findall(r"7f([0-9a-f]{64})", code[2:].lower())}
        consts = {c for c in consts if c.lstrip("0x").strip("0") and c not in ("0x" + "f" * 64)}
        cand_roles = set(rl) | set(KNOWN_ROLES) | consts | {"0x" + "0" * 64}
        cand_roles = list(cand_roles)[:60]

        async def admin_members_of(adm):
            mem = rl.get(adm, {}).get("members", [])
            if mem:
                return mem
            for c in [info.get("owner"), info.get("creator")] + [x for x, _ in info.get("slot_addrs", [])]:
                if c:
                    okh, rh = await rpc.try_call(token, SEL["hasRole"] + adm[2:] + c[2:].rjust(64, "0"))
                    if okh and rh and int(rh, 16) == 1:
                        rl.setdefault(adm, {"members": [], "admin": None})["members"] = [c]
                        return [c]
            return []

        from cmds.sim import calldata as worst_case
        tried = set()
        admins = await rpc.batch([("eth_call", [{"to": token, "data": SEL["getRoleAdmin"] + role[2:]}, "latest"]) for role in cand_roles])

        async def one_role(role, ar):
            if not (ar and len(ar) >= 66):
                return
            adm = ar
            adm_members = await admin_members_of(adm)
            if not adm_members:
                return
            granter = adm_members[0]
            gdata = SEL["grantRole"] + role[2:] + granter[2:].rjust(64, "0")
            enabled = []
            for f in danger:
                data = worst_case(f["sel"], f["name"], f["args"], granter)
                if not data or (role, f["sel"]) in tried:
                    continue
                tried.add((role, f["sel"]))
                okd, _ = await rpc.try_call(token, data, granter)
                if okd:
                    continue  # already callable without any grant — reported elsewhere
                res = await quicksim.simulate(rpc, [{"from": granter, "to": token, "data": encode_call("totalSupply()")},
                                                    {"from": granter, "to": token, "data": gdata},
                                                    {"from": granter, "to": token, "data": data},
                                                    {"from": granter, "to": token, "data": encode_call("totalSupply()")}])
                if res is None:
                    break
                if res[1]["ok"] and res[2]["ok"]:
                    s0, s1 = int(res[0]["ret"] or "0x0", 16), int(res[3]["ret"] or "0x0", 16)
                    enabled.append(f["name"] + (" (supply ↑)" if s1 > s0 else ""))
            if enabled:
                out["grantable"].append({"role": role_name(role), "by": granter, "via": role_name(adm), "enables": enabled,
                                         "holders_now": len(rl.get(role, {}).get("members", []))})
        await asyncio.gather(*[one_role(role, ar) for role, ar in zip(cand_roles, admins)])
    return out


def render(a):
    L = []
    if not a:
        return L
    L.append("== CONTROL (who can change the rules)")
    o = a.get("owner") or {}
    if o.get("address"):
        k = a["controllers"].get(o["address"], {})
        L.append(f"   owner {o['address']}: {k.get('label', '?')}")
    if o.get("pending"):
        L.append(f"   ! pending owner {o['pending']} (handover not finished — owner is not really renounced)")
    for addr, k in a.get("controllers", {}).items():
        if addr == o.get("address"):
            continue
        L.append(f"   {addr}: {k.get('label', '?')}")
    rl = a.get("roles") or {}
    for role, r in rl.items():
        can = a.get("role_can", {}).get(role, [])
        L.append(f"   role {role_name(role)}: {len(r['members'])} holder(s)" + (f" {', '.join(x[:10] + '…' for x in r['members'][:3])}" if r["members"] else "") +
                 (f" · admin {role_name(r['admin'])}" if r.get("admin") else "") + (f" · can call {', '.join(can[:4])}" if can else ""))
    for g in a.get("grantable", []):
        L.append(f"   ✗ CAN BE ENABLED LATER: {g['by'][:10]}… (holds {g['via']}) can grant {g['role']} to himself and then call {', '.join(g['enables'][:4])}" +
                 (f" — role has NO holder today" if g["holders_now"] == 0 else ""))
    for n in a.get("notes", []):
        L.append(f"   ! {n}")
    return L
