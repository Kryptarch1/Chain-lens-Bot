# DD Assistant on Codex — Simple Build Plan

Goal: ask about a token / wallet / project, get a verified answer in seconds.
Rule for every piece: it must make the bot faster, more accurate, or more thorough.

---

## 1. Data: one provider per job, one fallback

| Job | Use | Plan | Why | Fallback |
|---|---|---|---|---|
| EVM reads, history, traces, big log scans | **Chainstack** | Growth $49/mo | Archive + trace, no rate walls, covers Robinhood/Arc/Monad that others skip | Public RPC per chain (in `chains.json`) |
| Explorer data: tx lists, holders, funded-by, verified source | **Etherscan API V2** | Free (Lite $49 only if BSC/Base aren't on free) | One key, ~60 chains, replaces all explorer scraping | Blockscout (ETH/Base/Arb/Ink), Routescan (Avax), OKLink (X Layer) |
| Fork simulation | **anvil** forking the Chainstack endpoint | — | Free, impersonate any wallet, dry-run hidden functions | Chainstack `eth_call` with state override for 1-shot sims |
| Pools & prices | DexScreener | Free | Finds every pool across chains in one call | GeckoTerminal |
| Quick risk flags | GoPlus | Free | Fast first pass; **never trusted alone** (missed ARCADE's hidden mint) | — |
| Bridge history | Relay API | Free | Cross-chain links for wallet clusters | — |
| Function names | Local 4byte/openchain dump (SQLite) | Free | Instant instead of ~1 s per lookup | openchain API |
| Web / dApp | curl + Playwright | Free | Recon pages and run the dApp with a burner | — |
| X | *decision needed* (see end) | — | — | — |

Skip: Alchemy/QuickNode (overlap Chainstack), Dune (too slow for live), labels (later).

## 2. Speed: how answers come back in seconds

- **One command per question, one compact result.** `dd check`, `dd dev`, `dd cluster`, `dd lp`, `dd sim`, `dd watch`. Codex never writes checks from scratch.
- **Everything parallel.** Python asyncio + batched JSON-RPC; all independent calls fire at once.
- **Cache in SQLite.** Bytecode, source, deployer, selectors: forever. Holders, prices: short TTL.
- **Persistent connections.** One HTTP/WebSocket session per provider, reused across calls.
- **Local signature DB.** Function naming is the single biggest hidden delay today.
- **Launch streams.** WebSocket `eth_subscribe` on Chainstack for factory/launchpad events (v2 PairCreated, v4 Initialize, known launchpads) → `launches` table. Each new launch is pre-checked in the background so the answer is already cached when you ask.
- **Fastest provider per chain** is set once in `chains.json`; fallback switches automatically on error.

Targets once built: token+LP check 15–30 s · dev wallet <60 s · cluster 1–2 min · "is X linked to Y" <1 min, complete.

## 3. Architecture

```
dd/
  AGENTS.md          Codex reads this every session: role, rules, tool map ("for X run Y")
  chains.json        per chain: id, primary RPC, fallback RPC, explorer API, block-range limit
  keys/              chmod 600; Chainstack, Etherscan. Burner keys RAM-only (/dev/shm)
  bin/dd             dispatcher: dd <cmd> <chain> <target>
  lib/
    rpc.py           pooled sessions, batch calls, auto-fallback
    explorer.py      Etherscan V2 + Blockscout/Routescan/OKLink behind one interface
    cache.py         SQLite cache + signature DB
    sim.py           anvil fork + impersonate, state-override eth_call
  cmds/
    check.py         token functions + hidden-power sims + tax fork test + LP owner/locker → verdict
    lp.py            V2/V3/V4/custom: who owns liquidity, can they pull it, locker history
    dev.py           funding chain, deploys, swaps+PnL, bridges, all chains
    cluster.py       top holders → shared funders, direct transfers, bridges, same-block buys
    sim.py           arbitrary dry-runs
    watch.py         systemd watchers → Telegram on LP pull / mint / owner change / dev sell
    recon.js         site + dApp crawl with read-only wallet
  stream/            launch listeners (EVM websocket) → cache.db
  tests/cases.json   known rugs + known safe tokens; rerun after every change
  cache.db
```

Flow: you ask → Codex picks the command from AGENTS.md → script runs calls in parallel (cache first, provider, fallback) → one compact result → Codex explains it with links and a verdict.

`AGENTS.md` content: the short DD prompt (verify on-chain, four questions, WORKS/BROKEN/SUSPICIOUS, say what wasn't checked, short answers), burner rules, "paid audits only on request", and the tool map.

## 4. Setup, in order

1. **Server:** 4 vCPU / 8 GB VPS, non-root user, SSH keys only, firewall. Install Foundry, Python 3 + uv, Node 20 + Playwright, sqlite3, Codex CLI.
2. **Keys:** Chainstack (free first, Growth when daily), Etherscan free → `keys/`.
3. **Project skeleton:** `AGENTS.md`, `chains.json`, `bin/dd`, `lib/` (rpc, explorer, cache). Download signature dump.
4. **Build `dd check` + `dd lp` first** (the daily question). Test on ARCADE, COOK, NOOP, ONDEX, hXMAX locker.
5. **Build `dd dev` and `dd cluster`.** Test on the MILL holders and the BSC wallet-link case.
6. **Watchers + launch streams** as systemd services with Telegram alerts.
7. **Regression set** in `tests/`; rerun after any change.
8. Measure each command's time; fix the slowest step first.

---

## Phase 2 — after the basic commands work

Design v1 so these slot in without rewrites.

1. **Verdicts in the scripts, not the model.** `dd check` prints the verdict + reasons itself; Codex relays and only reasons when something is unclear. (Cheap, big win: do right after step 4.)
2. **Known-bad template DB.** Store bytecode fingerprints of every confirmed rug (ARCADE hidden mint, XIMD/BRAIN kit, COOK-style LP manager…). New tokens are matched in milliseconds before any simulation.
3. **Warm simulations.** One anvil fork per main chain always running, reset per check; state-override `eth_call` for single-call tests. Saves 5–10 s per check.
4. **Local indexer.** Store every token + native transfer for tokens launched in the last ~30 days on your main chains. Cluster and wallet-link checks become local lookups (<5 s).
5. **Co-locate.** VPS in the same region as the Chainstack nodes.
6. **Later, budget permitting:** Etherscan Lite+ if the 5 calls/s free limit throttles cluster checks; a dedicated node for your main chain.

---

## Two decisions that are yours

1. **Etherscan Lite ($49)?** Only if BSC/Base turn out not to be on the free tier (check docs.etherscan.io/supported-chains). Without it, "every tx this wallet sent" on those chains stays incomplete.
2. **X access?** Options: a pay-per-request X data API (read-only, works 24/7 from the server), or skip X and paste links/screenshots. Codex has no browser-extension route like this chat had.
