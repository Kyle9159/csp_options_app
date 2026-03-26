# CSP Options Trading Dashboard

A comprehensive options trading dashboard that scans for Cash-Secured Put (CSP) opportunities, tracks open positions, and provides AI-powered analysis using the Schwab API and Grok AI.

---

## Table of Contents

1. [What This App Does](#what-this-app-does)
2. [Prerequisites](#prerequisites)
3. [Step-by-Step Installation](#step-by-step-installation)
4. [Configuration](#configuration)
5. [Running the Dashboard](#running-the-dashboard)
6. [Troubleshooting](#troubleshooting)

---

## What This App Does

- **Scans** for profitable cash-secured put (CSP) options opportunities
- **Tracks** your open options positions from Google Sheets
- **Analyzes** trades using Grok AI for sentiment and recommendations
- **Visualizes** options chains with interactive heatmaps
- **Monitors** 0DTE (zero days to expiration) iron condor opportunities
- **Displays** real-time market data and portfolio metrics

---

## Prerequisites

Before you begin, you'll need accounts with the following services:

### Required Accounts

| Service | Purpose | Sign Up Link |
|---------|---------|--------------|
| **Schwab Developer** | Access to market data and options chains | [developer.schwab.com](https://developer.schwab.com) |
| **Google Cloud** | For Google Sheets integration | [console.cloud.google.com](https://console.cloud.google.com) |
| **xAI (Grok)** | AI-powered trade analysis | [x.ai](https://x.ai) |

### Required Software

| Software | Purpose | Download Link |
|----------|---------|---------------|
| **Python 3.10+** | Programming language | [python.org/downloads](https://www.python.org/downloads/) |
| **Git** | Version control | [git-scm.com/downloads](https://git-scm.com/downloads) |

---

## Step-by-Step Installation

### Step 1: Install Python

**Windows:**
1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Download the latest Python 3.x installer
3. Run the installer
4. **IMPORTANT**: Check the box that says "Add Python to PATH"
5. Click "Install Now"
6. Verify installation by opening Command Prompt and typing:
   ```
   python --version
   ```

**Mac:**
1. Open Terminal (press Cmd+Space, type "Terminal", press Enter)
2. Install Homebrew (if not installed):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
3. Install Python:
   ```bash
   brew install python
   ```
4. Verify installation:
   ```bash
   python3 --version
   ```

---

### Step 2: Install Git

**Windows:**
1. Go to [git-scm.com/downloads](https://git-scm.com/downloads)
2. Download the Windows installer
3. Run the installer, accept all defaults
4. Verify by opening Command Prompt:
   ```
   git --version
   ```

**Mac:**
1. Git is usually pre-installed. Check with:
   ```bash
   git --version
   ```
2. If not installed, run:
   ```bash
   xcode-select --install
   ```

---

### Step 3: Download This Project

1. Open Terminal (Mac) or Command Prompt (Windows)
2. Navigate to where you want to store the project:
   ```bash
   cd ~/Documents
   ```
3. Clone the repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/csp_options_app.git
   ```
4. Enter the project folder:
   ```bash
   cd csp_options_app
   ```

---

### Step 4: Create a Virtual Environment

A virtual environment keeps this project's packages separate from other Python projects.

**Mac/Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows:**
```cmd
python -m venv .venv
.venv\Scripts\activate
```

You should see `(.venv)` at the beginning of your command prompt. This means the virtual environment is active.

---

### Step 5: Install Required Packages

With your virtual environment active, run:

```bash
pip install -r requirements.txt
```

This will install all the necessary Python packages. It may take a few minutes.

---

### Step 6: Set Up Your Schwab Developer Account

1. Go to [developer.schwab.com](https://developer.schwab.com)
2. Click "Sign Up" and create an account (use your Schwab login)
3. Once logged in, click "Create App"
4. Fill in the app details:
   - **App Name**: `CSP Options Dashboard` (or any name you like)
   - **Callback URL**: `https://127.0.0.1:8182`
   - **App Description**: Personal options trading dashboard
5. After creating the app, you'll see:
   - **API Key** (also called App Key or Client ID)
   - **App Secret** (also called Client Secret)
6. **Save these values** - you'll need them in the configuration step

---

### Step 7: Set Up Google Sheets Integration

This allows the dashboard to read your open positions from a Google Sheet.

#### 7a: Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click "Select a project" at the top, then "New Project"
3. Name it `CSP Options Dashboard` and click "Create"
4. Wait for the project to be created

#### 7b: Enable the Google Sheets API

1. In the Google Cloud Console, go to "APIs & Services" > "Library"
2. Search for "Google Sheets API"
3. Click on it, then click "Enable"

#### 7c: Create Service Account Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "Service Account"
3. Fill in:
   - **Service account name**: `csp-dashboard`
   - **Service account ID**: (auto-fills)
4. Click "Create and Continue"
5. Skip the optional steps, click "Done"
6. Click on the newly created service account
7. Go to the "Keys" tab
8. Click "Add Key" > "Create new key"
9. Select "JSON" and click "Create"
10. A file will download - **save this file as `service_account.json`** in your project folder

#### 7d: Share Your Google Sheet

1. Open your Google Sheet with trading data
2. Click the "Share" button
3. Copy the email address from your `service_account.json` file (looks like `csp-dashboard@your-project.iam.gserviceaccount.com`)
4. Paste it in the sharing field and give it "Editor" access
5. Copy the Sheet ID from the URL:
   - URL looks like: `https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit`
   - Copy just the `SHEET_ID_HERE` part

---

### Step 8: Get Your Grok (xAI) API Key

1. Go to [x.ai](https://x.ai) or [console.x.ai](https://console.x.ai)
2. Sign up or log in
3. Navigate to API settings
4. Create a new API key
5. **Copy and save the API key**

---

### Step 9: Create Your Configuration File

1. In your project folder, create a new file called `.env`
2. Open it in a text editor (Notepad, TextEdit, VS Code, etc.)
3. Paste the following and fill in your values:

```env
# ===========================================
# SCHWAB API CREDENTIALS (REQUIRED)
# ===========================================
SCHWAB_API_KEY=your_schwab_api_key_here
SCHWAB_APP_SECRET=your_schwab_app_secret_here
REDIRECT_URI=https://127.0.0.1:8182

# ===========================================
# SCHWAB ACCOUNT IDs
# ===========================================
# Find these in your Schwab account settings or API response
PAPER_ACCOUNT_ID=your_paper_account_id
LIVE_ACCOUNT_ID=your_live_account_id

# Set to True for paper trading, False for live
PAPER_TRADING=True

# ===========================================
# GOOGLE SHEETS (REQUIRED for position tracking)
# ===========================================
GOOGLE_SHEET_ID=your_google_sheet_id_here

# ===========================================
# GROK AI (REQUIRED for AI analysis)
# ===========================================
XAI_API_KEY=your_xai_api_key_here

# ===========================================
# TRADING PARAMETERS (OPTIONAL - has defaults)
# ===========================================
# Total capital allocated for wheel strategy
WHEEL_CAPITAL=25000

# Maximum positions to hold
MAX_POSITIONS=5

# Maximum percentage of capital per trade (0.20 = 20%)
MAX_PER_TRADE_PCT=0.20

# Total account capital (for risk calculations)
ACCOUNT_CAPITAL=100000

# Maximum capital per single trade
MAX_CAPITAL_PER_TRADE=45000

# ===========================================
# TELEGRAM NOTIFICATIONS (OPTIONAL)
# ===========================================
# Uncomment and fill in if you want Telegram alerts
# PAPER_TRADE_MONITOR_TELEGRAM_TOKEN=your_telegram_bot_token
# PAPER_TRADE_MONITOR_CHAT_ID=your_chat_id
# SIMPLE_OPTIONS_SCANNER_TELEGRAM_TOKEN=your_telegram_bot_token
# COVERED_CALL_TELEGRAM_TOKEN=your_telegram_bot_token
# DIVIDEND_TRACKER_TELEGRAM_TOKEN=your_telegram_bot_token

# ===========================================
# ADDITIONAL DATA SOURCES (OPTIONAL)
# ===========================================
# EODHD API for additional market data
# EODHD_API_KEY=your_eodhd_api_key
```

4. Save the file

---

### Step 10: Authenticate with Schwab

The first time you run the app, you need to authenticate with Schwab.

1. Make sure your virtual environment is active (you see `(.venv)` in your prompt)
2. Run:
   ```bash
   python generate_dashboard.py
   ```
3. A browser window will open asking you to log in to Schwab
4. Log in with your Schwab credentials
5. Authorize the app
6. You'll be redirected to a page that may show an error - **this is normal**
7. Copy the **entire URL** from your browser's address bar
8. Paste it back into the terminal when prompted
9. The app will save your authentication tokens for future use

---

## Running the Dashboard

### Generate the Static Dashboard

```bash
python generate_dashboard.py
```

This creates an HTML file (`csp_dashboard.html`) that you can open in any web browser.

### Run the Live Dashboard Server

For a live, interactive dashboard:

```bash
python dashboard_server.py
```

Then open your browser to: [http://localhost:5000](http://localhost:5000)

### Run Individual Scanners

**Options Scanner:**
```bash
python simple_options_scanner.py
```

**LEAPS Scanner:**
```bash
python leaps_scanner.py
```

**0DTE Iron Condor Scanner:**
```bash
python zero_dte_spread_scanner.py
```

---

## Schwab Re-Authorization

Schwab OAuth tokens expire after **7 days**. When your token is expired or expiring soon, a yellow/red banner appears at the top of the dashboard.

### Re-authorizing from the Dashboard

1. Click the **Re-Authorize Schwab** button in the banner (or navigate to `https://127.0.0.1:5000/auth/start`)
2. A Schwab login page opens in your browser
3. Log in and click **Allow**
4. You'll be redirected to a URL starting with `https://127.0.0.1:8182/?code=...` — this page will look like an error, which is **expected**
5. Copy the **entire URL** from your browser's address bar
6. Paste it into the terminal where `dashboard_server.py` is running and press Enter
7. The banner will disappear and the dashboard will resume live data

### Schwab Developer Portal Setup (one-time)

These settings must be configured in the [Schwab Developer Portal](https://developer.schwab.com) for OAuth to work:

| Setting | Value |
|---------|-------|
| **App type** | Personal Use |
| **Callback URL** | `https://127.0.0.1:8182` |
| **Products** | MarketData API + Accounts and Trading Production |

The `REDIRECT_URI` in your `.env` **must exactly match** the callback URL registered in the portal (including the `https://` and no trailing slash).

### Token File Location

The token is saved to `cache_files/schwab_token.json` by default. You can override this with:

```env
TOKEN_PATH=cache_files/schwab_token.json
```

To force a fresh login, delete the token file:
```bash
rm cache_files/schwab_token.json
```

### Token Status API

`GET /api/auth/status` returns current token health:

```json
{ "status": "ok", "needs_reauth": false, "days_since_auth": 2.1, "message": "Schwab token is valid." }
```

Possible `status` values: `ok` · `expiring_soon` (≥5 days) · `expired` (≥7 days) · `missing` · `auth_in_progress` · `auth_error`

---

## Project Structure

```
csp_options_app/
├── .env                      # Your configuration (create this)
├── service_account.json      # Google Sheets credentials (create this)
├── requirements.txt          # Python dependencies
├── generate_dashboard.py     # Main dashboard generator
├── dashboard_server.py       # Live web server
├── simple_options_scanner.py # CSP opportunity scanner
├── leaps_scanner.py          # Long-term options scanner
├── zero_dte_spread_scanner.py # 0DTE iron condor scanner
├── grok_utils.py             # Grok AI integration
├── schwab_utils.py           # Schwab API utilities
├── helper_functions.py       # Shared utility functions
├── chain_visualizer.py       # Options chain heatmaps
├── cache_files/              # Cached data (auto-created)
└── token.json                # Schwab auth tokens (auto-created)
```

---

## Troubleshooting

### "Python not found" or "python is not recognized"

- **Windows**: Reinstall Python and make sure to check "Add Python to PATH"
- **Mac**: Use `python3` instead of `python`

### "ModuleNotFoundError: No module named 'xxx'"

Make sure your virtual environment is active:
```bash
# Mac/Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

Then reinstall packages:
```bash
pip install -r requirements.txt
```

### "Invalid API Key" or Schwab authentication errors

1. Double-check your `SCHWAB_API_KEY` and `SCHWAB_APP_SECRET` in `.env`
2. Make sure there are no extra spaces
3. Delete `token.json` and re-authenticate

### "Google Sheets API Error"

1. Verify `service_account.json` is in your project folder
2. Make sure you shared your Google Sheet with the service account email
3. Check that `GOOGLE_SHEET_ID` in `.env` is correct

### "Grok API Error"

1. Verify `XAI_API_KEY` in `.env` is correct
2. Check your xAI account has API credits available

### Dashboard shows "No data" or empty sections

1. Make sure the Schwab API is authenticated (run `generate_dashboard.py` first)
2. Check that the market is open (data updates during market hours)
3. Look at the terminal output for error messages

### Cache issues (old data showing)

Delete the cache files to force a refresh:
```bash
# Mac/Linux
rm -rf cache_files/*

# Windows
del /q cache_files\*
```

---

## Getting Help

If you run into issues:

1. Check the terminal output for error messages
2. Make sure all your API keys are correct in `.env`
3. Verify your virtual environment is active
4. Try deleting `token.json` and `cache_files/` and starting fresh

---

## Security Notes

- **Never share** your `.env` file or `service_account.json`
- **Never commit** these files to Git (they're in `.gitignore`)
- Keep your API keys private
- Use paper trading mode (`PAPER_TRADING=True`) until you're comfortable

---

## License

This project is for personal use. Use at your own risk. Options trading involves significant risk of loss.
