# DD Bot — Architecture Reference

State as of 2026-10-17. Written from the code in `/root/dd` (≈5,000 lines). Anything not confirmed from code or a live test is marked **UNCONFIRMED**.

---

## 1. Overview

The bot answers "is this token safe?" for EVM tokens. It runs on one VPS (Ubuntu, 4 vCPU, 7.8 GB RAM). Claude (Claude Code) is the interface; all checks are deterministic Python scripts that Claude runs and relays.

**Request flow (normal check):**

1. User sends a token link or address in chat.
2. Claude picks the chain (from the link, or by probing `eth_getCode` on every configured chain) and runs `dd check <chain> <token>`. Rules for what to run and how to answer live in `CLAUDE.md`.
3. `cmds/check.py` runs the phases in §6, most in parallel, each with its own timeout. A phase that fails or times out is recorded as NOT CHECKED, never as safe.
4. The script prints one report: ANSWERS block (LP locked? / malicious fns? / who controls? / honeypot? / mintable? / ruggable?), DETAILS, MARKET, LP, SELL TESTS, CONTROL, WHO, VERDICT, NOT CHECKED.
5. Claude relays the ANSWERS block first, then the details that matter. No extra manual lookups unless the verdict is UNCLEAR or the user asks.

**Opt-in modes (only when the user asks):**
- `dd check <chain> <token> --deep` adds stateful sell tests on a private fork.
- `dd sim <chain> <token>` runs the rug simulation, baseline buy/sell and owner powers on a fork.
- "tutor" → Claude explains how the user could have found each finding on an explorer (no script).

---

## 2. Components

### Entry point
| File | What it does |
|---|---|
| `bin/dd` (symlinked to `/usr/local/bin/dd`) | Dispatcher. `check`, `sim`, `trace`, `sell` → `cmds/<name>.py`. `chains` → prints `chains.json`. Anything else falls through to the old toolkit `/root/dd-kit/dd` (token, audit, cast, rpc, fork, wallet, load-key, wipe-key). |

### Commands (`cmds/`)
| File | What it does |
|---|---|
| `check.py` | The main check. Phases A–S (see §6), verdict logic, ANSWERS block, report rendering, `--deep`, `--json`. |
| `sim.py` | `dd sim`: rug simulation (via `lib/rugsim`), baseline buy/sell, every owner power executed on a fork. Also defines `sim_args`/`calldata` (worst-case arguments) used by other modules. |
| `sell.py` | `dd sell`: stateful sell tests only (`lib/sellsim`). |
| `trace.py` | `dd trace <chain> <wallet>`: money trail (`lib/trace`). |

### Libraries (`lib/`)
| File | What it does | Used by |
|---|---|---|
| `rpc.py` | Async JSON-RPC client: pooled keep-alive sessions, batching, endpoint fallback, chunked `eth_getLogs` that splits on range errors, routes big log ranges to HyperSync. Loads private endpoints from `keys/rpc_<chain>`. | everything |
| `cache.py` | SQLite cache at `data/cache.db`: `kv` (key/value + optional TTL), `sigs` (function selectors → names), `events` (event topics → names). | explorer, sigs |
| `evm.py` | ABI encode/decode, selectors, topics, formatting, bytecode string and hardcoded-address extraction. | everything |
| `sigs.py` | Selectors from bytecode (`cast selectors`, fallback PUSH4 scan); names from local DB then openchain; classifies functions as standard / view / risky / unknown / other. | check, lp, authority, rugsim, testnets |
| `explorer.py` | One interface over Etherscan V2 → Routescan → Blockscout → HyperSync: verified source, creator, tx list, token transfers, internal txs, holders, first funder. Also DexScreener and GoPlus helpers. Etherscan throttle (2.8 calls/s). | check, lp, who, trace, authority |
| `discover.py` | Creation block (explorer → first mint via RPC/HyperSync → binary search on code, archive only); pool discovery from the creation tx, from first token transfers, or by scanning PairCreated/PoolCreated/Initialize events; supply at launch vs now. | check |
| `lp.py` | LP ownership for v2 (LP-token holders), v3 (NFT positions), v4 (ModifyLiquidity events + live position liquidity read from PoolManager storage). Classifies owners (burn / known locker / plain wallet / contract), probes no-arg removal functions, detects already-pulled liquidity, reads v4 hook permissions. Verdict per pool: LOCKED / UNLOCKED / PULLED / PARTIAL / UNCLEAR. | check, sim |
| `quicksim.py` | Read-only `eth_simulateV1` tests (no fork): honeypot buy→sell, and the effect of each privileged function (supply change, caller balance change, can a holder still sell after it). | check |
| `sim.py` | `Fork` class (anvil on a free port, forked from the chain's private endpoint): snapshot/revert, mine blocks, warp time, impersonate, fund, send, deploy. `override_call` (state-override `eth_call`). | check, sim, sell, rugsim, sellsim, tests |
| `sellsim.py` | Stateful sell tests on a fork: fresh wallet buys (small = 0.05% of pool, large = 2%), sells at same block / +1 / +10 blocks / +1h / +24h (large: same block and +1h), half and 1% sells when a full sell fails, a second wallet, each owner power switched on then sell retried. v2 pools use a real `pair.swap`; v3/v4 use token transfers. Classifies: HONEYPOT, DELAYED HONEYPOT, cooldown, SELL CAP, size limit, FUNDS TAKEN, SELL BLOCKER, changing tax. | check `--deep`, sell |
| `rugsim.py` | Actually removes the liquidity on a fork as its controller: v2 burn, v3 decrease+collect, v4 NFT burn via PositionManager, custom LP contracts via guessed arguments (pool key, ticks, liquidity, tranche ids). Reports what the controller receives, or the lock expiry read from the revert data. | sim |
| `authority.py` | Control analysis: owner and pending owner, controller type (plain wallet / Safe n-of-m / timelock min delay / unresolved contract / proxy admin), AccessControl roles from RoleGranted/RoleRevoked events + known role names + bytecode constants, and "grant role then call" simulations to find power that can be enabled later. | check |
| `who.py` | Launch tx decoding, template/clone source, dev profile (txs, balance, age, deploys, ETH sent out), money trail, xscan lookup, testnet history, hook info (owner-only functions, fee constants, hardcoded swap fee). | check |
| `trace.py` | Funding trail: first funder → if Relay payout, jump to the source chain → stop at an exchange-type wallet (≥10,000 txs), a contract, Solana, or 3–4 hops. | who, trace cmd |
| `testnets.py` | Dev activity on 14 testnets (tx count, deploys, dates) and "rehearsal" detection: same or ≥80%-similar code deployed on a testnet. | who |
| `xscan.py` | xscan.cc lookup for eth/base/bsc: contracts with the same code deployed recently (scam-kit spraying). | who |
| `hypersync.py` | Envio HyperSync client: paginated whole-chain log queries, a wallet's full tx list, a wallet's first funding. | rpc, explorer, discover, testnets |
| `csmcp.py` | Minimal Chainstack MCP client (account/node management). Not used by any check; used to create nodes. | manual |

### Tests (`tests/`, `fixtures/`)
| File | What it does |
|---|---|
| `tests/cases.json` + `run.py` | 10 regression cases on real tokens (6 checks, 4 rug sims) with expected verdict and required phrases. |
| `fixtures/src/T.sol` + `tests/fixtures.py` | Our own trap tokens (clean, honeypot, delayed honeypot, cooldown, max-sell, owner drain, owner sell switch) deployed with a real PancakeSwap pair on a BSC fork; checks `sellsim` classification. |
| `fixtures/src/R.sol` + `tests/roles.py` | AccessControl token with no minter and a live/renounced admin; checks "grantable" detection. |
| `fixtures/foundry.toml` | Builds the fixtures with forge (solc 0.8.24, cancun). |

### Rules for Claude
`CLAUDE.md` (and an identical copy `AGENTS.md` for Codex): role, answer format, the three priority questions, tool map, opt-in rules (sim, deep, tutor), burner-wallet rules, data sources.

### Not part of the DD bot
- `/root/dd-kit/` — old toolkit (bash `dd`, `recon.js` site crawler, Serialized Audit caller, Solana token checks). Still reachable through `bin/dd` fall-through.
- `/root/dd-kit/watch/papermill.py` — stopped.
- `pools-bot.service` — separate launch-watcher bot, unrelated.

---

## 3. RPCs

Private endpoints: **Chainstack Growth** plan ($49/mo, 20M request units/mo, 250 req/s plan-wide, archive + debug/trace, 10-node limit — all 10 used). URLs live in `keys/rpc_<chain>` and are tried first; public endpoints from `chains.json` are fallbacks.

| Chain | Private (Chainstack) | Public fallbacks | Log search used | Notes |
|---|---|---|---|---|
| eth | yes | publicnode, drpc, flashbots | Chainstack 10k blocks/req; HyperSync above ~8M blocks | free log search was unreliable (marked `logs_unreliable`) |
| base | yes | mainnet.base.org, publicnode, llamarpc | Chainstack 10k | free endpoints can't search old logs |
| bsc | yes | bsc-dataseed, publicnode, defibit | Chainstack 10k | same |
| arb | yes | arb1.arbitrum.io, publicnode | Chainstack 10k | |
| avax | yes (path `/ext/bc/C/rpc/<key>`) | api.avax.network, publicnode | Chainstack 10k | public avax logs only through publicnode, needs an address filter; no `eth_simulateV1` |
| hyper | yes | rpc.hyperliquid.xyz (rate-limited, `rps: 3`), drpc | Chainstack 10k | public RPC ignores historical block numbers |
| rh | yes | rpc.mainnet.chain.robinhood.com (needs browser User-Agent, `rps: 3`) | Chainstack 10k | public has no historical state |
| arc | yes | thirdweb 5042, arcexplorer.org/rpc | Chainstack 10k | |
| monad | yes | rpc.monad.xyz | Chainstack 10k | no `eth_simulateV1` with `validation:false` |
| ink | yes | rpc-gel / rpc-qnd.inkonchain | Chainstack 5k (`private_logs_range`) | |
| sonic | no (removed for Arc) | rpc.soniclabs.com, publicnode | public, 50k blocks | |
| op | no | mainnet.optimism.io, publicnode | public, 5k blocks | |
| mega | no | mainnet.megaeth.com/rpc | public, 1k blocks | |
| xlayer | no | rpc.xlayer.tech, xlayerrpc.okx.com | public, 100 blocks | no explorer API configured |

**Fallback logic (`lib/rpc.py`):** endpoints tried in order; on HTTP 429/5xx/403 or "rate limit" errors it retries with backoff (up to 4 tries) then marks the endpoint bad for 60 s; provider-limit errors ("archive", "current plan", "pruned", "missing trie node", …) switch to the next endpoint; reverts and range errors are raised. Concurrency per chain: 24 with a private endpoint, else 12 or the configured `rps`.

**Testnet RPCs (`lib/testnets.py`):** public endpoints for Sepolia, Hoodi, Base Sepolia, BSC testnet, Arbitrum Sepolia, OP Sepolia, Avalanche Fuji, Sonic, Monad, HyperEVM, Ink Sepolia, MegaETH and Robinhood testnets; Arc testnet via HyperSync only (its RPC was unreachable).

---

## 4. External APIs

| API | Data | Used for | Limits / cost |
|---|---|---|---|
| **Etherscan V2** (key `etherscan`, free) | verified source, creator, tx lists, token/internal transfers, top holders | source code, creator, dev history | 3 calls/s shared (throttled to 2.8). Free tier: full on eth, arb, sonic, hyper, monad, mega; **source only** on base, bsc, op, avax, rh (learned at runtime, then skipped) |
| **Routescan** (no key) | Etherscan-style API for Avalanche | source, creator, history on avax | UNCONFIRMED limits |
| **Blockscout v2** (no key) | source, creator, txs, transfers, holders | fallback on eth, base, arb, op, ink, mega | Robinhood's Blockscout is Cloudflare-blocked from the VPS |
| **Envio HyperSync** (key `envio`, free token) | whole-chain logs, wallet tx lists, block timestamps | big log scans, wallet history on chains Etherscan free can't see, creation-by-first-mint, testnet history | free tier limits UNCONFIRMED (pricing page didn't load) |
| **DexScreener** (no key) | pools, price, liquidity, volume, socials | pool discovery, market line | rate limits UNCONFIRMED; misses some pools (then GoPlus / on-chain scan) |
| **GoPlus** (no key) | security flags, holders, DEX list | second opinion only; pool fallback | "too many requests" seen on bulk use |
| **openchain signature DB** (no key) | selector → function name, topic → event name | naming functions/events; results cached forever | |
| **Relay API** (no key) | bridge requests by user or tx hash | money trail across chains | |
| **xscan.cc** (unofficial, no key) | same-code contracts, new-deploy feed (eth, base, bsc) | "same code deployed N×" in WHO | only contracts it indexed at deploy time; `/decompile` returned errors on every test |
| **Chainstack MCP** (key `chainstack_api`) | account, node list, node creation | setup only | |
| **Serialized Audit** (key `serialized`) | AI contract audit | old toolkit only, only on explicit request | costs user credits |

**Not integrated:** Solana APIs (user scoped the bot to EVM), X/Twitter, wallet labels (Arkham etc.), any decompiler, Tenderly.

---

## 5. Data handling

- **Caching (`data/cache.db`, SQLite WAL):**
  - `kv`: verified source (forever, only once verified) and creator lookups (forever, only when a tx is known). No other TTL caching is used today.
  - `sigs` / `events`: selector and topic names, forever; misses stored as empty so they aren't re-queried.
  - Nothing else is cached: every check re-reads live state.
- **Batching:** `RPC.batch()` sends up to 50 calls per JSON-RPC batch, falling back to individual calls if a provider rejects batches. Used for metadata, proxy slots, storage slots, candidate addresses, pool quote balances.
- **Parallelism:** `asyncio` throughout; independent phases start together (see §6). Log scans run up to 20 chunks concurrently on private endpoints (6 on public).
- **Streaming vs polling:** no streams or watchers run today. Everything is on-demand. (Launch streams and `dd watch` are planned.)
- **Retries:** RPC (see §3); HyperSync 3 tries per page; Etherscan 3 tries on "rate limit"; explorer HTTP 3 tries.
- **Forks:** each `--deep`/`sim`/fallback test starts a fresh anvil process (`--hardfork cancun`, `--compute-units-per-second` 800 on private endpoints, 100 otherwise), killed at the end. State lives only in that process.
- **Keys:** `keys/` (chmod 700, files chmod 600). Burner private key RAM-only under `/dev/shm/dd-burner` (old toolkit).

---

## 6. Checks — `dd check` in run order

| # | Phase | What it does | Tools | Timeout |
|---|---|---|---|---|
| A | reads | code, name/symbol/decimals/supply/owner/paused/trading flags, proxy slots (EIP-1967 impl/admin/beacon, zos), storage slots 0–15, EIP-1167 clone target, DexScreener pairs, GoPlus, verified source, creator, creation block + tx, pools from the creation tx / first transfers / on-chain scan if DexScreener has none | RPC batch, explorer, DexScreener, GoPlus, discover, HyperSync | 45 s |
| — | mint history (background) | supply at creation (archive) vs now; fallback: scan Transfer-from-0x0 logs | RPC archive, logs | 40 s |
| — | who (background) | launch tx decode, template, dev profile, money trail, xscan, testnets | explorer, HyperSync, Relay, xscan, testnet RPCs | 20 s |
| B | functions | selectors → names → classify | `cast selectors`, sig DB, openchain | 20 s |
| C | privilege probes | for each risky/unknown/other state-changing fn: can a stranger call it? if not, which candidate (owner, creator, storage/hardcoded addresses that exist on-chain) can? | state-override `eth_call` | 30 s |
| Q | quick tests (parallel) | honeypot buy→sell; each callable privileged fn run and measured (supply, caller balance, sell still works) | `eth_simulateV1` | 20 s |
| S | sell tests (parallel, `--deep` only) | stateful sell matrix + owner powers (see `sellsim`) | anvil fork | 40 s (budget 18 s) |
| AU | control (parallel) | owner/pending owner, controller types, roles, grantable power | RPC, logs, `eth_simulateV1` | 25 s |
| E | LP | top 2 pools in parallel: owners, locks, removal powers, pulled liquidity, v4 hook permissions | lp.py | 75 s (45 s per pool) |
| D | fork tests (fallback) | only if the chain lacks `eth_simulateV1` (avax, monad): run privileged fns + transfer tax on a fork | anvil | 75 s |
| H | hook info | hook source: owner-only fns, fee constants, hardcoded swap fee | explorer | 10 s |
| V | verdict | bad → NOT SAFE, warnings → CAUTION, else LOOKS SAFE; ANSWERS block built from all phases | — | — |

**`dd sim` order:** reads + functions + LP → probes → fork → RUG SIM on the main pool → baseline buy/sell → owner powers (each run, then sell retried; mint-like fns retried after each).

---

## 7. Configuration

| Item | Controls |
|---|---|
| `chains.json` | per chain: `id`, `symbol`, `rpc` (ordered fallbacks), `logs_rpc`, `logs_range`, `private_logs_range`, `rps`, `logs_recent_only`, `logs_unreliable`, `headers`, `explorer` (etherscan id / blockscout / routescan / web), `dexscreener` id, `v4` (pool_manager, posm, state_view), `v2_factories`, `v3_factories`, `wnative`, `fork_rpc`, `fork_cups` |
| `CLAUDE.md` / `AGENTS.md` | Claude/Codex behaviour (copies; edited together) |
| `keys/rpc_<chain>` | private RPC URL (first line), auto-used |
| `keys/etherscan` | Etherscan V2 API key |
| `keys/envio` | HyperSync token |
| `keys/chainstack_api` | Chainstack platform/MCP key |
| `keys/serialized` | Serialized Audit key (old toolkit) |
| `DD_DEBUG` | print phase timings to stderr |
| `DD_CACHE` | cache DB path (default `data/cache.db`) |
| `DD_ETHERSCAN_RPS` | Etherscan throttle (default 2.8) |
| `DD_SHOW` | tests: print full output on failure |
| `MAXC` | `csmcp.py` output length |
| CLI flags | `check`: `--deep`, `--json` |

---

## 8. Performance (measured on the regression set, 2026-10-16/17)

| Run | Typical time |
|---|---|
| `dd check` (quick) | 2–4 s on most tokens; PULP (busy v4 pools, Robinhood) ~13–17 s |
| `dd check --deep` | +6–15 s (sell tests; MILL ~16 s total) |
| `dd sim` | 4–20 s |
| `dd trace` | 0.4–1.3 s |
| full regression (10 cases) | ~80 s |
| honeypot fixtures (7) | ~45 s |

**Slowest steps:** v4 pools with thousands of liquidity events (log scan + position reads), fork transactions (~0.3 s each on HyperEVM, the sell matrix sends ~25), mining many blocks on a fork, first-time function-name lookups.

**Cost per check:** UNCONFIRMED in request units. Roughly: quick check ~100–600 RPC calls (most batched), a deep check adds a fork that fetches state on demand (UNCONFIRMED, likely 1–3k calls). Archive and trace calls count 2 RU on Chainstack. Etherscan, HyperSync, DexScreener, GoPlus, Relay, xscan are free at current use.

---

## 9. Known issues

**Broken / misleading**
- `bin/dd` help text is stale (mentions `--fast`, lists `lp`, `dev`, `cluster`, `tx`). Those four commands have no script → `dd lp …` fails.
- Unverified v4 hooks: the hook's own swap fee is only read from **verified** source (`fee = x * N / 10000` pattern). Unverified hooks (e.g. IMDO's) show no fee line; reading fee getters like `currentFeeBps()` was promised but **not implemented**.
- LP verdict says UNCLEAR for launchpad hooks that hold liquidity and have fee-claim functions (FloodHook, StandardLaunchHook) even though they can't remove liquidity; needs a human read of the hook.
- `removal_powers` in `dd check` only probes removal functions with no arguments; arg-guessing exists only in `dd sim`.

**Unreliable / limited**
- Sell tests on v3/v4 pools are token-level transfers, not real swaps; hook logic during sells isn't exercised.
- Delayed-honeypot coverage stops at +24 h; triggers based on dates, holder count, price or volume aren't tested.
- Control analysis: Safe modules/guards and timelock operation queues are not resolved (reported as "not resolved"); role discovery relies on events, known names and bytecode constants.
- Dev tracing follows only the launching wallet; testnet history only that wallet.
- Etherscan free tier gives no wallet history on base, bsc, op, avax, rh (HyperSync fills this).
- xscan only knows recent deploys on eth/base/bsc; its decompiler errors.
- GoPlus rate-limits bulk use; DexScreener misses some pools.
- Sonic, Optimism, MegaETH, X Layer run on public RPCs only (10-node Chainstack limit). X Layer has no explorer API.
- Regression cases use live tokens whose state changes (e.g. hXMAX lock, MILL); fixtures run only on a BSC fork.

**Unused / duplicated**
- `cmds/check.py` contains dead code (`if not fast and False: pass`).
- Worst-case argument builders exist twice (`check.sample_args`, `sim.sim_args`); baseline sell logic exists in both `cmds/sim.py` and `lib/sellsim.py`; the legacy fork path `phase_d` overlaps with `quicksim`.
- SELL BLOCKER can be reported by both the quick test and the deep test (partially de-duplicated).
- `AGENTS.md` is a manual copy of `CLAUDE.md` and can drift.
- `lib/csmcp.py` and `chains.json` `v2_factories`/`v3_factories` are not used by checks.
- Signature-lookup misses are cached forever, so a name added to openchain later is never picked up.

## 10. Changes 2026-10-17 (execution evidence, v4 hooks, code analysis)

| Module | What it adds | Tier |
|---|---|---|
| `lib/tracer.py` | `debug_traceCall` / `debug_traceTransaction` (callTracer + prestate diff) on Chainstack and anvil → storage written, outside calls, token/native moves, delegatecall/selfdestruct, liquidity-removal events | quick + deep + sim |
| `lib/quicksim.py` | Transfer events from `eth_simulateV1` (who takes the buy/sell cut); holder balance after each owner fn → silent drains | quick |
| `lib/v4hook.py` + `fixtures/src/V4Helper.sol` | real v4 swaps / liquidity through the PoolManager on a fork; exact hook cut (Swap event vs trader deltas); recipients; callback map; +1h sell; hook owner fns then re-sell | deep + sim |
| `lib/sellsim.py` | v4 pools: buys/sells are real PoolManager swaps (hook included) | deep |
| `lib/lp.py` | v4 scan restarts from the real Initialize block; never PULLED while pool liquidity > 0; contract/hook-held LP: removal-like fns traced (pulls liquidity vs fees only) | quick |
| `lib/who.py` | hook fee getters read live (works unverified) | quick |
| `lib/decomp.py` (Heimdall 0.9.3, `tools/heimdall`) | unverified bytecode → per-fn access / writes / calls / revert messages; cached per code hash | deep (cached → quick) |
| `lib/static.py` + `lib/static_worker.py` (Slither 0.11, solc-select) | verified source compiled from the explorer's standard-JSON → per-fn gate / writes / calls; "owner switches every transfer reads" | deep (cached → quick) |
| `cmds/check.py` | INCOMPLETE verdict when LP / honeypot / functions weren't checked; code findings marked "read from code" | quick |
| `lib/sim.py` | anvil `--prune-history 64` (forks left ~270 MB each on disk; filled 28 GB) | all forks |

Tests: `tests/run.py` (17 live cases), `tests/fixtures.py` (sell kinds), `tests/hooks.py` (malicious v4 hook: fee raise / honeypot / delayed), `tests/decomp.py`, `tests/roles.py`.
Not done (cut as low value / high complexity): pinning every read to one block; incremental indexes; holder analysis.

## 11. Changes 2026-10-17 (later): one argument builder, switch tests, fast deep check

| Module | What it adds |
|---|---|
| `lib/args.py` | the only test-argument builder (probe / small / worst; `variants()` retries capped setters); old duplicates removed |
| `lib/quicksim.py` | getters read before/after each owner fn in one simulation ("changes: sellFee 300 → 9900"); `simulate_blocks()` multi-block helper; readable revert reasons |
| `cmds/check.py` phase D | chains without `eth_simulateV1` run the same quick tests on a fork (no duplicate code) |
| `lib/switches.py` | every variable Slither says transfers read and a protected fn writes is set directly in storage, then a holder sells |
| `lib/fastsell.py` + `fixtures/src/SellHelper.sol` | deep sell tests as parallel `eth_simulateV1` requests (no fork): same scenarios and output as `lib/sellsim.py` |
| `lib/v4hook.py` `analyze_fast` | v4 hook sim without a fork; fork version kept as fallback |
| tests | `tests/all.py` (everything), `tests/pinned.py` (real hooks frozen at a past block), `tests/switches.py`; live cases split into stable vs drift; fork start retries once |

Deep check timings (before → after): IMDO 10.6 s → 5.0 s, Wheel Smith 13.7 s → 4.3 s, MILL 16–25 s → 4.0 s, BSC token 15.6 s → 2.9 s.

## 12. `dd code` (2026-10-17): read one function

`cmds/code.py` + `lib/slots.py`. `dd code <chain> <address> <function> [--from 0x..] [--args a,b]`.
Verified → exact source of the function, its modifiers and every internal function it reaches (Slither `code` mode in
`lib/static_worker.py`, cached), compiler layout + live values. Unverified → Heimdall pseudocode for that selector
(full decompile cached per code hash), hex selectors/custom errors decoded, `store_*` renamed from getters whose whole body
returns them. `lib/slots.py` proves each getter's slot/offset/value by RPC (eth_createAccessList → prestate trace → fork)
and names mapping keys (incl. struct fields). TRACE = callTracer + prestate + diff of one simulated call (node, or fork
when the node has no debug_*). Test: `tests/code.py`.
