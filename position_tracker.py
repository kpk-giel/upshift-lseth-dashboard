#!/usr/bin/env python3
"""Live carry-trade metrics for the Upshift lsETH position (one wallet, two
legs: borrow on the lsETH/USDC Morpho market, deposit into KPK USDC Yield).

Built for Giel's ask (2026-08-04 DM): track delta APY (lend - borrow spread
on the USDC leg) and net APY denominated in lsETH terms (that spread scaled
by LTV -- the carry boost on top of just holding lsETH), for the wallet
Upshift shared as the trader subaccount.

Data source: the public Morpho GraphQL API (api.morpho.org/graphql, no key
needed) -- same endpoint rebalancer-check's onchain_query.py uses, but via
`userByAddress` instead of `vaultV2ByAddress` since this is a position query
for one wallet rather than a vault-wide state pull. V1/V2 vault positions
live under different fields (`vaultPositions` vs `vaultV2Positions`) --
KPK USDC Yield is V2, confirmed live 2026-08-05.

Usage:
    python3 position_tracker.py                 # default wallet, JSON to stdout
    python3 position_tracker.py --wallet 0x...   # any other wallet in the same shape
"""
import argparse
import json
import time
import urllib.request

MORPHO_GRAPHQL = "https://api.morpho.org/graphql"
DEFAULT_WALLET = "0x15d869A5a117480FF219d6dC62a42C794cbAcCba"
LSETH_USDC_MARKET_ID = "0xfb7d54e0ce71efc8fffd3f4e1db0afa9265882da5cc76604b62adfac64501e80"
USDC_YIELD_VAULT = "0xD5cCe260E7a755DDf0Fb9cdF06443d593AaeaA13"
# Second deploy vault since 2026-08-27: the borrowed USDC is split across USDC
# Yield and USDC Yield RWA (target 60/40; the realised split floats with each
# vault's caps and liquidity). The deposit leg below is the AGGREGATE of both,
# with APYs blended by the actual allocation.
USDC_YIELD_RWA_VAULT = "0x7a72bcD2c3F7F7e4D6679170a0625bAB15D7DDa1"

HISTORY_QUERY = """
query($marketId: String!, $vault: String!, $chainId: Int!, $opts: TimeseriesOptions!) {
  marketById(marketId: $marketId, chainId: $chainId) {
    creationTimestamp
    historicalState {
      borrowApy(options: $opts) { x y }
      monthlyBorrowApy(options: $opts) { x y }
    }
  }
  vaultV2ByAddress(address: $vault, chainId: $chainId) {
    historicalState {
      avgNetApy(options: $opts) { x y }
      sharePrice(options: $opts) { x y }
    }
  }
}
"""

QUERY = """
query($address: String!, $chainId: Int!) {
  userByAddress(address: $address, chainId: $chainId) {
    marketPositions {
      market {
        marketId
        lltv
        loanAsset { symbol decimals }
        collateralAsset { symbol decimals }
        state {
          borrowApy
          utilization
          dailyBorrowApy
          weeklyBorrowApy
          monthlyBorrowApy
          collateralAssetsUsd
          borrowAssetsUsd
        }
      }
      state { collateral borrowAssets }
    }
    vaultV2Positions {
      assets
      assetsUsd
      vault {
        address
        symbol
        asset { symbol decimals }
        netApy
        avgNetApy1d: avgNetApy(lookback: ONE_DAY)
        avgNetApy7d: avgNetApy(lookback: SEVEN_DAYS)
        avgNetApy30d: avgNetApy(lookback: THIRTY_DAYS)
      }
    }
  }
}
"""


def _post(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        MORPHO_GRAPHQL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def _query(wallet: str) -> dict:
    return _post(QUERY, {"address": wallet, "chainId": 1})["userByAddress"]


def fetch_position(wallet: str = DEFAULT_WALLET) -> dict:
    data = _query(wallet)

    market_pos = next(
        (p for p in data["marketPositions"] if p["market"]["marketId"] == LSETH_USDC_MARKET_ID),
        None,
    )
    deploy_addrs = {USDC_YIELD_VAULT.lower(), USDC_YIELD_RWA_VAULT.lower()}
    vault_positions = [
        p for p in data["vaultV2Positions"]
        if p["vault"]["address"].lower() in deploy_addrs and int(p["assets"]) > 0
    ]
    if market_pos is None or not vault_positions:
        return {
            "wallet": wallet,
            "error": (
                f"missing {'market' if market_pos is None else ''}"
                f"{' + ' if market_pos is None and not vault_positions else ''}"
                f"{'vault' if not vault_positions else ''} position for this wallet"
            ),
        }

    m, ms = market_pos["market"], market_pos["state"]
    collateral_lseth = int(ms["collateral"]) / 10 ** m["collateralAsset"]["decimals"]
    collateral_usd = m["state"]["collateralAssetsUsd"]
    borrow_usdc = ms["borrowAssets"] / 10 ** m["loanAsset"]["decimals"]
    borrow_usd = m["state"]["borrowAssetsUsd"]
    borrow_apy_pct = m["state"]["borrowApy"] * 100

    # Deposit leg: aggregate across both deploy vaults, APY weighted by where the
    # money actually sits. A plain average would misstate the leg whenever the
    # split drifts from 50/50 (it runs ~64/36).
    per_vault = []
    deposit_usdc = deposit_usd = 0.0
    for p in vault_positions:
        amt = int(p["assets"]) / 10 ** p["vault"]["asset"]["decimals"]
        deposit_usdc += amt
        deposit_usd += p["assetsUsd"]
        per_vault.append({
            "vault": p["vault"]["symbol"],
            "address": p["vault"]["address"],
            "amount": amt,
            "usd": p["assetsUsd"],
            "apy_pct": (p["vault"]["netApy"] or 0) * 100,
        })
    for v in per_vault:
        v["weight"] = v["amount"] / deposit_usdc if deposit_usdc else 0.0

    def _blend(key):
        """Allocation-weighted blend of a vault APY field, renormalised over the
        vaults that actually report it (the RWA vault is young, so its longer
        trailing windows can be null)."""
        num = den = 0.0
        for p in vault_positions:
            val = p["vault"].get(key)
            if val is None:
                continue
            w = int(p["assets"]) / 10 ** p["vault"]["asset"]["decimals"]
            num += w * val
            den += w
        return (num / den) if den else None

    vault_apy_pct = (_blend("netApy") or 0) * 100

    ltv_pct = (borrow_usd / collateral_usd * 100) if collateral_usd else None
    delta_apy_pp = vault_apy_pct - borrow_apy_pct
    net_apy_lseth_terms_pct = (delta_apy_pp * ltv_pct / 100) if ltv_pct is not None else None

    # LLTV is the market's liquidation threshold, scaled 1e18 on-chain. Read it
    # rather than hardcoding: the value circulated internally (86.5%) is wrong,
    # the market is actually 86%, and that gap matters for LTV headroom math.
    lltv_pct = int(m["lltv"]) / 1e18 * 100

    # Both headline APYs are INSTANTANEOUS spot rates, not trailing averages
    # (schema: "Instantaneous Borrow APY" / "Current net APY ... derived from
    # liquidity adapter rates"). Spot alone is a thin basis for a sales number,
    # so matched-horizon trailing spreads come along for comparison: each pairs
    # the vault's realized avgNetApy with the market's borrow APY over the SAME
    # window. Mixing horizons (e.g. 30d vault vs spot borrow) would fabricate a
    # spread neither leg ever earned.
    # Deposit-side APYs are the allocation-weighted blend across both deploy
    # vaults. Caveat, stated rather than hidden: trailing windows are blended with
    # TODAY'S weights, because per-hour historical weights are not available from
    # this API. Before 2026-08-27 all funds sat in USDC Yield, so a 30d blend
    # slightly misweights the pre-split stretch; with the two vaults ~16bps apart
    # the error is under 6bps and shrinks daily as the split ages.
    mstate = m["state"]

    def _spread(vault_apy, borrow_apy):
        if vault_apy is None or borrow_apy is None:
            return None
        return (vault_apy - borrow_apy) * 100

    spreads = {
        "spot": delta_apy_pp,
        "1d": _spread(_blend("avgNetApy1d"), mstate.get("dailyBorrowApy")),
        "7d": _spread(_blend("avgNetApy7d"), mstate.get("weeklyBorrowApy")),
        "30d": _spread(_blend("avgNetApy30d"), mstate.get("monthlyBorrowApy")),
    }

    # The individual legs per horizon, so a UI switching horizons can show the
    # working (yield - borrow = spread) with all three terms on the SAME basis.
    # Showing a spot yield minus a spot borrow next to a 30d spread produces an
    # equation that visibly doesn't add up.
    def _pair(vault_key, borrow_key):
        v, b = _blend(vault_key), mstate.get(borrow_key)
        if v is None or b is None:
            return None
        return {"yield_pct": v * 100, "borrow_pct": b * 100}

    legs = {
        "spot": {"yield_pct": vault_apy_pct, "borrow_pct": borrow_apy_pct},
        "1d": _pair("avgNetApy1d", "dailyBorrowApy"),
        "7d": _pair("avgNetApy7d", "weeklyBorrowApy"),
        "30d": _pair("avgNetApy30d", "monthlyBorrowApy"),
    }

    # Deliberately NOT exposing `apyAtTarget` as a basis. It is the rate Morpho's
    # IRM drifts toward when utilization is set by market forces, but KPK is the
    # sole supplier AND sole borrower in this market -- utilization is not
    # discovered here, it is set by how much USDC Prime allocates. So the IRM's
    # target rate describes a dynamic that does not apply, while the live
    # `borrowApy` is the rate KPK's automations actually govern. Live is the
    # forward-looking number for this market; the trailing averages are the
    # reality check on it.

    lseth_price_usd = (collateral_usd / collateral_lseth) if collateral_lseth else None

    return {
        "wallet": wallet,
        "collateral": {
            "asset": "lsETH", "amount": collateral_lseth, "usd": collateral_usd,
            "price_usd": lseth_price_usd,
        },
        "borrow": {"asset": "USDC", "amount": borrow_usdc, "usd": borrow_usd, "apy_pct": borrow_apy_pct},
        "deposit": {
            "vault": "KPK USDC vaults" if len(per_vault) > 1 else per_vault[0]["vault"],
            "asset": "USDC",
            "amount": deposit_usdc, "usd": deposit_usd, "apy_pct": vault_apy_pct,
            "vaults": per_vault,
        },
        "ltv_pct": ltv_pct,
        "lltv_pct": lltv_pct,
        "delta_apy_pp": delta_apy_pp,
        "net_apy_lseth_terms_pct": net_apy_lseth_terms_pct,
        "spreads_pp": spreads,
        "legs_by_horizon": legs,
        "utilization_pct": (mstate["utilization"] * 100) if mstate.get("utilization") is not None else None,
    }


ENTRY_QUERY = """
query($wallet: [String!], $marketId: [String!]) {
  marketTransactions(
    first: 200
    orderBy: Timestamp
    orderDirection: Asc
    where: { userAddress_in: $wallet, marketUniqueKey_in: $marketId, chainId_in: [1] }
  ) {
    items {
      timestamp
      type
      txHash
      data { __typename ... on MarketTransactionTransferData { assets } }
    }
  }
}
"""

# The wallet's first vault deposit was a 0.01 USDC plumbing test on 2026-07-31,
# days before the strategy actually ran. Anything at or below this is treated as
# a test, not the start of the position.
TEST_TX_USDC_CEILING = 1.0


def fetch_strategy_start(wallet: str = DEFAULT_WALLET) -> dict | None:
    """Timestamp at which this wallet actually opened the carry position: its
    first `Borrow` on the lsETH/USDC market above the test-transaction floor.

    Why the borrow and not the vault deposit: the deposit leg has a 0.01 USDC
    test tx predating the real position by four days, and a deposit alone isn't
    the carry trade -- the borrow is what makes it one. Returns None if the
    wallet has never borrowed here (e.g. after a full unwind, or a wrong
    wallet), so callers must handle the no-position case rather than assume a
    date exists.
    """
    items = _post(ENTRY_QUERY, {"wallet": [wallet], "marketId": [LSETH_USDC_MARKET_ID]})[
        "marketTransactions"
    ]["items"]
    borrows = [
        i for i in items
        if i["type"] == "Borrow"
        and int((i.get("data") or {}).get("assets") or 0) / 1e6 > TEST_TX_USDC_CEILING
    ]
    if not borrows:
        return None
    first = borrows[0]
    return {
        "timestamp": first["timestamp"],
        "tx_hash": first["txHash"],
        "borrowed_usdc": int(first["data"]["assets"]) / 1e6,
    }


LLAMA_POOLS = "https://yields.llama.fi/pools"


def fetch_lseth_staking_apy() -> float | None:
    """lsETH's own staking APY, for the 'total APY' figure.

    Morpho's API returns `null` for LsETH's `yield`, so the rate comes from
    DefiLlama's Liquid Collective pool (the protocol that issues lsETH) instead.
    Matched on project + symbol rather than a pool id so it survives DefiLlama
    re-keying, and on the largest-TVL match because several projects list an
    "LSETH" pool -- only Liquid Collective's is the staking rate itself.

    Returns None on any failure. Callers must handle that rather than
    substituting a plausible-looking constant: the carry is the number this
    dashboard is actually authoritative about, and a stale hardcoded staking
    rate would silently corrupt the headline total.
    """
    try:
        req = urllib.request.Request(LLAMA_POOLS, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            pools = json.loads(resp.read().decode())["data"]
    except Exception:
        return None

    matches = [
        p for p in pools
        if p.get("project") == "liquid-collective"
        and (p.get("symbol") or "").upper() == "LSETH"
        and p.get("apy") is not None
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: p.get("tvlUsd") or 0)["apy"]


# Comparable mainnet USDC markets at the same 86% LLTV, used to show that the
# lsETH market's borrow rate is competitive rather than set below market. These
# are the same two proxies the Phase 1 backtest used, chosen there for being
# deep and market-driven.
PEER_MARKETS = {
    "wstETH/USDC": "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc",
    "rETH/USDC": "0x0a15460ad263c2186fe0b5df20a8cf71d55f3cfa06de15edcf6138f6b8edd8bf",
}


def fetch_peer_borrow_rates() -> dict:
    """Live borrow APY on comparable 86%-LLTV USDC markets.

    NOT used by the dashboard: it names these markets as benchmarks but
    deliberately does not print their live rates, to avoid inviting a
    rate-shopping comparison on a client-facing page. Kept for ad-hoc checks that
    the lsETH market's rate is still competitive -- call it from the CLI rather
    than wiring it into the build.

    Queried one market per request: the API's complexity limit applies per
    request, and this keeps a single failing market from losing the others.
    Returns only the markets that answered, so callers must tolerate a partial
    or empty dict rather than assuming all peers are present.
    """
    out = {}
    for name, market_id in PEER_MARKETS.items():
        try:
            data = _post(
                "query($m:String!){ marketById(marketId:$m, chainId:1){"
                " state{ borrowApy monthlyBorrowApy } } }",
                {"m": market_id},
            )
            state = data["marketById"]["state"]
            if state.get("borrowApy") is not None:
                out[name] = {
                    "borrow_pct": state["borrowApy"] * 100,
                    "borrow_30d_pct": (state["monthlyBorrowApy"] * 100)
                    if state.get("monthlyBorrowApy") is not None else None,
                }
        except Exception:
            continue
    return out


def fetch_apy_history(interval: str = "DAY") -> dict:
    """Daily (or --interval) borrow APY on the lsETH/USDC market and net APY
    on KPK USDC Yield, for the full life of the market -- it's younger than
    the vault, so its creation is the natural start of "full history" for
    this position. Points come back newest-first; re-sorted ascending here."""
    now = int(time.time())
    # Two-call bootstrap: the market's own creationTimestamp sets the window
    # start, so this doesn't need the value hardcoded and re-derives cleanly
    # if the tracker ever points at a different market.
    creation = _post(
        "query($marketId:String!,$chainId:Int!){ marketById(marketId:$marketId, chainId:$chainId){ creationTimestamp } }",
        {"marketId": LSETH_USDC_MARKET_ID, "chainId": 1},
    )["marketById"]["creationTimestamp"]

    # Trend series need `window_days` of lead-in before the first plotted point,
    # or the trend starts a month late. The vault predates the market by ~6
    # months, so fetch from before the window opens and let the trend cover the
    # whole chart; the extra points are consumed by the lookback, not plotted.
    trend_window = 30
    lead_in = trend_window + 1
    opts = {
        "startTimestamp": creation - lead_in * 86400,
        "endTimestamp": now,
        "interval": interval,
    }
    data = _post(HISTORY_QUERY, {
        "marketId": LSETH_USDC_MARKET_ID, "vault": USDC_YIELD_VAULT, "chainId": 1, "opts": opts,
    })

    def _series(points):
        return sorted(
            [{"t": p["x"], "y": p["y"] * 100} for p in points if p["y"] is not None],
            key=lambda p: p["t"],
        )

    vault_hist = data["vaultV2ByAddress"]["historicalState"]
    market_hist = data["marketById"]["historicalState"]

    # Only the lead-in is fetched early; the plotted window still starts at the
    # market's creation. Without this clip the chart would show vault rates from
    # before the market existed.
    def _from_creation(series):
        return [p for p in series if p["t"] >= creation]

    return {
        "since": creation,
        "borrow_apy_pct": _from_creation(_series(market_hist["borrowApy"])),
        "yield_apy_pct": _from_creation(_series(vault_hist["avgNetApy"])),
        "borrow_apy_30d_pct": _from_creation(_series(market_hist["monthlyBorrowApy"])),
        "yield_apy_30d_pct": _from_creation(
            _share_price_apy(vault_hist["sharePrice"], window_days=trend_window)
        ),
    }


def _share_price_apy(points: list, window_days: int = 30) -> list:
    """Annualised growth in share price over a trailing window, as percentages.

    This is the same derivation the Phase 1 carry backtest uses
    (`POWER(share_price / share_price_30d_ago, 365.0/window) - 1`), rather than
    Morpho's `avgNetApy`. The reason: `avgNetApy` defaults to a short (6h)
    lookback, so a single day's share-price step annualises into a spike -- the
    lsETH/USDC chart showed apparent 25%, 15% and 14% days that were artefacts of
    that short window, not yield anyone earned. A 30-day window smooths them out
    and, being share-price based, still has fees and idle drag baked in.

    Returns [] if the series is too short to span the window, so callers must
    handle an empty result rather than assume coverage.
    """
    series = sorted(
        [(p["x"], p["y"]) for p in points if p.get("y") is not None],
        key=lambda p: p[0],
    )
    if len(series) <= window_days:
        return []

    out = []
    for i in range(window_days, len(series)):
        t, price = series[i]
        prior = series[i - window_days][1]
        if not prior or price is None or float(prior) <= 0:
            continue
        growth = float(price) / float(prior)
        out.append({"t": t, "y": (growth ** (365.0 / window_days) - 1) * 100})
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", default=DEFAULT_WALLET)
    parser.add_argument("--history", action="store_true", help="print full APY history instead of the position snapshot")
    parser.add_argument("--start", action="store_true", help="print when this wallet actually opened the carry position")
    parser.add_argument("--peers", action="store_true", help="compare the borrow rate against comparable 86%%-LLTV USDC markets")
    args = parser.parse_args()
    if args.history:
        print(json.dumps(fetch_apy_history(), indent=2))
    elif args.start:
        print(json.dumps(fetch_strategy_start(args.wallet), indent=2))
    elif args.peers:
        print(json.dumps(fetch_peer_borrow_rates(), indent=2))
    else:
        print(json.dumps(fetch_position(args.wallet), indent=2))


if __name__ == "__main__":
    main()
