"""Reads dotenvx files without running dotenvx.

`parse` is a port of dotenv's parser (motdotla/dotenv lib/main.js): the same LINE
expression, quote stripping and \\n handling, and nothing else. No variable
expansion and no command substitution, so a value is exactly the text that was
encrypted. `decrypt` opens one dotenvx `encrypted:` value (eciesjs: secp256k1 ECDH,
HKDF-SHA256 over the uncompressed ephemeral and shared points, AES-256-GCM with a
16-byte nonce).
"""
import base64
import re

LINE = re.compile(r"^\s*(?:export\s+)?([\w.-]+)(?:\s*=\s*?|:\s+?)(\s*'(?:\\'|[^'])*'|\s*\"(?:\\\"|[^\"])*\"|\s*`(?:\\`|[^`])*`|[^#\r\n]+)?\s*(?:#.*)?$", re.M)
QUOTED = re.compile(r"^(['\"`])([\s\S]*)\1$")
SECP256K1_P = 2**256 - 2**32 - 977


def parse(text):
    """[(name, value)] in file order, as dotenv would load them."""
    pairs = []
    for name, raw in LINE.findall(re.sub(r"\r\n?", "\n", text)):
        value = raw.strip()
        quote = value[:1]
        value = QUOTED.sub(r"\2", value)
        if quote == '"':
            value = value.replace("\\n", "\n").replace("\\r", "\r")
        pairs.append((name, value))
    return pairs


def decrypt(private_key_hex, value):
    """Plaintext of one `encrypted:` value. Raises if the key doesn't open it."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not value.startswith("encrypted:"):
        raise ValueError("not a dotenvx ciphertext")
    data = base64.b64decode(value[len("encrypted:"):], validate=True)
    if len(data) < 65 + 16 + 16:
        raise ValueError("ciphertext too short")
    sender, nonce, tag, body = data[:65], data[65:81], data[81:97], data[97:]
    curve = ec.SECP256K1()
    private = ec.derive_private_key(int(private_key_hex, 16), curve)
    x = int.from_bytes(private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(curve, sender)), "big")
    # ECDH yields only x; of the two points sharing it, AES-GCM authenticates exactly one.
    y = pow((x ** 3 + 7) % SECP256K1_P, (SECP256K1_P + 1) // 4, SECP256K1_P)
    for candidate in (y, SECP256K1_P - y):
        shared = b"\x04" + x.to_bytes(32, "big") + candidate.to_bytes(32, "big")
        key = HKDF(SHA256(), 32, None, None).derive(sender + shared)
        try:
            return AESGCM(key).decrypt(nonce, body + tag, None).decode()
        except Exception:
            continue
    raise ValueError("the key does not decrypt this value")
