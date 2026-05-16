# ⚡ Nifty Options Chain Dashboard

Interactive 5-min historical dashboard for Nifty 50 options — ATM to ATM+12 OTM, CE & PE.
Auto-deployed to GitHub Pages via a manual GitHub Actions workflow.

---

## 📊 What it does

| Feature | Detail |
|---|---|
| **Data** | 5-min OHLCV candles via Fyers API |
| **Chain** | ATM → ATM+12 OTM (13 strikes), CE & PE |
| **Compute** | VWAP per strike · Price & VWAP differences between adjacent strikes |
| **Charts** | 4 tabs: CE price/VWAP · PE price/VWAP · CE diff · PE diff |
| **Cache** | JSON files under `data/` keyed by `SYMBOL_DATE.json` |
| **Output** | `docs/index.html` — single self-contained HTML (Plotly CDN) |

---

## 🔧 One-time setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/nifty-options-dashboard.git
cd nifty-options-dashboard
```

### 2. Enable GitHub Pages

`Settings → Pages → Source: GitHub Actions`

### 3. Add repository secrets

`Settings → Secrets and variables → Actions → New repository secret`

| Secret name | Value |
|---|---|
| `FYERS_CLIENT_ID` | Your Fyers app client ID (e.g. `XY1234-100`) |
| `FYERS_ACCESS_TOKEN` | Daily access token (see below) |

### 4. Edit hardcoded config in `fetch_and_plot.py`

```python
ATM_STRIKE   = 24500        # ← set before each expiry week
STRIKE_STEP  = 50           # Nifty gap (always 50)
EXPIRY_DATE  = "25-05-2026" # DD-MM-YYYY, nearest Thursday expiry
```

---

## 🔑 Getting the Fyers access token (daily)

Fyers access tokens expire daily. Two options:

### Option A — Manual (simplest)
1. Login at [Fyers API](https://myapi.fyers.in/)
2. Go to **API v3 → Generate Token**
3. Copy the token
4. Update the `FYERS_ACCESS_TOKEN` secret in GitHub before running the workflow

### Option B — Automate token refresh  
Add a separate workflow or script that uses Fyers' OAuth flow to auto-refresh.
See [Fyers docs](https://myapi.fyers.in/docsV3) for the `generate_authcode` + `generate_token` flow.
Store `refresh_token` as a secret and generate fresh tokens daily.

---

## 🚀 Running the workflow

1. Go to **Actions → Nifty Options Dashboard → Run workflow**
2. Enter the date (or leave blank for today):
   - `today` — uses system date
   - `2026-05-15` — YYYY-MM-DD format
   - `15-05-2026` — DD-MM-YYYY format
3. Click **Run workflow**

The workflow will:
1. Restore cached data from previous runs
2. Fetch missing symbols from Fyers API (cached ones are skipped)
3. Generate `docs/index.html`
4. Commit the data cache + HTML back to the repo
5. Deploy `docs/` to GitHub Pages

---

## 💻 Local development

```bash
pip install -r requirements.txt

export FYERS_CLIENT_ID="XY1234-100"
export FYERS_ACCESS_TOKEN="eyJ..."

# Fetch today's data
python fetch_and_plot.py

# Fetch a specific date
python fetch_and_plot.py --date 15-05-2026

# Custom output path
python fetch_and_plot.py --date today --output /tmp/dashboard.html
```

---

## 📁 Project structure

```
├── fetch_and_plot.py           # Main script
├── requirements.txt
├── data/                       # Cache: SYMBOL_YYYY-MM-DD.json
│   └── NSE:NIFTY25MAY202624500CE_2026-05-15.json
├── docs/
│   └── index.html              # Generated dashboard (GitHub Pages root)
└── .github/
    └── workflows/
        └── dashboard.yml       # Manual trigger workflow
```

---

## 📈 Dashboard tabs

| Tab | Content |
|---|---|
| **CE Price & VWAP** | All 13 CE strikes overlaid — price (solid) + VWAP (dotted) |
| **PE Price & VWAP** | All 13 PE strikes overlaid — price (solid) + VWAP (dotted) |
| **CE Differences** | Δ(ATM−OTM1), Δ(OTM1−OTM2) … Δ(OTM11−OTM12) for CE |
| **PE Differences** | Same for PE |

All charts are interactive — zoom, pan, hover, toggle traces.

---

## 🛠 Troubleshooting

| Issue | Fix |
|---|---|
| `Fyers API error` | Regenerate access token; check `FYERS_CLIENT_ID` format |
| Empty candles | Market holiday or pre-market run; try a trading day |
| Workflow fails at push | Ensure the repo has **write** permissions for Actions (`Settings → Actions → General → Workflow permissions → Read and write`) |
| GitHub Pages 404 | Wait 2-3 min after first deploy; verify source is set to `GitHub Actions` |
