# Chain-lens-Bot

On-chain due diligence for new EVM tokens. Paste a contract address, get a verdict in about 3 seconds that answers
the four questions that matter before you buy:

| Question | How it's answered |
|---|---|
| **Is the LP locked?** | Reads who holds the liquidity right now (v2 LP tokens, v3/v4 position NFTs, lockers, hook-held liquidity) and whether that holder can remove it. Detects liquidity that was already pulled. |
| **Any malicious functions?** | Finds every owner-only function, works out who can call it, runs it in a simulation and records what it actually changed (supply, balances, fees, limits, oracle…). Reads the code itself, verified or not. |
| **Mintable / ruggable?** | Hidden mints (in code and in supply history), LP pulls, owner switches that block sells, funds taken from holders. |
| **Honeypot?** | Real buys and sells on a fresh wallet, now and up to 24 h later, two sizes, a second wallet, and after each owner power is switched on. Catches delayed honeypots, sell caps, blacklists, cooldowns and hidden v4 hook fees. |

Everything is verified on-chain: no trust in websites, docs or third-party scanners (GoPlus is shown only as a
cross-check). Nothing is ever broadcast: every test runs as a read-only simulation or on a private fork.

---

## Example

```
== ANSWERS
   LP locked?        YES — 100% in StandardLaunchHook — no removal path: collect(address) runs but removes no liquidity (traced)
   Malicious fns?    NONE found (35 functions checked)
   Honeypot?         NO — sold fine in 7 contexts, 2 sizes, 2 wallets, up to 24h later · hook takes 4.00% buy / 4.00% sell (measured)
   Mintable?         NO
   Ruggable?         no rug path found
```

---

## Commands

| Command | What it does | Time |
|---|---|---|
| `dd check <chain> <token>` | The full check: ANSWERS block, functions and who can call them, LP, market, dev wallet, money trail, testnet history, verdict | ~3–4 s |
| `dd check <chain> <token> --deep` | Adds stateful sell tests over time, real v4 swaps through the hook, decompiled/verified code analysis, owner-switch tests | ~3–5 s* |
| `dd sim <chain> <token>` | Rug simulation: actually removes the LP as its controller on a private fork and shows what they'd walk away with (or until when it's locked), plus every owner power executed | ~5–20 s |
| `dd code <chain> <address> <function>` | Shows what ONE function really does: exact Solidity (verified) or decompiled pseudocode (unverified), the storage it uses with live values, and a traced call (slots read/written, calls, events) | ~1–7 s |
| `dd sell <chain> <token>` | Only the stateful sell tests | ~1–5 s |
| `dd trace <chain> <wallet>` | Where a wallet's money came from (follows bridges such as Relay) | ~2–10 s |
| `dd chains` | Supported chains | — |

`--json` on `dd check` / `dd code` prints machine-readable output.
\* first run on a contract can take longer while its code is compiled or decompiled; results are cached.

---

## Supported chains

Ethereum · Base · BNB Chain · Arbitrum · Optimism · Avalanche · Sonic · HyperEVM · Ink · Robinhood Chain · X Layer ·
MegaETH · Monad · Arc — plus 14 testnets for checking a deployer's test history.

---

## What it checks

**Contract**
- Every function from the bytecode, named via signature databases; risky / unknown / owner-only classification
- Who can call each privileged function: owner, deployer, addresses held in storage, addresses hardcoded in the code
- Control: plain wallet vs Safe (n-of-m) vs timelock; AccessControl roles, including roles that can be granted later
- Proxies (EIP-1967, beacon, ZeppelinOS) and minimal clones (EIP-1167)
- Verified code: compiled exactly like the block explorer and analysed with **Slither**: per-function access, storage
  written, calls, and the "owner switches" every transfer depends on
- Unverified code: decompiled with **Heimdall**: per-function access checks, storage writes, outside calls, revert messages
- Owner switches: each setting transfers read is flipped directly in storage, then a holder tries to sell

**Trading**
- Buy and sell tax, and where the cut goes
- Sells at the same block, +1, +10 blocks, +1 h, +24 h, two sizes, a second wallet
- Each owner function switched on, then the holder sells again (sell blockers, funds taken, fee raises)
- Uniswap v4 hooks: real swaps through the PoolManager, exact hook fee (from the pool's own Swap event vs what the trader
  received), who receives it, what each callback does, whether outsiders can add/remove liquidity, and what each hook
  admin function does to the next sell

**Liquidity**
- v2 LP token holders, v3/v4 positions and NFT owners, known lockers, contracts and hooks holding liquidity
- Removal functions of LP-holding contracts are traced: "pulls liquidity" vs "only collects fees"
- Liquidity already pulled, with the transaction

**People**
- Launch transaction and launchpad, template, dev wallet age / activity / funds sent out
- Money trail of the dev's funding (through bridges)
- Testnet history and "rehearsals" (same code deployed on a testnet before launch)
- Same-code deployments seen elsewhere (scam kits)

---

## How it works

```
dd check
 ├─ reads          code, metadata, owner, proxy slots, pools (DexScreener → GoPlus → on-chain scan), source
 ├─ functions      selectors → names → classification
 ├─ probes         who can call each privileged function (state-override calls)
 ├─ quick tests    eth_simulateV1: honeypot buy/sell + effect of each owner function (+ getters before/after)
 ├─ LP             holders / positions / lockers / hooks, removal powers, pulled liquidity
 ├─ control        owner type, roles, timelocks, pending owner
 ├─ who            launch, dev profile, money trail, testnets, same-code matches
 └─ deep (opt-in)  multi-block eth_simulateV1 sell tests + v4 hook analysis (fork fallback), Slither / Heimdall
```

- **Simulations, not guesses.** Read-only `eth_simulateV1` (many blocks per request, run in parallel) and local
  `anvil` forks where a node can't simulate. Helper contracts (a router-style seller, a v4 swap router) are placed by
  state override — nothing is deployed on-chain.
- **Execution evidence.** Owner functions, sells and LP removals are traced (`debug_traceCall`): storage written,
  outside calls, tokens moved.
- **Data sources.** Archive RPC nodes (with tracing) on most chains, Envio HyperSync for whole-chain logs and wallet
  history, Etherscan V2 / Routescan / Blockscout for verified source, DexScreener for market data.

---

## Setup

Requirements: Linux, Python 3.12, [Foundry](https://getfoundry.sh) (`anvil`, `cast`, `forge`).

```bash
python3 -m venv .venv
.venv/bin/pip install aiohttp eth-abi eth-utils eth-hash[pycryptodome] slither-analyzer solc-select
# Heimdall decompiler: official binary into tools/heimdall/heimdall
#   https://github.com/Jon-Becker/heimdall-rs/releases (heimdall-linux-amd64)
# compile the helper / test contracts
(cd fixtures && forge build)
ln -s "$PWD/bin/dd" /usr/local/bin/dd
```

**Keys** go in `keys/` (one value per file, `chmod 600`, never committed — `keys/` is in `.gitignore`):

| File | What |
|---|---|
| `keys/etherscan` | Etherscan V2 API key |
| `keys/envio` | Envio HyperSync token |
| `keys/rpc_<chain>` | private RPC endpoint URL for a chain (optional; public RPCs are the fallback) |

Chain endpoints and settings live in `chains.json`.

---

## Tests

```bash
.venv/bin/python tests/all.py            # everything (~4 min)
.venv/bin/python tests/all.py --no-live  # deterministic only (~1.5 min)
```

| Suite | What it proves |
|---|---|
| `tests/fixtures.py` | Our own honeypot tokens (clean, honeypot, delayed, cooldown, sell cap, owner drain, owner switch) on a fork — both sell engines must classify each correctly |
| `tests/hooks.py` | Our own malicious v4 hook (fee raise, honeypot, delayed honeypot) — exact fee measured |
| `tests/switches.py` | Owner-switch flipping finds the hidden sell switch, flags nothing on a clean token |
| `tests/decomp.py` | Decompiler analysis finds the known powers in our own bytecode |
| `tests/code.py` | `dd code` names storage correctly even where the decompiler mislabels it |
| `tests/roles.py` | Grantable roles detected |
| `tests/pinned.py` | Real v4 hooks frozen at a past block: exact fee and recipient |
| `tests/run.py` | Live smoke tests on real tokens (stable facts fail, changeable facts report DRIFT) |

---

## Layout

```
bin/dd            command dispatcher
cmds/             check · sim · sell · trace · code
lib/              rpc, explorer, sigs, evm, cache, discover, lp, quicksim, sellsim, fastsell, rugsim, v4hook,
                  tracer, switches, slots, static (Slither), decomp (Heimdall), authority, who, trace, testnets, xscan
fixtures/src/     test tokens, malicious test hook, helper routers (Solidity)
tests/            all suites above
chains.json       per-chain RPCs and settings
ARCHITECTURE.md   detailed reference
```

---

## Limits

- Findings marked **"read from code"** are inferred; only simulations and traces count as proof.
- Decompiled code is an interpretation; `dd code` shows a traced call as ground truth.
- Chains on public RPCs (Sonic, MegaETH, Optimism) may lack tracing; the bot falls back to a private fork there.
- Not covered yet: dev-linked "bundle" wallets buying at launch.
- Facts and measurements only — not investment advice.
