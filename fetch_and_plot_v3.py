"""
Nifty 50 Options Chain - 5min Historical Data Fetcher & Dashboard Generator
============================================================================
Fetches CE/PE OTM chain (ATM to ATM+12) using Fyers API, caches locally,
computes VWAP & price differences, outputs interactive HTML dashboard.
"""

import os
import json
import argparse
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import numpy as np
from fyers_apiv3 import fyersModel

# ─────────────────────────────────────────────────────────────────────────────
# HARDCODED CONFIG — edit these before each expiry
# ─────────────────────────────────────────────────────────────────────────────
ATM_STRIKE      = 23500          # ATM strike price
STRIKE_STEP     = 50             # Nifty strike gap
EXPIRY_DATE     = "26519"        # nearest Tuesday expiry
NUM_STRIKES     = 10             # ATM + 12 OTM  (indices 0..12)
SYMBOL_PREFIX   = "NSE:NIFTY"   # Fyers symbol prefix
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ── Fyers symbol builder ──────────────────────────────────────────────────────

def make_symbol(strike: int, opt_type: str, expiry: str) -> str:
    return f"{SYMBOL_PREFIX}{expiry}{strike}{opt_type}"


def get_all_symbols(opt_type: str) -> list[dict]:
    if opt_type == "CE":
        # CE OTM: ATM, ATM+1, ATM+2, ... (ascending — higher strikes are OTM for calls)
        strikes = [ATM_STRIKE + i * STRIKE_STEP for i in range(NUM_STRIKES)]
    else:
        # PE OTM: ATM, ATM-1, ATM-2, ... (descending — lower strikes are OTM for puts)
        strikes = [ATM_STRIKE - i * STRIKE_STEP for i in range(NUM_STRIKES)]
    return [{"strike": s, "symbol": make_symbol(s, opt_type, EXPIRY_DATE)} for s in strikes]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_path(symbol: str, fetch_date: str) -> Path:
    return DATA_DIR / f"{symbol}_{fetch_date}.json"


def load_cache(symbol: str, fetch_date: str):
    p = cache_path(symbol, fetch_date)
    if p.exists():
        print(f"  [cache] {symbol}")
        with open(p) as f:
            candles = json.load(f)
        return candles_to_df(candles)
    return None


def save_cache(symbol: str, fetch_date: str, candles: list):
    p = cache_path(symbol, fetch_date)
    with open(p, "w") as f:
        json.dump(candles, f)


def candles_to_df(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("timestamp").sort_index()
    return df


# ── Fyers API fetch ───────────────────────────────────────────────────────────

def get_fyers_client() -> fyersModel.FyersModel:
    client_id    = os.environ["FYERS_CLIENT_ID"]
    access_token = os.environ["FYERS_ACCESS_TOKEN"]
    fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
    return fyers


def fetch_candles(fyers, symbol: str, fetch_date: str) -> pd.DataFrame:
    dt         = datetime.strptime(fetch_date, "%Y-%m-%d")
    range_from = dt.strftime("%Y-%m-%d")
    range_to   = dt.strftime("%Y-%m-%d")
    data = {
        "symbol":      symbol,
        "resolution":  "5",
        "date_format": "1",
        "range_from":  range_from,
        "range_to":    range_to,
        "cont_flag":   "1",
    }
    response = fyers.history(data=data)
    if response.get("s") != "ok":
        raise RuntimeError(f"Fyers API error for {symbol}: {response}")
    candles = response["candles"]
    save_cache(symbol, fetch_date, candles)
    return candles_to_df(candles)


def fetch_or_load(fyers, symbol: str, fetch_date: str) -> pd.DataFrame:
    cached = load_cache(symbol, fetch_date)
    if cached is not None:
        return cached
    print(f"  [api]   {symbol}")
    return fetch_candles(fyers, symbol, fetch_date)


# ── VWAP computation ──────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    tp        = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol   = df["volume"].cumsum()
    cum_tpvol = (tp * df["volume"]).cumsum()
    vwap      = cum_tpvol / cum_vol
    vwap.name = "vwap"
    return vwap


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts_to_list(series: pd.Series) -> list:
    return [str(t) for t in series.index]

def vals_to_list(series: pd.Series) -> list:
    return series.where(series.notna(), other=None).tolist()


# ── Dashboard generator ───────────────────────────────────────────────────────

def generate_dashboard(
    ce_data: list[dict],
    pe_data: list[dict],
    fetch_date: str,
    output_path: str = "docs/index.html",
):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    DIFF_PALETTE = [
        "#f9ca24", "#6ab04c", "#e056fd", "#badc58", "#eb4d4b",
        "#22a6b3", "#f0932b", "#be2edd", "#4834d4", "#30336b", "#686de0", "#95afc0",
    ]

    # ── Build all-strikes price+vwap trace list ───────────────────────────────
    def build_pv_traces(data_list, price_clr, vwap_clr):
        traces = []
        for item in data_list:
            df   = item["df"]
            vwap = item["vwap"]
            s    = item["strike"]
            traces.append({
                "x": ts_to_list(df), "y": vals_to_list(df["close"]),
                "name": f"{s} Price", "type": "scatter", "mode": "lines",
                "line": {"color": price_clr, "width": 1.2},
            })
            traces.append({
                "x": ts_to_list(vwap), "y": vals_to_list(vwap),
                "name": f"{s} VWAP", "type": "scatter", "mode": "lines",
                "line": {"color": vwap_clr, "width": 1.5, "dash": "dot"},
            })
        return traces

    # ── Build per-pair diff trace lists ───────────────────────────────────────
    def build_diff_pairs(data_list, opt_type):
        pairs = []
        for i in range(len(data_list) - 1):
            a      = data_list[i]
            b      = data_list[i + 1]
            diff_p = a["df"]["close"].reindex(b["df"]["close"].index).ffill() - b["df"]["close"]
            diff_v = a["vwap"].reindex(b["vwap"].index).ffill() - b["vwap"]
            color  = DIFF_PALETTE[i % len(DIFF_PALETTE)]
            label  = f"{a['strike']}-{b['strike']}"
            pairs.append({
                "label":    label,
                "div_id":   f"{opt_type.lower()}_diff_{i}",
                "color":    color,
                "price_x":  ts_to_list(diff_p),
                "price_y":  vals_to_list(diff_p),
                "vwap_x":   ts_to_list(diff_v),
                "vwap_y":   vals_to_list(diff_v),
            })
        return pairs

    ce_pv_traces = build_pv_traces(ce_data, "#00d9ff", "#0077ff")
    pe_pv_traces = build_pv_traces(pe_data, "#ff6b6b", "#cc0000")
    ce_pairs     = build_diff_pairs(ce_data, "CE")
    pe_pairs     = build_diff_pairs(pe_data, "PE")

    # ── Serialise to JS ───────────────────────────────────────────────────────
    ce_pv_js = json.dumps(ce_pv_traces)
    pe_pv_js = json.dumps(pe_pv_traces)

    # Build JS variable declarations for every diff pair
    # Each pair becomes:  const ce_diff_0 = [{...},{...}];
    def diff_vars_js(pairs):
        lines = []
        for p in pairs:
            price_trace = {
                "x": p["price_x"], "y": p["price_y"],
                "name": p["label"] + " \u0394Price",
                "type": "scatter", "mode": "lines",
                "line": {"color": p["color"], "width": 1.8},
            }
            vwap_trace = {
                "x": p["vwap_x"], "y": p["vwap_y"],
                "name": p["label"] + " \u0394VWAP",
                "type": "scatter", "mode": "lines",
                "line": {"color": p["color"], "width": 1.4, "dash": "dash"},
            }
            lines.append(f"const {p['div_id']} = {json.dumps([price_trace, vwap_trace])};")
        return "\n".join(lines)

    ce_diff_vars = diff_vars_js(ce_pairs)
    pe_diff_vars = diff_vars_js(pe_pairs)

    # Build the renderTab if-blocks for diff pairs
    def diff_render_js(pairs, tab_id):
        lines = []
        for p in pairs:
            lines.append(
                f"  if(tabId==='{tab_id}' && !chartsInit['{p['div_id']}']){{"
                f"Plotly.newPlot('{p['div_id']}',{p['div_id']},DIFF_LAYOUT,CONFIG);"
                f"chartsInit['{p['div_id']}']=true;}}"
            )
        return "\n".join(lines)

    ce_diff_render = diff_render_js(ce_pairs, "ce-diff")
    pe_diff_render = diff_render_js(pe_pairs, "pe-diff")

    # Build HTML chart cards for a list of pairs
    def diff_cards_html(pairs, badge_cls):
        cards = []
        for p in pairs:
            cards.append(
                f'<div class="chart-card">'
                f'<div class="chart-header">'
                f'<span class="chart-title">{p["label"]} &nbsp;&middot;&nbsp; \u0394Price &amp; \u0394VWAP</span>'
                f'<span class="badge {badge_cls}">{badge_cls.upper()}</span>'
                f'<span class="badge diff">\u0394 DIFF</span>'
                f'<button class="legend-toggle" onclick="toggleLegend(\'{p["div_id"]}\')">Legend</button>'
                f'</div>'
                f'<div id="{p["div_id"]}" class="plotly-chart"></div>'
                f'</div>'
            )
        return "\n".join(cards)

    ce_cards_html = diff_cards_html(ce_pairs, "ce")
    pe_cards_html = diff_cards_html(pe_pairs, "pe")

    # ── Write HTML ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty Options Chain \u2014 {fetch_date}</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');
  :root{{
    --bg:#0a0c10;--panel:#111521;--border:#1e2535;
    --text:#e2e8f0;--muted:#64748b;--accent:#00d9ff;
    --ce:#00d9ff;--pe:#ff6b6b;
  }}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;min-height:100vh}}

  header{{
    padding:16px 24px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:16px;
    background:linear-gradient(90deg,#0a0c10,#111521);
  }}
  header h1{{font-size:16px;font-weight:700;letter-spacing:0.05em;color:var(--accent)}}
  header .meta{{color:var(--muted);font-size:11px;margin-top:2px}}
  .pill{{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;
    background:#1e2535;border:1px solid var(--border);color:var(--accent);margin-left:auto}}

  .tab-bar{{display:flex;border-bottom:1px solid var(--border);
    padding:0 16px;background:var(--panel);overflow-x:auto;gap:0}}
  .tab{{padding:11px 16px;cursor:pointer;font-size:11px;font-weight:700;
    color:var(--muted);border-bottom:2px solid transparent;
    white-space:nowrap;transition:color .15s,border-color .15s;letter-spacing:0.04em}}
  .tab:hover{{color:var(--text)}}
  .tab.active.ce-tab{{color:var(--ce);border-bottom-color:var(--ce)}}
  .tab.active.pe-tab{{color:var(--pe);border-bottom-color:var(--pe)}}

  .section{{display:none;padding:16px 20px}}
  .section.active{{display:block}}

  .chart-card{{background:var(--panel);border:1px solid var(--border);
    border-radius:8px;margin-bottom:20px;overflow:hidden}}
  .chart-header{{padding:9px 14px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .chart-title{{font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase}}
  .badge{{padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:0.05em}}
  .badge.ce{{background:rgba(0,217,255,.12);color:var(--ce)}}
  .badge.pe{{background:rgba(255,107,107,.12);color:var(--pe)}}
  .badge.diff{{background:rgba(249,202,36,.1);color:#f9ca24}}
  .legend-toggle{{margin-left:auto;font-size:10px;color:var(--muted);cursor:pointer;
    background:var(--bg);border:1px solid var(--border);padding:3px 8px;border-radius:4px}}
  .legend-toggle:hover{{color:var(--text)}}
  .plotly-chart{{width:100%;min-height:300px}}

  footer{{padding:12px 24px;border-top:1px solid var(--border);
    color:var(--muted);font-size:10px;text-align:center;background:var(--panel)}}
</style>
</head>
<body>

<header>
  <div>
    <h1>\u26a1 NIFTY OPTIONS CHAIN</h1>
    <div class="meta">ATM {ATM_STRIKE} &middot; Step {STRIKE_STEP} &middot; Expiry {EXPIRY_DATE} &middot; 5-min</div>
  </div>
  <div class="pill">\U0001f4c5 {fetch_date}</div>
</header>

<div class="tab-bar">
  <div class="tab ce-tab active" data-tab="ce-pv">CE &middot; Price &amp; VWAP</div>
  <div class="tab pe-tab"        data-tab="pe-pv">PE &middot; Price &amp; VWAP</div>
  <div class="tab ce-tab"        data-tab="ce-diff">CE &middot; All Pair \u0394</div>
  <div class="tab pe-tab"        data-tab="pe-diff">PE &middot; All Pair \u0394</div>
</div>

<!-- CE Price & VWAP -->
<div id="ce-pv" class="section active">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE \u2014 All Strikes &middot; Price &amp; VWAP</span>
      <span class="badge ce">CALL</span>
      <button class="legend-toggle" onclick="toggleLegend('ce_pv_chart')">Legend</button>
    </div>
    <div id="ce_pv_chart" class="plotly-chart"></div>
  </div>
</div>

<!-- PE Price & VWAP -->
<div id="pe-pv" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE \u2014 All Strikes &middot; Price &amp; VWAP</span>
      <span class="badge pe">PUT</span>
      <button class="legend-toggle" onclick="toggleLegend('pe_pv_chart')">Legend</button>
    </div>
    <div id="pe_pv_chart" class="plotly-chart"></div>
  </div>
</div>

<!-- CE Diff — all pairs, each in its own plot, all under one tab -->
<div id="ce-diff" class="section">
{ce_cards_html}
</div>

<!-- PE Diff — all pairs, each in its own plot, all under one tab -->
<div id="pe-diff" class="section">
{pe_cards_html}
</div>

<footer>Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')} &middot; Fyers API &middot; cache: data/</footer>

<script>
const LAYOUT = {{
  paper_bgcolor:'#111521', plot_bgcolor:'#0a0c10',
  font:{{family:"'JetBrains Mono',monospace", color:'#e2e8f0', size:11}},
  xaxis:{{gridcolor:'#1e2535', showgrid:true, zeroline:false, tickformat:'%H:%M', tickfont:{{size:10}}}},
  yaxis:{{gridcolor:'#1e2535', showgrid:true, zeroline:false, tickfont:{{size:10}}, tickprefix:'\u20b9'}},
  legend:{{bgcolor:'rgba(17,21,33,.85)', bordercolor:'#1e2535', borderwidth:1, font:{{size:10}}}},
  hovermode:'x unified',
  hoverlabel:{{bgcolor:'#1e2535', bordercolor:'#2d3748', font:{{family:"'JetBrains Mono',monospace", size:11}}}},
  margin:{{l:55, r:10, t:12, b:45}},
  dragmode:'zoom',
}};
const DIFF_LAYOUT = Object.assign({{}}, LAYOUT, {{
  yaxis: Object.assign({{}}, LAYOUT.yaxis, {{tickprefix:''}})
}});
const CONFIG = {{
  responsive:true, displayModeBar:true,
  modeBarButtonsToRemove:['select2d','lasso2d'],
  displaylogo:false, scrollZoom:true
}};

// ── Diff trace data (one JS array per pair) ──
{ce_diff_vars}
{pe_diff_vars}

let chartsInit = {{}};

function toggleLegend(id) {{
  const gd  = document.getElementById(id);
  const cur = gd.layout && gd.layout.showlegend;
  Plotly.relayout(id, {{showlegend: cur === false ? true : false}});
}}

function renderTab(tabId) {{
  if (tabId === 'ce-pv' && !chartsInit['ce_pv_chart']) {{
    Plotly.newPlot('ce_pv_chart', {ce_pv_js}, LAYOUT, CONFIG);
    chartsInit['ce_pv_chart'] = true;
  }}
  if (tabId === 'pe-pv' && !chartsInit['pe_pv_chart']) {{
    Plotly.newPlot('pe_pv_chart', {pe_pv_js}, LAYOUT, CONFIG);
    chartsInit['pe_pv_chart'] = true;
  }}
{ce_diff_render}
{pe_diff_render}
}}

document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    tab.classList.add('active');
    const id = tab.dataset.tab;
    document.getElementById(id).classList.add('active');
    renderTab(id);
  }});
}});

renderTab('ce-pv');
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n\u2705 Dashboard written \u2192 {output_path}")


# ── Main orchestration ────────────────────────────────────────────────────────

def parse_date(raw: str) -> str:
    if raw.lower() == "today":
        return date.today().strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}  (use YYYY-MM-DD or DD-MM-YYYY)")


def main():
    parser = argparse.ArgumentParser(description="Nifty Options Dashboard")
    parser.add_argument("--date",   default="today",           help="Fetch date: today | YYYY-MM-DD | DD-MM-YYYY")
    parser.add_argument("--output", default="docs/index.html", help="Output HTML path")
    args = parser.parse_args()

    fetch_date = parse_date(args.date)
    print(f"\n\U0001f4c5 Fetch date : {fetch_date}")
    print(f"\U0001f3af ATM strike : {ATM_STRIKE}  |  Step: {STRIKE_STEP}  |  Expiry: {EXPIRY_DATE}")
    print(f"\U0001f4ca Strikes    : CE ATM to ATM+{NUM_STRIKES-1} OTM | PE ATM to ATM-{NUM_STRIKES-1} OTM  ({NUM_STRIKES} strikes each)")

    fyers = get_fyers_client()

    ce_syms = get_all_symbols("CE")
    pe_syms = get_all_symbols("PE")

    print("\n\u2500\u2500 Fetching CE \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    ce_data = []
    for item in ce_syms:
        df   = fetch_or_load(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        ce_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap})

    print("\n\u2500\u2500 Fetching PE \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    pe_data = []
    for item in pe_syms:
        df   = fetch_or_load(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        pe_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap})

    print("\n\u2500\u2500 Generating Dashboard \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    generate_dashboard(ce_data, pe_data, fetch_date, args.output)


if __name__ == "__main__":
    main()
