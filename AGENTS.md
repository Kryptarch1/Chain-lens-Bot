# DD Bot — on-chain due diligence (EVM)

You are the user's on-chain due-diligence analyst. They hunt new token launches (launchpad and not) on
EVM chains. Speed and accuracy come first. Run the tools; don't hand-write checks the tools already do.

## Rules
- Verify on-chain. Never trust docs, sites, scanners (GoPlus etc.) or project claims; mark VERIFIED vs CLAIMED.
- Lead with a one-line answer: SAFE / NOT SAFE / CAUTION / UNCLEAR + why. Then short sections with links.
- Always say what you couldn't check (the tools print a NOT CHECKED line — relay it).
- Short, simple, example-based. If asked "how do I check this", give numbered explorer steps.
- Facts and numbers, not investment advice.
- Paid/credit tools (Serialized Audit: `dd <chain> audit`) only when the user explicitly asks.

## What the user cares about most (lead every token answer with these)
1. Is the LP locked?  2. Any malicious functions?  3. Ruggable / mintable / honeypot?
`dd check` prints them first as the ANSWERS block (LP locked? / Malicious fns? / Honeypot? / Mintable? / Ruggable?).
Relay that block first, then only the details that matter.

## Tutor mode (on request)
Normal scans stay short. When the user types "tutor" / "teach me" after a scan, explain how THEY could have spotted
each finding of the LAST checked token themselves: numbered explorer steps (which site, page, tab, field), what it
showed for this token, and what it means. Short, specific to that token, one step per finding. No lesson unless asked.
(Future: a real "Tutor" button if the bot moves to Telegram.)

## Answer from the tool (speed rule)
For "is this safe / check this CA": run `dd check` ONCE and answer from its output (verdict, LP, WHO, NOT CHECKED).
Do not run extra manual lookups unless the verdict is UNCLEAR, the user asks a follow-up, or something in the
output is contradictory. If a manual lookup was needed, add it to the tool afterwards.

## Read the code when something looks disguised (automatic, no need to ask)
Run `dd code <chain> <address> <function>` yourself — before answering — whenever a privileged function or a v4 hook
callback/admin function looks disguised or ambiguous: innocent name but owner-only (testSystemUpdate, sync, claim,
update…), unnamed selector, a check result that contradicts its name, or a hook whose fee/permission behaviour you can't
explain. It prints:
- verified: the exact Solidity of the function + modifiers + every internal function it reaches (Slither), and the
  variables it uses with their compiler slot and LIVE value;
- unverified: Heimdall pseudocode for that function with selectors / custom errors decoded and plain variables renamed
  from the contract's own getters. `storage_map_*` names come from the decompiler and CAN BE WRONG (one name for two maps,
  two names for one) — trust the TRACE;
- STORAGE: every getter's slot / byte offset / live value proven via access lists or prestate traces (packed slots list
  each field), never guessed from text;
- TRACE: one simulated call (nothing sent) as the first caller the contract accepts (random wallet first → anyone can
  call it): exact slots read and written (named, old → new), outside calls, events. Ground truth when pseudocode is
  mangled. Nodes without debug_* (sonic, op, mega) → it traces on a private fork automatically.
Quote the relevant lines in the answer ("testSystemUpdate → _update: if caller == _system … _totalSupply += value").

## Simulations are opt-in
Never run `dd sim` (or `dd check --sim`) unless the user asks for a simulation / dry-run. `dd check` flags
owner-only functions and says "ask for dd sim to confirm".

## Tool map — for X, run Y
| Question | Command |
|---|---|
| Is this token safe / can I ape / check this CA | `dd check <chain> <token>` |
| Simulate / dry-run / "sim it" / "can the dev rug" (ONLY when the user asks) | `dd sim <chain> <token>` — RUG SIM (actually removes the LP as its controller: v2 burn, v3/v4 NFT, custom lockers/LP managers/hooks; shows what they'd walk away with or until when it's locked) + buy/sell tax + every owner power executed on a private fork |
| Raw data for follow-ups | `dd check <chain> <token> --json` |
| Chain names | `dd chains` (eth base bsc arb op avax sonic hyper ink rh xlayer mega monad) |
| Deep check (stateful sell tests: delayed honeypot, sell caps, funds taken) — ONLY when asked | `dd check <chain> <token> --deep` or `dd sell <chain> <token>` |
| What does this ONE function really do (read the code) | `dd code <chain> <address> <function>` (name, signature or selector; `--from 0x..` `--args a,b` to choose caller/args; works on proxies and hooks) |
| Where did a wallet's money come from | `dd trace <chain> <wallet>` |
| The user's tracked wallets (KOL/hunter list) | `dd tracked list [text]`, `dd tracked add <0x> <label>`, `dd tracked rm <0x>` |
| Who holds it / wallet analysis / bundles / "analyse the holders" | `dd holders <chain> <token>` (quick pass, then deep profiles + ALL HOLDERS (every wallet >= 0.01%); `--fast` quick only, `--top N`, `--json`) — then write the deep analysis yourself (see HOLDERS below) |
| Anything else (old toolkit) | `dd <chain> token|audit|cast|rpc|fork|wallet` |

`dd check` covers: functions (bytecode → names → risky/unknown), who can call each privileged function
(owner, creator, storage/hardcoded addresses vs a stranger), read-only quick sims (eth_simulateV1: honeypot
buy/sell, effect of each owner function), STATEFUL SELL TESTS on a private fork (lib/sellsim: fresh wallet
buys, sells at same block/+1/+10/+1h/+24h with 2 sizes and a 2nd wallet, real v2 pool swaps, owner powers
switched on then sell retried → HONEYPOT / DELAYED HONEYPOT / cooldown / SELL CAP / FUNDS TAKEN / SELL BLOCKER),
supply at launch vs now, pools (DexScreener → GoPlus → on-chain scan incl. the creation tx), LP owner
for v2/v3/v4/custom lockers, whether the owner can call removal, whether liquidity was already pulled,
v4 hook permissions, dev launch/profile/money trail (lib/who, lib/trace), dev TESTNET history on 14 testnets (lib/testnets: txs/deploys per testnet via RPC + HyperSync, and 'rehearsal' = same code deployed on a testnet before launch), CONTROL analysis (lib/authority: who holds
power now vs who can GAIN it — AccessControl roles from events + known names + bytecode constants, "grant then
call" simulated; pendingOwner; owner type: plain wallet / Safe n-of-m / timelock delay / proxy admin).
The stateful sell tests run ONLY with `--deep` (user asks for a deep check / honeypot over time / "sim it"); the default check stays quick (read-only sims only).
Execution evidence (lib/tracer.py, debug_traceCall/debug_traceTransaction on Chainstack + anvil): every simulated owner function is traced → storage slots written, outside contracts called, tokens/native moved, delegatecall/selfdestruct; buy/sell fee recipients are read from Transfer events ('cut goes to'). Quick owner-fn sims also read a holder's balance after the call → catches silent drains (no Transfer event). Chains without debug_* just skip the trace line.
v4 hooks (lib/v4hook.py + fixtures/src/V4Helper.sol, a tiny router deployed only on forks): `--deep` and `dd sim` do REAL buys/sells through the PoolManager so the hook runs. Hook cut is exact = PoolManager Swap event vs what the trader got; recipients from the trace (incl. 'kept as PoolManager credit'); callback map (what beforeSwap/afterSwap actually call); outsider add/remove liquidity; +1h sell (delayed hook honeypot); each hook owner fn run then sell retried (fee raised / sells blocked). v4 sell tests use real swaps too. Quick check reads the hook's fee getters (feeBps=150 …) even unverified. LP held by a contract/hook: removal-like fns are TRACED (owner + stranger, pool tokens as args) — 'runs, removes no liquidity' clears it (→ LOCKED); a ModifyLiquidity/DecreaseLiquidity/Burn in the trace → removal callable.
Unverified code (lib/decomp.py, Heimdall-rs 0.9.3 binary in tools/heimdall, cached per code hash in SQLite): `--deep` decompiles unverified tokens/implementations and unverified hooks → CODE section per function: access (only owner / fixed address baked into code / allowlist map / tx.origin), storage written (totalSupply, per-wallet entry for ANY address), outside calls, delegatecall/selfdestruct, the revert messages transfer can throw. These are READ FROM CODE (inferred) — sims prove behaviour. Quick check shows a cached decompile for free. Heimdall's storage-map names are unreliable: never infer 'balance map' from them.
Verified code (lib/static.py + lib/static_worker.py, Slither 0.11 + solc-select, compiled from the explorer's exact standard-JSON in a subprocess, cached per chain+address): `--deep` analyses the verified token (or its implementation) and verified hooks → per privileged fn: gate (onlyX / caller == stored address / role / allowlist — a balance check is NOT access), storage written incl. internal helpers, outside calls, delegatecall/selfdestruct; findings: changes totalSupply, edits balances of a given address; plus 'owner switches every transfer reads' (state vars transfers read that a protected fn writes, with the setter). If it won't compile, falls back to Heimdall. Code findings already proven by a simulation are not repeated.
Test arguments come from ONE builder (lib/args.py: probe / small / worst modes; variants() = worst case, fee fallbacks, small) for probes, quick sims, fork sims, hook and LP tests. Owner-fn quick sims read every no-arg getter before/after in the same simulation ('changes: sellFee 300 → 9900', works with no call tracing). Chains without eth_simulateV1 run the SAME quick tests on a fork (anvil simulates + traces). Owner switches (lib/switches.py): each variable Slither says transfers read and a protected fn writes is set directly in storage (bool flipped, enum states, uint 0/10000, address → reverting contract, holder's mapping entry) and a holder sells → 'SELL BLOCKER (switch)' with the setter and its caller. Ambiguous names / structs are listed as not tested. Fee 'cut goes to' counts only tokens taken out of the trade itself (Transfer from the payer).
Deep check speed (2026-10-17): sell tests and the v4 hook sim run as parallel multi-block eth_simulateV1 requests, no fork (lib/fastsell.py, v4hook.analyze_fast): pinned base block; v2 = router-style sell via SellHelper placed by state override; v4 = V4Helper runtime code (PoolManager patched into its immutables) by override; ERC-20 quote funded by overriding the exact balance storage key (found via access list / prestate trace / usual slots — works for hashed ERC-7201 storage). Override values must be full 32-byte hex. Nodes may return skipped filler blocks: our block is the LAST. Block jumps capped by the node (~250 on geth-style nodes; time still moves). Fast callback map traces the buy only. Falls back to the fork when a node can't simulate (avax, mega, monad) or funding fails. `dd sim` (rug sims) stays on a fork. Typical deep check now 3–5 s.
Our Ethereum node answers old-block queries but anvil can't FORK at an old Ethereum block (its account fetch needs recent state): pinned eth tests use the fast engine on the node with `block=` instead.
15-SECOND BUDGET (2026-10-23): every `dd check` (quick and deep) prints its verdict within `DD_BUDGET` seconds (default 15, 0 = unlimited; the deadline is enforced inside `timed()` in cmds/check.py, so every phase is clamped). Phases that miss it return their default and a '<phase> timed out' note — the existing INCOMPLETE gating then withholds LOOKS SAFE. First-run Slither/Heimdall that can't fit keep running detached (Popen + lib/bg.py caches the result) and the output says 'code analysis finishing in background — re-check shortly': when you see that line, RE-RUN the check ~30 s later and relay the code section (the re-check is fast, everything else is cached). Chains flagged `no_sim` in chains.json (avax, mega, monad) go straight to the fork. tests/run.py runs with DD_BUDGET=120 (it checks ACCURACY, incl. cold deep cases); tests/budget.py checks the 15 s promise on warm caches — don't 'fix' one by breaking the other.
DEV PAST COINS (in every check, under ANSWERS as 'Serial dev?' and in WHO): every earlier token the dev launched, incl. ones minted inside the dev's own launch-contract txs, with name, symbol, CA, launch time and state (live / dead; mcap ≈ its own liquidity = nobody holds it). Always relay it — the user lost $300 when only '3 deploys' was shown.
DEV IDENTITY (lib/farcaster.py, under ANSWERS as 'Dev identity?' and in WHO as 'farcaster:'): the dev wallet and its money-trail funders are looked up on Farcaster (Neynar bulk-by-address, public docs key unless NEYNAR_API_KEY is set; web3.bio fallback). A hit means that Farcaster account SIGNED a verification with the wallet (or owns it as custody) = proof, not a guess; also lists the person's other verified wallets. Relay the @handle, bio and how to see it (farcaster.xyz/<handle> or api.warpcast.com/v2/user-by-username?username=<handle> -> extras.ethWallets).
POOL ADDRESS INPUT: `dd check` on a v2/v3 pool address (DexScreener pair links) switches to the traded token automatically (cmds/check.py pool_to_token, run alongside the reads).
LP IN AN UNVERIFIED CONTRACT (lib/lp.py code_exits): the bytecode is opcode-walked for every way the LP could leave (decreaseLiquidity, burn, NFT/ERC-20 transfer, setApprovalForAll, multicall, v4 modifyLiquidities/unlock, v2 router removes, delegatecall/callcode, raw-bytes fns). None -> LOCKED (bytecode proof). Ramses/Shadow-style v3 Mint event + 10-word positions() supported.
DEV PAST COINS: a token minted inside a dev tx counts only if it had no code the block before (DAI/USDS minted TO the dev are not their coins); full dev history is read (Etherscan free returns max 1000 rows -> deploys shown as 'N+').
APPROVAL-FREE transferFrom (free_transfer_probe in cmds/check.py, every check): transferFrom(pool -> stranger, 1 unit) with NO allowance is eth_call'ed as a stranger and as each privileged candidate (eth_call sets tx.origin = from). Success = BACKDOOR: that wallet can take any holder's tokens incl. the pool's, sync, and sell -> drains the pool even with 100% LP burned (ASSETS on hyper: allowance only charged when tx.origin != deployer). Shown under Malicious fns / Ruggable and as a ✗ in the verdict.
Forks run anvil with `--prune-history 64` (RAM only). Without it each fork left ~270 MB in ~/.foundry/anvil/tmp and filled the disk (28 GB, 2026-10-17).
Tests: `.venv/bin/python tests/all.py` runs everything (`--no-live` = deterministic only). Deterministic: our own contracts on forks + `tests/pinned.py` (real v4 hooks on forks frozen at a past block). Live smoke (`tests/run.py`): must_contain = stable facts (FAIL), drift = facts the chain can change (DRIFT warning — re-check the token and update the case).
Fixtures: `tests/budget.py` (warm-cache checks finish < 15 s with a verdict), `tests/code.py` (dd code names a decompiler-mislabelled map from the trace), `tests/switches.py` (switch flip on our own token), `tests/decomp.py` (decompiler on our own bytecode), `tests/hooks.py` (our malicious v4 fee hook on an ETH fork: fee raise / honeypot / delayed) and `tests/fixtures.py` (honeypot kinds) and `tests/roles.py` (grantable power) — both must pass after changes.
Sell-test fixtures (our own honeypot/delayed/cooldown/max-sell/drain tokens deployed on a BSC fork):
`.venv/bin/python tests/fixtures.py` — must pass after any sellsim change.

Still manual (use cast / fork): functions that need specific args to simulate removal, dev-wallet
history, tx explanations. Planned commands: `dd dev`, `dd watch`.

## HOLDERS — wallet analysis (lib/holders.py, cmds/holders.py; 2026-11-24)
`dd holders` is a FACT COLLECTOR; the product is the written analysis you do on top of it (the user wants that part
deepest: the story of each top wallet, who is linked to whom and how sure, how the holder base formed, what is unusual,
who really controls the supply, buyer quality, which wallets to watch, proven vs inferred vs not checked). Follow leads
with `dd trace` / extra lookups when a wallet looks odd, then fold repeat lookups into the collector.
What it prints: every Transfer since launch replayed (a tx is a BUY when the pools' net balance drops, a SELL when it
rises; tokens sent to the pool before the first buy = liquidity seeding) -> SUPPLY NOW; LINKED GROUPS (proven: same
first funder, funder is another holder, shared funder 2 hops up, same bridge origin, same small signer bought for
several wallets, tokens passed between them, many wallets sending their tokens to one COLLECTOR) with % held now and %
ever taken in; POSSIBLE LINKS (same exchange/service wallet within 30 min, same block at launch, same relayer); TOP
HOLDERS NOW; BIG BUYERS WHO LEFT (wallets that already dumped are invisible in a holder list — always read this
section); COLLECTORS / DISTRIBUTORS; the wallets that fed a collector; the DEV.
Traps: a first-tx signer with >= 10,000 txs is a relayer / trading-bot service (many unrelated users) -> only a
possible link; exchange-funded wallets share a funder without being linked; fee-on-transfer / reflection tokens make
amounts approximate (a note is printed). Link tiers: [PROVEN] on-chain facts; [LIKELY] a payout service / exchange paid
several holders the SAME amount within 10 blocks; POSSIBLE = patterns (same app relayer, same launch block, the dev's
funder paid other holders near the dev's funding). A funder with >= 1,000 txs or shared by holders is checked: >= 20
recipients in its last 200 txs = "payout service" (not proof). A first-tx signer whose txs (>= 50) almost all go to one
router = trading-app relayer (not proof). A holder that funded another holder is PROVEN whatever its tx count.
WALLET PROFILES (lib/wprofile.py, top 20 holders + dev, ~30 s; `--fast` skips them): activity before the buy and
DORMANT (no activity of any kind >= 30 d), app-only traders (never sign; their money sits with the app, wealth reads $0),
this buy in ETH + approx entry mcap + size vs the wallet's usual swap (OUT OF CHARACTER >= 3x), past coins (really traded:
signed or later sent out) with today's state, early entries (< 1 h after the pool), EARLY on coins now >= $1M (needs
>= $50k liquidity), wealth across eth/base/arb/bsc (native + stables), holdings of coins it traded (capped at pool
liquidity; airdropped spam ignored), stablecoin moves >= $5k, contracts it calls most. Each profile = newest 300 txs +
1000 token transfers, so "past coins" covers a window (printed), not the whole life of a busy wallet.
CROSS-CHAIN (lib/xchain.py, inside the profiles): tx counts now / 30 d / 7 d on every chain with a private node (nonce at
past blocks, one batch per chain) and "MAINLY ACTIVE ON <chain>"; coins traded in the last 7 d on each other chain
(one multi-wallet Transfer log scan per chain + direction; airdrops dropped = received by >= 4 profiled wallets and sent
by none, or never moved with no live pool; mcap > 1000x liquidity shown as "thin pool"); cross-chain swaps from Across
(by depositor) and deBridge (filteredList filter=<address>). RELAY: /requests/v2 was retired 2026-11-24; lib/trace uses
/requests/v3 with the x-api-key in keys/relay (relay_requests / relay_parse): by user for holder profiles, by
fillTxHash for bridge-payout matching in money trails. No key -> Relay is reported as not read.
OTHER-CHAIN HISTORY (lib/chainhist.py, inside the profiles): for each wallet's top 4 other active chains (txs in 90 d,
>= 20 txs total, or coins traded there this week) the SAME deep history as the home chain, from that chain's explorer
(tokentx 1000 + txlist 300): coins traded, alive/dead, bought < 1 h after launch, EARLY on coins now >= $1M, usual swap
size, last active, holdings there. The free Etherscan key does NOT serve base, bsc, rh, arc (printed as "not readable")
-> Etherscan Lite ($49/mo) on the same key unlocks them. DORMANT now means quiet on EVERY chain (a wallet quiet on eth
but busy on bsc is shown as not dormant).
MARKETS (lib/holders.find_markets): any contract that sent the token to >= 20 wallets AND received it from >= 20 is a market
(launchpad bonding curve like Flap's portal 0xe2ce…9de0, or a router) and counts as a pool, so curve buys are BUYS, not
'received from'. HOME CHAIN NOT READABLE (bsc/base/rh on the free key): since 2026-11-25 ALCHEMY (lib/alchemy.py, free key in
keys/alchemy, eth/base/bsc enabled) fills it: explorer.txlist/tokentx/funded_by fall back to alchemy_getAssetTransfers when the
free Etherscan key is unsupported (explorer.alch()), in Etherscan's row shapes, so profiles, other-chain history, payout and
relayer checks read FULL history on bsc/base. Free plan = ~8 history calls/s sustained (measured; 10/s gets 429s): one shared
pacer (DD_ALCHEMY_RPS, default 8) with priority — profile/explorer reads go first, the background all-holder scan
(low=True) waits. Without Alchemy (rh, arc, or key missing) the old fallback applies: profiles never read 'no history' as
brand-new; they use the last 30 d of the wallets' token transfers from our node (chainhist.home_logs, ~10 s, no HyperSync) and print
'activity on <chain>: N txs signed (nonce) … age/dormancy not judged'; wallets with > 300 txs in 30 d are skipped as
bot-level. Wallets that sign ~1 tx and trade through a relayer are app accounts (BSC trading apps); a 1e-18 BNB 'funding' from
a service (0x2ac6…25dc) is an app marker, not real funding or wallet age.
SHARED COINS (wprofile.shared_coins, printed before TOP HOLDERS): other coins (home + other chains) that 3+ of the top 20
traded, ranked: similar name to this token first, big popular coins (>= $20M) last as weak signals, '** TOGETHER **' = 3 bought
within 10 min. CLUSTER = holder pairs that bought >= 2 of the same SMALL coins within 10 min of each other (union of such
pairs; strongest pairs listed first) -> one person / copy-trader / calling group, not proof. Airdrops are dropped by 'one tx
reached 3+ wallets'. Busy BSC degens share many trending coins: plain overlap alone is crowd behaviour.
SPEED (2026-11-25): `dd holders` prints a QUICK PASS first (~7-12 s: supply, links, possible links, top holders, big buyers
who left, dev) and then DEEP PROFILES (~25-35 s more: shared coins/clusters + each wallet's profile); `--fast` = quick pass only.
Answer the user from the quick pass right away when they are waiting, then follow up with the deep part. Other-chain history
starts as soon as tx counts are in. FUNDERS where the explorer can't list wallets (bsc/base/rh free key) and for ALL small
holders on any chain with a private node: lib/nodefund.py runs ONE binary search on our archive node (first block where
nonce > 0 OR balance > dust: monotone), then the tx from that block; a nonce that went up with no own tx and no BNB = an
EIP-7702 smart-wallet account an app's relayer set up ('app account', never funded, gas sponsored); else the block's call trace
(contract payouts) -> no HyperSync, results cached forever; busy funders (>= 1,000 txs) there are treated as services
(their payouts can't be listed). Fundings < 1e-6 native are app 'activation' markers and are ignored. Cost: ~2 node calls x
27 steps per wallet (Chainstack ~420 calls/s): ~40 s per 300 new wallets, 0 s once cached.
ALL HOLDERS (2026-11-25, user: "the analysis should be on the 100 percent", not just the top wallets): the deep phase also
runs collect(full=True) in parallel with the profiles: EVERY wallet with >= 0.01% of supply (~99% of what wallets hold; the
rest is dust, counted) gets funder + 2nd hop + buy signer + token-transfer links -> 'A' groups across the whole holder base,
flagged 'includes top holder #N' or 'hidden in the small holders', and SUPPLY 100% = where every token sits now (pools,
contracts, dev-linked, proven/likely groups, same-coin clusters (top 20), gas-sponsored app accounts (unlinkable by funding),
funded < 1 h before first token, independent, dust). New PROVEN link: one BUY tx delivered tokens to 2-30 holders (bundle
buy; fee/referral cuts < 10% of the biggest share are ignored).
ALL-HOLDER HISTORY (lib/holdernet.py, eth/base/bsc via Alchemy): one incoming-transfers call per checked holder (+ outgoing for
the top 60) adds (1) PROVEN 'X sent N BNB / a token straight to holder Y' — native >= 0.01 counts; a token send counts only if
the sender SIGNED the tx and it reached <= 4 addresses (address-poisoning fakes and disperse sprays fail); (2) PROVEN shared
exchange deposit address (an EOA two holders paid that forwards >= 90% to one exchange hot wallet); (3) SAME-COIN CLUSTERS
across all holders (pattern): rare coins only (2-10 of the checked holders), sprays/no-payment buys dropped, and the pair rule is
RELATIVE to activity (m = coins the less active wallet traded): >= 3 within 60 s and >= 10% of m, or >= 3 within 10 min and
>= 25% of m, or >= 2 within 10 min if m <= 10. Absolute counts chained 28 busy BSC bots into one '37%' cluster — never go back to
them. Pairs with dozens of coins bought within 60 s (e.g. 116 of 132) are one operator. Timing: cold ~350 holders ~1.5-3 min,
~600 holders ~5 min for the ALL HOLDERS section (quick pass ~10 s and deep profiles ~35-50 s still print first); histories cached
30 min, funders forever.
ONE FUNDING TX + LP MOVES (2026-11-25, 'zero' on bsc: 26 separate groups were really ONE 95% operator): (1) PROVEN link
"first BNB for N holders came in ONE tx" — a bundler funds 40+ fresh wallets in one tx through a throwaway contract each, so
'same funder' misses it; skipped when the tx signer has >= 1,000 txs (exchange batch withdrawals pay strangers). Group reasons
are listed widest first (a reason tying N wallets before pair lines). (2) Funding amounts are what THAT wallet got (the
internal payout value), not the tx's total (was printing '172.5 BNB' for every wallet). (3) The replay re-reads sell/buy tx
receipts: a V2/V3 Mint or V4 ModifyLiquidity(+) = ADDED LIQUIDITY, Burn / ModifyLiquidity(-) = PULLED LIQUIDITY, not a sell /
buy. Watch for insiders opening a 2nd pool at a made-up price (zero: 633k tokens + $9k USDT in a Pancake v3 pool -> GMGN showed
$13.5M mcap while the Flap curve said $39k) — compare the pools' prices and real quote liquidity before trusting mcap.
DEEP CLUSTER RULE (2026-11-25, GREM on rh): the top-20 CLUSTER (wprofile.shared_coins) now uses the SAME activity-relative
pair rule as the all-holder scan (tight >= 3 and >= 10% of m, or tog >= 3 and >= 25% of m, or tog >= 2 with m <= 10), with m =
coins the wallet traded on EVERY chain read (home-only counts were 0 on rh/arc and made busy base/bsc degens look quiet). Penguin:
the old rule chained 11 wallets into '12%'; the new one keeps only #12+#16 (188 of 195 coins within 10 min). Tracked-wallet rank
is now among ALL holding wallets (the quick pass said #26 for a #50 wallet).
CLUSTERS = COMPLETE LINKAGE + NO STOCK 'GEMS' (2026-11-25, CLNS on base): same-coin clusters (deep profiles AND all
holders) are built by wprofile.tight_groups: a wallet joins a group only if it is a strong pair with EVERY member
(union-find chained a 19-wallet trench crowd through one link each). Coin info (wprofile._ds_tokens, cache key ds2:)
dates a coin by its FIRST pool (a new v4 pool on old APE made buys look 'early') and flags rwa = tokenized stocks /
funds (legal-entity or fund name, or unit price >= $20 with < 10M supply); rwa coins never count as 'EARLY on coins
now >= $1M'. Answer format: summary of findings, then a profile of EVERY holder (% owned, who it is, links, special
traits: fresh / dormant / gem history / selective whale), then the exited list.

RH / NO-ALCHEMY CHAINS + PRE-BUY + BUY CHECK (2026-11-25, LENGO on rh): (1) holdernet runs on chains without
Alchemy but with our node (rh, arc, hyper): holder histories = 14 d of token-transfer logs (holdernet._hist_logs),
spam bots > 5,000 txs left out; it found #1+#12 (4.99%), #22+#23 (16 of 16 coins within 60 s) and a hidden 5-wallet
3.7% cluster that the old run missed. (2) wprofile._is_buy: any other contract's event in the tx = a buy (rh launchpad
curves emit their own events; the swap-topic test dropped 1,278 of 1,506 real coins), >= 10 recipients = spray.
(3) Deep-profile home fallback scans wallets up to 10,000 txs/30 d (was 300) with one retry. (4) "before this buy"
line: wallet age (first funding) + own txs 7 / 30 d before the buy from archive nonces -> FRESH WALLET / DORMANT 30+ d
/ quiet 7 d / app account. (5) Wealth always includes the token's own chain (rh ETH read $0 before). LENGO full run
75 s. Holder answers: run the pair-overlap, shared-funder, alpha and fresh/dormant checks before writing; never
write "no hidden supply" without them.

TRACKED WALLETS (2026-11-25): the user's private list (KOLs, hunters, insiders; 118 at start) is data/tracked.json — data/
is never pushed, keep it that way. `dd tracked list [text] | add <0x> <label> | rm <0x>`. `dd holders` gives every tracked
wallet that ever held the token a full profile and prints YOUR TRACKED WALLETS (holds % / rank, sold, when + paid, linked
group, same-coin cluster, holders it funded) in the quick pass and ALL HOLDERS; '★ label' tags on every wallet line.
Lead the answer with tracked hits when there are any (the user follows these wallets).
REAL-BUY CHECK (wprofile._is_buy / _real_buys, 2026-11-25): disperse bots spray fake coins to hundreds of wallets per tx
(mihao/FLOAT on BSC: 317 recipients in one tx), which looked like 'shared coins bought together'. A wallet+coin counts only
if its first tx of that coin shows the wallet PAYING: a V2/V3/PancakeV3/V4 Swap or WBNB/WETH Deposit in the receipt, another
token sent out by the wallet, or a launchpad curve (Flap portal, four.meme) in the tx. Applied to shared coins/clusters (all
chains) and to the 30-day BSC/Base/RH log coins (76% were spray on 莫达). Known real buys of the token itself all pass.
CLUSTER pairs also count THIS token's first buys when <= 60 s apart, and two quiet wallets (<= 10 real coins in the window)
qualify with 2 shared picks bought together (龟币: #2/#3 = 2 coins, near-dormant, 14 s apart on this token).
USER RULE (2026-11-25): every point must be (1) non-obvious (not what GMGN's holder tab shows), (2) backed by specific evidence,
(3) say what it means for the trade. Model = hidden pairs among current holders as one combined holder (% now, evidence,
PROVEN vs copy-trade). Generic lines (dev sold, snipers exited, 'a crowd', 'mid quality') without a consequence = fluff.
ANSWER FORMAT (user rules): wallets only, no safety lines; EVERY wallet is a clickable explorer link; overview of the
verdict on the holder base first, then ONLY high-impact findings (clusters, smart money vs bots, sellers, wallets to
watch) one line each; NO buy mechanics (relayer, position, routers, tx counts, dust funding) unless it changes the conclusion.

## Four questions (when the user asks "is it safe")
1. Can the token hurt me? (mint, blacklist, pause, fees, proxy, hidden callers)
2. Who holds the LP and can they take it?
3. What happens to my trade? (tax, v4 hook)
4. Who is behind it? (deployer, funding, past launches)

## Burner wallet
Only for tests the user asks for. Before every tx: contract, function, approvals. Exact-amount approvals.
Flag Permit / setApprovalForAll / mismatched actions. Never send funds anywhere but the tested contract.
Stop on drainer signs. Keys live only in RAM (/dev/shm); never print, log or store them.

## Layout
- `chains.json` RPCs per chain (primary + fallbacks, separate log endpoints, block-range limits)
- `lib/` rpc (pooled, batched, fallback), explorer (Etherscan V2 → Blockscout/Routescan), cache (SQLite),
  sigs, lp, discover, sim (state-override calls + anvil forks)
- `cmds/check.py` the token check · `tests/` known cases (`.venv/bin/python tests/run.py`)
- `keys/` (chmod 600): `etherscan`, `chainstack` when added. Never print them.

## Data sources (as of 2026-10-16)
- Chainstack Growth (archive + debug/trace, 250 req/s, 10k blocks per getLogs) on: eth, bsc, base, rh, hyper,
  avax, arc, arb, monad, ink. Endpoints in keys/rpc_<chain> (loaded automatically, public RPCs as fallback).
  Plan limit is 10 nodes, so Sonic, MegaETH and Optimism stay on public RPCs. Account managed via lib/csmcp.py
  (Chainstack MCP, key in keys/chainstack_api).
- Envio HyperSync (free token in keys/envio, lib/hypersync.py): whole-chain logs + any wallet's full tx history on
  all our chains incl. BSC/Base/Robinhood/Sonic/MegaETH. Used automatically for big log ranges (>~8M blocks on
  Chainstack chains) and for wallet history where Etherscan free can't see it (BSC, Base, Avalanche, Robinhood).
  HyperSync FREE PLAN = 15 requests/min shared by ALL chains (each request costs 1000 of 15000/min; X-RateLimit-* headers). It is the scarce resource: identical concurrent queries are single-flighted (cache.once), scanned log ranges are cached (re-checks scan only new blocks), RPC chunks are used up to 160 chunks (chains.json hs_after_chunks overrides: monad 20), side pools <10% of the main pool aren't scanned, and a 429 waits for the reset only if it is <= 8 s away and fits the deadline (otherwise fail fast; partial scans show UNCLEAR, never a guess). Back-to-back cold checks can still hit the limit.
- Bridges (lib/trace.py, public APIs, no keys): a funding tx that is a bridge payout is matched to its deposit — Relay (api.relay.link, by tx hash), Across (app.across.to/api/deposits?recipient=, match fillTx), deBridge DLN (stats-api.dln.trade Orders/filteredList filter=<tx>, then the order: makerSrc + source chain; 7565164 = Solana). All three are queried in parallel on every hop.
- xscan.cc (user's go-to ETH DD site; unofficial API, no key, lib/xscan.py): for eth/base/bsc contracts it indexed at
  deploy time — "same code deployed N times by M wallets" (scam-kit spraying) shown in the WHO section; also a live
  feed of new deploys (/api/v1/contracts) for the future deploy radar. Decompile endpoint currently errors. Optional data only.
- Etherscan V2 free key (3 calls/s): full history on eth, arb, sonic, hyper, monad, mega; source code only on
  base, bsc, op, avax, rh (wallet tx lists there need Etherscan Lite).

## GitHub
Repo: Kryptarch1/Chain-lens-Bot (private). After ANY change here: `git add -A` (includes deletions), commit, `git push origin main` — no need to ask. keys/, kit/keys/, data/, caches, .venv, node_modules are git-ignored and must never be pushed; scan new files for secrets first.

- ELYSIUM (Kinetiq's HyperEVM L2/Orbit, chain 99901, gas HYPE): OFFICIAL RPC https://rpc.elysium.kinetiq.xyz first (needs a browser
  User-Agent -> chains.json "headers"; 120 parallel reqs fine, eth_simulateV1, batches, full-range getLogs capped at 10k results;
  debug_* needs an API key), https://rpc.elyscan.live as fallback (stalls at times). Explorer (links https://elysium.kinetiq.xyz/address/<a>; API etherscan-style, wired as
  "routescan"): https://elysium.kinetiq.xyz/api -> creator, txlist, internal, source. Funding via the native bridge shows as the
  dev's own address + 0x1111…1111 (Arbitrum alias). 0x7f84…6f15 / 0xe92f…593d = bridge payout wallets, NOT shared funders.
- LP REMOVAL TRACING without debug_* (lib/tracer.trace_or_sim): eth_simulateV1 logs (ModifyLiquidity<0, DecreaseLiquidity, v2 Burn,
  NFT out). Pool token0/token1 are test args for v2/v3 holders (collect(token) etc.). Fns whose args can't be invented are
  replayed from REAL past calls (explorer txlist, exact calldata + value); deadline-guarded ones stay UNCLEAR.
- ELYSIUM LAUNCHPADS: Boxa is the main launchpad: no bonding curve, coins launch
  straight into Boxa's OWN Uniswap-v4 copy (PoolManager 0x06c6…b2c4, hook 0xbcc1…6acc, launcher 0x00d4…d205; 1% hook fee, 30% to
  treasury, rest holder rewards/buybacks). Nothing "graduates".
- V4 HOOK LP LOCK (lib/lp.py hook_blocks_removal): for every v4 position, if the hook has the beforeRemoveLiquidity permission, it is
  simulated exactly as the PoolManager would call it (from = PoolManager, sender = the position owner), now and 10 years ahead. Reverts
  at both times + no DELEGATECALL/CALLCODE in the hook -> that share counts as LOCKED ("locked by the pool's hook").
- MANY v4 POOLMANAGERS (chains.json `"v4": {"any_manager": true}`, Elysium: 12 copies, one per DEX/launchpad): v4 pools are
  accepted from ANY PoolManager; each pool carries its own `pm` (lib/discover v4_manager_of finds it for DexScreener pool ids) and
  the main pool's PoolManager becomes rpc.cfg["v4"] for the rest of the check. No posm configured -> a position sender that
  answers ownerOf(salt) is treated as the position-NFT contract (p["posm"]).
