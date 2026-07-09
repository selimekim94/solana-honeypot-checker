"""
64-byte private key array (Solflare / Solana id.json) -> base58 string
for PRIVATE_KEY_B58 env used by solana_honeypot.py
"""
from solders.keypair import Keypair

# Solflare "export private key" as byte array, OR contents of id.json:
#   [12, 34, ..., 64 numbers]
private_key = [
    
]

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return ("1" * pad) + (out or "1")


if not private_key or len(private_key) != 64:
    print("Paste exactly 64 integers (0-255) into private_key = [...]")
    raise SystemExit(1)

kp = Keypair.from_bytes(bytes(private_key))
# Full 64-byte keypair encoding (what Phantom/Solflare base58 export uses)
secret_b58 = b58encode(bytes(kp))

print("pubkey:         ", kp.pubkey())
print("PRIVATE_KEY_B58=", secret_b58)
print()
print("PowerShell:")
print(f'  $env:PRIVATE_KEY_B58="{secret_b58}"')
