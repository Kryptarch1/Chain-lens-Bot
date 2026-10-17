<div align="center">

# Chain-lens-Bot

**Token and project analysis for crypto hunters — on-chain verdicts in seconds, product claims tested, not trusted.**

![Python](https://img.shields.io/badge/python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Chains](https://img.shields.io/badge/EVM_chains-14-6E56CF?style=for-the-badge&logo=ethereum&logoColor=white)
![Uniswap v4](https://img.shields.io/badge/Uniswap-v2_·_v3_·_v4-FF007A?style=for-the-badge&logo=uniswap&logoColor=white)
![Foundry](https://img.shields.io/badge/Foundry-anvil_·_cast_·_forge-1F2937?style=for-the-badge)
![Slither](https://img.shields.io/badge/Slither-verified_code-0F766E?style=for-the-badge)
![Heimdall](https://img.shields.io/badge/Heimdall-unverified_code-B45309?style=for-the-badge)

[Quick start](#quick-start) · [Commands](#commands) · [What it checks](#what-it-checks) · [How it works](#how-it-works) · [Tests](#tests)

</div>

---

## Overview

Paste a contract address. Chain-lens answers the four questions that matter before you buy, verified on-chain with
simulations and traces — websites, docs and scanners are never trusted (GoPlus appears only as a cross-check).
**Nothing is ever broadcast.**

| Question | How it's answered |
|---|---|
| **Is the LP locked?** | Who holds the liquidity right now — v2 LP tokens, v3/v4 NFTs, lockers, hooks — and whether they can remove it |
| **Any malicious functions?** | Every owner-only function is found, its callers identified, and its real effect simulated |
| **Mintable or ruggable?** | Hidden mints, supply raised since launch, LP pulls, sell-blocking owner switches, funds taken |
| **Honeypot?** | Real buys and sells from fresh wallets — now, later, at two sizes, and after each owner power is switched on |

---

## Quick start

```bash
# check a token (≈3 s)
dd check base 0xYourToken

# go deeper: sell tests over time, real v4 hook swaps, code analysis
dd check base 0xYourToken --deep

# read exactly what one function does
dd code base 0xYourToken setFees
```

**Example output**

```text
== ANSWERS
   LP locked?        YES — 100% in StandardLaunchHook — no removal path (traced)
   Malicious fns?    NONE found (35 functions checked)
   Honeypot?         NO — sold fine in 7 contexts, 2 sizes, 2 wallets, up to 24h later
                     · hook takes 4.00% buy / 4.00% sell (measured)
   Mintable?         NO
   Ruggable?         no rug path found
```

---

## Commands

| Command | What it does | Time |
|---|---|:-:|
| `dd check <chain> <token>` | Full check: answers, functions & callers, LP, market, dev wallet, money trail, verdict | ~3 s |
| `dd check … --deep` | + sell tests over time, real v4 swaps through the hook, verified/decompiled code analysis, owner-switch tests | ~3–5 s* |
| `dd sim <chain> <token>` | Rug simulation: removes the LP as its controller on a private fork and shows the payout or lock expiry | ~5–20 s |
| `dd code <chain> <addr> <fn>` | One function's real code + storage with live values + a traced call | ~1–7 s |
| `dd sell <chain> <token>` | Stateful sell tests only | ~1–5 s |
| `dd trace <chain> <wallet>` | Where a wallet's money came from, across bridges | ~2–10 s |
| `dd chains` | List supported chains | — |

**Project analysis and toolkit** (`kit/`)

| Command | What it does |
|---|---|
| `recon <site> [github] [--app <dapp>]` | Product fact sheet: crawls the site and dApp in a headless browser with a read-only wallet (captures and rejects any transaction or signature request), lists the contracts the dApp actually calls, the stack, fake-data signals, and GitHub activity |
| `dd <chain> token <addr>` | One-shot contract snapshot: proxy, owner, authorities, DEX pairs |
| `dd <chain> audit <addr>` | Serialized Audit verdict (paid credits; needs your own key) |
| `dd <chain> fork [port]` | Local anvil fork for free dry runs |
| `dd <chain> cast …` / `rpc …` | Raw `cast` / JSON-RPC with the chain's endpoint wired in |
| `dd <chain> wallet` · `load-key` · `wipe-key` | Burner wallet for real product tests; the key is held in RAM only, never on disk |

<sub>`--json` gives machine-readable output for `check` and `code`. \* First run on a contract is slower while its code is compiled or decompiled; results are cached.</sub>

---

## Supported chains

| | | | | |
|---|---|---|---|---|
| Ethereum | Base | BNB Chain | Arbitrum | Optimism |
| Avalanche | Sonic | HyperEVM | Ink | Robinhood Chain |
| X Layer | MegaETH | Monad | Arc | |

Plus **14 testnets** for checking a deployer's test history.

---

## What it checks

<table>
<tr>
<td valign="top" width="50%">

**Contract**
- Every function, named and classified
- Who can call each privileged function
- Owner type: wallet, Safe (n-of-m), timelock, roles
- Proxies and minimal clones resolved
- **Slither** on verified code, **Heimdall** on unverified
- Owner switches flipped in storage, then a sell

</td>
<td valign="top" width="50%">

**Trading**
- Buy / sell tax and who receives the cut
- Sells at +0, +1, +10 blocks, +1 h, +24 h
- Two sizes, a second wallet
- Each owner power switched on, then re-sold
- v4 hooks: exact fee, recipient, callbacks, admin powers

</td>
</tr>
<tr>
<td valign="top">

**Liquidity**
- v2 holders, v3/v4 positions and NFT owners
- Lockers, contracts and hooks holding LP
- Removal functions traced: pulls LP vs. collects fees
- Liquidity already pulled, with the transaction

</td>
<td valign="top">

**People**
- Launch transaction and launchpad
- Dev wallet age, activity, funds sent out
- Money trail of the dev's funding
- Testnet rehearsals and same-code deployments

</td>
</tr>
</table>

---

## How it works

```text
dd check
 ├─ reads         code · owner · proxy slots · pools (DexScreener → GoPlus → on-chain) · source
 ├─ functions     selectors → names → risk classification
 ├─ probes        who can call each privileged function
 ├─ quick tests   honeypot buy/sell + effect of every owner function   (eth_simulateV1)
 ├─ LP            holders · positions · lockers · hooks · removal powers
 ├─ control       owner type · roles · timelocks · pending owner
 ├─ who           launch · dev profile · money trail · testnets
 └─ deep          multi-block sell tests · v4 hook analysis · Slither / Heimdall   (opt-in)
```

- **Simulated, never sent.** Read-only `eth_simulateV1` requests run in parallel; local `anvil` forks where a node can't simulate. Helper contracts are placed by state override — nothing is deployed.
- **Proof, not guesses.** Owner functions, sells and LP removals are traced: storage written, calls made, tokens moved.
- **Data.** Archive RPC nodes with tracing · Envio HyperSync for logs and wallet history · Etherscan V2 / Routescan / Blockscout for source · DexScreener for market data.

---

## Installation

**Requirements:** Linux · Python 3.12 · Node.js 20+ · [Foundry](https://getfoundry.sh)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

(cd kit && npm install && npx playwright install chromium)   # recon crawler
ln -s "$PWD/kit/recon.js" /usr/local/bin/recon

# Heimdall decompiler → tools/heimdall/heimdall
#   https://github.com/Jon-Becker/heimdall-rs/releases  (heimdall-linux-amd64)

(cd fixtures && forge build)                 # helper and test contracts
ln -s "$PWD/bin/dd" /usr/local/bin/dd
```

### Configuration

Keys live in `keys/` — one value per file, `chmod 600`, **never committed** (`keys/` is git-ignored).

| File | Contents |
|---|---|
| `keys/etherscan` | Etherscan V2 API key |
| `keys/envio` | Envio HyperSync token |
| `keys/rpc_<chain>` | Private RPC URL for a chain *(optional — public RPCs are the fallback)* |
| `kit/keys/serialized` | Serialized Audit API key *(optional)* |

Chain endpoints and settings live in `chains.json`.

---

## Tests

```bash
.venv/bin/python tests/all.py              # everything        (~4 min)
.venv/bin/python tests/all.py --no-live    # deterministic only (~1.5 min)
```

| Suite | Proves |
|---|---|
| `fixtures.py` | Seven honeypot types on a fork are classified correctly by both sell engines |
| `hooks.py` | A malicious v4 hook's fee raise, honeypot and delayed honeypot are caught, fee measured exactly |
| `switches.py` | Hidden sell switches are found; clean tokens flag nothing |
| `decomp.py` | Known powers are recovered from our own bytecode |
| `code.py` | `dd code` names storage correctly where the decompiler mislabels it |
| `roles.py` | Grantable roles are detected |
| `pinned.py` | Real v4 hooks frozen at a past block give the exact fee and recipient |
| `run.py` | Live smoke tests on real tokens — changeable facts report `DRIFT`, not failure |

---

## Project layout

```text
bin/dd           command dispatcher
cmds/            check · sim · sell · trace · code
lib/             rpc · explorer · lp · quicksim · fastsell · sellsim · rugsim · v4hook · switches
                 tracer · slots · static (Slither) · decomp (Heimdall) · authority · who · trace · testnets
kit/             project analysis (recon.js crawler) and the original toolkit (token · audit · fork · wallet)
fixtures/src/    test tokens, malicious test hook, helper routers (Solidity)
tests/           test suites
chains.json      per-chain RPCs and settings
requirements.txt Python dependencies
docs/plans/      build plans and roadmap
ARCHITECTURE.md  detailed reference
CLAUDE.md        working rules for the AI analyst (AGENTS.md: same, for Codex)
```

---

## Limits

- Findings marked **"read from code"** are inferred — simulations and traces are the proof.
- Decompiled code is an interpretation; `dd code` adds a traced call as ground truth.
- Chains on public RPCs (Sonic, MegaETH, Optimism) may lack tracing; a private fork is used instead.
- Not yet covered: dev-linked "bundle" wallets buying at launch.

<div align="center">

<sub>Facts and measurements only — not investment advice.</sub>

</div>
