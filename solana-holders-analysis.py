"""
solana-holders-analysis.py

Top holders via Helius (or SOLANA_RPC) + optional local Surfpool sell simulation.

Env:
  HELIUS_API_KEY / SOLANA_RPC  — mainnet read RPC for holders
  LOCAL_RPC                    — Surfpool (default http://127.0.0.1:8899)
  OUTPUT_MINT                  — token mint
  TOP_N                        — largest accounts to fetch (default 20)
  SIM_TOP                      — how many holders to simulate (default 10)
  SELL_PCT                     — preferred fraction of balance (default 0.01)
  MAX_SELL_SOL                 — cap sell size by expected SOL out (default 1.0)
  MIN_SELL_UI                  — min tokens to try (default 10)
  SKIP_LOCAL_SIM               — set 1 to only list holders
  SKIP_PDA_SIM                 — skip PDA owners (default 1; set 0 to try PDAs)
  JUPITER_API_KEY              — optional
  SLIPPAGE_BPS                 — default 1500 for holder sims
  JUPITER_MIN_INTERVAL         — seconds between Jupiter calls (default 1.0)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from typing import Any, Optional

import requests
from solders.pubkey import Pubkey

from env_loader import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_env_file = load_dotenv()
if _env_file:
    print(f"Loaded env: {_env_file}")

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
SOLANA_RPC = os.environ.get("SOLANA_RPC", "").strip()
if not SOLANA_RPC and HELIUS_API_KEY:
    SOLANA_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
LOCAL_RPC = os.environ.get("LOCAL_RPC", "http://127.0.0.1:8899").strip()
OUTPUT_MINT = os.environ.get("OUTPUT_MINT", "6AJcP7wuLwmRYLBNbi825wgguaPsWzPBEHcHndpRpump").strip()
SOL_MINT = "So11111111111111111111111111111111111111112"
TOP_N = int(os.environ.get("TOP_N", "20"))
SIM_TOP = int(os.environ.get("SIM_TOP", "10"))
SELL_PCT = float(os.environ.get("SELL_PCT", "0.01"))
MAX_SELL_SOL = float(os.environ.get("MAX_SELL_SOL", "1.0"))
MIN_SELL_UI = float(os.environ.get("MIN_SELL_UI", "10"))
SKIP_LOCAL = os.environ.get("SKIP_LOCAL_SIM", "").strip() in ("1", "true", "yes")
# PDAs cannot pay tx fees → default skip (set SKIP_PDA_SIM=0 to force try)
SKIP_PDA = os.environ.get("SKIP_PDA_SIM", "1").strip().lower() not in ("0", "false", "no")
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "").strip()
SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "1500"))  # loose for fork sims

JUPITER_QUOTE = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP = "https://api.jup.ag/swap/v1/swap"

if not SOLANA_RPC or SOLANA_RPC.endswith("api-key="):
    print("Error: set HELIUS_API_KEY or SOLANA_RPC in .env or environment")
    sys.exit(1)

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})
if JUPITER_API_KEY:
    SESSION.headers["x-api-key"] = JUPITER_API_KEY

RPC_TIMEOUT = float(os.environ.get("RPC_TIMEOUT", "30"))

# Common program error codes seen in sims
ERROR_HINTS = {
    3007: "Meteora 3007 (0xbbf) — incomplete DLMM bin/bitmap on Surfpool fork (not necessarily honeypot)",
    1: "Insufficient funds / account",
    6001: "Slippage / min out — quote stale vs fork price; try higher SLIPPAGE_BPS",
    6022: "Whirlpool InvalidTimestamp — Surfpool clock behind mainnet; auto time-travel should fix",
    6024: "Anchor constraint",
}


def rpc(url: str, method: str, params: list, timeout: float = RPC_TIMEOUT) -> dict:
    r = SESSION.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout
    )
    r.raise_for_status()
    return r.json()


def local_ok() -> bool:
    try:
        j = rpc(LOCAL_RPC, "getHealth", [], timeout=3)
        return j.get("result") == "ok" or "result" in j
    except Exception:
        return False


def fund_sol(owner: str, lamports: int = 10_000_000_000) -> None:
    try:
        j = rpc(
            LOCAL_RPC,
            "surfnet_setAccount",
            [owner, {"lamports": lamports, "owner": "11111111111111111111111111111111"}],
            timeout=10,
        )
        if "error" not in j:
            return
    except Exception:
        pass
    try:
        rpc(LOCAL_RPC, "requestAirdrop", [owner, lamports], timeout=15)
    except Exception:
        pass


def fund_tokens(owner: str, mint: str, amount: int) -> None:
    """Ensure owner has `amount` of mint on Surfpool (cheatcode)."""
    if amount <= 0:
        return
    try:
        rpc(
            LOCAL_RPC,
            "surfnet_setTokenAccount",
            [owner, mint, {"amount": amount, "state": "initialized"}],
            timeout=10,
        )
    except Exception:
        pass


def sync_surfpool_clock() -> str:
    """
    Align Surfpool clock with wall time / mainnet block time.
    Fixes Orca Whirlpool Anchor Error 6022 InvalidTimestamp.
    """
    # Prefer mainnet block time (seconds) → ms for surfnet_timeTravel
    ts_ms = int(time.time() * 1000)
    try:
        slot_r = rpc(SOLANA_RPC, "getSlot", [{"commitment": "processed"}], timeout=10)
        slot = slot_r.get("result")
        if isinstance(slot, int):
            bt = rpc(SOLANA_RPC, "getBlockTime", [slot], timeout=10)
            if isinstance(bt.get("result"), int) and bt["result"] > 0:
                ts_ms = int(bt["result"]) * 1000 + 2000  # slight future skew
    except Exception:
        pass

    try:
        # docs: absoluteTimestamp is milliseconds
        j = rpc(LOCAL_RPC, "surfnet_timeTravel", [{"absoluteTimestamp": ts_ms}], timeout=10)
        if "error" in j:
            # fallback: absoluteSlot slightly ahead of local
            try:
                local_slot = rpc(LOCAL_RPC, "getSlot", [], timeout=5).get("result") or 0
                main_slot = rpc(SOLANA_RPC, "getSlot", [], timeout=10).get("result") or local_slot
                target = max(int(local_slot) + 1, int(main_slot))
                j2 = rpc(LOCAL_RPC, "surfnet_timeTravel", [{"absoluteSlot": target}], timeout=10)
                if "error" not in j2:
                    return f"slot→{target}"
            except Exception:
                pass
            return f"timeTravel err: {j.get('error')}"
        return f"ts_ms={ts_ms}"
    except Exception as e:
        return f"clock sync failed: {e}"


def _explain_err(err: Any) -> str:
    s = str(err)
    if s == "InvalidAccountForFee" or "InvalidAccountForFee" in s:
        return f"{s} — fee payer is PDA/system-ineligible; use SKIP_PDA_SIM=1 (default)"
    # {'InstructionError': [3, {'Custom': 3007}]}
    if isinstance(err, dict):
        ie = err.get("InstructionError")
        if isinstance(ie, list) and len(ie) >= 2:
            inner = ie[1]
            code = None
            if isinstance(inner, dict):
                code = inner.get("Custom")
            if code is not None and int(code) in ERROR_HINTS:
                return f"{s} — {ERROR_HINTS[int(code)]}"
    if "0xbbf" in s or "3007" in s:
        return f"{s} — {ERROR_HINTS[3007]}"
    if "0x1786" in s or "6022" in s or "InvalidTimestamp" in s:
        return f"{s} — {ERROR_HINTS[6022]}"
    if "0x1771" in s or "6001" in s:
        return f"{s} — {ERROR_HINTS[6001]}"
    return s


# ---------------------------------------------------------------------------
# Holders
# ---------------------------------------------------------------------------
def fetch_holders(mint: str, top_n: int) -> list[dict]:
    print(f"Fetching top {top_n} accounts for {mint[:12]}...")
    largest = rpc(SOLANA_RPC, "getTokenLargestAccounts", [mint])
    if "error" in largest:
        raise RuntimeError(f"getTokenLargestAccounts: {largest['error']}")
    top = ((largest.get("result") or {}).get("value") or [])[:top_n]
    if not top:
        return []

    addrs = [a["address"] for a in top]
    multi = rpc(SOLANA_RPC, "getMultipleAccounts", [addrs, {"encoding": "base64"}])
    if "error" in multi:
        raise RuntimeError(f"getMultipleAccounts: {multi['error']}")
    infos = (multi.get("result") or {}).get("value") or []

    holders: list[dict] = []
    for i, (acc, info) in enumerate(zip(top, infos), 1):
        try:
            if not info or not info.get("data"):
                print(f"  #{i} empty account data")
                continue
            data_b64 = info["data"][0] if isinstance(info["data"], list) else info["data"]
            raw = base64.b64decode(data_b64)
            if len(raw) < 64:
                continue
            owner = Pubkey.from_bytes(raw[32:64])
            on_curve = owner.is_on_curve()
            raw_amount = int(acc["amount"])
            decimals = int(acc["decimals"])
            ui = raw_amount / (10 ** decimals)
            holders.append({
                "token_account": acc["address"],
                "owner": str(owner),
                "raw_amount": raw_amount,
                "ui_amount": ui,
                "decimals": decimals,
                "on_curve": on_curve,
            })
            tag = "" if on_curve else " [PDA]"
            print(f"  {i:2}. {str(owner)[:8]}... | {ui:,.4f}{tag}")
        except Exception as e:
            print(f"  #{i} skip: {e}")
    return holders


# ---------------------------------------------------------------------------
# Jupiter + Surfpool warm
# ---------------------------------------------------------------------------
_last_jup_err = ""
_JUP_MIN_INTERVAL = float(os.environ.get("JUPITER_MIN_INTERVAL", "1.0"))  # sec between calls
_jup_last_call = 0.0
# mint -> (probe_raw, out_lamports) price cache
_price_cache: dict[str, tuple[int, int]] = {}


def _jup_throttle() -> None:
    global _jup_last_call
    wait = _JUP_MIN_INTERVAL - (time.time() - _jup_last_call)
    if wait > 0:
        time.sleep(wait)
    _jup_last_call = time.time()


def _jup_request(method: str, url: str, **kwargs) -> tuple[Optional[requests.Response], str]:
    """HTTP with throttle + 429 exponential backoff (max ~3 retries)."""
    global _last_jup_err
    delay = 2.0
    for attempt in range(4):
        _jup_throttle()
        try:
            if method == "GET":
                r = SESSION.get(url, timeout=kwargs.pop("timeout", 20), **kwargs)
            else:
                r = SESSION.post(url, timeout=kwargs.pop("timeout", 30), **kwargs)
        except Exception as e:
            _last_jup_err = str(e)[:200]
            return None, _last_jup_err

        if r.status_code == 429:
            _last_jup_err = f"HTTP 429: {r.text[:120]}"
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
            continue
        if r.status_code != 200:
            _last_jup_err = f"HTTP {r.status_code}: {r.text[:160]}"
            return None, _last_jup_err
        return r, ""
    return None, _last_jup_err or "HTTP 429 rate limited"


def jupiter_quote_sell(
    mint: str,
    amount: int,
    slippage_bps: int = 500,
    only_direct: bool = False,
) -> Optional[dict]:
    global _last_jup_err
    if amount <= 0:
        return None
    r, err = _jup_request(
        "GET",
        JUPITER_QUOTE,
        params={
            "inputMint": mint,
            "outputMint": SOL_MINT,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "asLegacyTransaction": "false",
            "onlyDirectRoutes": "true" if only_direct else "false",
        },
        timeout=20,
    )
    if not r:
        _last_jup_err = err
        return None
    try:
        q = r.json()
    except Exception as e:
        _last_jup_err = str(e)
        return None
    if "outAmount" not in q:
        _last_jup_err = str(q)[:200]
        return None
    _last_jup_err = ""
    return q


def jupiter_swap(quote: dict, user: str) -> tuple[Optional[str], str]:
    """Returns (tx_b64, error_message)."""
    r, err = _jup_request(
        "POST",
        JUPITER_SWAP,
        json={
            "quoteResponse": quote,
            "userPublicKey": user,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
            "asLegacyTransaction": False,
        },
        timeout=30,
    )
    if not r:
        return None, err
    try:
        data = r.json()
    except Exception as e:
        return None, str(e)[:200]
    tx = data.get("swapTransaction")
    if not tx:
        return None, str(data)[:200]
    return tx, ""


def warm_accounts_for_tx(tx_b64: str) -> int:
    """
    Force Surfpool to lazy-clone every account referenced by the swap tx
    (static keys + ALT-resolved addresses) from mainnet datasource.
    """
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0

    try:
        vtx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    except Exception:
        return 0

    keys: list[str] = []
    msg = vtx.message
    try:
        # MessageV0 / legacy both expose account_keys
        for k in msg.account_keys:
            keys.append(str(k))
    except Exception:
        pass

    # Resolve address lookup tables via mainnet RPC, then warm those accounts
    try:
        lookups = getattr(msg, "address_table_lookups", None) or []
        for lu in lookups:
            table_key = str(lu.account_key)
            keys.append(table_key)
            tr = rpc(SOLANA_RPC, "getAccountInfo", [table_key, {"encoding": "base64"}], timeout=15)
            # Also ask local to fetch the table (clone)
            try:
                rpc(LOCAL_RPC, "getAccountInfo", [table_key, {"encoding": "base64"}], timeout=15)
            except Exception:
                pass
            # Parse ALT addresses from mainnet account if possible via jsonParsed
            tr2 = rpc(SOLANA_RPC, "getAccountInfo", [table_key, {"encoding": "jsonParsed"}], timeout=15)
            val = (tr2.get("result") or {}).get("value")
            if val and isinstance(val.get("data"), dict):
                parsed = val["data"].get("parsed") or {}
                info = parsed.get("info") or {}
                for addr in info.get("addresses") or []:
                    keys.append(addr)
    except Exception:
        pass

    # Batch warm on local (triggers copy-on-read)
    uniq = list(dict.fromkeys(keys))
    warmed = 0
    # chunk getMultipleAccounts on local
    for i in range(0, len(uniq), 50):
        chunk = uniq[i : i + 50]
        try:
            rpc(LOCAL_RPC, "getMultipleAccounts", [chunk, {"encoding": "base64"}], timeout=30)
            warmed += len(chunk)
        except Exception:
            for a in chunk:
                try:
                    rpc(LOCAL_RPC, "getAccountInfo", [a, {"encoding": "base64"}], timeout=10)
                    warmed += 1
                except Exception:
                    pass
    return warmed


def simulate_local(tx_b64: str) -> dict:
    return rpc(
        LOCAL_RPC,
        "simulateTransaction",
        [
            tx_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            },
        ],
        timeout=45,
    )


def ensure_price(mint: str, decimals: int) -> tuple[int, int]:
    """
    One probe quote per mint for the whole run.
    Returns (probe_raw, out_lamports). Cached.
    """
    if mint in _price_cache:
        return _price_cache[mint]
    unit = 10 ** decimals
    probe = max(1, int(MIN_SELL_UI * unit))
    q = jupiter_quote_sell(mint, probe, slippage_bps=500, only_direct=False)
    if not q:
        q = jupiter_quote_sell(mint, probe, slippage_bps=800, only_direct=True)
    if not q or int(q.get("outAmount", 0)) <= 0:
        raise RuntimeError(f"price probe failed: {_last_jup_err or 'no route'}")
    _price_cache[mint] = (probe, int(q["outAmount"]))
    return _price_cache[mint]


def sell_raw_for_holder(raw_balance: int, decimals: int, mint: str) -> int:
    """Compute sell size from cached price — no extra Jupiter calls."""
    unit = 10 ** decimals
    min_raw = max(1, int(MIN_SELL_UI * unit))
    if raw_balance < min_raw:
        return 0
    probe, out_lamports = ensure_price(mint, decimals)
    sol_per_raw = out_lamports / probe
    max_by_sol = int((MAX_SELL_SOL * 1e9) / sol_per_raw) if sol_per_raw > 0 else min_raw
    pct_raw = max(min_raw, int(raw_balance * SELL_PCT))
    return max(min_raw, min(raw_balance, pct_raw, max_by_sol))


def sim_holder(h: dict, mint: str) -> dict:
    owner = h["owner"]
    decimals = h["decimals"]
    out: dict[str, Any] = {
        "owner": owner,
        "ui": h["ui_amount"],
        "sell_raw": 0,
        "on_curve": h["on_curve"],
        "status": "skip",
        "out_sol": None,
        "err": None,
        "note": "",
    }

    if SKIP_PDA and not h["on_curve"]:
        out["status"] = "skip_pda"
        return out

    try:
        sell_amount = sell_raw_for_holder(h["raw_amount"], decimals, mint)
    except RuntimeError as e:
        out["status"] = "no_route"
        out["err"] = str(e)
        out["note"] = str(e)
        return out

    out["sell_raw"] = sell_amount
    if sell_amount <= 0:
        out["status"] = "too_small"
        return out

    # Prefer direct routes (fewer programs → better fork fidelity)
    quote = jupiter_quote_sell(mint, sell_amount, slippage_bps=SLIPPAGE_BPS, only_direct=True)
    if not quote:
        quote = jupiter_quote_sell(mint, sell_amount, slippage_bps=SLIPPAGE_BPS, only_direct=False)
    if not quote:
        out["status"] = "no_route"
        out["err"] = _last_jup_err
        out["note"] = _last_jup_err or "quote failed"
        return out

    out["out_sol"] = int(quote["outAmount"]) / 1e9
    labels = [s.get("swapInfo", {}).get("label", "?") for s in (quote.get("routePlan") or [])]
    out["note"] = f"≤{MAX_SELL_SOL}SOL route={'>'.join(labels) or '?'}"

    fund_sol(owner)
    fund_tokens(owner, mint, sell_amount * 2 + 10 ** decimals)

    # Re-sync clock right before swap build/sim (Whirlpool oracle/timestamp)
    sync_surfpool_clock()

    tx_b64, swap_err = jupiter_swap(quote, owner)
    if not tx_b64:
        out["status"] = "swap_build_fail"
        out["err"] = swap_err
        return out

    n_warm = warm_accounts_for_tx(tx_b64)
    out["note"] = f"{out['note']}; warmed {n_warm}"

    # Clock again after warm (warm can take seconds)
    sync_surfpool_clock()
    sim = simulate_local(tx_b64)
    if "error" in sim:
        out["status"] = "rpc_error"
        out["err"] = sim["error"]
        return out

    val = (sim.get("result") or {}).get("value") or {}
    err = val.get("err")
    if err is None:
        out["status"] = "success"
        return out

    out["status"] = "sim_fail"
    out["err"] = err
    out["err_explain"] = _explain_err(err)
    out["logs"] = (val.get("logs") or [])[-5:]

    # Timestamp fail → hard re-sync + re-sim same tx once
    err_s = str(err)
    if "6022" in err_s or "0x1786" in err_s or "InvalidTimestamp" in err_s:
        sync_surfpool_clock()
        time.sleep(0.3)
        sync_surfpool_clock()
        sim_r = simulate_local(tx_b64)
        vr = (sim_r.get("result") or {}).get("value") or {}
        if not sim_r.get("error") and not vr.get("err"):
            out["status"] = "success"
            out["note"] = out["note"] + " (clock retry)"
            out["err"] = None
            out["logs"] = []
            return out

    # Smaller size retry (1 quote + 1 swap)
    smaller = max(1, sell_amount // 10)
    min_raw = max(1, int(MIN_SELL_UI * 10 ** decimals))
    if smaller >= min_raw and smaller < sell_amount:
        q3 = jupiter_quote_sell(mint, smaller, slippage_bps=SLIPPAGE_BPS, only_direct=True)
        if not q3:
            q3 = jupiter_quote_sell(mint, smaller, slippage_bps=SLIPPAGE_BPS, only_direct=False)
        if q3:
            fund_tokens(owner, mint, smaller * 3)
            sync_surfpool_clock()
            tx3, _ = jupiter_swap(q3, owner)
            if tx3:
                warm_accounts_for_tx(tx3)
                sync_surfpool_clock()
                sim3 = simulate_local(tx3)
                v3 = (sim3.get("result") or {}).get("value") or {}
                if not sim3.get("error") and not v3.get("err"):
                    out["status"] = "success"
                    out["sell_raw"] = smaller
                    out["out_sol"] = int(q3["outAmount"]) / 1e9
                    out["note"] = out["note"] + " (retry /10)"
                    out["err"] = None
                    out["logs"] = []
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    _rpc_show = SOLANA_RPC.split("api-key=")[0] + ("api-key=***" if "api-key=" in SOLANA_RPC else "")
    print("=" * 60)
    print("TOP Holders + optional Surfpool sell sim")
    print(f"Mint:  {OUTPUT_MINT}")
    print(f"Read:  {_rpc_show[:56]}")
    print(f"Local: {LOCAL_RPC}  skip={SKIP_LOCAL}")
    print(f"Sell:  up to {SELL_PCT*100:.1f}% bal, max ~{MAX_SELL_SOL} SOL out")
    print("=" * 60)

    holders = fetch_holders(OUTPUT_MINT, TOP_N)
    print(f"\nParsed {len(holders)} holders")
    if not holders:
        print("No holders found.")
        sys.exit(1)

    print(f"\nRank | Owner | Balance")
    print("-" * 50)
    for i, h in enumerate(holders, 1):
        tag = "" if h["on_curve"] else " PDA"
        print(f"{i:3} | {h['owner'][:10]}... | {h['ui_amount']:>14,.4f}{tag}")

    if SKIP_LOCAL:
        print("\nSKIP_LOCAL_SIM set — done.")
        print(f"Time: {time.time() - t0:.2f}s")
        return

    if not local_ok():
        print(f"\nLocal RPC not healthy at {LOCAL_RPC}")
        print("Start Surfpool or set SKIP_LOCAL_SIM=1")
        sys.exit(2)

    sim_n = min(SIM_TOP, len(holders))
    print(f"\nSimulating capped sells on Surfpool for top {sim_n}...")
    print(f"(Jupiter interval {_JUP_MIN_INTERVAL}s; slippage={SLIPPAGE_BPS}bps; skip_pda={SKIP_PDA})")

    clk = sync_surfpool_clock()
    print(f"Surfpool clock sync: {clk}")

    # One price probe for the whole mint (avoids 429)
    try:
        pr, po = ensure_price(OUTPUT_MINT, holders[0]["decimals"])
        print(f"Price probe: {pr / (10 ** holders[0]['decimals']):.2f} tok -> {po / 1e9:.6f} SOL")
    except RuntimeError as e:
        print(f"Price probe failed: {e}")
        print("Wait ~1 min for Jupiter rate limit, or set JUPITER_API_KEY / increase JUPITER_MIN_INTERVAL")
        sys.exit(3)

    results = []
    for i, h in enumerate(holders[:sim_n], 1):
        print(f"\n{i}. {h['owner'][:10]}... | bal={h['ui_amount']:,.4f}", end="", flush=True)
        r = sim_holder(h, OUTPUT_MINT)
        results.append(r)
        sell_ui = r["sell_raw"] / (10 ** h["decimals"]) if r["sell_raw"] else 0

        if r["status"] == "skip_pda":
            print(" | skip PDA")
        elif r["status"] == "too_small":
            print(" | too small")
        elif r["status"] == "no_route":
            print(f" | no Jupiter route ({r.get('note') or r.get('err') or ''})")
        elif r["status"] == "swap_build_fail":
            print(f" | swap build fail (quote {r.get('out_sol') or 0:.4f} SOL)")
            if r.get("err"):
                print(f"    {str(r['err'])[:120]}")
        elif r["status"] == "success":
            print(f" | sell {sell_ui:,.2f} tok -> ~{r['out_sol']:.4f} SOL | SUCCESS [{r.get('note','')}]")
        elif r["status"] == "sim_fail":
            print(f" | sell {sell_ui:,.2f} tok ~{r.get('out_sol') or 0:.4f} SOL | FAIL [{r.get('note','')}]")
            print(f"    {_explain_err(r.get('err'))}")
            for lg in r.get("logs") or []:
                print(f"    {lg[:110]}")
        else:
            print(f" | {r['status']}: {r.get('err')}")

    ok = sum(1 for r in results if r["status"] == "success")
    fail = sum(1 for r in results if r["status"] == "sim_fail")
    skip = sum(1 for r in results if r["status"] in ("skip_pda", "too_small", "skip"))
    print("\n" + "=" * 60)
    print(f"Sim summary: success={ok} fail={fail} skipped={skip} other={len(results) - ok - fail - skip}")
    # Classify fails
    for label, pred in [
        ("timestamp/clock", lambda e: e and ("6022" in str(e) or "0x1786" in str(e))),
        ("meteora-fork", lambda e: e and ("3007" in str(e) or "0xbbf" in str(e))),
        ("slippage", lambda e: e and ("6001" in str(e) or "0x1771" in str(e))),
        ("pda-fee", lambda e: e and "InvalidAccountForFee" in str(e)),
    ]:
        n = sum(1 for r in results if r["status"] == "sim_fail" and pred(r.get("err")))
        if n:
            print(f"  fail/{label}: {n}")
    print("Notes:")
    print("  - success = holder can sell capped size on this fork (not full bag dump)")
    print("  - meteora-fork/timestamp fails = Surfpool limits, not automatic honeypot")
    print("  - PDA skipped by default (cannot pay fees)")
    print(f"Time: {time.time() - t0:.2f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
