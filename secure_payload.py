import base64
import hashlib
import hmac
import json
import os
import time

from Crypto.Cipher import AES


PROTOCOL_NAME = "riot-login-v1"
REQUEST_AUTH_NAME = "lan-agent-request-v1"
MAX_CLOCK_SKEW_SECONDS = 90
PBKDF2_ITERATIONS = 200000


class SecurePayloadError(ValueError):
    pass


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value, field_name):
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise SecurePayloadError(f"invalid_{field_name}") from exc


def _derive_keys(token, salt):
    if not token:
        raise SecurePayloadError("missing_token")
    material = hashlib.pbkdf2_hmac(
        "sha256",
        token.encode("utf-8"),
        salt + PROTOCOL_NAME.encode("ascii"),
        PBKDF2_ITERATIONS,
        dklen=64,
    )
    return material[:32], material[32:]


def _request_authentication_key(token):
    if not token:
        raise SecurePayloadError("missing_token")
    return hashlib.sha256(REQUEST_AUTH_NAME.encode("ascii") + b":" + token.encode("utf-8")).digest()


def create_request_authentication(token, body):
    issued_at = str(int(time.time()))
    request_nonce = _encode(os.urandom(16))
    signature = hmac.new(
        _request_authentication_key(token),
        issued_at.encode("ascii") + b"." + request_nonce.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return issued_at, request_nonce, signature


def verify_request_authentication(token, body, issued_at, request_nonce, signature, now=None):
    try:
        timestamp = int(issued_at)
    except (TypeError, ValueError) as exc:
        raise SecurePayloadError("invalid_timestamp") from exc
    current_time = int(time.time() if now is None else now)
    if abs(current_time - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise SecurePayloadError("request_expired")
    nonce = _decode(request_nonce, "request_nonce")
    if len(nonce) != 16:
        raise SecurePayloadError("invalid_request_nonce")
    expected_signature = hmac.new(
        _request_authentication_key(token),
        issued_at.encode("ascii") + b"." + request_nonce.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature or ""):
        raise SecurePayloadError("invalid_signature")


def create_riot_login_request(token, username, password):
    issued_at = str(int(time.time()))
    salt = os.urandom(16)
    nonce = os.urandom(12)
    encryption_key, authentication_key = _derive_keys(token, salt)
    plaintext = json.dumps(
        {"username": username, "password": password},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    cipher = AES.new(encryption_key, AES.MODE_GCM, nonce=nonce)
    cipher.update((PROTOCOL_NAME + ":" + issued_at).encode("ascii"))
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    payload = {
        "script": "riot_login",
        "secure": {
            "protocol": PROTOCOL_NAME,
            "salt": _encode(salt),
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
            "tag": _encode(tag),
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        authentication_key,
        issued_at.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return body, issued_at, signature


def decrypt_riot_login_request(token, body, issued_at, signature, now=None):
    try:
        timestamp = int(issued_at)
    except (TypeError, ValueError) as exc:
        raise SecurePayloadError("invalid_timestamp") from exc
    current_time = int(time.time() if now is None else now)
    if abs(current_time - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise SecurePayloadError("request_expired")

    try:
        payload = json.loads(body.decode("utf-8"))
        secure = payload["secure"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SecurePayloadError("invalid_secure_payload") from exc
    if payload.get("script") != "riot_login" or secure.get("protocol") != PROTOCOL_NAME:
        raise SecurePayloadError("invalid_protocol")

    salt = _decode(secure.get("salt"), "salt")
    nonce = _decode(secure.get("nonce"), "nonce")
    ciphertext = _decode(secure.get("ciphertext"), "ciphertext")
    tag = _decode(secure.get("tag"), "tag")
    if len(salt) != 16 or len(nonce) != 12 or len(tag) != 16:
        raise SecurePayloadError("invalid_secure_payload")

    encryption_key, authentication_key = _derive_keys(token, salt)
    expected_signature = hmac.new(
        authentication_key,
        issued_at.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature or ""):
        raise SecurePayloadError("invalid_signature")

    try:
        cipher = AES.new(encryption_key, AES.MODE_GCM, nonce=nonce)
        cipher.update((PROTOCOL_NAME + ":" + issued_at).encode("ascii"))
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        credentials = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurePayloadError("decrypt_failed") from exc

    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise SecurePayloadError("invalid_credentials")
    return username, password
