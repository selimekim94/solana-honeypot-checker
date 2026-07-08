import requests
import base64
import time
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import Message, MessageV0
from solana.rpc.api import Client


# Paste your Helius API key here (get at https://dev.helius.xyz)
HELIUS_API_KEY = "YOUR_HELIUS_API_KEY"

if HELIUS_API_KEY == "YOUR_HELIUS_API_KEY" or not HELIUS_API_KEY:
    print("Error: Set your HELIUS_API_KEY. Get one at https://dev.helius.xyz")
    exit()

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
LOCAL_RPC = "http://localhost:8899"
OUTPUT_MINT = "6AJcP7wuLwmRYLBNbi825wgguaPsWzPBEHcHndpRpump"
SOL_MINT = "So11111111111111111111111111111111111111112"
print("TOP20 Holders Analysis + Sell Simulation")
# 1. Fetch top 20 token accounts via Helius HTTP (avoid solders client timeout)
print("Fetching holders from Helius...")
sess = requests.Session()

largest_resp = sess.post(HELIUS_RPC, json={
    "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
    "params": [OUTPUT_MINT]
}, timeout=30).json()
top_accounts = largest_resp["result"]["value"][:20]

# Batch resolve: token account -> owner
ta_addresses = [acc["address"] for acc in top_accounts]
batch_resp = sess.post(HELIUS_RPC, json={
    "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
    "params": [ta_addresses, {"encoding": "base64"}]
}, timeout=30).json()
batch_infos = batch_resp["result"]["value"]

holders = []
for i, (acc, info) in enumerate(zip(top_accounts, batch_infos), 1):
    try:
        if info and info.get("data"):
            data_b64 = info["data"][0] if isinstance(info["data"], list) else info["data"]
            raw = base64.b64decode(data_b64)
            if len(raw) >= 64:
                owner = Pubkey.from_bytes(raw[32:64])
                if owner.is_on_curve():
                    raw_amount = int(acc["amount"])
                    decimals = acc["decimals"]
                    ui_amount = raw_amount / (10**decimals)
                    holders.append({
                        "owner": str(owner),
                        "ui_amount": ui_amount,
                        "decimals": decimals,
                    })
                    print(f"{str(owner)[:8]}... | {ui_amount:,.0f}")
                else:
                    print(f"Skip PDA/Program: {str(owner)[:8]}...")
        else:
            print(f"Empty data for #{i}")
    except Exception as e:
        print(f"Skip #{i}: {e}")

print(f"\n{len(holders)}/20 Holders Parsed!")
if len(holders) < 5:
    print("Too few holders. Check your key.")
    exit()

print("\nTOP20 + 1% Sell Estimate:")
print("Rank | Owner | Token | ~SOL")
print("-" * 45)
total_est_sol = 0
for i, h in enumerate(holders, 1):
    est = (h["ui_amount"] * 0.01) * 0.02
    total_est_sol += est
    print(f"{i:3} | {h['owner'][:8]}... | {h['ui_amount']:>10,.0f} | {est:>6.2f}")
print(f"TOTAL 1%: ~${total_est_sol:,.0f}")

# 2. Local SurfPool: airdrop + sell simulation (top 10 holders)
local_client = Client(LOCAL_RPC)
print(f"\nSimulating 1% sell via SurfPool for top {min(10, len(holders))} holders...")

for i, h in enumerate(holders[:10], 1):
    owner_str = h["owner"]
    ui_amount = h["ui_amount"]
    decimals = h["decimals"]
    sell_amount = int(ui_amount * 0.01 * (10**decimals))

    print(f"\n{i}. {owner_str[:8]}... | {ui_amount:,.0f} Token", end="")

    if sell_amount <= 1_000_000:
        print(" | <1 Token (too small)")
        continue

    # Airdrop 10 SOL on SurfPool
    try:
        local_client.request_airdrop(Pubkey.from_string(owner_str), 10_000_000_000)
    except Exception:
        pass

    try:
        # Quote
        q = sess.get("https://api.jup.ag/swap/v1/quote", params={
            "inputMint": OUTPUT_MINT, "outputMint": SOL_MINT,
            "amount": sell_amount, "slippageBps": 50, "asLegacyTransaction": "true"
        }, timeout=10).json()

        if "error" in q:
            print(f" | No route ({q['error']})")
            continue
        if "outAmount" not in q:
            print(" | No route (no liquidity)")
            continue

        out_sol = int(q["outAmount"]) / 1e9
        print(f" | {sell_amount / 1e9:.2f} Token -> {out_sol:.4f} SOL", end="")

        # Swap (retry once on failure)
        sw = sess.post("https://api.jup.ag/swap/v1/swap", json={
            "quoteResponse": q, "userPublicKey": owner_str,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto", "asLegacyTransaction": True
        }, timeout=10).json()

        if "swapTransaction" not in sw:
            time.sleep(1)
            sw = sess.post("https://api.jup.ag/swap/v1/swap", json={
                "quoteResponse": q, "userPublicKey": owner_str,
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto", "asLegacyTransaction": True
            }, timeout=10).json()

        if "swapTransaction" not in sw:
            print(" | Swap fail")
            continue

        tx_b64 = sw["swapTransaction"]

        # Decode, replace blockhash, simulate on SurfPool
        vtx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        latest_bh = local_client.get_latest_blockhash()
        local_blockhash = latest_bh.value.blockhash

        msg = vtx.message
        msg_bytes = bytes(msg)
        orig_bh_bytes = bytes(msg.recent_blockhash)
        bh_offset = msg_bytes.find(orig_bh_bytes)
        new_msg_bytes = msg_bytes[:bh_offset] + bytes(local_blockhash) + msg_bytes[bh_offset+32:]

        if hasattr(msg, "address_table_lookups"):
            new_msg = MessageV0.from_bytes(new_msg_bytes)
        else:
            new_msg = Message.from_bytes(new_msg_bytes)

        new_vtx = VersionedTransaction.populate(new_msg, vtx.signatures)
        tx_bytes = bytes(new_vtx)
        tx_b64_local = base64.b64encode(tx_bytes).decode("utf-8")

        sim = sess.post(LOCAL_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [tx_b64_local, {"encoding": "base64", "sigVerify": False}]
        }, timeout=5).json()

        if "result" in sim:
            val = sim["result"]["value"]
            err = val.get("err")
            if err is None:
                print(" | SUCCESS")
            else:
                print(f" | {err}")
                for log in val.get("logs", [])[:3]:
                    print(f"    {log[:80]}...")
        else:
            print(f" | RPC error: {sim.get('error', 'Unknown')}")

    except Exception as e:
        print(f" | Error: {str(e)[:80]}")

    time.sleep(0.5)

print("\nDone.")
