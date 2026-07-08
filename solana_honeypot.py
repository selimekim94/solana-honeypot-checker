import base64

import requests

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey

# --- CONFIGURATION ---
HELIUS_API_KEY = "YOUR_HELIUS_API_KEY"
if HELIUS_API_KEY == "YOUR_HELIUS_API_KEY" or not HELIUS_API_KEY:
    print("Error: Set your HELIUS_API_KEY. Get one at https://dev.helius.xyz")
    exit()

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# --- TRANSACTION DETAILS ---
BUY_AMOUNT_LAMPORTS = 1000000
INPUT_MINT = 'So11111111111111111111111111111111111111112'
OUTPUT_MINT = '6AJcP7wuLwmRYLBNbi825wgguaPsWzPBEHcHndpRpump'
SLIPPAGE_BPS = 500

# Private key (64 integers, 0-255). Export from Solflare via Settings > Export Private Key
private_key = []

if not private_key or len(private_key) != 64:
    print("Error: private_key must be a list of 64 integers. Export from Solflare: Settings > Export Private Key")
    exit()
keypair = Keypair.from_bytes(bytearray(private_key))
user_public_key = str(keypair.pubkey())
user_pubkey = Pubkey.from_string(user_public_key)
print(f"Wallet: {user_public_key} — ensure at least 0.1 SOL balance for simulation")

JUPITER_QUOTE_API = 'https://api.jup.ag/swap/v1/quote'
JUPITER_SWAP_API = 'https://api.jup.ag/swap/v1/swap'
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def rpc_call(method, params):
    resp = requests.post(HELIUS_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    return resp.json()


# --- MINT / FREEZE AUTHORITY CHECK ---
def check_authorities(mint_str):
    print("\n--- Mint Authority / Freeze Authority Check ---")
    result = rpc_call("getAccountInfo", [mint_str, {"encoding": "jsonParsed"}])
    if "result" not in result or result["result"]["value"] is None:
        print(f"getAccountInfo failed: {result.get('error', 'unknown')}")
        return None, None
    mint_parsed = result["result"]["value"]["data"]["parsed"]["info"]
    mint_auth = mint_parsed.get("mintAuthority")
    freeze_auth = mint_parsed.get("freezeAuthority")

    if mint_auth:
        print(f"⚠️  Mint Authority: {mint_auth}")
        if mint_auth == user_public_key:
            print("    -> It's YOU! (renounced or self-owned)")
        else:
            print("    -> Creator can mint unlimited tokens (RED FLAG)")
    else:
        print("Mint Authority: None (no one can mint new tokens)")

    if freeze_auth:
        print(f"⚠️  Freeze Authority: {freeze_auth}")
        if freeze_auth == user_public_key:
            print("    -> It's YOU!")
        else:
            print("    -> Creator can freeze wallets (RED FLAG)")
    else:
        print("Freeze Authority: None (no one can freeze wallets)")
    return mint_auth, freeze_auth


# --- JUPITER QUOTE + SWAP ---
def get_jupiter_swap_transaction(input_mint, output_mint, amount, user_public_key, slippage):
    params = {'inputMint': input_mint, 'outputMint': output_mint, 'amount': amount, 'slippageBps': slippage,
              'asLegacyTransaction': 'true'}
    try:
        quote_response = requests.get(JUPITER_QUOTE_API, params=params)
        quote_response.raise_for_status()
        quote_data = quote_response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to get quote from Jupiter: {e}")
        if e.response is not None: print(f"Response: {e.response.text}")
        return None, None

    payload = {"quoteResponse": quote_data, "userPublicKey": user_public_key, "wrapAndUnwrapSol": True,
               "dynamicComputeUnitLimit": True,
               "prioritizationFeeLamports": "auto",
               "asLegacyTransaction": True}
    try:
        swap_response = requests.post(JUPITER_SWAP_API, json=payload)
        swap_response.raise_for_status()
        swap_data = swap_response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to create swap transaction from Jupiter: {e}")
        if e.response is not None: print(f"Response: {e.response.text}")
        return None, None

    return swap_data.get("swapTransaction"), quote_data


def sign_tx(tx_b64, private_key_list):
    tx_bytes = base64.b64decode(tx_b64)
    raw_tx = VersionedTransaction.from_bytes(tx_bytes)
    keypair = Keypair.from_bytes(bytearray(private_key_list))
    signature = keypair.sign_message(to_bytes_versioned(raw_tx.message))
    signed_tx = VersionedTransaction.populate(raw_tx.message, [signature])
    signed_tx_bytes = bytes(signed_tx)
    signed_tx_b64 = base64.b64encode(signed_tx_bytes).decode('utf-8')
    return signed_tx_b64


# --- MAIN ---
def main():
    print("=" * 60)
    print("Buy & Sell Simulation (Separate Blocks + Authorities)")
    print(f"Wallet: {user_public_key}")
    print(f"Trade: {BUY_AMOUNT_LAMPORTS / 1e9} SOL -> {OUTPUT_MINT}")
    print("=" * 60)

    # 1) Mint / Freeze Authority
    mint_auth, freeze_auth = check_authorities(OUTPUT_MINT)

    # 2) Get Jupiter txns
    print("\n[1/2] Fetching swap transactions from Jupiter...")
    buy_tx_b64, buy_quote = get_jupiter_swap_transaction(INPUT_MINT, OUTPUT_MINT, BUY_AMOUNT_LAMPORTS, user_public_key, SLIPPAGE_BPS)
    if not buy_tx_b64 or not buy_quote: return
    estimated_out_amount = int(buy_quote.get('outAmount', '0'))
    print(f"Estimated buy amount: {estimated_out_amount / 1e6} {OUTPUT_MINT[:10]}...")
    if estimated_out_amount == 0:
        print("Buy results in 0 tokens, stopping.")
        return

    sell_tx_b64, sell_quote = get_jupiter_swap_transaction(OUTPUT_MINT, INPUT_MINT, estimated_out_amount, user_public_key, SLIPPAGE_BPS)
    if not sell_tx_b64: return
    expected_sell_out = int(sell_quote.get('outAmount', '0'))
    print("Buy and sell transactions created successfully.")

    signed_buy_tx_b64 = sign_tx(buy_tx_b64, private_key)
    signed_sell_tx_b64 = sign_tx(sell_tx_b64, private_key)

    print("\n[2/2] Simulating BUNDLE (buy+sell same block)...")
    # --- Bundle ---
    bundle_buy_actual = None
    bundle_sell_actual = 0
    bundle = rpc_call("simulateBundle", [{
        "encodedTransactions": [signed_buy_tx_b64, signed_sell_tx_b64],
        "replaceRecentBlockhash": True
    }])
    bundle_err = bundle.get("result", {}).get("value", {}).get("err")
    tx_results = bundle.get("result", {}).get("value", {}).get("transactionResults", [])
    if bundle_err:
        print(f"Bundle simulation FAILED: {bundle_err}")
    elif len(tx_results) >= 2:
        for ptb in (tx_results[0].get("postTokenBalances") or []):
            if ptb.get("mint") == OUTPUT_MINT and ptb.get("owner") == user_public_key:
                bundle_buy_actual = int(ptb["uiTokenAmount"]["amount"])
                break
        sr = tx_results[1]
        pre = (sr.get("preBalances") or [0])[0]
        post = (sr.get("postBalances") or [0])[0]
        fee = sr.get("fee") or 0
        bundle_sell_actual = post - pre + fee

        print(f"Bundle Buy Expected:  {estimated_out_amount}")
        if bundle_buy_actual:
            bbt = round((estimated_out_amount - bundle_buy_actual) / estimated_out_amount * 100 * 10) / 10
            print(f"Bundle Buy Actual:    {bundle_buy_actual}  Tax: {bbt}%")
        print(f"Bundle Sell Expected: {expected_sell_out}")
        print(f"Bundle Sell Actual:   {bundle_sell_actual}  Tax: {round((expected_sell_out - bundle_sell_actual) / expected_sell_out * 100 * 10) / 10}%")

    # Summary
    bundle_buy_tax = round((estimated_out_amount - bundle_buy_actual) / estimated_out_amount * 100 * 10) / 10 if bundle_buy_actual else None
    bundle_sell_tax = round((expected_sell_out - bundle_sell_actual) / expected_sell_out * 100 * 10) / 10

    print("\n" + "=" * 25 + " SUMMARY " + "=" * 26)
    print(f"Token:                {OUTPUT_MINT}")
    print(f"Mint Authority:       {'None' if not mint_auth else f'YES ({mint_auth})'}")
    print(f"Freeze Authority:     {'None' if not freeze_auth else f'YES ({freeze_auth})'}")
    print(f"Buy Tax:              {bundle_buy_tax if bundle_buy_tax is not None else 'N/A'}%")
    print(f"Sell Tax:             {bundle_sell_tax}%")
    print("=" * 60)


if __name__ == "__main__":
    main()
