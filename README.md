# Upshift lsETH carry dashboard

Static, self-contained dashboard for the Upshift lsETH carry position: lsETH
supplied as collateral on Morpho, USDC borrowed against it, that USDC redeployed
into KPK USDC Morpho vaults. Shows the borrow/lend spread, the carry it produces
per 1 lsETH at a chosen LTV, and the full rate history.

**Live page:** https://kpk-giel.github.io/upshift-lseth-dashboard/

## How it updates

`index.html` is **generated — do not edit it here.** A GitHub Actions workflow
(`.github/workflows/refresh.yml`) rebuilds it hourly from live data and commits
only when a rate actually moved, so the history stays meaningful rather than
recording one commit per hour. Run it on demand from the Actions tab
("Refresh dashboard" → Run workflow).

No secrets or API keys are involved: both upstream sources are public and
keyless.

| Source | Used for |
|---|---|
| `api.morpho.org/graphql` | market borrow rates, vault net APY, position, share-price history |
| `yields.llama.fi/pools` | lsETH staking APY (Liquid Collective pool) |

## Files

| File | |
|---|---|
| `index.html` | generated output — the published page |
| `build_dashboard.py` | renders the page; `python3 build_dashboard.py [outfile]` |
| `position_tracker.py` | all Morpho queries; `--history`, `--start`, `--peers` for ad-hoc reads |
| `assets/` | Lexend weights + KPK wordmark, inlined into the output at build time |
| `robots.txt` | link-reachable, deliberately not search-indexed |

Stdlib only — no `pip install` needed. All assets are inlined, so the published
page makes no external requests at view time.

## Reading the page

- **Carry** is what 1 lsETH earns *in addition to* its own staking yield, net of
  the 10% performance fee. Add it to the staking APY for the total.
- **Spread basis** — "Live" is the forward-looking view; the trailing averages are
  a reality check, each pairing the vault's realized net APY with the borrow rate
  over the same window.
- The **LTV slider** is a projection at a fixed spread. Today's LTV is deliberately
  conservative while the automations are finetuned, so read it as a floor.
- The header's "As of" timestamp is when the data was read. The page does not
  refresh itself in the browser.
