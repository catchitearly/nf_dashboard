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
ATM_STRIKE      = 23700          # ATM strike price
STRIKE_STEP     = 50             # Nifty strike gap
EXPIRY_DATE     = "26519"        #  nearest Tuesday expiry
NUM_STRIKES     = 13             # ATM + 12 OTM  (indices 0..12)
SYMBOL_PREFIX   = "NSE:NIFTY"   # Fyers symbol prefix
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ── Fyers symbol builder ──────────────────────────────────────────────────────

def make_symbol(strike: int, opt_type: str, expiry: str) -> str:
    """Build Fyers option symbol, e.g. NSE:NIFTY25MAY2450024500CE"""
    dt = expiry
    #exp_str = dt.strftime("%d%b%Y").upper()          # 25MAY2026
    return f"{SYMBOL_PREFIX}{dt}{strike}{opt_type}"


def get_all_symbols(opt_type: str) -> list[dict]:
    strikes = [ATM_STRIKE + i * STRIKE_STEP for i in range(NUM_STRIKES)]
    return [{"strike": s, "symbol": make_symbol(s, opt_type, EXPIRY_DATE)} for s in strikes]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_path(symbol: str, fetch_date: str) -> Path:
    return DATA_DIR / f"{symbol}_{fetch_date}.json"


def load_cache(symbol: str, fetch_date: str) -> pd.DataFrame | None:
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
    client_id   = os.environ["FYERS_CLIENT_ID"]
    access_token = os.environ["FYERS_ACCESS_TOKEN"]
    fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
    return fyers


def fetch_candles(fyers, symbol: str, fetch_date: str) -> pd.DataFrame:
    """Fetch 5-min candles for a single symbol on fetch_date."""
    dt = datetime.strptime(fetch_date, "%Y-%m-%d")
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
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol   = df["volume"].cumsum()
    cum_tpvol = (tp * df["volume"]).cumsum()
    vwap = cum_tpvol / cum_vol
    vwap.name = "vwap"
    return vwap


# ── Difference series ─────────────────────────────────────────────────────────

def difference_series(df_a: pd.DataFrame, df_b: pd.DataFrame, col: str) -> pd.Series:
    """Aligned subtraction: strike[i] - strike[i+1]"""
    aligned = df_a[col].reindex(df_b[col].index).ffill()
    diff = aligned - df_b[col]
    return diff


# ── HTML dashboard generator ──────────────────────────────────────────────────

PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.32.0.min.js"

def ts_to_js(series: pd.Series) -> str:
    """Convert DatetimeIndex → ISO strings for Plotly."""
    return json.dumps([str(t) for t in series.index])


def vals_to_js(series: pd.Series) -> str:
    vals = series.where(series.notna(), other=None).tolist()
    return json.dumps(vals)


def generate_dashboard(
    ce_data: list[dict],   # [{"strike":…, "df":…, "vwap":…}, …]
    pe_data: list[dict],
    fetch_date: str,
    output_path: str = "docs/index.html",
):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    strikes = [d["strike"] for d in ce_data]

    # ── Build JS traces ──────────────────────────────────────────────────────
    def price_vwap_traces(data_list, opt_type, color_price, color_vwap):
        traces = []
        for item in data_list:
            df   = item["df"]
            vwap = item["vwap"]
            s    = item["strike"]
            label = f"{opt_type} {s}"
            traces.append({
                "x":    ts_to_js(df),
                "y":    vals_to_js(df["close"]),
                "name": f"{label} Price",
                "line": {"color": color_price, "width": 1.2},
                "hovertemplate": f"<b>{label} Price</b><br>%{{x}}<br>₹%{{y:.2f}}<extra></extra>",
            })
            traces.append({
                "x":    ts_to_js(vwap),
                "y":    vals_to_js(vwap),
                "name": f"{label} VWAP",
                "line": {"color": color_vwap, "width": 1.5, "dash": "dot"},
                "hovertemplate": f"<b>{label} VWAP</b><br>%{{x}}<br>₹%{{y:.2f}}<extra></extra>",
            })
        return traces

    def diff_traces(data_list, opt_type, colors):
        traces = []
        for i in range(len(data_list) - 1):
            a = data_list[i]
            b = data_list[i + 1]
            diff_p = difference_series(a["df"], b["df"], "close")
            diff_v = difference_series(a["vwap"].to_frame(), b["vwap"].to_frame(), "vwap")["vwap"]
            label = f"{opt_type} {a['strike']}–{b['strike']}"
            color = colors[i % len(colors)]
            traces.append({
                "x":    ts_to_js(diff_p),
                "y":    vals_to_js(diff_p),
                "name": f"{label} ΔPrice",
                "line": {"color": color, "width": 1.5},
                "hovertemplate": f"<b>{label} ΔPrice</b><br>%{{x}}<br>%{{y:.2f}}<extra></extra>",
            })
            traces.append({
                "x":    ts_to_js(diff_v),
                "y":    vals_to_js(diff_v),
                "name": f"{label} ΔVWAP",
                "line": {"color": color, "width": 1.2, "dash": "dash"},
                "hovertemplate": f"<b>{label} ΔVWAP</b><br>%{{x}}<br>%{{y:.2f}}<extra></extra>",
            })
        return traces

    CE_PRICE_CLR  = "#00d9ff"
    CE_VWAP_CLR   = "#0077ff"
    PE_PRICE_CLR  = "#ff6b6b"
    PE_VWAP_CLR   = "#cc0000"
    DIFF_PALETTE  = [
        "#f9ca24","#6ab04c","#e056fd","#badc58","#eb4d4b",
        "#22a6b3","#f0932b","#be2edd","#4834d4","#30336b","#686de0","#95afc0",
    ]

    ce_pv_traces   = price_vwap_traces(ce_data, "CE", CE_PRICE_CLR, CE_VWAP_CLR)
    pe_pv_traces   = price_vwap_traces(pe_data, "PE", PE_PRICE_CLR, PE_VWAP_CLR)
    ce_diff_traces = diff_traces(ce_data, "CE", DIFF_PALETTE)
    pe_diff_traces = diff_traces(pe_data, "PE", DIFF_PALETTE)

    def traces_js(tlist):
        items = []
        for t in tlist:
            line = t.get("line", {})
            line_str = json.dumps(line)
            items.append(
                f"{{"
                f"x:{t['x']},y:{t['y']},"
                f"name:{json.dumps(t['name'])},"
                f"type:'scatter',mode:'lines',"
                f"line:{line_str},"
                f"hovertemplate:{json.dumps(t['hovertemplate'])},"
                f"visible:true"
                f"}}"
            )
        return "[" + ",\n".join(items) + "]"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty Options Chain — {fetch_date}</title>
<script src="{PLOTLY_CDN}"></script>
<style>
  :root{{
    --bg:#0a0c10;--panel:#111521;--border:#1e2535;
    --text:#e2e8f0;--muted:#64748b;--accent:#00d9ff;
    --ce:#00d9ff;--pe:#ff6b6b;
    --font:'JetBrains Mono',monospace;
  }}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh}}
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');

  header{{
    padding:18px 28px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:20px;
    background:linear-gradient(90deg,#0a0c10 0%,#111521 100%);
  }}
  header h1{{font-size:17px;font-weight:700;letter-spacing:0.04em;color:var(--accent)}}
  header .meta{{color:var(--muted);font-size:11px}}
  header .pill{{
    padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;
    background:#1e2535;border:1px solid var(--border);color:var(--accent);
  }}

  .tab-bar{{
    display:flex;gap:0;border-bottom:1px solid var(--border);
    padding:0 16px;background:var(--panel);overflow-x:auto;
  }}
  .tab{{
    padding:11px 18px;cursor:pointer;font-size:12px;font-weight:600;
    color:var(--muted);border-bottom:2px solid transparent;
    white-space:nowrap;transition:color .15s,border-color .15s;
    letter-spacing:0.03em;
  }}
  .tab:hover{{color:var(--text)}}
  .tab.active{{color:var(--accent);border-bottom-color:var(--accent)}}
  .tab.ce{{color:var(--muted)}} .tab.ce.active{{color:var(--ce);border-bottom-color:var(--ce)}}
  .tab.pe{{color:var(--muted)}} .tab.pe.active{{color:var(--pe);border-bottom-color:var(--pe)}}

  .section{{display:none;padding:16px}}
  .section.active{{display:block}}

  .chart-card{{
    background:var(--panel);border:1px solid var(--border);border-radius:8px;
    margin-bottom:16px;overflow:hidden;
  }}
  .chart-header{{
    padding:10px 16px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:10px;
  }}
  .chart-title{{font-size:12px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase}}
  .badge{{
    padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;
    letter-spacing:0.06em;
  }}
  .badge.ce{{background:rgba(0,217,255,.12);color:var(--ce)}}
  .badge.pe{{background:rgba(255,107,107,.12);color:var(--pe)}}
  .badge.diff{{background:rgba(249,202,36,.1);color:#f9ca24}}

  .plotly-chart{{width:100%;min-height:340px}}

  footer{{
    padding:14px 28px;border-top:1px solid var(--border);
    color:var(--muted);font-size:10px;text-align:center;
    background:var(--panel);
  }}

  /* toggle legend */
  .legend-toggle{{margin-left:auto;font-size:10px;color:var(--muted);cursor:pointer;
    background:var(--bg);border:1px solid var(--border);padding:3px 8px;border-radius:4px}}
  .legend-toggle:hover{{color:var(--text)}}
</style>
</head>
<body>

<header>
  <div>
    <h1>⚡ NIFTY OPTIONS CHAIN</h1>
    <div class="meta">ATM {ATM_STRIKE} · Step {STRIKE_STEP} · Expiry {EXPIRY_DATE} · 5-min candles</div>
  </div>
  <div class="pill">📅 {fetch_date}</div>
  <div class="pill">ATM+12 OTM</div>
</header>

<div class="tab-bar">
  <div class="tab ce active" data-tab="ce-pv">CE Price & VWAP</div>
  <div class="tab pe"        data-tab="pe-pv">PE Price & VWAP</div>
  <div class="tab ce"        data-tab="ce-diff">CE Differences</div>
  <div class="tab pe"        data-tab="pe-diff">PE Differences</div>
</div>

<!-- CE Price & VWAP -->
<div id="ce-pv" class="section active">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE — All Strikes · Price &amp; VWAP</span>
      <span class="badge ce">CALL</span>
      <button class="legend-toggle" onclick="toggleLegend('ce_pv_chart')">Toggle Legend</button>
    </div>
    <div id="ce_pv_chart" class="plotly-chart"></div>
  </div>
</div>

<!-- PE Price & VWAP -->
<div id="pe-pv" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE — All Strikes · Price &amp; VWAP</span>
      <span class="badge pe">PUT</span>
      <button class="legend-toggle" onclick="toggleLegend('pe_pv_chart')">Toggle Legend</button>
    </div>
    <div id="pe_pv_chart" class="plotly-chart"></div>
  </div>
</div>

<!-- CE Differences -->
<div id="ce-diff" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE — Strike-to-Strike Price &amp; VWAP Difference</span>
      <span class="badge ce">CALL</span>
      <span class="badge diff">Δ DIFF</span>
      <button class="legend-toggle" onclick="toggleLegend('ce_diff_chart')">Toggle Legend</button>
    </div>
    <div id="ce_diff_chart" class="plotly-chart"></div>
  </div>
</div>

<!-- PE Differences -->
<div id="pe-diff" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE — Strike-to-Strike Price &amp; VWAP Difference</span>
      <span class="badge pe">PUT</span>
      <span class="badge diff">Δ DIFF</span>
      <button class="legend-toggle" onclick="toggleLegend('pe_diff_chart')">Toggle Legend</button>
    </div>
    <div id="pe_diff_chart" class="plotly-chart"></div>
  </div>
</div>

<footer>Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')} · Data: Fyers API · Cache: data/</footer>

<script>
const LAYOUT_BASE = {{
  paper_bgcolor:'#111521',
  plot_bgcolor:'#0a0c10',
  font:{{family:"'JetBrains Mono', monospace",color:'#e2e8f0',size:11}},
  xaxis:{{
    gridcolor:'#1e2535',showgrid:true,zeroline:false,
    tickformat:'%H:%M',tickfont:{{size:10}},
  }},
  yaxis:{{
    gridcolor:'#1e2535',showgrid:true,zeroline:false,
    tickfont:{{size:10}},tickprefix:'₹',
  }},
  legend:{{
    bgcolor:'rgba(17,21,33,0.85)',bordercolor:'#1e2535',borderwidth:1,
    font:{{size:10}},orientation:'v',x:1.01,y:1,
  }},
  hovermode:'x unified',
  hoverlabel:{{bgcolor:'#1e2535',bordercolor:'#2d3748',font:{{family:"'JetBrains Mono', monospace",size:11}}}},
  margin:{{l:55,r:10,t:15,b:45}},
  dragmode:'zoom',
}};

const DIFF_LAYOUT = JSON.parse(JSON.stringify(LAYOUT_BASE));
DIFF_LAYOUT.yaxis.tickprefix = '';

const CONFIG = {{
  responsive:true,
  displayModeBar:true,
  modeBarButtonsToRemove:['select2d','lasso2d','autoScale2d'],
  displaylogo:false,
  scrollZoom:true,
}};

const cePvTraces   = {traces_js(ce_pv_traces)};
const pePvTraces   = {traces_js(pe_pv_traces)};
const ceDiffTraces = {traces_js(ce_diff_traces)};
const peDiffTraces = {traces_js(pe_diff_traces)};

let chartsInit = {{}};

function initChart(divId, traces, layout) {{
  Plotly.newPlot(divId, traces, layout, CONFIG);
  chartsInit[divId] = true;
}}

function renderVisible(tabId) {{
  if (tabId === 'ce-pv'   && !chartsInit['ce_pv_chart'])   initChart('ce_pv_chart',   cePvTraces,   LAYOUT_BASE);
  if (tabId === 'pe-pv'   && !chartsInit['pe_pv_chart'])   initChart('pe_pv_chart',   pePvTraces,   LAYOUT_BASE);
  if (tabId === 'ce-diff' && !chartsInit['ce_diff_chart']) initChart('ce_diff_chart', ceDiffTraces, DIFF_LAYOUT);
  if (tabId === 'pe-diff' && !chartsInit['pe_diff_chart']) initChart('pe_diff_chart', peDiffTraces, DIFF_LAYOUT);
}}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    tab.classList.add('active');
    const id = tab.dataset.tab;
    document.getElementById(id).classList.add('active');
    renderVisible(id);
  }});
}});

function toggleLegend(divId) {{
  const gd = document.getElementById(divId);
  const curr = gd.layout?.showlegend;
  Plotly.relayout(divId, {{showlegend: curr === false ? true : false}});
}}

// Init first tab
renderVisible('ce-pv');
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ Dashboard written → {output_path}")


# ── Main orchestration ────────────────────────────────────────────────────────

def parse_date(raw: str) -> str:
    """Accept 'today', YYYY-MM-DD, or DD-MM-YYYY → returns YYYY-MM-DD."""
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
    parser.add_argument(
        "--date", default="today",
        help="Fetch date: 'today' or YYYY-MM-DD or DD-MM-YYYY (default: today)"
    )
    parser.add_argument(
        "--output", default="docs/index.html",
        help="Output HTML path (default: docs/index.html)"
    )
    args = parser.parse_args()

    fetch_date = parse_date(args.date)
    print(f"\n📅 Fetch date : {fetch_date}")
    print(f"🎯 ATM strike : {ATM_STRIKE}  |  Step: {STRIKE_STEP}  |  Expiry: {EXPIRY_DATE}")
    print(f"📊 Strikes    : ATM to ATM+{NUM_STRIKES-1} OTM  ({NUM_STRIKES} strikes)")

    fyers = get_fyers_client()

    ce_syms = get_all_symbols("CE")
    pe_syms = get_all_symbols("PE")

    print("\n── Fetching CE ──────────────────────────────────")
    ce_data = []
    for item in ce_syms:
        df = fetch_or_load(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        ce_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap})

    print("\n── Fetching PE ──────────────────────────────────")
    pe_data = []
    for item in pe_syms:
        df = fetch_or_load(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        pe_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap})

    print("\n── Generating Dashboard ─────────────────────────")
    generate_dashboard(ce_data, pe_data, fetch_date, args.output)


if __name__ == "__main__":
    main()
