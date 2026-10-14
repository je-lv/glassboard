"""Easier TLS onboarding. Stdlib only: used by the CLI and the admin tool.

The board's root certificate is public, so the board serves it at `GET /ca.crt`. A new participant
runs `glassboard login --fetch-ca --ca-fingerprint SHA256:…`:
  1. download `/ca.crt` WITHOUT verifying the connection. This is bootstrap only: nothing sent, no
     token, and the bytes are not trusted yet;
  2. accept them only if their SHA-256 fingerprint equals the one the admin gave out
     (`glassboard-admin setup-tls` prints it); otherwise stop;
  3. from then on, every connection is verified against that pinned root, as with `--ca-file`.
A man in the middle can answer step 1, but cannot produce a certificate with the admin's
fingerprint. This is the only unverified connection in the code base (tests/test_easy_setup.py checks the source).
"""
from __future__ import annotations

import hashlib
import re
import ssl
import urllib.parse
import urllib.request

MAX_CA_BYTES = 16 * 1024
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*")


class TlsBootError(RuntimeError):
    pass


def check_ca_pem(pem: str) -> str:
    """Exactly one PEM certificate and nothing else (never a private key)."""
    if "PRIVATE KEY" in pem:
        raise TlsBootError("this file contains a private key; give the CA certificate (root.crt) only")
    if not _PEM.fullmatch(pem.strip() + "\n"):
        raise TlsBootError("not a single PEM certificate")
    return pem.strip() + "\n"


def fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(check_ca_pem(pem))
    h = hashlib.sha256(der).hexdigest().upper()
    return "SHA256:" + ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def normalize(fp: str) -> str:
    hexes = re.sub(r"[^0-9A-Fa-f]", "", re.sub(r"(?i)^\s*sha-?256:?", "", fp)).upper()
    if len(hexes) != 64:
        raise TlsBootError("a fingerprint is SHA256: followed by 64 hex digits (colons optional)")
    return "SHA256:" + ":".join(hexes[i:i + 2] for i in range(0, 64, 2))


def fetch_ca(base_url: str, timeout: float = 15) -> str:
    """Step 1 above. Only https URLs; only the fixed path /ca.crt; size-capped."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise TlsBootError("--fetch-ca needs an https:// board URL")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE        # bootstrap only: the result is pinned by fingerprint (see above)
    req = urllib.request.Request(base_url.rstrip("/") + "/ca.crt", headers={"Accept": "application/x-pem-file"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:  # nosec B310 - https required above
            raw = r.read(MAX_CA_BYTES + 1)
    except Exception as e:                  # noqa: BLE001 - reported as one line, no secrets involved
        raise TlsBootError(f"could not download the board CA: {type(e).__name__}") from None
    if len(raw) > MAX_CA_BYTES:
        raise TlsBootError("the board CA is larger than 16 KiB")
    return check_ca_pem(raw.decode("ascii", "replace"))
