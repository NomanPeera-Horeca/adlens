"""Parse Meta signed_request payloads for compliance callbacks."""
import base64
import hashlib
import hmac
import json


def parse_signed_request(signed_request: str, app_secret: str) -> dict | None:
    if not signed_request or not app_secret:
        return None
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = base64.urlsafe_b64decode(encoded_sig + "=" * (-len(encoded_sig) % 4))
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expected = hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return data
    except Exception:
        return None
