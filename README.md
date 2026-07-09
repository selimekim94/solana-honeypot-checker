# Solana Token Honeypot Checker

Tools to analyze Solana tokens for honeypot-like behavior:

| Script | Purpose | Surfpool? |
|--------|---------|-----------|
| `solana_honeypot.py` | Buy/sell tax via Jupiter + Helius `simulateBundle` | **No** |
| `solana-holders-analysis.py` | Top holders + capped sell sims on local fork | **Yes** (optional) |

Shared: `.env` loading (`env_loader.py`), Helius/Jupiter APIs.

---

## Scripts

### 1. `solana_honeypot.py`

Mainnet-only check (Helius or any Solana RPC). **Does not use Surfpool.**

**Features:**
- Mint / freeze authority
- Jupiter quote + versioned swap txs (buy SOL→token, sell token→SOL)
- Pool analysis per hop in the route:
  - Route hop summary (DEX, mints, amounts)
  - Pair vault reserves (Whirlpool / DLMM layouts when known; filtered hop mints)
  - Token supply + top holders (burn/null owners called out)
  - Classic LP burn % only for Raydium AMM v4 when LP mint resolves
  - CLMM / DLMM / Pump: **LP burn N/A** (by design)
- Simulation:
  - Prefer Helius `simulateBundle` (buy+sell same block)
  - Fallback: sequential `simulateTransaction` with `replaceRecentBlockhash` + `sigVerify: false`
- Tax from token post-balances (buy) and native SOL / WSOL delta (sell)

**Usage:**
```bash
# Configure .env (see below), then:
python solana_honeypot.py
```

**Config (`.env` or environment):**

| Variable | Description | Default |
|----------|-------------|---------|
| `HELIUS_API_KEY` | Helius key | — (or use `SOLANA_RPC`) |
| `SOLANA_RPC` | Full RPC URL | built from Helius key if set |
| `OUTPUT_MINT` | Token mint to test | sample mint in code |
| `BUY_LAMPORTS` | Buy size in lamports | `1000000` (0.001 SOL) |
| `SLIPPAGE_BPS` | Slippage (bps) | `500` |
| `PRIVATE_KEY_B58` | Wallet secret (base58) | ephemeral if missing |
| `JUPITER_API_KEY` | Optional Jupiter portal key | — |
| `RPC_TIMEOUT` | HTTP timeout seconds | `30` |

**Private key (base58):**
```bash
# Phantom / Solflare export (base58 string):
PRIVATE_KEY_B58=4Nd1mY...

# Or convert 64-byte array with convert.py, then set PRIVATE_KEY_B58
```

> Funded wallet recommended for realistic buy sim. Without funds, quotes/authorities still work; buy sim may fail.

---

### 2. `solana-holders-analysis.py`

Lists top holders via mainnet RPC; optionally simulates **capped** sells on **Surfpool**.

**Features:**
- Top N accounts via `getTokenLargestAccounts` + owner decode (`getMultipleAccounts`)
- PDA owners flagged / **skipped by default** (cannot pay fees)
- Sell size = min(`SELL_PCT` of balance, ~`MAX_SELL_SOL` expected out)
- One Jupiter price probe per mint + ~1 quote + 1 swap per holder (rate-limit friendly)
- Surfpool: `surfnet_setAccount` / `surfnet_setTokenAccount`, clock sync (`surfnet_timeTravel`), account warm before sim
- Fail classification: Meteora fork, timestamp, slippage, PDA fee (not all = honeypot)

**Prerequisites (for local sim):** Surfpool at `LOCAL_RPC` (default `http://127.0.0.1:8899`).

**Usage:**
```bash
# Optional: start Surfpool with mainnet datasource
surfpool start --rpc-url https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

python solana-holders-analysis.py

# Holders list only (no Surfpool):
# SKIP_LOCAL_SIM=1 in .env
```

**Config (`.env` or environment):**

| Variable | Description | Default |
|----------|-------------|---------|
| `HELIUS_API_KEY` / `SOLANA_RPC` | Mainnet read RPC | — |
| `LOCAL_RPC` | Surfpool URL | `http://127.0.0.1:8899` |
| `OUTPUT_MINT` | Token mint | — |
| `TOP_N` | Largest accounts to list | `20` |
| `SIM_TOP` | Holders to simulate | `10` |
| `SELL_PCT` | Max fraction of balance | `0.01` |
| `MAX_SELL_SOL` | Cap sell by expected SOL | `1.0` |
| `MIN_SELL_UI` | Min tokens to try | `10` |
| `SLIPPAGE_BPS` | Jupiter slippage for holder sims | `1500` |
| `JUPITER_MIN_INTERVAL` | Seconds between Jupiter calls | `1.0` |
| `SKIP_LOCAL_SIM` | `1` = list holders only | off |
| `SKIP_PDA_SIM` | `0` = try PDAs too | **on** (`1`) |
| `JUPITER_API_KEY` | Optional | — |

---

## Requirements

- **Python 3.9+**
- **Helius** (or any Solana RPC) — https://dev.helius.xyz  
- **Jupiter** — https://api.jup.ag (optional API key)  
- **Surfpool** — only for holder sell sims — https://surfpool.run  

## Setup

```bash
git clone <repo-url>
cd honeypot

python -m venv env
# Windows:
env\Scripts\activate
# Linux/macOS:
# source env/bin/activate

pip install -r requirements.txt

# Config
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
# Edit .env: HELIUS_API_KEY, OUTPUT_MINT, PRIVATE_KEY_B58, ...
```

### `.env` example

```env
HELIUS_API_KEY=your_helius_key_here
PRIVATE_KEY_B58=
OUTPUT_MINT=9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump
BUY_LAMPORTS=1000000
SLIPPAGE_BPS=500

LOCAL_RPC=http://127.0.0.1:8899
TOP_N=20
SIM_TOP=10
SELL_PCT=0.01
MAX_SELL_SOL=1.0
JUPITER_MIN_INTERVAL=1.0
```

Do **not** commit `.env` (listed in `.gitignore`).

---

## How it works

### `solana_honeypot.py`
1. Load `.env` via `env_loader.load_dotenv()`
2. Mint/freeze via `getAccountInfo` (jsonParsed)
3. Jupiter buy quote → swap tx (versioned)
4. Pool analysis (route hops, vaults, token holders, LP burn when applicable)
5. Jupiter sell quote → swap tx
6. `simulateBundle` if available; else sequential `simulateTransaction`
7. Buy tax from target mint post balance; sell tax from native SOL and/or WSOL delta

### `solana-holders-analysis.py`
1. Load `.env`
2. Top accounts + owner decode (curve check → PDA flag)
3. Optional Surfpool health check
4. One Jupiter **price probe** for the mint (shared)
5. Per holder: capped amount → 1 quote → 1 swap → warm accounts → clock sync → local sim
6. Summarize success/fail with error class hints

---

## Interpreting results

| Signal | Likely meaning |
|--------|----------------|
| Buy/sell tax ~0% on honeypot script | Round-trip OK at small size |
| Holder sim **SUCCESS** | That wallet can sell a **capped** size on the fork |
| Holder **3007 / 0xbbf** | Meteora state incomplete on Surfpool — often **not** honeypot |
| Holder **6022 InvalidTimestamp** | Fork clock drift — script tries `surfnet_timeTravel` |
| Holder **InvalidAccountForFee** | PDA fee payer — skipped by default |
| Large top holder (e.g. 50%+) | Concentration / pool vault risk — separate from tax honeypot |

Full-bag dumps are **not** simulated; use `MAX_SELL_SOL` for safe, comparable checks.

---

## Project layout (Solana)

```
honeypot/
  solana_honeypot.py
  solana-holders-analysis.py
  env_loader.py
  convert.py              # optional: 64-byte array → base58
  .env.example
  requirements.txt
  README.md
```

---

## Notes

- Solana scripts use **versioned** Jupiter txs (`asLegacyTransaction=false`)
- Holder local sim: `sigVerify: false` + `replaceRecentBlockhash: true` (no manual blockhash patch)
- Helius free tier may **403 JSON-RPC batch**; honeypot falls back to sequential RPC
- Jupiter free tier is rate-limited; raise `JUPITER_MIN_INTERVAL` or set `JUPITER_API_KEY` on 429
- Rotate API keys if they appear in logs/terminals

---

## License

MIT
