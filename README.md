# Upshift lsETH carry dashboard

Static, self-contained dashboard for the Upshift lsETH carry position: lsETH
supplied as collateral on Morpho, USDC borrowed against it, that USDC redeployed
into KPK USDC Morpho vaults.

**Live page:** https://kpk-giel.github.io/upshift-lseth-dashboard/

`index.html` is generated — do not edit it here. It is built from live Morpho
GraphQL data by `build_dashboard.py` in the private working folder and pushed by
a daily cron job, so any manual edit is overwritten on the next run.

All assets (fonts, logo) are inlined; the page makes no external requests at
view time. The "As of" timestamp in the header shows when the data was read.
