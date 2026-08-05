#!/usr/bin/env python3
"""Regenerates dashboard.html from live Morpho data. Run in place:
    python3 build_dashboard.py
"""
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "assets/fonts"
# Overridable so CI can write index.html directly (GitHub Pages serves that name)
# without a copy step. Defaults to dashboard.html for local runs.
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "dashboard.html"
LOGO_SVG = HERE / "assets/logo/KPKDark.svg"

sys.path.insert(0, str(HERE))
import position_tracker as pt  # noqa: E402

data = pt.fetch_position()
hist = pt.fetch_apy_history()
start = pt.fetch_strategy_start()
lseth_apy = pt.fetch_lseth_staking_apy()


def nice_step(raw_max):
    for step in (0.5, 1, 2, 5, 10, 20):
        if raw_max / step <= 7:
            return step
    return 50


def percentile(values, pct):
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct / 100))
    return s[idx]


def build_chart(hist, start=None):
    borrow = hist["borrow_apy_pct"]
    yieldp = hist["yield_apy_pct"]

    x0, x1 = min(borrow[0]["t"], yieldp[0]["t"]), max(borrow[-1]["t"], yieldp[-1]["t"])

    # The vault's net APY was volatile (6-25%) for roughly its first two
    # weeks on thin TVL before settling near 6-7.5% -- scaling the axis to
    # that transient would flatten the whole rest of the series. The 90th
    # percentile is a robust cap: it rides with the steady-state band
    # regardless of exactly how long the noisy stretch turns out to be, no
    # hardcoded day count. True values still render/label/tooltip correctly;
    # see the clipped-point annotation below.
    y_max_core = max(max(p["y"] for p in borrow), percentile([p["y"] for p in yieldp], 90))
    step = nice_step(y_max_core * 1.15)
    y_max = step * (int(y_max_core * 1.15 / step) + 1)
    y_min = 0

    pad_l, pad_r, pad_t, pad_b = 44, 10, 16, 30
    W, H = 840, 300
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b

    def sx(t):
        return pad_l + (t - x0) / (x1 - x0) * plot_w

    def sy(y):
        y = min(y, y_max)  # clamp for pixel placement only; true value kept for labels/tooltip
        return pad_t + (1 - (y - y_min) / (y_max - y_min)) * plot_h

    def path_for(series):
        return " ".join(f"{'M' if i == 0 else 'L'}{sx(p['t']):.1f},{sy(p['y']):.1f}" for i, p in enumerate(series))

    # y gridlines / ticks
    ticks = []
    n = int(y_max / step)
    for i in range(n + 1):
        yv = i * step
        ticks.append((sy(yv), yv))

    # x ticks: 5 evenly spaced dates across the domain
    x_ticks = []
    for i in range(5):
        t = x0 + (x1 - x0) * i / 4
        x_ticks.append((sx(t), datetime.fromtimestamp(t, tz=timezone.utc).strftime("%b %-d")))

    clipped = [p for p in yieldp if p["y"] > y_max]
    annotation = ""
    if clipped:
        peak = max(clipped, key=lambda p: p["y"])
        ax, ay = sx(peak["t"]), pad_t
        span_start = datetime.fromtimestamp(min(p["t"] for p in clipped), tz=timezone.utc).strftime("%b %-d")
        span_end = datetime.fromtimestamp(max(p["t"] for p in clipped), tz=timezone.utc).strftime("%b %-d")
        # Don't assert a cause here. These spikes were checked against vault TVL
        # and allocation history: the vault was ~6 months old and held $3M+ on the
        # first one, so they are NOT a seeding artefact, and no forceDeallocate
        # events exist on the vault. They are single-day jumps in a share-price-
        # derived APY, which a short lookback amplifies. State what is observed.
        label = (
            f"{len(clipped)} day(s) above {y_max:g}% ({span_start}–{span_end}), "
            f"peak {peak['y']:.0f}% — single-day spikes in share-price-derived "
            f"APY; capped for readability"
        )
        annotation = (
            f'<g class="chart-annot">'
            f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{ax:.1f}" y2="{ay + 10:.1f}" stroke="var(--ink-soft)" stroke-width="1"/>'
            f'<text x="{max(ax, pad_l + 4):.1f}" y="{ay - 4:.1f}" text-anchor="start">{label}</text>'
            f"</g>"
        )

    # Marker for when the carry position actually opened. Everything left of it
    # is market/vault history that predates us being in the trade at all -- worth
    # shading, because otherwise the early seed-funding APY noise reads as if it
    # were our performance. Clamped into the plot in case the entry precedes the
    # chart window (it can't today, but a re-pointed market could).
    entry_svg = ""
    if start:
        ex = min(max(sx(start["timestamp"]), pad_l), W - pad_r)
        entry_label = datetime.fromtimestamp(start["timestamp"], tz=timezone.utc).strftime("%b %-d")
        # Label sits left of the rule when it would otherwise overflow the right edge.
        flip = ex > W - pad_r - 150
        entry_svg = (
            f'<g class="chart-entry">'
            f'<rect x="{pad_l}" y="{pad_t}" width="{max(0, ex - pad_l):.1f}" height="{plot_h}" '
            f'class="chart-preentry"/>'
            f'<line x1="{ex:.1f}" y1="{pad_t}" x2="{ex:.1f}" y2="{pad_t + plot_h}" class="chart-entryline"/>'
            f'<text x="{ex + (-6 if flip else 6):.1f}" y="{pad_t + plot_h - 8:.1f}" '
            f'text-anchor="{"end" if flip else "start"}" class="chart-entrylabel">'
            f'◀ before position · position opened {entry_label}</text>'
            f"</g>"
        )

    # 30-day trend lines over the raw daily series. Deliberately an overlay, not a
    # replacement: the daily variation is real and drives actual performance, so
    # smoothing it away would hide the thing this chart exists to show. The trend
    # just gives the eye a stable reference through the noise. Dashed and thinner
    # so it reads as derived rather than as a second measurement.
    # Only the yield leg gets a trend. The borrow rate is near-flat over this
    # window (3.8-4.1%), so a trend line there is redundant; and Morpho returns
    # nulls for `monthlyBorrowApy` until the market itself is 30 days old, so it
    # would start a month late regardless. The vault's trend is derived from
    # share price here, which is why it can cover the full window.
    trend_paths = ""
    yield_trend = hist.get("yield_apy_30d_pct")
    if yield_trend and len(yield_trend) > 1:
        trend_paths = (
            f'<path d="{path_for(yield_trend)}" fill="none" stroke="var(--chart-yield)" '
            f'stroke-width="1.25" stroke-dasharray="5 4" opacity="0.85" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    borrow_end = borrow[-1]
    yield_end = yieldp[-1]
    ey_b, ey_y = sy(borrow_end["y"]), sy(yield_end["y"])
    if abs(ey_b - ey_y) < 16:
        mid = (ey_b + ey_y) / 2
        ey_b, ey_y = mid - 8, mid + 8

    grid_svg = "\n".join(
        f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" class="chart-grid"/>'
        f'<text x="{pad_l - 8}" y="{y:.1f}" class="chart-ytick" text-anchor="end" dominant-baseline="middle">{yv:g}%</text>'
        for y, yv in ticks
    )
    xtick_svg = "\n".join(
        f'<text x="{x:.1f}" y="{H - pad_b + 18:.1f}" class="chart-xtick" text-anchor="middle">{label}</text>'
        for x, label in x_ticks
    )

    svg = f"""
    <svg viewBox="0 0 {W} {H}" class="chart-svg" role="img" aria-label="Borrow APY and Yield APY, daily, since market inception">
      <g>{grid_svg}</g>
      <g>{xtick_svg}</g>
      {entry_svg}
      {annotation}
      <path d="{path_for(borrow)}" fill="none" stroke="var(--chart-borrow)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <path d="{path_for(yieldp)}" fill="none" stroke="var(--chart-yield)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      {trend_paths}
      <circle cx="{sx(borrow_end['t']):.1f}" cy="{ey_b:.1f}" r="4" fill="var(--chart-borrow)" stroke="var(--surface)" stroke-width="2"/>
      <circle cx="{sx(yield_end['t']):.1f}" cy="{ey_y:.1f}" r="4" fill="var(--chart-yield)" stroke="var(--surface)" stroke-width="2"/>
      <text x="{sx(borrow_end['t']) - 8:.1f}" y="{ey_b:.1f}" text-anchor="end" dominant-baseline="middle" class="chart-endlabel" fill="var(--chart-borrow)">{borrow_end['y']:.2f}%</text>
      <text x="{sx(yield_end['t']) - 8:.1f}" y="{ey_y:.1f}" text-anchor="end" dominant-baseline="middle" class="chart-endlabel" fill="var(--chart-yield)">{yield_end['y']:.2f}%</text>
      <rect id="hoverRect" x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="transparent"/>
      <line id="crosshair" x1="0" y1="{pad_t}" x2="0" y2="{pad_t + plot_h}" class="chart-crosshair" visibility="hidden"/>
    </svg>
    """

    table_rows = []
    # Key on the UTC date, not the raw timestamp: the market's series ends with
    # a partial-day sample stamped at the current wall-clock second, which
    # never equals the vault's midnight-aligned stamp. Matching on timestamp
    # emitted a duplicate row for today with an em-dash yield; matching on date
    # pairs them, and keeping the newest sample per date lets the live partial
    # point supersede that date's earlier midnight one.
    yield_by_date = {}
    for p in yieldp:
        yield_by_date[datetime.fromtimestamp(p["t"], tz=timezone.utc).strftime("%Y-%m-%d")] = p["y"]

    seen_dates = set()
    for p in sorted(borrow, key=lambda p: p["t"], reverse=True):
        d = datetime.fromtimestamp(p["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        if d in seen_dates:
            continue
        seen_dates.add(d)
        yv = yield_by_date.get(d)
        delta_v = (yv - p["y"]) if yv is not None else None
        table_rows.append(
            f"<tr><td>{d}</td><td>{p['y']:.2f}%</td>"
            f"<td>{yv:.2f}%</td><td>{delta_v:+.2f} pp</td></tr>"
            if yv is not None else
            f"<tr><td>{d}</td><td>{p['y']:.2f}%</td><td>—</td><td>—</td></tr>"
        )

    # Left newest-first: the current rate is what a reader opening this table
    # wants, and it's the row that corroborates the headline above.

    borrow_js = json.dumps([[p["t"], round(p["y"], 4)] for p in borrow])
    yield_js = json.dumps([[p["t"], round(p["y"], 4)] for p in yieldp])

    return {
        "svg": svg,
        "table_rows": "\n".join(table_rows),
        "borrow_js": borrow_js,
        "yield_js": yield_js,
        "scale": {"x0": x0, "x1": x1, "y_min": y_min, "y_max": y_max,
                  "pad_l": pad_l, "plot_w": plot_w, "pad_t": pad_t, "plot_h": plot_h},
    }


chart = build_chart(hist, start)
since_date = datetime.fromtimestamp(hist["since"], tz=timezone.utc).strftime("%b %-d, %Y")


def b64_font(name):
    return base64.b64encode((FONT_DIR / name).read_bytes()).decode()


fonts = {
    "regular": b64_font("Lexend-Regular.ttf"),
    "medium": b64_font("Lexend-Medium.ttf"),
    "semibold": b64_font("Lexend-SemiBold.ttf"),
    "bold": b64_font("Lexend-Bold.ttf"),
}

logo_svg = LOGO_SVG.read_text().replace('fill="#1C1C1C"', 'fill="currentColor"')
# strip the xml viewbox wrapper's fixed width/height so CSS can size it
logo_svg = logo_svg.replace('width="941" height="282" ', "")

wallet = data["wallet"]
coll = data["collateral"]
borrow = data["borrow"]
dep = data["deposit"]
ltv = data["ltv_pct"]
delta = data["delta_apy_pp"]
net_lseth = data["net_apy_lseth_terms_pct"]
lltv = data["lltv_pct"]
lseth_price = coll["price_usd"]
spreads = data["spreads_pp"]
per_unit_borrow = lseth_price * ltv / 100

# Gradient stops for the LTV track, as a fraction of the 0..LLTV span.
#
# Four bands, so "above target" and "near liquidation" are visually distinct
# rather than both red: green to 70, olive 71-75 (through the operating target),
# orange 76-80, red above. 75% is the intended target so it must not look like a
# warning, but the ramp has to bite soon after -- at 86% LLTV there is very little
# buffer left by 81%.
grad_safe_pct = 70.0 / lltv * 100     # green ends
grad_mid_pct = 75.0 / lltv * 100      # olive, through the target
grad_warn_pct = 80.0 / lltv * 100     # orange ends, red beyond

# 10% performance fee on the carry, matching the Phase 1 backtest's `x 0.9`
# (Notion: "lsETH Phase 1 carry -- backtest methodology & results"). Stated
# explicitly on the page because a reader who recomputes spread x LTV without it
# gets a higher number and assumes an error.
# Live first and default. KPK is the sole supplier and sole borrower in this
# market, so the live borrow rate is the one its own automations govern -- that
# makes it the forward-looking basis here, not a mere snapshot. The trailing
# averages are the reality check on it.
_SPREAD_LABELS = [
    ("spot", "Live"),
    ("7d", "7d avg"),
    ("30d", "30d avg"),
]
_available_spreads = [(k, lbl) for k, lbl in _SPREAD_LABELS if spreads.get(k) is not None]
_default_basis = "spot"
legs_by_horizon = data["legs_by_horizon"]
utilization_pct = data.get("utilization_pct") or 0.0

PERF_FEE = 0.10
net_fee_multiplier = 1 - PERF_FEE

# Headline runs on the default (forward) basis so the page's first number and the
# simulator agree on load. `delta`/`net_lseth` stay spot for the live-position
# table, which reports what the position is actually earning right now.
default_spread = spreads[_default_basis]
default_legs = legs_by_horizon[_default_basis]
net_lseth_after_fee = default_spread * ltv / 100 * net_fee_multiplier

# Total APY = lsETH's own staking yield + the carry. The staking leg comes from
# DefiLlama (Morpho serves null for it); if that fetch failed, fall back to
# showing LLTV rather than inventing a staking rate.
if lseth_apy is not None:
    total_apy_card = f"""<div class="mini-stat">
        <span class="label">
          Total APY on lsETH
          <em class="sublabel">{lseth_apy:.2f}% staking + <span id="carryPart">{net_lseth_after_fee:.2f}</span>% carry</em>
        </span>
        <span class="value" style="color:var(--good)" id="totalApy">{lseth_apy + net_lseth_after_fee:.2f}%</span>
      </div>"""
else:
    total_apy_card = f"""<div class="mini-stat">
        <span class="label">Liquidation LTV (LLTV) on this market</span>
        <span class="value">{lltv:.1f}%</span>
      </div>"""

lseth_apy_js = f"{lseth_apy:.6f}" if lseth_apy is not None else "null"

# What the same structure yields at the 75% operating target, on the 30-day
# spread rather than today's spot -- the pairing that matches the backtest's
# basis. Falls back to the spot spread if the 30d average isn't available.
TARGET_LTV = 75.0
carry_at_target = default_spread * TARGET_LTV / 100 * net_fee_multiplier

default_basis_label = dict(_SPREAD_LABELS)[_default_basis]
default_basis_sub = (
    "live spot rates" if _default_basis == "spot"
    else f"{default_basis_label} (trailing)"
)
default_yield_sub = "Yield APY" if _default_basis == "spot" else f"Yield, {default_basis_label}"
default_borrow_sub = "Borrow APY" if _default_basis == "spot" else f"Borrow, {default_basis_label}"

# Trailing spreads, rendered only where the API returned a value (a young vault
# has no 30d realized APY yet, so these can legitimately be None).
spread_buttons = "\n".join(
    f'<button type="button" class="spread-btn" data-spread="{spreads[key]:.6f}"'
    f' data-yield="{legs_by_horizon[key]["yield_pct"]:.6f}"'
    f' data-borrow="{legs_by_horizon[key]["borrow_pct"]:.6f}"'
    f' aria-current="{"true" if key == _default_basis else "false"}">'
    f'<i>{label}</i><b>{spreads[key]:+.2f} pp</b></button>'
    for key, label in _available_spreads
    if legs_by_horizon.get(key)
)

if start:
    _age_days = max(0, (datetime.now(timezone.utc)
                        - datetime.fromtimestamp(start["timestamp"], tz=timezone.utc)).days)
    _opened = datetime.fromtimestamp(start["timestamp"], tz=timezone.utc).strftime("%b %-d, %Y at %H:%M UTC")
    position_age_note = (
        f"Position opened {_opened} "
        f"({'today' if _age_days == 0 else f'{_age_days} day(s) ago'}), "
        f"borrowing {start['borrowed_usdc']:,.0f} USDC."
    )
    entry_sentence = (
        f"The position itself opened {_opened} — the market and vault history "
        f"before that line is context, not our track record."
    )
else:
    position_age_note = "No borrow found for this wallet on the lsETH/USDC market."
    entry_sentence = ""

market_url = "https://app.morpho.org/ethereum/market/0xfb7d54e0ce71efc8fffd3f4e1db0afa9265882da5cc76604b62adfac64501e80/lseth-usdc"
vault_url = "https://app.morpho.org/ethereum/vault/0xD5cCe260E7a755DDf0Fb9cdF06443d593AaeaA13"
debank_url = f"https://debank.com/profile/{wallet}"
# Phase 1 carry backtest -- public Dune query behind the 2.11% cross-check.
backtest_url = "https://dune.com/queries/8163239"
as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

html = f"""<title>Upshift lsETH Carry — KPK</title>
<!-- Reachable by link, not discoverable by search. robots.txt asks crawlers not
     to fetch; this asks them not to index if they fetch anyway. -->
<meta name="robots" content="noindex, nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  @font-face {{
    font-family: 'Lexend';
    font-weight: 400;
    src: url(data:font/ttf;base64,{fonts['regular']}) format('truetype');
    font-display: swap;
  }}
  @font-face {{
    font-family: 'Lexend';
    font-weight: 500;
    src: url(data:font/ttf;base64,{fonts['medium']}) format('truetype');
    font-display: swap;
  }}
  @font-face {{
    font-family: 'Lexend';
    font-weight: 600;
    src: url(data:font/ttf;base64,{fonts['semibold']}) format('truetype');
    font-display: swap;
  }}
  @font-face {{
    font-family: 'Lexend';
    font-weight: 700;
    src: url(data:font/ttf;base64,{fonts['bold']}) format('truetype');
    font-display: swap;
  }}

  :root {{
    --bg: #E8E7E5;
    --surface: #DEDDD8;
    --surface-2: #F4F3F1;
    --ink: #1F1F1F;
    --ink-soft: #706E66;
    --line: #CAC8C0;
    --accent-borrow: #206697;
    --accent-borrow-bg: #E2EFF9;
    --accent-yield: #22676D;
    --accent-yield-bg: #E4F5F7;
    --good: #2D8561;
    --good-bg: #ECF9F3;
    --bad: #A5122B;
    --bad-bg: #FEF3F5;
    /* Risk ramp for the LTV track. Separate from --good/--bad because those are
       tuned for text contrast; these are fills that must stay legible as a
       gradient and read as safe -> caution -> danger in both themes. */
    --risk-lo: #2D8561;
    --risk-mid: #8A8F32;
    --risk-warn: #C0721A;
    --risk-hi: #A5122B;
    --chart-borrow: #3F97D6;
    --chart-yield: #DC5C39;
    --radius: 18px;
    --font-ui: 'Lexend', 'Montserrat', -apple-system, sans-serif;
  }}

  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1A1916;
      --surface: #232019;
      --surface-2: #2B2820;
      --ink: #F8F8F6;
      --ink-soft: #ACA89D;
      --line: #423E33;
      --accent-borrow: #80BAE4;
      --accent-borrow-bg: #194F7633;
      --accent-yield: #4DBEC8;
      --accent-yield-bg: #17464A55;
      --good: #56C698;
      --good-bg: #0F2E2144;
      --bad: #F6B0BC;
      --bad-bg: #4908133a;
      --risk-lo: #56C698;
      --risk-mid: #B6C45C;
      --risk-warn: #E8973F;
      --risk-hi: #E8556F;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #1A1916; --surface: #232019; --surface-2: #2B2820;
    --ink: #F8F8F6; --ink-soft: #ACA89D; --line: #423E33;
    --accent-borrow: #80BAE4; --accent-borrow-bg: #194F7633;
    --accent-yield: #4DBEC8; --accent-yield-bg: #17464A55;
    --good: #56C698; --good-bg: #0F2E2144;
    --bad: #F6B0BC; --bad-bg: #4908133a;
    --risk-lo: #56C698; --risk-mid: #B6C45C; --risk-warn: #E8973F; --risk-hi: #E8556F;
  }}
  :root[data-theme="light"] {{
    --bg: #E8E7E5; --surface: #DEDDD8; --surface-2: #F4F3F1;
    --ink: #1F1F1F; --ink-soft: #706E66; --line: #CAC8C0;
    --accent-borrow: #206697; --accent-borrow-bg: #E2EFF9;
    --accent-yield: #22676D; --accent-yield-bg: #E4F5F7;
    --good: #2D8561; --good-bg: #ECF9F3;
    --bad: #A5122B; --bad-bg: #FEF3F5;
    --risk-lo: #2D8561; --risk-mid: #8A8F32; --risk-warn: #C0721A; --risk-hi: #A5122B;
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-ui);
    font-weight: 400;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }}

  .page {{
    max-width: 920px;
    margin: 0 auto;
    padding: 26px 24px 64px;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }}

  header.top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }}
  .logo {{ width: 92px; color: var(--ink); flex-shrink: 0; }}
  .logo svg {{ width: 100%; height: auto; display: block; }}

  /* One row rather than three stacked lines: this is provenance, not content,
     and the vertical space it was taking pushed the simulator below the fold. */
  .id-block {{
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 7px;
    justify-content: flex-end;
    text-align: right;
    font-family: var(--font-ui);
  }}
  .id-sep {{ color: var(--ink-soft); opacity: 0.5; font-size: 11px; }}
  .id-eyebrow {{
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-soft);
  }}
  .id-wallet a {{
    color: var(--ink);
    text-decoration: none;
    font-weight: 500;
    font-size: 12px;
    border-bottom: 1px solid var(--line);
  }}
  .id-wallet a:hover {{ border-bottom-color: var(--ink-soft); }}

  h1 {{
    font-size: 26px;
    font-weight: 600;
    margin: 0;
    text-wrap: balance;
    letter-spacing: -0.01em;
  }}
  .subhead {{
    color: var(--ink-soft);
    font-size: 15px;
    margin-top: 4px;
    max-width: 60ch;
  }}

  .hero {{
    display: grid;
    grid-template-columns: 1.3fr 1fr;
    gap: 16px;
  }}
  @media (max-width: 640px) {{ .hero {{ grid-template-columns: 1fr; }} }}

  .card {{
    background: var(--surface);
    border-radius: var(--radius);
    padding: 24px 26px;
    border: 1px solid var(--line);
  }}

  .stat-label {{
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-soft);
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 100px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: none;
  }}
  .pill.good {{ background: var(--good-bg); color: var(--good); }}
  .pill.bad {{ background: var(--bad-bg); color: var(--bad); }}

  .stat-value {{
    font-size: 52px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    margin-top: 10px;
    line-height: 1;
  }}
  .stat-value.good {{ color: var(--good); }}
  .stat-note {{
    margin-top: 10px;
    font-size: 13.5px;
    color: var(--ink-soft);
  }}

  .side-stats {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .mini-stat {{
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px 20px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex: 1;
  }}
  .mini-stat .label {{
    font-size: 13px;
    font-weight: 500;
    color: var(--ink-soft);
  }}
  .mini-stat .value {{
    font-size: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}

  .flow-title {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--ink-soft);
    margin: 4px 0 -6px 2px;
  }}

  .flow {{
    display: grid;
    grid-template-columns: 1fr auto 1fr auto 1fr;
    align-items: center;
    gap: 12px;
  }}
  @media (max-width: 720px) {{
    .flow {{ grid-template-columns: 1fr; }}
    .flow .arrow {{ transform: rotate(90deg); justify-self: center; }}
  }}

  .leg {{
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .leg-kind {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 2px 0;
  }}
  .leg.collateral .leg-kind {{ color: var(--ink-soft); }}
  .leg.borrow .leg-kind {{ color: var(--accent-borrow); }}
  .leg.yield .leg-kind {{ color: var(--accent-yield); }}
  .leg-amount {{
    font-size: 20px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}
  .leg-usd {{
    font-size: 13px;
    color: var(--ink-soft);
    font-variant-numeric: tabular-nums;
  }}
  .leg-apy {{
    margin-top: 4px;
    font-size: 13px;
    font-weight: 500;
  }}
  .leg.borrow .leg-apy {{ color: var(--accent-borrow); }}
  .leg.yield .leg-apy {{ color: var(--accent-yield); }}
  .leg a {{ color: inherit; text-decoration: none; border-bottom: 1px dotted currentColor; }}

  .arrow {{
    color: var(--ink-soft);
    font-size: 20px;
    justify-self: center;
  }}

  .math {{
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 20px 24px;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    font-size: 15px;
  }}
  .math .term {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
  .math .op {{ color: var(--ink-soft); }}
  .math .sub {{ font-size: 11.5px; color: var(--ink-soft); display: block; font-weight: 400; margin-top: 1px;}}
  .math .chunk {{ display: flex; flex-direction: column; align-items: center; }}

  .section-title {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--ink-soft);
    margin: 4px 0 -6px 2px;
  }}
  .section-note {{
    font-size: 13.5px;
    color: var(--ink-soft);
    margin-top: 2px;
  }}

  .chart-legend {{
    display: flex;
    gap: 20px;
    margin-bottom: 4px;
    font-size: 13px;
    font-weight: 500;
  }}
  .chart-legend .key {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }}
  .chart-legend .swatch {{
    width: 14px;
    height: 2px;
    border-radius: 2px;
    display: inline-block;
  }}
  /* Style-only key: the dashed overlay appears in both line colours, so the
     swatch shows the pattern rather than claiming a colour. */
  .chart-legend .swatch-dash {{
    height: 0; border-top: 1.25px dashed var(--ink-soft); border-radius: 0;
  }}

  .chart-wrap {{ position: relative; }}
  .chart-svg {{ width: 100%; height: auto; display: block; overflow: visible; }}
  .chart-svg text {{ font-family: var(--font-ui); fill: var(--ink-soft); font-size: 11px; }}
  .chart-grid {{ stroke: var(--line); stroke-width: 1; }}
  .chart-endlabel {{ font-size: 12px; font-weight: 600; }}
  .chart-crosshair {{ stroke: var(--ink-soft); stroke-width: 1; }}
  .chart-annot text {{ font-size: 10.5px; }}
  .chart-preentry {{ fill: var(--ink); opacity: 0.045; }}
  .chart-entryline {{ stroke: var(--ink-soft); stroke-width: 1.5; stroke-dasharray: 4 3; }}
  .chart-entrylabel {{ font-size: 10.5px; font-weight: 500; }}

  /* LTV sensitivity control */
  .ltv-panel {{ margin-top: 4px; }}
  /* Sentence case at h2 scale, not the uppercase grey label used elsewhere: this
     section is interactive and should read as an invitation, not a caption. */
  .panel-title {{
    font-size: 19px; font-weight: 600; color: var(--ink);
    letter-spacing: -0.01em; margin: 0;
  }}
  .ltv-head {{
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 14px;
  }}
  .ltv-readout {{ display: flex; align-items: baseline; gap: 10px; }}
  .ltv-readout .big {{
    font-size: 34px; font-weight: 600; letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums; color: var(--good);
  }}
  .ltv-readout .unit {{ font-size: 13px; color: var(--ink-soft); }}
  .ltv-current {{ font-size: 12.5px; color: var(--ink-soft); }}
  .ltv-current b {{ color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }}
  .slider-row {{ display: flex; align-items: flex-start; gap: 14px; }}
  /* The scale must share the track's exact box, not the row's: the row also
     holds the readout, so measuring tick percentages against row width drifts
     them right of the values they label. */
  .slider-track {{ flex: 1; min-width: 0; }}
  .slider-row input[type=range] {{
    display: block; width: 100%; margin: 0;
    -webkit-appearance: none; appearance: none;
    height: 6px; border-radius: 4px; outline: none;
    /* Stops are anchored to real LTV thresholds (set from Python, since they
       depend on the market's LLTV) rather than spread evenly, so the red is
       concentrated near liquidation where the risk actually is. */
    background: linear-gradient(to right,
      var(--risk-lo) 0%,
      var(--risk-lo) {grad_safe_pct:.1f}%,
      var(--risk-mid) {grad_mid_pct:.1f}%,
      var(--risk-warn) {grad_warn_pct:.1f}%,
      var(--risk-hi) 100%);
  }}
  /* --thumb-color is set from JS to the risk tier at the current LTV, so the
     handle itself carries the same safe/caution/danger signal as the track. */
  .slider-row input[type=range] {{ --thumb-color: var(--risk-lo); }}
  .slider-row input[type=range]::-webkit-slider-thumb {{
    -webkit-appearance: none; appearance: none;
    width: 20px; height: 20px; border-radius: 50%;
    background: var(--thumb-color); border: 3px solid var(--surface);
    box-shadow: 0 1px 4px rgba(0,0,0,0.3); cursor: pointer;
  }}
  .slider-row input[type=range]::-moz-range-thumb {{
    width: 20px; height: 20px; border-radius: 50%;
    background: var(--thumb-color); border: 3px solid var(--surface);
    box-shadow: 0 1px 4px rgba(0,0,0,0.3); cursor: pointer;
  }}
  .slider-val {{
    font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums;
    min-width: 58px; text-align: right;
    /* Drop past the endpoint labels above the rail so the readout sits level
       with the rail itself, not with "liquidation 86%". */
    margin-top: 12px;
  }}
  .ltv-scale {{
    position: relative; height: 34px; margin-top: 6px;
    font-size: 10px; color: var(--ink-soft);
  }}
  /* Below this the four markers cannot fit side by side at any sensible font
     size, so the two contextual ones drop out and the anchors that matter --
     live and liquidation -- keep their positions. */
  @media (max-width: 560px) {{
    .ltv-scale .mark.is-optional {{ display: none; }}
  }}
  /* Inset by the thumb radius: a range thumb's centre travels from 10px to
     (width - 10px), never the full 0-100%, so ticks placed on raw percentages
     sit wrong at both ends. `left` is set as a calc() in JS against this. */
  .ltv-scale {{ --thumb-r: 10px; }}
  /* Marks are buttons, not labels: clicking one snaps the slider to that LTV,
     which is the fastest way to compare live vs target without dragging. */
  .ltv-scale .mark {{
    position: absolute; transform: translateX(-50%);
    text-align: center; white-space: nowrap;
    background: none; border: 0; padding: 3px 4px 0;
    font: inherit; color: var(--ink-soft); cursor: pointer;
    border-radius: 5px;
  }}
  .ltv-scale .mark:hover {{ color: var(--ink); background: var(--surface); z-index: 2; }}
  .ltv-scale .mark[aria-current="true"] {{ color: var(--ink); font-weight: 600; }}
  /* The operating target is the number readers should anchor on, so it stays
     emphasised even when the slider is parked elsewhere. */
  .ltv-scale .mark.is-target {{ color: var(--ink); font-weight: 600; }}
  .ltv-scale .mark.is-target::before {{ border-color: var(--ink); }}
  /* Track endpoints render ABOVE the rail (they precede the input in the DOM),
     keeping them clear of the clickable markers below it -- the 75% target lands
     at ~87% of an 86% scale, exactly where a right-aligned end label would sit. */
  .track-ends {{
    display: flex; justify-content: space-between;
    font-size: 10px; color: var(--ink-soft); margin-bottom: 5px;
  }}
  .track-ends .liq-end {{ color: var(--risk-hi); font-weight: 500; }}
  .ltv-scale .mark::before {{
    content: ''; display: block; width: 7px; height: 7px; border-radius: 50%;
    border: 1.5px solid var(--ink-soft); background: var(--surface);
    margin: 0 auto 4px; box-sizing: border-box;
  }}
  .ltv-scale .mark:hover::before {{ border-color: var(--ink); }}
  .ltv-scale .mark[aria-current="true"]::before {{ background: var(--ink); border-color: var(--ink); }}
  /* End labels are pulled inward so they don't overflow the card, but the tick
     must keep pointing at the real value -- so it's positioned against the
     label's own edge rather than its centre. */
  .ltv-scale .mark.at-end::before {{ margin-right: 0; }}
  .ltv-scale .mark.at-start::before {{ margin-left: 0; }}
  .target-row {{
    display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px;
    padding-top: 14px; border-top: 1px solid var(--line);
  }}
  .target {{
    flex: 1; min-width: 150px; padding: 10px 12px;
    border: 1px solid var(--line); border-radius: 10px; background: var(--surface-2);
  }}
  .target .t-label {{ font-size: 11px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.04em; }}
  .target .t-val {{ font-size: 15px; font-weight: 600; margin-top: 3px; font-variant-numeric: tabular-nums; }}
  /* Tiles carry the SAME risk tier as the slider track at that LTV, so a value
     reads consistently wherever it appears: green inside the operating band,
     amber approaching the target, red near liquidation, muted when unreachable. */
  .target.risk-lo {{ border-color: var(--risk-lo); background: var(--good-bg); }}
  .target.risk-lo .t-val {{ color: var(--risk-lo); }}
  .target.risk-mid {{ border-color: var(--risk-mid); }}
  .target.risk-mid .t-val {{ color: var(--risk-mid); }}
  .target.risk-warn {{ border-color: var(--risk-warn); }}
  .target.risk-warn .t-val {{ color: var(--risk-warn); }}
  .target.risk-hi {{ border-color: var(--risk-hi); background: var(--bad-bg); }}
  .target.risk-hi .t-val {{ color: var(--risk-hi); }}
  .target.unreachable .t-val {{ color: var(--ink-soft); }}
  .per-unit-note {{ font-size: 12px; color: var(--ink-soft); margin-top: 10px; }}
  .per-unit-note a {{ color: var(--accent-borrow); text-decoration: none; border-bottom: 1px dotted currentColor; }}

  /* The conservative-LTV framing is load-bearing for how every number on this
     page should be read, so it gets its own block rather than a footnote. */
  .callout {{
    display: flex; gap: 12px; align-items: flex-start;
    padding: 14px 16px; border-radius: 12px;
    background: var(--accent-borrow-bg);
    border: 1px solid var(--accent-borrow);
  }}
  .callout-mark {{
    flex: none; width: 20px; height: 20px; border-radius: 50%;
    background: var(--accent-borrow); color: var(--surface);
    font-size: 13px; font-weight: 700; line-height: 20px; text-align: center;
    margin-top: 1px;
  }}
  .callout-title {{ font-size: 14px; font-weight: 600; color: var(--ink); }}
  .callout-body {{ font-size: 12.5px; color: var(--ink-soft); margin-top: 4px; line-height: 1.5; }}
  .callout-body b {{ color: var(--ink); font-weight: 600; }}

  /* Supporting evidence between the simulator and the chart: its own quiet block
     rather than a footnote inside the simulator card, which made that card long
     enough to push the interactive controls off-screen. */
  .spread-strip {{
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    padding: 11px 14px; border: 1px solid var(--line); border-radius: 10px;
    background: var(--surface-2); font-size: 12px; color: var(--ink-soft);
  }}
  .spread-strip .strip-label {{
    font-weight: 600; color: var(--ink); font-size: 12px; flex: none;
  }}
  .spread-strip .strip-body {{ flex: 1 1 340px; line-height: 1.55; }}
  .spread-strip b {{ color: var(--good); font-weight: 600; }}
  .spread-strip a {{
    color: var(--accent-borrow); text-decoration: none;
    border-bottom: 1px dotted currentColor; white-space: nowrap;
  }}

  .mini-stat .sublabel {{
    display: block; font-style: normal; font-size: 10.5px;
    color: var(--ink-soft); opacity: 0.85; margin-top: 2px;
  }}

  /* Spread is the simulation's second input, so the horizon comparison lives
     here as a control rather than as a separate read-only strip. */
  .spread-picker {{
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    margin-bottom: 18px;
  }}
  .spread-picker .picker-label {{
    font-size: 12px; color: var(--ink-soft); margin-right: 2px;
  }}
  .spread-btn {{
    display: flex; flex-direction: column; align-items: flex-start; gap: 1px;
    padding: 6px 11px; border: 1px solid var(--line); border-radius: 9px;
    background: var(--surface-2); color: var(--ink-soft);
    font: inherit; cursor: pointer; text-align: left;
  }}
  .spread-btn i {{ font-style: normal; font-size: 10.5px; letter-spacing: 0.03em; }}
  .spread-btn b {{ font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .spread-btn:hover {{ border-color: var(--ink-soft); color: var(--ink); }}
  .spread-btn[aria-current="true"] {{
    border-color: var(--good); background: var(--good-bg); color: var(--ink);
  }}
  .spread-btn[aria-current="true"] b {{ color: var(--good); }}
  .spread-picker .picker-note {{
    flex: 1 1 100%; font-size: 11.5px; color: var(--ink-soft); margin-top: 2px;
  }}

  .chart-tooltip {{
    position: absolute;
    pointer-events: none;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 12.5px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.12);
    visibility: hidden;
    transform: translate(-50%, calc(-100% - 12px));
    white-space: nowrap;
    z-index: 5;
  }}
  .chart-tooltip .date {{ color: var(--ink-soft); font-size: 11px; margin-bottom: 3px; }}
  .chart-tooltip .row {{ display: flex; align-items: center; gap: 6px; }}
  .chart-tooltip .key {{ width: 10px; height: 2px; border-radius: 2px; display: inline-block; }}
  .chart-tooltip .val {{ font-weight: 600; font-variant-numeric: tabular-nums; margin-left: auto; padding-left: 10px; }}

  details.chart-table {{ font-size: 13px; }}
  details.chart-table summary {{
    cursor: pointer;
    color: var(--ink-soft);
    font-weight: 500;
    font-size: 13px;
  }}
  .table-scroll {{ overflow-x: auto; margin-top: 10px; }}
  table.data {{
    border-collapse: collapse;
    width: 100%;
    font-size: 12.5px;
  }}
  table.data th, table.data td {{
    text-align: right;
    padding: 5px 10px;
    font-variant-numeric: tabular-nums;
    border-bottom: 1px solid var(--line);
  }}
  table.data th:first-child, table.data td:first-child {{ text-align: left; font-variant-numeric: normal; }}
  table.data th {{ color: var(--ink-soft); font-weight: 500; }}

  footer {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12.5px;
    color: var(--ink-soft);
    border-top: 1px solid var(--line);
    padding-top: 16px;
  }}
  footer a {{ color: var(--ink-soft); text-decoration: underline; }}

  a:focus-visible, button:focus-visible {{
    outline: 2px solid var(--accent-borrow);
    outline-offset: 2px;
  }}

  @media (prefers-reduced-motion: no-preference) {{
    .card, .leg, .mini-stat {{ transition: border-color 0.15s ease; }}
  }}
</style>

<div class="page">

  <header class="top">
    <div class="logo">{logo_svg}</div>
    <div class="id-block">
      <span class="id-eyebrow">Ethereum · Upshift trader subaccount</span>
      <span class="id-sep" aria-hidden="true">·</span>
      <span class="id-wallet"><a href="{debank_url}" target="_blank" rel="noopener">{wallet[:6]}…{wallet[-4:]}</a></span>
      <span class="id-sep" aria-hidden="true">·</span>
      <span class="id-eyebrow">{as_of}</span>
    </div>
  </header>

  <div>
    <h1>lsETH carry position</h1>
    <div class="subhead">
      Borrow USDC against lsETH on Morpho, redeploy it into KPK USDC Morpho
      vaults, and keep the spread — extra yield on lsETH you were holding anyway.
    </div>
  </div>

  <div class="hero">
    <div class="card">
      <div class="stat-label">Carry on top of lsETH <span class="pill good" id="carryPill">↑ carry positive</span></div>
      <div class="stat-value good" id="heroCarry">+{net_lseth_after_fee:.2f}%</div>
      <div class="stat-note">
        What 1 lsETH earns <b>in addition to</b> its own staking yield, by borrowing
        USDC against it at <b id="heroLtv">{ltv:.0f}%</b> LTV and lending that USDC
        out — net of the 10% performance fee.
      </div>
    </div>
    <div class="side-stats">
      <div class="mini-stat">
        <span class="label">
          Spread: lend rate − borrow rate
          <em class="sublabel" id="spreadBasisTop">{default_basis_sub}</em>
        </span>
        <span class="value" style="color:var(--good)" id="spreadTop">+{default_spread:.2f} pp</span>
      </div>
      {total_apy_card}
    </div>
  </div>

  <div>
    <div class="flow-title">How the trade works, per 1 lsETH</div>
    <div class="flow">
      <div class="leg collateral">
        <div class="leg-kind">Collateral</div>
        <div class="leg-amount">1.000 lsETH</div>
        <div class="leg-usd">${lseth_price:,.0f}</div>
      </div>
      <div class="arrow">→</div>
      <div class="leg borrow">
        <div class="leg-kind">Borrow · <a href="{market_url}" target="_blank" rel="noopener">lsETH/USDC market ↗</a></div>
        <div class="leg-amount"><span id="perUnitBorrow">{per_unit_borrow:,.0f}</span> USDC</div>
        <div class="leg-usd">at <span id="perUnitLtv">{ltv:.0f}</span>% LTV</div>
        <div class="leg-apy">−{borrow['apy_pct']:.2f}% paid (spot rate)</div>
      </div>
      <div class="arrow">→</div>
      <div class="leg yield">
        <div class="leg-kind">Deposit · <a href="{vault_url}" target="_blank" rel="noopener">KPK USDC Yield ↗</a></div>
        <div class="leg-amount"><span id="perUnitDeposit">{per_unit_borrow:,.0f}</span> USDC</div>
        <div class="leg-usd">redeployed in full</div>
        <div class="leg-apy">+{dep['apy_pct']:.2f}% earned (spot, net of fees)</div>
      </div>
    </div>
    <div class="per-unit-note">
      KPK curates both sides of this market, and holds the borrow rate competitive
      with comparable 86%-LLTV USDC markets such as wstETH/USDC and rETH/USDC.
      The working below follows whichever spread you pick in the simulator.
      Scales linearly: 1 lsETH or 10,000 lsETH earns the same percentage.
      lsETH marked at ${lseth_price:,.0f} by the market's oracle.
    </div>
  </div>

  <div class="math">
    <div class="chunk"><span class="term" style="color:var(--accent-yield)" id="mathYield">{default_legs['yield_pct']:.2f}%</span><span class="sub" id="mathYieldSub">{default_yield_sub}</span></div>
    <span class="op">−</span>
    <div class="chunk"><span class="term" style="color:var(--accent-borrow)" id="mathBorrow">{default_legs['borrow_pct']:.2f}%</span><span class="sub" id="mathBorrowSub">{default_borrow_sub}</span></div>
    <span class="op">=</span>
    <div class="chunk"><span class="term" id="mathDelta">{default_spread:.2f} pp</span><span class="sub">Delta APY</span></div>
    <span class="op">×</span>
    <div class="chunk"><span class="term" id="mathLtv">{ltv:.0f}%</span><span class="sub">LTV</span></div>
    <span class="op">×</span>
    <div class="chunk"><span class="term">0.9</span><span class="sub">After 10% fee</span></div>
    <span class="op">=</span>
    <div class="chunk"><span class="term" style="color:var(--good)" id="mathCarry">+{net_lseth_after_fee:.2f}%</span><span class="sub">Carry on lsETH</span></div>
  </div>

  <div class="card ltv-panel">
    <div class="ltv-head">
      <div>
        <div class="panel-title">Simulate carry at your LTV</div>
        <div class="section-note" style="margin-top:4px">
          Pick a spread, then drag the handle or click a marker below.
        </div>
      </div>
      <div class="ltv-readout">
        <span class="big" id="sliderCarry">+{net_lseth_after_fee:.2f}%</span>
        <span class="unit">on lsETH, after fee<br>at <span id="spreadBasis">{default_basis_label}</span> spread</span>
      </div>
    </div>

    <div class="spread-picker" role="group" aria-label="Which spread to simulate with">
      <span class="picker-label">Spread</span>
      {spread_buttons}
      <span class="picker-note">
        <b>Live</b> is the forward-looking basis. The trailing options are a reality
        check on it, each pairing the vault's realized net APY with the borrow rate
        over the same window.
      </span>
    </div>

    <div class="slider-row">
      <div class="slider-track">
        <div class="track-ends" aria-hidden="true">
          <span>0%</span>
          <span class="liq-end">liquidation {lltv:.0f}%</span>
        </div>
        <input type="range" id="ltvSlider" min="0" max="{lltv:.0f}" step="1" value="{ltv:.0f}"
               aria-label="Borrow loan-to-value, percent">
        <div class="ltv-scale" id="ltvScale"></div>
      </div>
      <span class="slider-val" id="sliderLtv">{ltv:.0f}%</span>
    </div>

    <div class="target-row" id="targetRow">
      <div class="target">
        <div class="t-label">LTV needed for +1.5% APY</div>
        <div class="t-val" id="ltvFor15">—</div>
      </div>
      <div class="target">
        <div class="t-label">LTV needed for +2.0% APY</div>
        <div class="t-val" id="ltvFor20">—</div>
      </div>
      <div class="target">
        <div class="t-label">LTV needed for +2.5% APY</div>
        <div class="t-val" id="ltvFor25">—</div>
      </div>
    </div>
  </div>

  <div class="callout">
    <div class="callout-mark" aria-hidden="true">!</div>
    <div>
      <div class="callout-title">
        Today's {ltv:.0f}% LTV is deliberately conservative — read it as a floor,
        not a cap
      </div>
      <div class="callout-body">
        The position is run well below target while the rebalancing and
        anti-liquidation automations — already live — are finetuned. The intended
        range is <b>60–65%</b> as that tuning settles, rising to <b>~75%</b> once
        they have a longer track record. At 75% on the 30-day spread the same
        structure yields <b>+{carry_at_target:.2f}%</b> — drag the slider above to
        see any point in between.
      </div>
    </div>
  </div>

  <div class="spread-strip">
    <span class="strip-label">Backtest cross-check</span>
    <span class="strip-body">
      Over the 180 days to 30 Jul 2026, on reconstructed on-chain borrow rates and
      the vault's realized share price, this strategy at 75% LTV averaged
      <b>+2.11%</b> net over lsETH — monthly range 1.32% to 3.17%, with the 1.5%
      floor cleared on 75% of days. That run used deeper 86%-LLTV proxy markets,
      because lsETH/USDC had no rate history at the time; its own 45 days since
      have averaged 4.01% borrow at 89% utilization, against the proxy's 4.01%.
      <a href="{backtest_url}" target="_blank" rel="noopener">Query ↗</a>
    </span>
  </div>



  <div>
    <div class="section-title">Full history</div>
    <div class="section-note">
      Daily borrow rate (lsETH/USDC market) vs. net lend rate (KPK USDC Yield),
      since the market went live on {since_date}. {entry_sentence}
      Solid lines are the daily rates — the variation is real and drives realized
      performance. The dashed line is the vault's 30-day trend: its lend rate is
      share-price-derived, so a single day's step annualises into a spike, and the
      trend is the like-for-like view against the backtest. The borrow rate needs
      no trend line; it has held between 3.8% and 4.1% throughout.
    </div>
  </div>

  <div class="card chart-wrap">
    <div class="chart-legend">
      <span class="key"><span class="swatch" style="background:var(--chart-borrow)"></span>Borrow APY · lsETH/USDC</span>
      <span class="key"><span class="swatch" style="background:var(--chart-yield)"></span>Yield APY · KPK USDC Yield</span>
      <span class="key"><span class="swatch swatch-dash"></span>Yield · 30-day trend</span>
    </div>
    {chart['svg']}
    <div id="chartTooltip" class="chart-tooltip">
      <div class="date" id="ttDate"></div>
      <div class="row"><span class="key" style="background:var(--chart-yield)"></span>Yield<span class="val" id="ttYield"></span></div>
      <div class="row"><span class="key" style="background:var(--chart-borrow)"></span>Borrow<span class="val" id="ttBorrow"></span></div>
      <div class="row"><span style="width:10px"></span>Delta<span class="val" id="ttDelta"></span></div>
    </div>
  </div>

  <details class="chart-table">
    <summary>Show actual live position ({coll['amount']:.2f} lsETH)</summary>
    <div class="table-scroll">
      <table class="data">
        <thead><tr><th>Leg</th><th>Amount</th><th>USD</th><th>APY</th></tr></thead>
        <tbody>
          <tr><td>Collateral · lsETH</td><td>{coll['amount']:.4f}</td><td>${coll['usd']:,.0f}</td><td>—</td></tr>
          <tr><td>Borrow · USDC</td><td>{borrow['amount']:,.2f}</td><td>${borrow['usd']:,.0f}</td><td>{borrow['apy_pct']:.2f}%</td></tr>
          <tr><td>Deposit · KPK USDC Yield</td><td>{dep['amount']:,.2f}</td><td>${dep['usd']:,.0f}</td><td>{dep['apy_pct']:.2f}%</td></tr>
          <tr><td>Live LTV</td><td>{ltv:.2f}%</td><td>—</td><td>—</td></tr>
          <tr><td>Carry at live LTV</td><td>—</td><td>—</td><td>+{net_lseth:.2f}%</td></tr>
        </tbody>
      </table>
    </div>
    <div class="per-unit-note">
      {position_age_note} Sizing is a deliberately small pilot — the strategy's
      economics are a rate and don't depend on it.
      Verify the collateral and borrow legs on
      <a href="{debank_url}" target="_blank" rel="noopener">DeBank ↗</a>
      — it shows both positions, but not the underlying yield.
    </div>
  </details>

  <details class="chart-table">
    <summary>Show daily data table</summary>
    <div class="table-scroll">
      <table class="data">
        <thead><tr><th>Date</th><th>Borrow APY</th><th>Yield APY</th><th>Delta</th></tr></thead>
        <tbody>
          {chart['table_rows']}
        </tbody>
      </table>
    </div>
  </details>

  <footer>
    <span>Snapshot + history from Morpho GraphQL (api.morpho.org) — not auto-refreshing. Ask for an updated read, or ask to schedule one.</span>
    <span><a href="https://etherscan.io/address/{wallet}" target="_blank" rel="noopener">wallet ↗</a></span>
  </footer>

</div>

<script>
(function() {{
  // LTV sensitivity. Carry = spread x LTV, with the spread held at its live
  // value -- see the on-page caveat: real spreads compress as LTV rises, so the
  // high end is an upper bound.
  var SPREAD = {default_spread:.6f};   // mutable: the spread picker reassigns this
  var LIVE_LTV = {ltv:.4f};
  var LLTV = {lltv:.2f};
  // Must match the CSS gradient stops. Four bands: green to SAFE_MAX, olive
  // through the 75% operating target, orange above it, red near liquidation.
  var SAFE_MAX = 70, TARGET_MAX = 75, WARN_MAX = 80;
  var NET_OF_FEE = {net_fee_multiplier:.2f};   // 10% performance fee, as in the Phase 1 backtest
  var LSETH_APY = {lseth_apy_js};              // null if the staking rate fetch failed

  var slider = document.getElementById('ltvSlider');
  if (!slider) return;

  // Reference marks on the LTV axis: where we are, the pre-automation safe band,
  // the post-automation target, and the liquidation line.
  var MARKS = [
    // Only three marks: at any realistic width a fourth at LLTV collides with
    // the 75% target (they sit 11 LTV points apart on an 86-point scale). The
    // liquidation threshold is instead the red end of the track itself, labelled
    // once to the right of the scale.
    {{ ltv: LIVE_LTV, label: 'live ' + LIVE_LTV.toFixed(0) + '%' }},
    {{ ltv: 62.5, label: 'safe 60–65%', optional: true }},
    {{ ltv: 75, label: 'target 75%', emphasis: true }}
  ];
  var scale = document.getElementById('ltvScale');
  var markEls = [];
  MARKS.forEach(function(m) {{
    var el = document.createElement('button');
    el.type = 'button';
    el.className = 'mark';
    el.title = 'Set LTV to ' + m.ltv.toFixed(0) + '%';
    el.addEventListener('click', function() {{
      slider.value = m.ltv;
      render(m.ltv);
    }});
    markEls.push({{ el: el, ltv: m.ltv }});
    // Match the thumb's real travel: centre goes from +r to (width - r).
    var f = m.ltv / LLTV;
    el.style.left = 'calc(' + (f * 100) + '% + ' + ((0.5 - f) * 20) + 'px)';
    el.textContent = m.label;
    if (m.emphasis) el.classList.add('is-target');
    if (m.optional) el.classList.add('is-optional');
    // The end marks would otherwise hang off the track; anchor them inward so
    // the text stays inside the card while the tick still points at the value.
    // 0.85 rather than 0.88: the 75% target sits at 87% of an 86% LLTV scale, so
    // a higher threshold left it centred and overlapping the liquidation label.
    if (f >= 0.99) {{ el.classList.add('at-end'); el.style.transform = 'translateX(-100%)'; el.style.textAlign = 'right'; }}
    else if (f <= 0.01) {{ el.classList.add('at-start'); el.style.transform = 'translateX(0)'; el.style.textAlign = 'left'; }}
    else if (f > 0.8) {{
      // Left-anchor marks close to the end: the final mark is right-anchored, so
      // leaving this one centred makes the pair converge as the track narrows.
      el.classList.add('at-start'); el.style.transform = 'translateX(0)'; el.style.textAlign = 'left';
    }}
    scale.appendChild(el);
  }});

  // Inverse of carry = spread x LTV x (1 - fee). Returns the required LTV so the
  // label and the reachability colour derive from ONE number -- computing them
  // separately previously let them disagree by the fee factor, showing a green
  // tile that still read "needs a wider spread".
  function ltvNeededFor(target) {{
    return SPREAD > 0 ? target / (SPREAD * NET_OF_FEE) * 100 : Infinity;
  }}

  function ltvLabel(need) {{
    // Not reachable *at the selected spread* -- a wider spread lowers the
    // required LTV, so this is about current conditions, not a permanent
    // ceiling. Word it that way rather than printing a fake number.
    if (!isFinite(need)) return 'n/a at a negative spread';
    return need > LLTV ? 'needs a wider spread' : need.toFixed(0) + '% LTV';
  }}

  function render(ltv) {{
    var carry = SPREAD * ltv / 100 * NET_OF_FEE;
    var txt = (carry >= 0 ? '+' : '') + carry.toFixed(2) + '%';

    document.getElementById('sliderCarry').textContent = txt;
    document.getElementById('sliderLtv').textContent = ltv.toFixed(0) + '%';
    document.getElementById('heroCarry').textContent = txt;
    document.getElementById('heroLtv').textContent = ltv.toFixed(0) + '%';
    document.getElementById('mathLtv').textContent = ltv.toFixed(0) + '%';
    document.getElementById('mathCarry').textContent = txt;
    document.getElementById('perUnitLtv').textContent = ltv.toFixed(0);

    var perUnit = {lseth_price:.4f} * ltv / 100;
    var money = perUnit.toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
    document.getElementById('perUnitBorrow').textContent = money;
    document.getElementById('perUnitDeposit').textContent = money;

    var pill = document.getElementById('carryPill');
    pill.textContent = carry > 0 ? '↑ carry positive' : '↓ carry negative';
    pill.className = 'pill ' + (carry > 0 ? 'good' : 'bad');

    // Thumb takes the risk tier of wherever it now sits.
    slider.style.setProperty('--thumb-color', 'var(--' + riskTier(ltv) + ')');

    // Highlight a preset only when the slider is actually parked on it.
    markEls.forEach(function(m) {{
      m.el.setAttribute('aria-current', Math.abs(m.ltv - ltv) < 0.5 ? 'true' : 'false');
    }});

    // Total APY = lsETH staking + carry. Absent when the staking rate couldn't
    // be fetched, so guard rather than assume the element exists.
    var totalEl = document.getElementById('totalApy');
    if (totalEl && LSETH_APY !== null) {{
      totalEl.textContent = (LSETH_APY + carry).toFixed(2) + '%';
      document.getElementById('carryPart').textContent = carry.toFixed(2);
    }}

  }}

  // Depends on the spread rather than the slider, so it recomputes when the
  // spread changes. Colour carries reachability: green where a safe LTV gets
  // there, muted where the required LTV would exceed liquidation.
  // Shared with the track gradient and the thumb, so one LTV never reads as two
  // different risk levels in two places on the page.
  function riskTier(ltv) {{
    return ltv <= SAFE_MAX ? 'risk-lo'
      : ltv <= TARGET_MAX ? 'risk-mid'
      : ltv <= WARN_MAX ? 'risk-warn'
      : 'risk-hi';
  }}

  function renderTargets() {{
    [['ltvFor15', 1.5], ['ltvFor20', 2.0], ['ltvFor25', 2.5]].forEach(function(t) {{
      var need = ltvNeededFor(t[1]);
      var el = document.getElementById(t[0]);
      el.textContent = ltvLabel(need);
      // Tier off the ROUNDED value, which is what the tile displays: 82.05 shown
      // as "82%" must take the same colour the slider gives 82%, or the two
      // disagree at the band boundary.
      el.parentElement.className = 'target ' +
        (need <= LLTV ? riskTier(Math.round(need)) : 'unreachable');
    }});
  }}

  // Spread picker: switching horizon re-runs the whole simulation at the current
  // LTV, so the headline, the math strip and the targets all stay consistent.
  var spreadBtns = [].slice.call(document.querySelectorAll('.spread-btn'));
  spreadBtns.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      SPREAD = parseFloat(btn.getAttribute('data-spread'));
      spreadBtns.forEach(function(b) {{
        b.setAttribute('aria-current', b === btn ? 'true' : 'false');
      }});
      var basis = btn.querySelector('i').textContent;
      document.getElementById('spreadBasis').textContent = basis;

      // Move the whole working strip onto the selected basis. All three terms
      // must change together: a spot yield minus a spot borrow shown next to a
      // 30d spread is an equation that visibly doesn't add up.
      var yv = parseFloat(btn.getAttribute('data-yield'));
      var bv = parseFloat(btn.getAttribute('data-borrow'));
      var isSpot = basis === 'Live';
      document.getElementById('mathYield').textContent = yv.toFixed(2) + '%';
      document.getElementById('mathBorrow').textContent = bv.toFixed(2) + '%';
      document.getElementById('mathDelta').textContent = SPREAD.toFixed(2) + ' pp';
      document.getElementById('mathYieldSub').textContent = isSpot ? 'Yield APY' : 'Yield, ' + basis;
      document.getElementById('mathBorrowSub').textContent = isSpot ? 'Borrow APY' : 'Borrow, ' + basis;
      document.getElementById('spreadBasisTop').textContent =
        isSpot ? 'live spot rates' : basis + ' (trailing)';
      // Keep the hero's spread card in step with the picker, and say which
      // basis it's showing -- otherwise it silently contradicts the simulation.
      document.getElementById('spreadTop').textContent =
        (SPREAD >= 0 ? '+' : '') + SPREAD.toFixed(2) + ' pp';
      renderTargets();
      render(parseFloat(slider.value));
    }});
  }});

  slider.addEventListener('input', function() {{ render(parseFloat(slider.value)); }});
  renderTargets();
  render(parseFloat(slider.value));
}})();
</script>

<script>
(function() {{
  var borrow = {chart['borrow_js']};
  var yieldp = {chart['yield_js']};
  var scale = {{ x0: {chart['scale']['x0']}, x1: {chart['scale']['x1']}, padL: {chart['scale']['pad_l']}, plotW: {chart['scale']['plot_w']} }};
  var svg = document.querySelector('.chart-svg');
  var rect = document.getElementById('hoverRect');
  var crosshair = document.getElementById('crosshair');
  var tooltip = document.getElementById('chartTooltip');
  var elDate = document.getElementById('ttDate');
  var elYield = document.getElementById('ttYield');
  var elBorrow = document.getElementById('ttBorrow');
  var elDelta = document.getElementById('ttDelta');

  function nearest(series, t) {{
    var lo = 0, hi = series.length - 1;
    while (lo < hi) {{
      var mid = (lo + hi) >> 1;
      if (series[mid][0] < t) lo = mid + 1; else hi = mid;
    }}
    if (lo > 0 && Math.abs(series[lo - 1][0] - t) < Math.abs(series[lo][0] - t)) lo -= 1;
    return series[lo];
  }}

  function fmtDate(t) {{
    return new Date(t * 1000).toLocaleDateString(undefined, {{ year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }});
  }}

  if (rect) {{
    rect.addEventListener('pointermove', function(e) {{
      var box = svg.getBoundingClientRect();
      var px = e.clientX - box.left;
      var frac = Math.max(0, Math.min(1, (px * (840 / box.width) - scale.padL) / scale.plotW));
      var t = scale.x0 + frac * (scale.x1 - scale.x0);
      var b = nearest(borrow, t), y = nearest(yieldp, t);

      var cx = (scale.padL + (b[0] - scale.x0) / (scale.x1 - scale.x0) * scale.plotW) / 840 * box.width;
      crosshair.setAttribute('x1', (b[0] - scale.x0) / (scale.x1 - scale.x0) * scale.plotW + scale.padL);
      crosshair.setAttribute('x2', (b[0] - scale.x0) / (scale.x1 - scale.x0) * scale.plotW + scale.padL);
      crosshair.setAttribute('visibility', 'visible');

      elDate.textContent = fmtDate(b[0]);
      elYield.textContent = y[1].toFixed(2) + '%';
      elBorrow.textContent = b[1].toFixed(2) + '%';
      var d = y[1] - b[1];
      elDelta.textContent = (d >= 0 ? '+' : '') + d.toFixed(2) + ' pp';

      tooltip.style.left = cx + 'px';
      tooltip.style.top = (e.clientY - box.top) + 'px';
      tooltip.style.visibility = 'visible';
    }});
    rect.addEventListener('pointerleave', function() {{
      crosshair.setAttribute('visibility', 'hidden');
      tooltip.style.visibility = 'hidden';
    }});
  }}
}})();
</script>
"""

OUT.write_text(html)
print(f"wrote {OUT} ({len(html):,} chars)")
