# DD Bot — Fresh Build Plan (EVM, speed-first)

Goal: a Claude Code bot that answers "is this safe / who is behind it / does it work"
in seconds, verified on-chain, with blunt verdicts. Built from what actually slowed
the current bot down: scraping explorers, free-RPC limits, no history, and writing
every check by hand.

---

## Phase 0 — Server (day 1, ~1 hr)

- VPS: 4 vCPU / 8 GB RAM / 80 GB SSD, US-East or EU (close to RPC providers).
  The current 2 GB box is too small for forks + parallel checks.
- Ubuntu LTS, non-root user for the bot, SSH keys only, firewall (only SSH open),
  unattended security updates.
- Install: Foundry (cast/anvil), Node 20 + Playwright Chromium, Python 3 + `uv`,
  `jq`, `git`.
- Secrets: `/opt/dd/keys/` (chmod 600, owned by bot user). Burner keys live only in
  RAM (`/dev/shm`), never on disk, never in logs, never pasted in chat.

## Phase 1 — Data access (day 1, ~30 min)

| Priority | Get | Why |
|---|---|---|
| 1 | **Etherscan API V2 key** (free → paid if limits hit) | One key, ~60 chains: wallet history, token transfers, holders, deployer, verified source, "funded by". Replaces browser scraping. |
| 2 | **One paid RPC**: Alchemy (if mostly ETH/Base/Arb) or QuickNode (if BSC/HyperEVM) | Archive history, traces/internal transfers, big getLogs ranges, fast `eth_call`/simulation. |
| 3 | Free extras | DexScreener (pools), GoPlus (quick flags only — never trust alone), Relay API (bridge history), Blockscout/Routescan where Etherscan doesn't cover. |
| Later | Arkham-style labels | Auto-tag exchanges/market makers/known ruggers. Nice, not speed-critical. |

Small chains (Robinhood, X Layer, Ink, MegaETH) stay on public RPC + their explorer.
Keep one `chains.json`: chainId, RPC (paid → public fallback), explorer API, block-range limit.

## Phase 2 — Core tools (days 2–4, the biggest speed win)

All tools: parallel calls, cached results, one-screen output with a verdict line.

1. **`dd check <chain> <token>`** — target 15–30 s
   - Functions + names from a **local signature DB** (offline 4byte dump, instant)
   - Flag non-standard functions; owner/roles/proxy (1967, beacon, 1167, custom)
   - **Hidden-power test:** simulate each suspicious function from deployer vs a
     random address (catches disguised mints like `testSystemUpdate`)
   - Tax: fork buy + sell
   - LP: find pool, type (V2/V3/V4/custom), who owns liquidity, simulate removal
     by the controller, locker history (early withdrawals = fake lock), decoy burns
   - Output: SAFE / NOT SAFE / UNCLEAR + reasons + links
2. **`dd dev <wallet>`** — target <60 s
   - Funding chain (2–3 hops), deploys, swaps with PnL, bridge history, every EVM
     chain it's active on. Skips airdrops.
3. **`dd cluster <token> [--min 1%]`** — target 1–2 min
   - Top holders → shared funders, direct transfers on all chains, bridge links,
     same-block buys, fresh-wallet patterns → groups with total % and strength.
4. **`dd sim <chain>`** — fork with impersonation for any custom test.
5. **`dd watch <wallet|token|lp>`** — systemd watchers that alert (Telegram) on
   LP removal, mint, owner change, dev sells.
6. **`recon <site>`** — keep the existing site/dApp crawler for product tests.

## Phase 3 — The bot's brain (day 2, ~1 hr)

`CLAUDE.md` in the bot's project:
- Role + rules (the short DD prompt: verify on-chain, four questions, verdicts,
  short answers, say what wasn't checked).
- **Tool map:** "for X, run Y" so the bot never writes checks from scratch.
- Burner rules (state contract/function/approval before every tx, exact approvals,
  stop on drainer signs).
- Paid tools (credit-spending audits) only on explicit request.

Skills for repeat workflows: `token-check`, `lp-check`, `wallet-cluster`,
`dev-rundown`, `product-test`, `how-to-check` (explorer steps for learning).

## Phase 4 — Testing & accuracy (day 5)

- Regression set: known cases from this bot's history, re-run after every change.
  - Rugs/traps: ARCADE (hidden mint), COOK (dev LP manager), NOOP, XIMD/BRAIN (decoy
    burn + hook), DYAH, WARREN, BANK, PSTR, hXMAX locker (fake lock).
  - Safe: NESTED, OCTO, Vyper, Lamina, TREE, HALCYON.
- Every check must name what it couldn't verify.

## Phase 5 — Speed tuning (ongoing)

- Cache: signatures, contract code, verified source, deployer lookups.
- Parallelize every independent call; batch JSON-RPC.
- Keep fast mode on; keep a Chrome profile (no wallets) as the fallback for
  sites/X the APIs can't reach.

---

## First steps (do today)

1. Spin up the 8 GB VPS and harden it.
2. Get the Etherscan API V2 key; pick Alchemy or QuickNode based on where you hunt most.
3. Create the project with `CLAUDE.md` + `chains.json`, then build `dd check` first.

## Target speeds once built

| Check | Now | Target |
|---|---|---|
| Token + LP safety | 1–3 min | 15–30 s |
| Dev wallet rundown | several min, gaps | <60 s |
| Holder clusters | 10–15 min | 1–2 min |
| Cross-chain wallet link | often "couldn't check" | <1 min, complete |
