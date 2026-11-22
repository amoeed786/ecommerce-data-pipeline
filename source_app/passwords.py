"""Password hashing with scrypt (Python standard library - no extra dependency).

Stored format:  scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>
A random salt per password means two users with the same password get different hashes, and the
cost parameters are stored with the hash so they can be raised later without breaking old logins.
"""
import hashlib
import hmac
import secrets

N, R, P, KEY_LEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=KEY_LEN)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
        return hmac.compare_digest(digest.hex(), hash_hex)  # constant time: no timing leaks
    except (ValueError, TypeError):
        return False
