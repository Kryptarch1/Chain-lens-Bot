"""Run Slither on one verified contract (called in a subprocess by lib/static.py so a hang/crash can't take dd down).
argv: input.json (sources+settings+solc path+contract name) output.json"""
import json, sys


import re
SENDER = r"(?:msg\.sender|_msgSender\(\))"
EQ = re.compile(rf"{SENDER}\s*[!=]=|[!=]=\s*{SENDER}")
EQ_ONLY = re.compile(rf"{SENDER}\s*==|==\s*{SENDER}")
ROLE = re.compile(rf"(?:hasRole|_checkRole|isAuthorized|isAdmin|isOperator)\([^)]*{SENDER}|_checkRole\(|_checkOwner\(\)")
ALLOW = re.compile(rf"(?:require\(|if\s*\(\s*!?)\s*!?\w+\[{SENDER}\]\s*[),]")


def access_gate(f):
    """Who may call f: 'caller == <address>' / 'role' / 'allowlist' or None. A balance check on msg.sender is NOT access."""
    nodes = list(f.all_nodes())
    for m in f.modifiers:
        nodes += list(m.all_nodes())
    for n in nodes:
        e = str(n.expression or "")
        if not e:
            continue
        # require(msg.sender != X) only excludes X (anyone else may call); require(msg.sender == X) and
        # if (msg.sender != X) revert(...) are real gates
        is_require = "require(" in e or "assert(" in e
        if (is_require and EQ_ONLY.search(e)) or (not is_require and EQ.search(e)):
            return "caller == fixed/stored address"
        if ROLE.search(e):
            return "role check"
        if ALLOW.search(e):
            return "allowlist"
    return None


def code_of(c, selector):
    """-> {sig, source:[{where, name, code}], reads, writes, layout} for the public function with this selector."""
    from eth_utils import keccak
    target = None
    hits = [f for f in c.functions if f.visibility in ("public", "external") and not f.is_constructor
            and "0x" + keccak(text=f.solidity_signature)[:4].hex() == selector.lower()]
    hits.sort(key=lambda f: not f.is_implemented)  # the real body before an interface declaration
    target = hits[0] if hits else None
    if target is None:
        return {"error": f"no public function {selector} in {c.name}"}
    seen, parts = set(), []

    def add(fn, role):
        key = (fn.contract_declarer.name if hasattr(fn, "contract_declarer") else "", fn.canonical_name)
        if key in seen:
            return
        seen.add(key)
        try:
            code = fn.source_mapping.content
        except Exception:
            code = None
        if code:
            parts.append({"role": role, "where": getattr(fn.contract_declarer, "name", ""), "name": fn.name, "code": code})

    add(target, "function")
    for m in target.modifiers:
        add(m, "modifier")
    for ic in target.all_internal_calls():
        fn = getattr(ic, "function", ic)
        if hasattr(fn, "source_mapping") and hasattr(fn, "canonical_name"):
            add(fn, "internal")
    layout = {}
    cu = c.compilation_unit
    for v in sorted({*target.all_state_variables_read(), *target.all_state_variables_written()}, key=lambda v: v.name):
        if v.is_constant or getattr(v, "is_immutable", False):
            layout[v.name] = {"constant": True, "type": str(v.type)}
            continue
        try:
            slot, off = cu.storage_layout_of(c, v)
            layout[v.name] = {"slot": slot, "offset": off, "type": str(v.type)}
        except Exception:
            pass
    return {"contract": c.name, "sig": target.solidity_signature, "source": parts,
            "reads": sorted(v.name for v in target.all_state_variables_read()),
            "writes": sorted(v.name for v in target.all_state_variables_written()), "layout": layout}


def main(inp, outp):
    from crytic_compile import CryticCompile
    from crytic_compile.platform.solc_standard_json import SolcStandardJson
    from slither import Slither
    from slither.slithir.operations import HighLevelCall, LowLevelCall, Transfer, Send, SolidityCall, InternalCall

    j = json.load(open(inp))
    import os
    root = os.path.realpath(j["workdir"])
    for path, code in j["files"].items():  # crytic-compile needs every source on disk at its import path
        dst = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if not dst.startswith(root + os.sep):  # explorer data is untrusted: never write outside the temp dir
            raise SystemExit(f"refusing source path {path!r}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as fh:
            fh.write(code)
    settings = dict(j.get("settings") or {})
    settings.pop("outputSelection", None)
    settings.pop("compilationTarget", None)
    std = SolcStandardJson(target={"language": "Solidity", "sources": {p: {"content": c} for p, c in j["files"].items()}, "settings": settings})
    cc = CryticCompile(std, solc=j["solc"], solc_working_dir=j["workdir"])
    sl = Slither(cc)
    cands = sl.get_contract_from_name(j["name"]) if j.get("name") else []
    c = cands[0] if cands else max((x for x in sl.contracts if not x.is_interface and not x.is_library), key=lambda x: len(x.functions))

    def vars_read(sig):
        f = c.get_function_from_signature(sig)
        return sorted({v.name for v in f.all_state_variables_read()}) if f else []

    if j.get("mode") == "code":  # one function's exact source + everything it reaches
        json.dump(code_of(c, j["selector"]), open(outp, "w"))
        return
    if j.get("mode") == "code_all":  # every public function at once: one compile serves all later `dd code` calls
        from eth_utils import keccak
        sels = sorted({"0x" + keccak(text=f.solidity_signature)[:4].hex() for f in c.functions
                       if f.visibility in ("public", "external") and not f.is_constructor})
        json.dump({"functions": {sel: code_of(c, sel) for sel in sels}}, open(outp, "w"))
        return
    supply_vars, balance_vars = vars_read("totalSupply()"), vars_read("balanceOf(address)")
    fns = []
    for f in c.functions_entry_points:
        if f.is_constructor or f.view or f.pure:
            continue
        calls, ops = [], []
        for ir in f.all_slithir_operations():
            if isinstance(ir, HighLevelCall):
                dest = getattr(ir.destination, "name", str(ir.destination))
                calls.append(f"{dest}.{ir.function_name}")
            elif isinstance(ir, LowLevelCall):
                dest = getattr(ir.destination, "name", str(ir.destination))
                if ir.function_name == "delegatecall":
                    ops.append(f"delegatecall to {dest}")
                elif ir.call_value is not None:
                    ops.append(f"sends native to {dest}")
                else:
                    calls.append(f"{dest}.call")
            elif isinstance(ir, (Transfer, Send)):
                ops.append(f"sends native to {getattr(ir.destination, 'name', str(ir.destination))}")
            elif isinstance(ir, SolidityCall) and "selfdestruct" in ir.function.name:
                ops.append("selfdestruct")
        gate = access_gate(f)
        mods = [m.name for m in f.modifiers]
        fns.append({
            "sig": f.solidity_signature, "name": f.name, "params": [str(p.type) for p in f.parameters],
            "protected": bool(gate), "gate": gate, "modifiers": mods,
            "writes": sorted({v.name for v in f.all_state_variables_written()}),
            "reads": sorted({v.name for v in f.all_state_variables_read()}),
            "calls": list(dict.fromkeys(calls))[:12], "ops": list(dict.fromkeys(ops)),
        })
    layout, dupes = {}, set()
    try:
        cu = c.compilation_unit
        for v in c.state_variables_ordered:
            if v.is_constant or getattr(v, "is_immutable", False):
                continue
            try:
                slot, off = cu.storage_layout_of(c, v)
            except Exception:
                continue
            t = str(v.type)
            try:  # enums are stored as uint8: tag them with how many values they have
                from slither.core.solidity_types import UserDefinedType
                from slither.core.declarations import Enum
                if isinstance(v.type, UserDefinedType) and isinstance(v.type.type, Enum):
                    t = f"enum:{len(v.type.type.values)}"
            except Exception:
                pass
            if v.name in layout or v.name in dupes:  # same name in two parent contracts: ambiguous, never guess the slot
                layout.pop(v.name, None); dupes.add(v.name); continue
            layout[v.name] = {"slot": slot, "offset": off, "type": t}
    except Exception:
        pass
    json.dump({"contract": c.name, "supply_vars": supply_vars, "balance_vars": balance_vars, "fns": fns, "layout": layout}, open(outp, "w"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
