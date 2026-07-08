# Solana Token Honeypot Checker

Tools to check honeypot tokens on Solana. Features buy/sell tax detection via Helius bundle simulation, Mint/Freeze authority checks, and holder-level sell restriction analysis using Jupiter + SurfPool.

## Scripts

### 1. `solana_honeypot.py` — Buy + Sell Bundle + Authority Checks

Simulates a buy and sell pair via Helius `simulateBundle`. Detects buy/sell taxes and checks token mint/freeze authorities via `getAccountInfo`.

**Features:**
- Bundle simulation (buy + sell in same block)
- Mint Authority detection (can creator mint unlimited tokens?)
- Freeze Authority detection (can creator freeze wallets?)
- Buy/sell tax calculation from `postTokenBalances` and native SOL balance change

**Usage:**
```bash
python solana_honeypot.py
```

**Configuration (edit the file):**
| Variable | Description |
|---|---|
| `HELIUS_API_KEY` | Your Helius API key (get at https://dev.helius.xyz) |
| `BUY_AMOUNT_LAMPORTS` | Buy amount in lamports (1 SOL = 1e9) |
| `INPUT_MINT` | Token you spend (default: SOL) |
| `OUTPUT_MINT` | Token you buy (the token to test) |
| `SLIPPAGE_BPS` | Slippage in bps (500 = 5%) |
| `private_key` | Your Solana private key as a list of 64 integers (0-255) |

> **Important:** The wallet must have at least **0.1 SOL** (mainnet balance) — Helius validates the balance before simulating, even with state overrides.

**Private Key Format:**
Export from **Solflare** → Settings → Export Private Key. You'll get a list of 64 integers like:
```python
private_key = [123, 45, 67, ... 89, 12]  # 64 numbers total
```

### 2. `solana-holders-analysis.py` — Top Holders + Local Sell Simulation

Fetches the top 20 token holders via Helius, resolves owner addresses from token account data, airdrops 10 SOL on a local SurfPool validator, and simulates selling 1% of each top holder's position via Jupiter.

**Features:**
- Top 20 holder fetch via `getTokenLargestAccounts` + `getMultipleAccounts` (owner resolution)
- 1% sell estimate table
- SurfPool simulation (airdrop + blockhash replace + local `simulateTransaction`)
- Per-holder honeypot detection (some wallets may be blocked while others aren't)

**Prerequisites:** A running SurfPool validator at `http://localhost:8899`.

**Usage:**
```bash
# Start SurfPool first (requires WSL or Linux):
curl -sL https://run.surfpool.run/ | bash
surfpool start

# Then run the script:
python solana-holders-analysis.py
```

**Configuration (edit the file):**
| Variable | Description |
|---|---|
| `HELIUS_API_KEY` | Your Helius API key (get at https://dev.helius.xyz) |
| `LOCAL_RPC` | SurfPool RPC URL (default: `http://localhost:8899`, change if SurfPool uses a different port) |
| `OUTPUT_MINT` | Token mint address to analyze |
| `SOL_MINT` | SOL mint address |

## Requirements

- **Python 3.9+**
- **Helius API key** — free tier at https://dev.helius.xyz (required for both scripts)
- **SurfPool** — only needed for `solana-holders-analysis.py` (Linux/WSL, get at https://surfpool.run)
- **Solflare** — for exporting private key (https://solflare.com)

## Setup

```bash
# Clone the repo
git clone <repo-url>
cd honeypot

# Create virtual environment (recommended)
python -m venv env
env\Scripts\activate  # Windows
# source env/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

> **Note:** `solana>=0.27,<0.31` pins the Solana SDK to a version compatible with `solana.rpc.api.Client`.

## How It Works

### `solana_honeypot.py`
1. Checks **Mint Authority** and **Freeze Authority** via Helius `getAccountInfo` with `jsonParsed`
2. Fetches buy quote and builds swap transaction via Jupiter API (`api.jup.ag/swap/v1`)
3. Signs both buy and sell transactions with your private key
4. Sends both as a bundle to Helius `simulateBundle`
5. Extracts actual buy amount from `postTokenBalances` and actual sell amount from native SOL balance change
6. Calculates buy/sell taxes

### `solana-holders-analysis.py`
1. Fetches top 20 token accounts via Helius `getTokenLargestAccounts`
2. Batch resolves owner addresses via `getMultipleAccounts` (decodes from token account data)
3. Filters out PDAs (program-derived addresses), keeps only standard wallets
4. Airdrops 10 SOL to each top holder on local SurfPool for fees
5. Quotes and builds a Jupiter swap for selling 1% of their holdings
6. Replaces the blockhash with the local one and simulates via SurfPool `simulateTransaction`
7. Detects per-holder restrictions — some wallets may succeed while others get `Custom 6001` (honeypot transfer block)

## API Keys

| Service | Where to Get | Cost |
|---|---|---|
| Helius | https://dev.helius.xyz | Free tier available |
| Jupiter | https://api.jup.ag | Free (no key needed) |

## Notes

- Transactions use `asLegacyTransaction=true` for maximum compatibility with local validators
- Simulation uses `sigVerify: false` (no real signature needed for local simulation)
- Blockhash is replaced via byte-level manipulation to work with both `Message` (legacy) and `MessageV0` (versioned) formats
- The `solana_honeypot.py` script requires a real private key with enough SOL to pass Helius signature validation
- Helius HTTP calls use `requests.Session` with 30s timeout to avoid solders `Client` timeout issues
- Jupiter swap has built-in retry (1 retry on failure)

## License

MIT
