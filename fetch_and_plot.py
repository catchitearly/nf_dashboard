"""
Nifty 50 Options Chain - 5min Historical Data Fetcher & Dashboard Generator
============================================================================
Fetches CE/PE OTM chain (ATM to ATM+12) + Nifty spot using Fyers API,
caches locally, computes VWAP, IV (trading-minutes T), and IV crush analysis,
outputs interactive HTML dashboard.
"""

import os
import json
import argparse
import warnings
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from fyers_apiv3 import fyersModel

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# HARDCODED CONFIG — edit these before each expiry
# ─────────────────────────────────────────────────────────────────────────────
ATM_STRIKE      = 23700              # ATM strike price
STRIKE_STEP     = 50                 # Nifty strike gap
EXPIRY_DATE     = "26519"            # Fyers expiry string (nearest Tuesday)
EXPIRY_DATETIME = datetime(2026, 5, 19, 15, 30)  # actual expiry datetime (IST) — update per expiry
NUM_STRIKES     = 9                 # ATM + 12 OTM  (indices 0..12)
SYMBOL_PREFIX   = "NSE:NIFTY"       # Fyers symbol prefix
SPOT_SYMBOL     = "NSE:NIFTY50-INDEX"
RISK_FREE_RATE  = 0.0525              # RBI repo rate ~6.5%
DIVIDEND_YIELD  = 0.0                # assume 0 for intraday

# NSE market hours (IST)
MARKET_OPEN     = dtime(9, 15)
MARKET_CLOSE    = dtime(15, 30)
MINUTES_PER_DAY = 375               # 9:15 to 15:30
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


# ── CSV cache helpers ─────────────────────────────────────────────────────────
#
# Each symbol+date gets its own CSV: data/<safe_symbol>_<date>.csv
# On every cron run:
#   - If CSV exists: load it, fetch fresh API data, append only rows newer
#     than the last stored timestamp, re-save.
#   - If CSV missing: fetch full day, save fresh.
# The CSV grows candle-by-candle during live market hours.

def csv_path(symbol: str, fetch_date: str) -> Path:
    safe = symbol.replace(":", "_").replace("-", "_")
    return DATA_DIR / f"{safe}_{fetch_date}.csv"


def candles_to_df(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("timestamp").sort_index()
    return df


def load_csv(symbol: str, fetch_date: str):
    p = csv_path(symbol, fetch_date)
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col="timestamp", parse_dates=["timestamp"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")
    return df.sort_index()


def save_csv(symbol: str, fetch_date: str, df: pd.DataFrame):
    csv_path(symbol, fetch_date).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path(symbol, fetch_date))


def fetch_and_append(fyers, symbol: str, fetch_date: str) -> pd.DataFrame:
    """
    Always calls the Fyers API for the latest candles.
    If a CSV already exists, appends only rows newer than the last stored
    timestamp. If no CSV, saves the full day as a fresh file.
    Returns the complete up-to-date DataFrame.
    """
    existing = load_csv(symbol, fetch_date)

    dt = datetime.strptime(fetch_date, "%Y-%m-%d")
    data = {
        "symbol":      symbol,
        "resolution":  "5",
        "date_format": "1",
        "range_from":  dt.strftime("%Y-%m-%d"),
        "range_to":    dt.strftime("%Y-%m-%d"),
        "cont_flag":   "1",
    }
    response = fyers.history(data=data)
    if response.get("s") != "ok":
        raise RuntimeError(f"Fyers API error for {symbol}: {response}")

    fresh_df = candles_to_df(response["candles"])

    if existing is None:
        save_csv(symbol, fetch_date, fresh_df)
        print(f"  [new csv ] {symbol}  ({len(fresh_df)} candles)")
        return fresh_df

    last_ts  = existing.index.max()
    new_rows = fresh_df[fresh_df.index > last_ts]

    if new_rows.empty:
        print(f"  [no new  ] {symbol}  (last: {last_ts.strftime('%H:%M')})")
        return existing

    combined = pd.concat([existing, new_rows]).sort_index()
    save_csv(symbol, fetch_date, combined)
    print(f"  [+{len(new_rows)} rows] {symbol}  "
          f"{last_ts.strftime('%H:%M')} → {new_rows.index.max().strftime('%H:%M')}")
    return combined


# ── Fyers API client ─────────────────────────────────────────────────────────

def get_fyers_client() -> fyersModel.FyersModel:
    client_id    = os.environ["FYERS_CLIENT_ID"]
    access_token = os.environ["FYERS_ACCESS_TOKEN"]
    fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
    return fyers


# ── VWAP computation ──────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    tp        = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol   = df["volume"].cumsum()
    cum_tpvol = (tp * df["volume"]).cumsum()
    vwap      = cum_tpvol / cum_vol
    vwap.name = "vwap"
    return vwap


# ── Trading-minutes T (time to expiry) ───────────────────────────────────────

def trading_minutes_to_expiry(ts: pd.Timestamp) -> float:
    """
    Returns T in years based on remaining trading minutes only.

    Logic:
      - Count remaining market minutes from ts to EXPIRY_DATETIME
      - Same-day: minutes left in today's session
      - Multi-day: minutes left today + full trading days between × 375
                   + minutes on expiry day from open to 15:30
      - Weekends are skipped (no holiday calendar — close enough for intraday)
      - T = total_minutes / (252 × 375)
    """
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    expiry_aware = ist.localize(EXPIRY_DATETIME)

    if ts >= expiry_aware:
        return 1e-6  # effectively expired

    current_t = ts.time()
    current_d = ts.date()
    expiry_d  = expiry_aware.date()

    def minutes_remaining_today(t: dtime) -> float:
        close_m = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
        cur_m   = t.hour * 60 + t.minute
        return max(0.0, close_m - cur_m)

    def full_trading_days_between(d1: date, d2: date) -> int:
        """Weekdays strictly between d1 and d2 (exclusive)."""
        count = 0
        cur = d1 + timedelta(days=1)
        while cur < d2:
            if cur.weekday() < 5:
                count += 1
            cur += timedelta(days=1)
        return count

    if current_d == expiry_d:
        total_minutes = minutes_remaining_today(current_t)
    else:
        mins_today      = minutes_remaining_today(current_t)
        full_days       = full_trading_days_between(current_d, expiry_d)
        open_m          = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        close_m         = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
        mins_expiry_day = close_m - open_m
        total_minutes   = mins_today + (full_days * MINUTES_PER_DAY) + mins_expiry_day

    T = total_minutes / (252.0 * MINUTES_PER_DAY)
    return max(T, 1e-6)


# ── IV computation ────────────────────────────────────────────────────────────

def compute_iv_series(
    opt_df: pd.DataFrame,
    spot_df: pd.DataFrame,
    strike: int,
    opt_type: str,   # 'CE' or 'PE'
) -> pd.Series:
    """
    Compute BSM implied volatility for each 5-min bar.
    T is computed from trading minutes only (no calendar days).
    Returns IV as a percentage (e.g. 18.5 means 18.5%).
    """
    from py_vollib.black_scholes_merton.implied_volatility import implied_volatility

    flag = "c" if opt_type == "CE" else "p"

    # align spot closes to option timestamps
    spot_aligned = spot_df["close"].reindex(opt_df.index, method="ffill")

    ivs = []
    for ts, row in opt_df.iterrows():
        S = spot_aligned.get(ts, None)
        V = row["close"]
        K = float(strike)

        if S is None or np.isnan(S) or np.isnan(V) or V <= 0.1:
            ivs.append(np.nan)
            continue

        T = trading_minutes_to_expiry(ts)

        try:
            iv = implied_volatility(V, S, K, T, RISK_FREE_RATE, DIVIDEND_YIELD, flag)
            # sanity range: 1% to 300%
            if iv < 0.01 or iv > 3.0:
                iv = np.nan
        except Exception:
            iv = np.nan

        ivs.append(iv)

    s = pd.Series(ivs, index=opt_df.index, name="iv")
    return s * 100  # → percentage


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts_to_list(series: pd.Series) -> list:
    return [str(t) for t in series.index]

def vals_to_list(series: pd.Series) -> list:
    return series.where(series.notna(), other=None).tolist()


# ── Dashboard generator ───────────────────────────────────────────────────────

def generate_dashboard(
    ce_data: list[dict],
    pe_data: list[dict],
    spot_df: pd.DataFrame,
    fetch_date: str,
    output_path: str = "docs/index.html",
):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    DIFF_PALETTE = [
        "#f9ca24", "#6ab04c", "#e056fd", "#badc58", "#eb4d4b",
        "#22a6b3", "#f0932b", "#be2edd", "#4834d4", "#30336b", "#686de0", "#95afc0",
    ]
    IV_PALETTE = [
        "#00d9ff", "#ff6b6b", "#f9ca24", "#6ab04c", "#e056fd",
        "#badc58", "#eb4d4b", "#22a6b3", "#f0932b", "#be2edd",
        "#4834d4", "#686de0", "#95afc0",
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

    # ── Build IV traces per strike ────────────────────────────────────────────
    def build_iv_traces(data_list):
        traces = []
        for i, item in enumerate(data_list):
            iv  = item["iv"].dropna()
            s   = item["strike"]
            clr = IV_PALETTE[i % len(IV_PALETTE)]
            traces.append({
                "x": ts_to_list(iv), "y": vals_to_list(iv),
                "name": f"{s} IV", "type": "scatter", "mode": "lines",
                "line": {"color": clr, "width": 1.5},
            })
        return traces

    # ── ATM IV vs Spot (dual-axis) ────────────────────────────────────────────
    def build_atm_iv_spot_traces(ce_data, pe_data, spot_df):
        ce_atm = ce_data[0]
        pe_atm = pe_data[0]

        ce_iv = ce_atm["iv"].dropna()
        pe_iv = pe_atm["iv"].dropna()

        # align both IV series on a common index
        common_idx = ce_iv.index.intersection(pe_iv.index)
        ce_aligned = ce_iv.reindex(common_idx)
        pe_aligned = pe_iv.reindex(common_idx)
        diff       = pe_aligned - ce_aligned   # positive = put skew, negative = call skew

        THRESHOLD = 0.9  # percentage points

        put_skew_mask  = diff >  THRESHOLD    # PE IV dominates → bearish skew
        call_skew_mask = diff < -THRESHOLD    # CE IV dominates → bullish skew

        def marker_trace(mask, label, color, symbol):
            ts_masked = common_idx[mask]
            # place markers on the higher of the two IV lines
            higher_iv = np.where(diff[mask] > 0, pe_aligned[mask], ce_aligned[mask])
            diff_vals = diff[mask].round(2).tolist()
            return {
                "x":    [str(t) for t in ts_masked],
                "y":    higher_iv.tolist(),
                "name": label,
                "type": "scatter",
                "mode": "markers",
                "yaxis": "y",
                "marker": {
                    "symbol": symbol,
                    "size":   10,
                    "color":  color,
                    "line":   {"color": "#0a0c10", "width": 1},
                },
                "text":      [f"PE\u2212CE = {v:+.2f}%" for v in diff_vals],
                "hoverinfo": "x+text",
            }

        put_skew_markers  = marker_trace(put_skew_mask,  "PE skew >0.9%", "#ff6b6b", "triangle-down")
        call_skew_markers = marker_trace(call_skew_mask, "CE skew >0.9%", "#00d9ff", "triangle-up")

        spot_trace = {
            "x": ts_to_list(spot_df), "y": vals_to_list(spot_df["close"]),
            "name": "Nifty Spot", "type": "scatter", "mode": "lines",
            "line": {"color": "#ffffff", "width": 1.8},
            "yaxis": "y2",
        }
        ce_iv_trace = {
            "x": ts_to_list(ce_iv), "y": vals_to_list(ce_iv),
            "name": f"ATM CE IV ({ce_atm['strike']})", "type": "scatter", "mode": "lines",
            "line": {"color": "#00d9ff", "width": 2},
            "yaxis": "y",
        }
        pe_iv_trace = {
            "x": ts_to_list(pe_iv), "y": vals_to_list(pe_iv),
            "name": f"ATM PE IV ({pe_atm['strike']})", "type": "scatter", "mode": "lines",
            "line": {"color": "#ff6b6b", "width": 2},
            "yaxis": "y",
        }
        return [ce_iv_trace, pe_iv_trace, spot_trace, put_skew_markers, call_skew_markers]

    # ── IV Crush Score bar chart ──────────────────────────────────────────────
    def build_iv_crush_traces(data_list, opt_type):
        strikes, crush_scores = [], []
        for item in data_list:
            iv = item["iv"].dropna()
            if len(iv) < 2:
                continue
            iv_open  = iv.iloc[0]
            iv_close = iv.iloc[-1]
            if iv_open > 0:
                crush = (iv_open - iv_close) / iv_open * 100
                strikes.append(str(item["strike"]))
                crush_scores.append(round(crush, 2))

        return [{
            "x": strikes, "y": crush_scores,
            "name": f"{opt_type} IV Crush %",
            "type": "bar",
            "marker": {
                "color": crush_scores,
                "colorscale": [
                    [0.0, "#eb4d4b"],
                    [0.5, "#f9ca24"],
                    [1.0, "#6ab04c"],
                ],
                "cmin": -50, "cmax": 50,
                "showscale": True,
                "colorbar": {"title": "Crush %", "titlefont": {"size": 10}},
            },
            "text": [f"{v:.1f}%" for v in crush_scores],
            "textposition": "outside",
        }]

    # ── IV Heatmap ────────────────────────────────────────────────────────────
    def build_iv_heatmap(data_list):
        all_ts  = sorted(set().union(*[set(item["iv"].dropna().index) for item in data_list]))
        ts_strs = [str(t) for t in all_ts]
        strikes = [str(item["strike"]) for item in data_list]
        z = []
        for item in data_list:
            iv_aligned = item["iv"].reindex(all_ts)
            z.append(vals_to_list(iv_aligned))
        return [{
            "x": ts_strs, "y": strikes, "z": z,
            "type": "heatmap",
            "colorscale": "RdYlGn",
            "reversescale": True,
            "colorbar": {"title": "IV %", "titlefont": {"size": 10}},
            "hoverongaps": False,
            "zsmooth": "best",
        }]

    # ── Assemble ──────────────────────────────────────────────────────────────
    ce_pv_traces       = build_pv_traces(ce_data, "#00d9ff", "#0077ff")
    pe_pv_traces       = build_pv_traces(pe_data, "#ff6b6b", "#cc0000")
    ce_pairs           = build_diff_pairs(ce_data, "CE")
    pe_pairs           = build_diff_pairs(pe_data, "PE")
    ce_iv_traces       = build_iv_traces(ce_data)
    pe_iv_traces       = build_iv_traces(pe_data)
    atm_iv_spot_traces = build_atm_iv_spot_traces(ce_data, pe_data, spot_df)
    ce_crush_traces    = build_iv_crush_traces(ce_data, "CE")
    pe_crush_traces    = build_iv_crush_traces(pe_data, "PE")
    ce_heatmap_traces  = build_iv_heatmap(ce_data)
    pe_heatmap_traces  = build_iv_heatmap(pe_data)

    ce_pv_js          = json.dumps(ce_pv_traces)
    pe_pv_js          = json.dumps(pe_pv_traces)
    ce_iv_js          = json.dumps(ce_iv_traces)
    pe_iv_js          = json.dumps(pe_iv_traces)
    atm_iv_spot_js    = json.dumps(atm_iv_spot_traces)
    ce_crush_js       = json.dumps(ce_crush_traces)
    pe_crush_js       = json.dumps(pe_crush_traces)
    ce_heatmap_js     = json.dumps(ce_heatmap_traces)
    pe_heatmap_js     = json.dumps(pe_heatmap_traces)

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

    def diff_render_js(pairs, tab_id):
        lines = []
        for p in pairs:
            lines.append(
                f"  if(tabId==='{tab_id}' && !chartsInit['{p['div_id']}']){{"
                f"Plotly.newPlot('{p['div_id']}',{p['div_id']},DIFF_LAYOUT,CONFIG);"
                f"chartsInit['{p['div_id']}']=true;}}"
            )
        return "\n".join(lines)

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

    ce_diff_vars   = diff_vars_js(ce_pairs)
    pe_diff_vars   = diff_vars_js(pe_pairs)
    ce_diff_render = diff_render_js(ce_pairs, "ce-diff")
    pe_diff_render = diff_render_js(pe_pairs, "pe-diff")
    ce_cards_html  = diff_cards_html(ce_pairs, "ce")
    pe_cards_html  = diff_cards_html(pe_pairs, "pe")

    # ── HTML ──────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty Options Chain &mdash; {fetch_date}</title>
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
  header{{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px;
    background:linear-gradient(90deg,#0a0c10,#111521)}}
  header h1{{font-size:16px;font-weight:700;letter-spacing:0.05em;color:var(--accent)}}
  header .meta{{color:var(--muted);font-size:11px;margin-top:2px}}
  .pill{{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;
    background:#1e2535;border:1px solid var(--border);color:var(--accent);margin-left:auto}}
  .tab-bar{{display:flex;border-bottom:1px solid var(--border);padding:0 16px;
    background:var(--panel);overflow-x:auto;gap:0;flex-wrap:nowrap}}
  .tab{{padding:11px 14px;cursor:pointer;font-size:11px;font-weight:700;
    color:var(--muted);border-bottom:2px solid transparent;
    white-space:nowrap;transition:color .15s,border-color .15s;letter-spacing:0.04em}}
  .tab:hover{{color:var(--text)}}
  .tab.active.ce-tab{{color:var(--ce);border-bottom-color:var(--ce)}}
  .tab.active.pe-tab{{color:var(--pe);border-bottom-color:var(--pe)}}
  .tab.active.iv-tab{{color:#f9ca24;border-bottom-color:#f9ca24}}
  .section{{display:none;padding:16px 20px}}
  .section.active{{display:block}}
  .chart-card{{background:var(--panel);border:1px solid var(--border);border-radius:8px;margin-bottom:20px;overflow:hidden}}
  .chart-header{{padding:9px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .chart-title{{font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase}}
  .badge{{padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:0.05em}}
  .badge.ce{{background:rgba(0,217,255,.12);color:var(--ce)}}
  .badge.pe{{background:rgba(255,107,107,.12);color:var(--pe)}}
  .badge.diff{{background:rgba(249,202,36,.1);color:#f9ca24}}
  .badge.iv{{background:rgba(106,176,76,.15);color:#6ab04c}}
  .badge.crush{{background:rgba(224,86,253,.15);color:#e056fd}}
  .legend-toggle{{margin-left:auto;font-size:10px;color:var(--muted);cursor:pointer;
    background:var(--bg);border:1px solid var(--border);padding:3px 8px;border-radius:4px}}
  .legend-toggle:hover{{color:var(--text)}}
  .plotly-chart{{width:100%;min-height:300px}}
  .plotly-chart.tall{{min-height:420px}}
  .plotly-chart.heatmap{{min-height:500px}}
  .crush-note{{margin:0 0 16px 0;padding:10px 14px;background:#111521;border:1px solid #1e2535;
    border-radius:6px;color:#94a3b8;font-size:11px;line-height:1.7}}
  .crush-note strong{{color:#f9ca24}}
  footer{{padding:12px 24px;border-top:1px solid var(--border);color:var(--muted);font-size:10px;text-align:center;background:var(--panel)}}
</style>
</head>
<body>

<header>
  <div>
    <h1>&#9889; NIFTY OPTIONS CHAIN</h1>
    <div class="meta">ATM {ATM_STRIKE} &middot; Step {STRIKE_STEP} &middot; Expiry {EXPIRY_DATE} &middot; 5-min &middot; T = Trading Minutes Only</div>
  </div>
  <div class="pill">&#128197; {fetch_date}</div>
</header>

<div class="tab-bar">
  <div class="tab ce-tab active" data-tab="ce-pv">CE &middot; Price &amp; VWAP</div>
  <div class="tab pe-tab"        data-tab="pe-pv">PE &middot; Price &amp; VWAP</div>
  <div class="tab ce-tab"        data-tab="ce-diff">CE &middot; Pair &Delta;</div>
  <div class="tab pe-tab"        data-tab="pe-diff">PE &middot; Pair &Delta;</div>
  <div class="tab iv-tab"        data-tab="ce-iv">CE &middot; IV</div>
  <div class="tab iv-tab"        data-tab="pe-iv">PE &middot; IV</div>
  <div class="tab iv-tab"        data-tab="atm-iv-spot">ATM IV vs Spot</div>
  <div class="tab iv-tab"        data-tab="iv-crush">IV Crush</div>
  <div class="tab iv-tab"        data-tab="iv-heatmap">IV Heatmap</div>
</div>

<div id="ce-pv" class="section active">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE &mdash; All Strikes &middot; Price &amp; VWAP</span>
      <span class="badge ce">CALL</span>
      <button class="legend-toggle" onclick="toggleLegend('ce_pv_chart')">Legend</button>
    </div>
    <div id="ce_pv_chart" class="plotly-chart tall"></div>
  </div>
</div>

<div id="pe-pv" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE &mdash; All Strikes &middot; Price &amp; VWAP</span>
      <span class="badge pe">PUT</span>
      <button class="legend-toggle" onclick="toggleLegend('pe_pv_chart')">Legend</button>
    </div>
    <div id="pe_pv_chart" class="plotly-chart tall"></div>
  </div>
</div>

<div id="ce-diff" class="section">{ce_cards_html}</div>
<div id="pe-diff" class="section">{pe_cards_html}</div>

<div id="ce-iv" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE &mdash; Implied Volatility per Strike</span>
      <span class="badge ce">CALL</span><span class="badge iv">IV %</span>
      <button class="legend-toggle" onclick="toggleLegend('ce_iv_chart')">Legend</button>
    </div>
    <div id="ce_iv_chart" class="plotly-chart tall"></div>
  </div>
</div>

<div id="pe-iv" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE &mdash; Implied Volatility per Strike</span>
      <span class="badge pe">PUT</span><span class="badge iv">IV %</span>
      <button class="legend-toggle" onclick="toggleLegend('pe_iv_chart')">Legend</button>
    </div>
    <div id="pe_iv_chart" class="plotly-chart tall"></div>
  </div>
</div>

<div id="atm-iv-spot" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">ATM IV (CE &amp; PE) vs Nifty Spot &mdash; Dual Axis</span>
      <span class="badge iv">IV %</span><span class="badge crush">SPOT</span>
      <button class="legend-toggle" onclick="toggleLegend('atm_iv_spot_chart')">Legend</button>
    </div>
    <div id="atm_iv_spot_chart" class="plotly-chart tall"></div>
  </div>
</div>

<div id="iv-crush" class="section">
  <div class="crush-note">
    <strong>IV Crush Score</strong> = (IV at open bar &minus; IV at close bar) / IV at open bar &times; 100
    &nbsp;&middot;&nbsp; <strong style="color:#6ab04c">Green</strong> = IV compressed (crush happened)
    &nbsp;&middot;&nbsp; <strong style="color:#eb4d4b">Red</strong> = IV expanded
    &nbsp;&middot;&nbsp; T uses trading minutes only (252d &times; 375min/d).
  </div>
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE &mdash; IV Crush Score per Strike</span>
      <span class="badge ce">CALL</span><span class="badge crush">CRUSH %</span>
    </div>
    <div id="ce_crush_chart" class="plotly-chart"></div>
  </div>
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE &mdash; IV Crush Score per Strike</span>
      <span class="badge pe">PUT</span><span class="badge crush">CRUSH %</span>
    </div>
    <div id="pe_crush_chart" class="plotly-chart"></div>
  </div>
</div>

<div id="iv-heatmap" class="section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">CE &mdash; IV Heatmap &middot; Strike &times; Time</span>
      <span class="badge ce">CALL</span><span class="badge iv">HEATMAP</span>
    </div>
    <div id="ce_heatmap_chart" class="plotly-chart heatmap"></div>
  </div>
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">PE &mdash; IV Heatmap &middot; Strike &times; Time</span>
      <span class="badge pe">PUT</span><span class="badge iv">HEATMAP</span>
    </div>
    <div id="pe_heatmap_chart" class="plotly-chart heatmap"></div>
  </div>
</div>

<footer>Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')} &middot; Fyers API &middot; IV via BSM (trading-minutes T, r={RISK_FREE_RATE*100:.1f}%) &middot; cache: data/</footer>

<script>
const BASE = {{
  paper_bgcolor:'#111521', plot_bgcolor:'#0a0c10',
  font:{{family:"'JetBrains Mono',monospace", color:'#e2e8f0', size:11}},
  xaxis:{{gridcolor:'#1e2535', showgrid:true, zeroline:false, tickformat:'%H:%M', tickfont:{{size:10}}}},
  yaxis:{{gridcolor:'#1e2535', showgrid:true, zeroline:false, tickfont:{{size:10}}}},
  legend:{{bgcolor:'rgba(17,21,33,.85)', bordercolor:'#1e2535', borderwidth:1, font:{{size:10}}}},
  hovermode:'x unified',
  hoverlabel:{{bgcolor:'#1e2535', bordercolor:'#2d3748', font:{{family:"'JetBrains Mono',monospace", size:11}}}},
  margin:{{l:60, r:15, t:12, b:45}},
  dragmode:'zoom',
}};
const LAYOUT      = Object.assign({{}}, BASE, {{yaxis: Object.assign({{}}, BASE.yaxis, {{tickprefix:'\u20b9'}})}});
const DIFF_LAYOUT = Object.assign({{}}, BASE, {{yaxis: Object.assign({{}}, BASE.yaxis, {{tickprefix:''}})}});
const IV_LAYOUT   = Object.assign({{}}, BASE, {{yaxis: Object.assign({{}}, BASE.yaxis, {{tickprefix:'', ticksuffix:'%', title:'IV %'}})}});
const CRUSH_LAYOUT = Object.assign({{}}, BASE, {{
  yaxis: Object.assign({{}}, BASE.yaxis, {{tickprefix:'', ticksuffix:'%', title:'Crush %', zeroline:true, zerolinecolor:'#64748b'}}),
  hovermode:'closest', showlegend:false,
}});
const ATM_SPOT_LAYOUT = Object.assign({{}}, BASE, {{
  yaxis:  Object.assign({{}}, BASE.yaxis, {{tickprefix:'', ticksuffix:'%', title:'IV %', side:'left'}}),
  yaxis2: {{
    gridcolor:'#1e2535', showgrid:false, zeroline:false, tickfont:{{size:10}}, tickprefix:'\u20b9',
    overlaying:'y', side:'right', title:'Spot',
    titlefont:{{color:'#ffffff'}},
  }},
}});
const HEATMAP_LAYOUT = Object.assign({{}}, BASE, {{
  xaxis: Object.assign({{}}, BASE.xaxis, {{title:'Time'}}),
  yaxis: Object.assign({{}}, BASE.yaxis, {{tickprefix:'', title:'Strike', type:'category'}}),
  margin: {{l:75, r:90, t:12, b:55}},
}});
const CONFIG = {{
  responsive:true, displayModeBar:true,
  modeBarButtonsToRemove:['select2d','lasso2d'],
  displaylogo:false, scrollZoom:true,
}};

{ce_diff_vars}
{pe_diff_vars}

let chartsInit = {{}};

function toggleLegend(id) {{
  const gd = document.getElementById(id);
  const cur = gd.layout && gd.layout.showlegend;
  Plotly.relayout(id, {{showlegend: cur === false ? true : false}});
}}

function renderTab(tabId) {{
  if (tabId==='ce-pv'       && !chartsInit['ce_pv_chart'])        {{ Plotly.newPlot('ce_pv_chart',       {ce_pv_js},        LAYOUT,          CONFIG); chartsInit['ce_pv_chart']=true; }}
  if (tabId==='pe-pv'       && !chartsInit['pe_pv_chart'])        {{ Plotly.newPlot('pe_pv_chart',       {pe_pv_js},        LAYOUT,          CONFIG); chartsInit['pe_pv_chart']=true; }}
  if (tabId==='ce-iv'       && !chartsInit['ce_iv_chart'])        {{ Plotly.newPlot('ce_iv_chart',       {ce_iv_js},        IV_LAYOUT,       CONFIG); chartsInit['ce_iv_chart']=true; }}
  if (tabId==='pe-iv'       && !chartsInit['pe_iv_chart'])        {{ Plotly.newPlot('pe_iv_chart',       {pe_iv_js},        IV_LAYOUT,       CONFIG); chartsInit['pe_iv_chart']=true; }}
  if (tabId==='atm-iv-spot' && !chartsInit['atm_iv_spot_chart'])  {{ Plotly.newPlot('atm_iv_spot_chart', {atm_iv_spot_js},  ATM_SPOT_LAYOUT, CONFIG); chartsInit['atm_iv_spot_chart']=true; }}
  if (tabId==='iv-crush'    && !chartsInit['ce_crush_chart'])     {{
    Plotly.newPlot('ce_crush_chart', {ce_crush_js}, CRUSH_LAYOUT, CONFIG);
    Plotly.newPlot('pe_crush_chart', {pe_crush_js}, CRUSH_LAYOUT, CONFIG);
    chartsInit['ce_crush_chart']=true;
  }}
  if (tabId==='iv-heatmap'  && !chartsInit['ce_heatmap_chart'])   {{
    Plotly.newPlot('ce_heatmap_chart', {ce_heatmap_js}, HEATMAP_LAYOUT, CONFIG);
    Plotly.newPlot('pe_heatmap_chart', {pe_heatmap_js}, HEATMAP_LAYOUT, CONFIG);
    chartsInit['ce_heatmap_chart']=true;
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


# ── Telegram helper ──────────────────────────────────────────────────────────

def send_telegram(html_path: str, fetch_date: str, run_time: str):
    """
    Sends the dashboard HTML file to a Telegram chat via Bot API.
    Requires env vars:
      TELEGRAM_BOT_TOKEN  — from @BotFather
      TELEGRAM_CHAT_ID    — your chat/channel id (use @username or numeric id)
    Silently skips if env vars are not set.
    """
    import urllib.request
    import urllib.parse

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("  [telegram] skipped — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return

    html_file = Path(html_path)
    if not html_file.exists():
        print(f"  [telegram] skipped — {html_path} not found")
        return

    caption = (
        f"📊 Nifty Options Dashboard\n"
        f"📅 {fetch_date}  •  🕐 updated {run_time} IST"
    )

    # sendDocument endpoint — sends file as a document attachment
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    with open(html_file, "rb") as fh:
        file_data = fh.read()

    boundary = "----NiftyDashBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{caption}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="nifty_dashboard_{fetch_date}.html"\r\n'
        f"Content-Type: text/html\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"  [telegram] ✅ sent to chat {chat_id}")
            else:
                print(f"  [telegram] ⚠️  API error: {result}")
    except Exception as e:
        print(f"  [telegram] ❌ failed: {e}")


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
    parser.add_argument("--date",      default="today",           help="Fetch date: today | YYYY-MM-DD | DD-MM-YYYY")
    parser.add_argument("--output",    default="docs/index.html", help="Output HTML path")
    parser.add_argument("--no-telegram", action="store_true",     help="Skip Telegram send")
    args = parser.parse_args()

    fetch_date = parse_date(args.date)
    run_time   = datetime.now().strftime("%H:%M:%S")

    print(f"\n📅 Fetch date  : {fetch_date}  |  run: {run_time}")
    print(f"🎯 ATM strike  : {ATM_STRIKE}  |  Step: {STRIKE_STEP}  |  Expiry: {EXPIRY_DATE}")
    print(f"📊 CE strikes  : {ATM_STRIKE} → {ATM_STRIKE + (NUM_STRIKES-1)*STRIKE_STEP}  (OTM calls)")
    print(f"📊 PE strikes  : {ATM_STRIKE} → {ATM_STRIKE - (NUM_STRIKES-1)*STRIKE_STEP}  (OTM puts)")
    print(f"⏱  T method    : Trading minutes only  (252d × {MINUTES_PER_DAY}min/d)")
    print(f"💹 Risk-free r : {RISK_FREE_RATE*100:.1f}%")

    fyers = get_fyers_client()

    # ── Spot ──────────────────────────────────────────────────────────────────
    print(f"\n── Fetching Spot ───────────────────────────────────────────────")
    spot_df = fetch_and_append(fyers, SPOT_SYMBOL, fetch_date)

    # ── CE ────────────────────────────────────────────────────────────────────
    ce_syms = get_all_symbols("CE")
    print(f"\n── Fetching CE + computing IV ──────────────────────────────────")
    ce_data = []
    for item in ce_syms:
        df   = fetch_and_append(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        iv   = compute_iv_series(df, spot_df, item["strike"], "CE")
        iv_clean = iv.dropna()
        if len(iv_clean):
            print(f"    IV [{item['strike']} CE]: {iv_clean.min():.1f}% – {iv_clean.max():.1f}%  "
                  f"(last bar: {iv_clean.iloc[-1]:.1f}%)")
        ce_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap, "iv": iv})

    # ── PE ────────────────────────────────────────────────────────────────────
    pe_syms = get_all_symbols("PE")
    print(f"\n── Fetching PE + computing IV ──────────────────────────────────")
    pe_data = []
    for item in pe_syms:
        df   = fetch_and_append(fyers, item["symbol"], fetch_date)
        vwap = compute_vwap(df)
        iv   = compute_iv_series(df, spot_df, item["strike"], "PE")
        iv_clean = iv.dropna()
        if len(iv_clean):
            print(f"    IV [{item['strike']} PE]: {iv_clean.min():.1f}% – {iv_clean.max():.1f}%  "
                  f"(last bar: {iv_clean.iloc[-1]:.1f}%)")
        pe_data.append({"strike": item["strike"], "symbol": item["symbol"], "df": df, "vwap": vwap, "iv": iv})

    # ── Dashboard ─────────────────────────────────────────────────────────────
    print(f"\n── Generating Dashboard ────────────────────────────────────────")
    generate_dashboard(ce_data, pe_data, spot_df, fetch_date, args.output)

    # ── Telegram ──────────────────────────────────────────────────────────────
    if not args.no_telegram:
        print(f"\n── Sending via Telegram ────────────────────────────────────────")
        send_telegram(args.output, fetch_date, run_time)
    else:
        print("  [telegram] skipped via --no-telegram flag")


if __name__ == "__main__":
    main()
