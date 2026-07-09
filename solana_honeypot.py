"""
solana_honeypot.py

Buy + sell honeypot check via Jupiter quote/swap + RPC simulation.
Uses Helius (or any Solana RPC). Does NOT require Surfpool.

Env:
  HELIUS_API_KEY   — Helius key (or set SOLANA_RPC fully)
  SOLANA_RPC       — override RPC URL
  OUTPUT_MINT      — token mint to test
  BUY_LAMPORTS     — SOL amount in lamports (default 1_000_000 = 0.001 SOL)
  SLIPPAGE_BPS     — default 500
  PRIVATE_KEY_B58  — base58 secret (optional; ephemeral keypair if missing)
  JUPITER_API_KEY  — optional Jupiter portal key
"""

from __future__ import annotations

import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from env_loader import load_dotenv

# ---------------------------------------------------------------------------
# Config (.env then process env)
# ---------------------------------------------------------------------------
_env_file = load_dotenv()
if _env_file:
    print(f"Loaded env: {_env_file}")

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
SOLANA_RPC = os.environ.get("SOLANA_RPC", "").strip()
if not SOLANA_RPC and HELIUS_API_KEY:
    SOLANA_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

if not SOLANA_RPC or "YOUR_HELIUS" in SOLANA_RPC or SOLANA_RPC.endswith("api-key="):
    print("Error: set HELIUS_API_KEY or SOLANA_RPC in .env or environment")
    print("  Create .env next to this script (see .env.example)")
    sys.exit(1)

OUTPUT_MINT = os.environ.get("OUTPUT_MINT", "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump").strip()
BUY_AMOUNT_LAMPORTS = int(os.environ.get("BUY_LAMPORTS", "1000000"))
INPUT_MINT = "So11111111111111111111111111111111111111112"
SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "500"))
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "").strip()

JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1/swap"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Owners / token-accounts treated as burned / null LP
BURN_OWNERS = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
}
KNOWN_MINTS = {
    INPUT_MINT: "WSOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Programs without classic fungible LP mint (CLMM / prop AMMs)
NO_CLASSIC_LP_PROGRAMS = {
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool (CLMM — LP is NFT, no burn %)",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM (LP is NFT, no burn %)",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Raydium CPMM (check LP mint if present)",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM (bin liquidity, no classic LP burn)",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora pools",
    "riptK81hDxhe5pW5jSzSM9iRA8azgEgLJ4dXkPtBS7j": "Riptide (no standard Raydium LP mint)",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump.fun AMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun bonding curve",
}

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})
if JUPITER_API_KEY:
    SESSION.headers["x-api-key"] = JUPITER_API_KEY

RPC_TIMEOUT = float(os.environ.get("RPC_TIMEOUT", "30"))


def _load_keypair() -> Keypair:
    b58 = os.environ.get("PRIVATE_KEY_B58", "").strip()
    if b58:
        try:
            return Keypair.from_base58_string(b58)
        except Exception as e:
            print(f"Warning: PRIVATE_KEY_B58 invalid ({e}), using ephemeral keypair")
    # Ephemeral — fine for quote building; simulation may fail without funded SOL
    kp = Keypair()
    print("Note: no PRIVATE_KEY_B58 — using ephemeral wallet (fund it or sim may fail)")
    return kp


KEYPAIR = _load_keypair()
USER_PUBKEY = str(KEYPAIR.pubkey())


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
def rpc_call(method: str, params: list, timeout: float = RPC_TIMEOUT) -> dict:
    r = SESSION.post(
        SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def rpc_batch(calls: list[tuple[str, list]], timeout: float = RPC_TIMEOUT) -> list[dict]:
    """Try JSON-RPC batch; fall back to sequential (Helius free tier often 403s on batch)."""
    if not calls:
        return []
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
        for i, (m, p) in enumerate(calls)
    ]
    try:
        r = SESSION.post(SOLANA_RPC, json=payload, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                by_id = {item.get("id"): item for item in data}
                return [by_id.get(i, {"error": "missing"}) for i in range(len(calls))]
            # single object — not a real batch response
            if isinstance(data, dict) and "error" not in data:
                return [data] * len(calls)
    except requests.RequestException:
        pass

    # Sequential fallback
    out: list[dict] = []
    for method, params in calls:
        try:
            out.append(rpc_call(method, params, timeout=timeout))
        except requests.RequestException as e:
            out.append({"error": str(e)})
    return out


def _tax(expected: int, actual: int) -> Optional[float]:
    if expected <= 0:
        return None
    if actual <= 0:
        return 100.0
    if actual >= expected:
        return 0.0
    return round((expected - actual) / expected * 100 * 10) / 10


# ---------------------------------------------------------------------------
# Mint authorities
# ---------------------------------------------------------------------------
def check_authorities(mint_str: str) -> tuple[Optional[str], Optional[str], int]:
    print("\n--- Mint / Freeze Authority ---")
    result = rpc_call("getAccountInfo", [mint_str, {"encoding": "jsonParsed"}])
    val = (result.get("result") or {}).get("value")
    if not val:
        print(f"getAccountInfo failed: {result.get('error', 'unknown')}")
        return None, None, 6

    data = val.get("data")
    if not isinstance(data, dict) or "parsed" not in data:
        print("Mint account not jsonParsed (wrong address or program?)")
        return None, None, 6

    info = data["parsed"]["info"]
    mint_auth = info.get("mintAuthority")
    freeze_auth = info.get("freezeAuthority")
    decimals = int(info.get("decimals", 6))

    if mint_auth:
        flag = " (YOU)" if mint_auth == USER_PUBKEY else "  ** RED FLAG: can mint **"
        print(f"Mint Authority:   {mint_auth}{flag}")
    else:
        print("Mint Authority:   None")

    if freeze_auth:
        flag = " (YOU)" if freeze_auth == USER_PUBKEY else "  ** RED FLAG: can freeze **"
        print(f"Freeze Authority: {freeze_auth}{flag}")
    else:
        print("Freeze Authority: None")

    print(f"Decimals:         {decimals}")
    return mint_auth, freeze_auth, decimals


# ---------------------------------------------------------------------------
# Jupiter
# ---------------------------------------------------------------------------
def jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage: int) -> Optional[dict]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage),
        # versioned tx (v0) — matches solders VersionedTransaction signing
        "asLegacyTransaction": "false",
    }
    try:
        r = SESSION.get(JUPITER_QUOTE_API, params=params, timeout=20)
        r.raise_for_status()
        q = r.json()
        if "error" in q or "outAmount" not in q:
            print(f"Jupiter quote error: {q}")
            return None
        return q
    except requests.RequestException as e:
        print(f"Jupiter quote failed: {e}")
        if getattr(e, "response", None) is not None:
            print(f"  {e.response.text[:300]}")
        return None


def jupiter_swap_tx(quote: dict, user_pubkey: str) -> Optional[str]:
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
        "asLegacyTransaction": False,
    }
    try:
        r = SESSION.post(JUPITER_SWAP_API, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        tx = data.get("swapTransaction")
        if not tx:
            print(f"Jupiter swap missing tx: {data}")
        return tx
    except requests.RequestException as e:
        print(f"Jupiter swap failed: {e}")
        if getattr(e, "response", None) is not None:
            print(f"  {e.response.text[:300]}")
        return None


def sign_tx_b64(tx_b64: str, keypair: Keypair) -> str:
    raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    sig = keypair.sign_message(to_bytes_versioned(raw.message))
    signed = VersionedTransaction.populate(raw.message, [sig])
    return base64.b64encode(bytes(signed)).decode("utf-8")


# ---------------------------------------------------------------------------
# Pool analysis (batched RPC)
# ---------------------------------------------------------------------------
def _read_pubkey(data: bytes, offset: int) -> str:
    return str(Pubkey.from_bytes(data[offset : offset + 32]))


def _parse_token_ui(val: dict) -> Optional[dict]:
    d = val.get("data")
    if isinstance(d, dict) and d.get("program") in ("spl-token", "spl-token-2022"):
        p = d["parsed"]["info"]
        ta = p["tokenAmount"]
        return {
            "mint": p["mint"],
            "amount": int(ta["amount"]),
            "decimals": int(ta["decimals"]),
            "ui": int(ta["amount"]) / (10 ** int(ta["decimals"])),
        }
    return None


def _mint_label(mint: str, output_mint: str) -> str:
    if mint == output_mint:
        return "OUTPUT (token)"
    if mint in KNOWN_MINTS:
        return KNOWN_MINTS[mint]
    return f"{mint[:8]}..."


def _print_token_supply_stats(mint: str, title: str = "Token") -> None:
    """Total supply + top holders for a mint (token or LP)."""
    sr, lo = rpc_batch([
        ("getTokenSupply", [mint]),
        ("getTokenLargestAccounts", [mint]),
    ])
    sv = (sr.get("result") or {}).get("value")
    if not sv:
        print(f"  {title} supply: n/a")
        return
    total_raw = int(sv["amount"])
    dec = int(sv["decimals"])
    if total_raw <= 0:
        print(f"  {title} supply: 0")
        return
    total_ui = total_raw / (10 ** dec)
    print(f"  {title} supply: {total_ui:,.4f}  (mint {mint[:12]}...)")

    entries = (lo.get("result") or {}).get("value") or []
    if not entries:
        return

    # Resolve owners for top token accounts (batch)
    addrs = [e["address"] for e in entries[:8]]
    multi = rpc_call("getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}])
    infos = (multi.get("result") or {}).get("value") or []

    burned_raw = 0
    print(f"  {title} top holders:")
    shown = 0
    for e, info in zip(entries[:8], infos):
        amt = int(e["amount"])
        pct = amt / total_raw * 100
        owner = "?"
        if info and isinstance(info.get("data"), dict):
            try:
                owner = info["data"]["parsed"]["info"]["owner"]
            except (KeyError, TypeError):
                owner = e["address"][:12] + "..."
        else:
            owner = e["address"][:12] + "..."

        is_burn = owner in BURN_OWNERS or e["address"] in BURN_OWNERS
        if is_burn:
            burned_raw += amt
            print(f"    BURN/null {owner[:12]}...  {pct:.1f}%")
        else:
            print(f"    {owner[:12]}...  {pct:.1f}%")
            shown += 1

    burn_pct = burned_raw / total_raw * 100 if total_raw else 0
    if burn_pct > 0:
        print(f"  {title} burned/null (top accounts): {burn_pct:.1f}%")
    circulating_hint = 100.0 - burn_pct
    print(f"  {title} non-burn in top list: ~{circulating_hint:.1f}% of supply in listed accounts")


def _print_lp_stats(lp_mint: str) -> None:
    print(f"  LP mint: {lp_mint}")
    _print_token_supply_stats(lp_mint, title="LP")


# Orca Whirlpool account offsets (incl. 8-byte Anchor discriminator)
_WHIRLPOOL_MINT_A = 101
_WHIRLPOOL_VAULT_A = 133
_WHIRLPOOL_MINT_B = 181
_WHIRLPOOL_VAULT_B = 213
# Meteora DLMM LbPair (common layout after disc)
_DLMM_TOKEN_X_MINT = 88
_DLMM_TOKEN_Y_MINT = 120
_DLMM_RESERVE_X = 152
_DLMM_RESERVE_Y = 184


def _whirlpool_vaults(raw: bytes) -> list[str]:
    if len(raw) < _WHIRLPOOL_VAULT_B + 32:
        return []
    return [
        _read_pubkey(raw, _WHIRLPOOL_VAULT_A),
        _read_pubkey(raw, _WHIRLPOOL_VAULT_B),
    ]


def _dlmm_vaults(raw: bytes) -> list[str]:
    if len(raw) < _DLMM_RESERVE_Y + 32:
        return []
    return [
        _read_pubkey(raw, _DLMM_RESERVE_X),
        _read_pubkey(raw, _DLMM_RESERVE_Y),
    ]


def _hop_pair_mints(hop_info: dict, output_mint: str) -> set[str]:
    """Mints involved in this hop (from Jupiter swapInfo)."""
    mints = {output_mint, INPUT_MINT}
    for k in ("inputMint", "outputMint"):
        v = hop_info.get(k)
        if v:
            mints.add(v)
    return mints


def _fetch_vault_reserves(vaults: list[str]) -> dict[str, float]:
    reserves: dict[str, float] = {}
    if not vaults:
        return reserves
    vbatch = rpc_batch([
        ("getAccountInfo", [v, {"encoding": "jsonParsed"}]) for v in vaults
    ])
    for vresp in vbatch:
        vv = (vresp.get("result") or {}).get("value")
        if not vv:
            continue
        parsed = _parse_token_ui(vv)
        if not parsed:
            continue
        reserves[parsed["mint"]] = reserves.get(parsed["mint"], 0.0) + parsed["ui"]
    return reserves


def analyze_pools(quote_data: dict, output_mint: str) -> None:
    route = quote_data.get("routePlan") or []
    if not route:
        print("\n(no routePlan in quote — skip pool analysis)")
        return

    print("\n" + "=" * 60)
    print("Pool Liquidity Analysis")
    print("=" * 60)

    print("\n--- Target token supply / holders ---")
    _print_token_supply_stats(output_mint, title="Token")

    print("\n--- Route hops ---")
    for idx, step in enumerate(route):
        info = step.get("swapInfo") or {}
        in_m = info.get("inputMint", "?")
        out_m = info.get("outputMint", "?")
        print(
            f"  Hop {idx + 1}: {info.get('label', '?')}  "
            f"{_mint_label(in_m, output_mint) if len(str(in_m)) > 8 else in_m} "
            f"-> {_mint_label(out_m, output_mint) if len(str(out_m)) > 8 else out_m}  "
            f"in={info.get('inAmount', '?')} out={info.get('outAmount', '?')}"
        )

    meta = []
    for idx, step in enumerate(route):
        info = step.get("swapInfo") or {}
        pool = info.get("ammKey")
        if pool:
            meta.append((idx, info.get("label", "Unknown"), pool, info))
    if not meta:
        return

    batch = rpc_batch([
        ("getAccountInfo", [p, {"encoding": "base64"}]) for _, _, p, _ in meta
    ])

    for (idx, dex, pool_addr, hop_info), acc_resp in zip(meta, batch):
        print(f"\nPool {idx + 1}: {pool_addr}")
        print(f"DEX:     {dex}")
        val = (acc_resp.get("result") or {}).get("value")
        if not val:
            print(f"  Could not fetch pool: {acc_resp.get('error')}")
            continue

        owner = val.get("owner", "")
        print(f"Program: {owner}")
        if owner in NO_CLASSIC_LP_PROGRAMS:
            print(f"  Note:   {NO_CLASSIC_LP_PROGRAMS[owner]}")

        raw_b64 = val["data"][0] if isinstance(val.get("data"), list) else None
        if not raw_b64:
            continue
        raw = base64.b64decode(raw_b64)

        lp_mint = None
        vaults: list[str] = []
        hop_mints = _hop_pair_mints(hop_info, output_mint)

        # Prefer layout-specific vaults (accurate) over getTokenAccountsByOwner (noisy)
        if owner == "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":
            vaults = _whirlpool_vaults(raw)
        elif owner == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo":
            vaults = _dlmm_vaults(raw)
        elif owner == RAYDIUM_AMM_V4 and len(raw) >= 400:
            usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            want = [INPUT_MINT, output_mint, usdc]
            positions = []
            for m in want:
                pos = raw.find(bytes(Pubkey.from_string(m)))
                if pos >= 0:
                    positions.append(pos)
            if len(positions) >= 2:
                positions.sort()
                coin_off = positions[0]
                va, vb = coin_off - 64, coin_off - 32
                lp_off = positions[-1] + 32
                if va >= 0 and lp_off + 32 <= len(raw):
                    vaults = [_read_pubkey(raw, va), _read_pubkey(raw, vb)]
                    lp_mint = _read_pubkey(raw, lp_off)
            if not vaults and len(raw) >= 400:
                try:
                    lp_mint = _read_pubkey(raw, 336)
                    vaults = [_read_pubkey(raw, 240), _read_pubkey(raw, 272)]
                except Exception:
                    pass

        reserves = _fetch_vault_reserves(vaults)

        # Fallback: owner token accounts, filtered to hop mints only
        if not reserves:
            for prog in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                vr = rpc_call(
                    "getTokenAccountsByOwner",
                    [pool_addr, {"programId": prog}, {"encoding": "jsonParsed"}],
                )
                for va in (vr.get("result") or {}).get("value") or []:
                    p = va["account"]["data"]["parsed"]["info"]
                    mint = p["mint"]
                    if mint not in hop_mints:
                        continue
                    ui = int(p["tokenAmount"]["amount"]) / (10 ** int(p["tokenAmount"]["decimals"]))
                    if ui < 1e-6:
                        continue
                    reserves[mint] = reserves.get(mint, 0.0) + ui

        # Filter noise: keep hop pair + well-known quotes, drop dust
        if reserves:
            filtered = {
                m: u for m, u in reserves.items()
                if (m in hop_mints or m in KNOWN_MINTS) and u >= 0.0001
            }
            # if filter wiped everything, show top 2 by size
            if not filtered:
                filtered = dict(sorted(reserves.items(), key=lambda x: -x[1])[:2])
            print("  Reserves (pair vaults):")
            for mint, ui in sorted(filtered.items(), key=lambda x: -x[1]):
                print(f"    {_mint_label(mint, output_mint)}: {ui:,.4f}")
        else:
            print("  Reserves: could not read pair vaults")
            print(f"  Hop mints: {', '.join(_mint_label(m, output_mint) for m in hop_mints)}")

        if lp_mint and lp_mint not in hop_mints and lp_mint not in KNOWN_MINTS:
            mi = rpc_call("getAccountInfo", [lp_mint, {"encoding": "jsonParsed"}])
            mval = (mi.get("result") or {}).get("value")
            if mval and mval.get("owner") in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                _print_lp_stats(lp_mint)
            else:
                print("  LP burn: N/A (no valid LP mint)")
        elif owner == RAYDIUM_AMM_V4:
            print("  LP burn: could not resolve LP mint")
        elif owner in NO_CLASSIC_LP_PROGRAMS:
            print("  LP burn: N/A for this DEX type")
        else:
            print("  LP burn: N/A")


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def simulate_tx(signed_b64: str) -> dict:
    """Single-tx simulation with blockhash refresh (no sig verify needed for analysis)."""
    return rpc_call(
        "simulateTransaction",
        [
            signed_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            },
        ],
    )


def simulate_bundle(buy_b64: str, sell_b64: str) -> dict:
    """Jito-style bundle if RPC supports it; else empty error for fallback."""
    try:
        return rpc_call(
            "simulateBundle",
            [{
                "encodedTransactions": [buy_b64, sell_b64],
                "replaceRecentBlockhash": True,
            }],
        )
    except Exception as e:
        return {"error": str(e)}


def _token_amount(balances: list | None, mint: str, owner: str | None = None) -> int:
    total = 0
    for t in balances or []:
        if t.get("mint") != mint:
            continue
        if owner and t.get("owner") and t.get("owner") != owner:
            continue
        try:
            total += int(t["uiTokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            pass
    return total


def _buy_actual_from_sim(sim_val: dict, mint: str, owner: str) -> Optional[int]:
    """Post-balance of target mint for owner (after buy)."""
    post = _token_amount(sim_val.get("postTokenBalances"), mint, owner)
    if post > 0:
        return post
    post_any = _token_amount(sim_val.get("postTokenBalances"), mint, None)
    return post_any if post_any > 0 else None


def _native_sol_delta(sim_val: dict) -> int:
    """Fee-payer native SOL change + fee (≈ received/spent excluding fee)."""
    pre = sim_val.get("preBalances") or []
    post = sim_val.get("postBalances") or []
    if not pre or not post:
        return 0
    fee = int(sim_val.get("fee") or 0)
    return int(post[0]) - int(pre[0]) + fee


def _wsol_delta(sim_val: dict, owner: str) -> int:
    """WSOL ATA delta for owner (lamports units, 9 decimals)."""
    pre = _token_amount(sim_val.get("preTokenBalances"), INPUT_MINT, owner)
    post = _token_amount(sim_val.get("postTokenBalances"), INPUT_MINT, owner)
    return post - pre


def _sell_sol_received(sim_val: dict, owner: str) -> tuple[int, str]:
    """
    SOL received from a sell sim.
    Prefer max(native SOL delta, WSOL ATA delta) — Jupiter may leave WSOL unwrapped
    mid-meta or only credit WSOL depending on path.
    Returns (amount_lamports, source_label).
    """
    native = _native_sol_delta(sim_val)
    wsol = _wsol_delta(sim_val, owner)
    # Also sum WSOL without owner filter (some sims omit owner field)
    wsol_any = _token_amount(sim_val.get("postTokenBalances"), INPUT_MINT, None) - _token_amount(
        sim_val.get("preTokenBalances"), INPUT_MINT, None
    )

    candidates = [
        (native, "native_sol"),
        (wsol, "wsol_ata"),
        (wsol_any, "wsol_any"),
    ]
    best_amt, best_src = max(candidates, key=lambda x: x[0])
    if best_amt < 0:
        return 0, "none"
    return best_amt, best_src


def _sim_err(sim_val: dict) -> Optional[object]:
    return sim_val.get("err")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("Solana Buy/Sell Honeypot Check")
    print(f"Wallet:  {USER_PUBKEY}")
    print(f"Trade:   {BUY_AMOUNT_LAMPORTS / 1e9} SOL -> {OUTPUT_MINT}")
    # Don't print full API key
    _rpc_show = SOLANA_RPC.split("api-key=")[0] + ("api-key=***" if "api-key=" in SOLANA_RPC else "")
    print(f"RPC:     {_rpc_show[:64]}")
    print("=" * 60)

    mint_auth, freeze_auth, decimals = check_authorities(OUTPUT_MINT)

    print("\n[1/3] Jupiter buy quote + swap tx...")
    buy_quote = jupiter_quote(INPUT_MINT, OUTPUT_MINT, BUY_AMOUNT_LAMPORTS, SLIPPAGE_BPS)
    if not buy_quote:
        return
    estimated_out = int(buy_quote.get("outAmount", "0"))
    ui_out = estimated_out / (10 ** decimals)
    print(f"  Estimated out: {ui_out:,.6f} (raw {estimated_out})")
    if estimated_out == 0:
        print("Buy quotes 0 tokens — stop.")
        return

    buy_tx_b64 = jupiter_swap_tx(buy_quote, USER_PUBKEY)
    if not buy_tx_b64:
        return

    print("\n[2/3] Pool analysis...")
    analyze_pools(buy_quote, OUTPUT_MINT)

    print("\n[3/3] Jupiter sell quote + swap tx + simulation...")
    # Fetch sell quote in parallel with signing buy (small win)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sell_q = ex.submit(jupiter_quote, OUTPUT_MINT, INPUT_MINT, estimated_out, SLIPPAGE_BPS)
        signed_buy = sign_tx_b64(buy_tx_b64, KEYPAIR)
        sell_quote = f_sell_q.result()

    if not sell_quote:
        print("Sell quote failed")
        return
    expected_sell = int(sell_quote.get("outAmount", "0"))
    print(f"  Estimated sell out: {expected_sell / 1e9:.6f} SOL")

    sell_tx_b64 = jupiter_swap_tx(sell_quote, USER_PUBKEY)
    if not sell_tx_b64:
        return
    signed_sell = sign_tx_b64(sell_tx_b64, KEYPAIR)

    buy_actual: Optional[int] = None
    sell_actual: Optional[int] = None
    sell_src = "none"
    sell_failed = False
    mode = "bundle"

    bundle = simulate_bundle(signed_buy, signed_sell)
    berr = bundle.get("error")
    bval = (bundle.get("result") or {}).get("value") or {}
    # Helius/Jito may nest under summary / transactionResults
    tx_results = (
        bval.get("transactionResults")
        or bval.get("transactions")
        or []
    )
    # Some providers wrap each result as {result: {value: ...}} or flat meta
    def _unwrap_tx(r: dict) -> dict:
        if not isinstance(r, dict):
            return {}
        if "postBalances" in r or "postTokenBalances" in r or "err" in r:
            return r
        inner = r.get("result") or r.get("value") or r.get("meta") or {}
        if isinstance(inner, dict) and ("postBalances" in inner or "err" in inner):
            return inner
        return r

    bundle_ok = (
        not berr
        and len(tx_results) >= 2
        and not bval.get("err")
    )
    if bundle_ok:
        t0 = _unwrap_tx(tx_results[0])
        t1 = _unwrap_tx(tx_results[1])
        e0, e1 = _sim_err(t0), _sim_err(t1)
        if e0:
            print(f"  Bundle buy FAILED: {e0}")
            bundle_ok = False
        if e1:
            print(f"  Bundle sell FAILED: {e1}")
            sell_failed = True
            for lg in (t1.get("logs") or [])[-5:]:
                print(f"    {lg[:100]}")
        if not e0:
            buy_actual = _buy_actual_from_sim(t0, OUTPUT_MINT, USER_PUBKEY)
        if not e1:
            sell_actual, sell_src = _sell_sol_received(t1, USER_PUBKEY)
            print(f"  Simulation: simulateBundle OK (sell via {sell_src})")
        else:
            print("  Simulation: simulateBundle partial (buy ok / sell fail)")
    if not bundle_ok and sell_actual is None:
        mode = "sequential"
        print(f"  Bundle unavailable ({berr or bval.get('err') or 'empty/partial'}) — sequential sim")
        # Buy sim
        sb = simulate_tx(signed_buy)
        if "error" in sb:
            print(f"  Buy sim RPC error: {sb['error']}")
        else:
            bv = (sb.get("result") or {}).get("value") or {}
            if bv.get("err"):
                print(f"  Buy sim FAILED: {bv['err']}")
                for lg in (bv.get("logs") or [])[-5:]:
                    print(f"    {lg[:100]}")
            else:
                buy_actual = _buy_actual_from_sim(bv, OUTPUT_MINT, USER_PUBKEY)
                print(f"  Buy sim OK  actual={buy_actual}")

        # Sell sim alone only works if wallet already holds tokens on this RPC.
        # After buy sim, state is not committed — sell may fail with insufficient funds.
        ss = simulate_tx(signed_sell)
        if "error" in ss:
            print(f"  Sell sim RPC error: {ss['error']}")
        else:
            sv = (ss.get("result") or {}).get("value") or {}
            if sv.get("err"):
                sell_failed = True
                print(f"  Sell sim FAILED: {sv['err']}")
                print("  (Expected if sequential: sell tx assumes tokens already in wallet.)")
                for lg in (sv.get("logs") or [])[-5:]:
                    print(f"    {lg[:100]}")
            else:
                sell_actual, sell_src = _sell_sol_received(sv, USER_PUBKEY)
                print(f"  Sell sim OK  sol={sell_actual} via {sell_src}")

    buy_tax = _tax(estimated_out, buy_actual or 0) if buy_actual is not None else None
    if sell_failed and (sell_actual is None or sell_actual == 0):
        sell_tax = None  # don't report 100% tax on sim/measurement failure
    elif sell_actual is not None and expected_sell > 0:
        sell_tax = _tax(expected_sell, sell_actual)
    else:
        sell_tax = None

    print("\n" + "=" * 25 + " SUMMARY " + "=" * 26)
    print(f"Token:            {OUTPUT_MINT}")
    print(f"Mode:             {mode}")
    print(f"Mint Authority:   {'None' if not mint_auth else mint_auth}")
    print(f"Freeze Authority: {'None' if not freeze_auth else freeze_auth}")
    print(f"Buy expected:     {estimated_out}  actual: {buy_actual}")
    print(f"Buy Tax:          {buy_tax if buy_tax is not None else 'N/A'}%")
    print(f"Sell expected:    {expected_sell}  actual: {sell_actual}  ({sell_src})")
    if sell_failed and sell_tax is None:
        print("Sell Tax:         N/A (sell sim failed or unmeasured — not necessarily honeypot)")
    else:
        print(f"Sell Tax:         {sell_tax if sell_tax is not None else 'N/A'}%")
    if sell_actual == 0 and not sell_failed and expected_sell > 0:
        print("Note: sell actual 0 with no err — check WSOL/native parsing; may be dust path.")
    print("=" * 60)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\nTotal time: {time.time() - t0:.2f}s")
