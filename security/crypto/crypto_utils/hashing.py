import hashlib, hmac
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
def hmac_sha256_hex(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()
