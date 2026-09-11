"""Signature verification for LLBot's OneBot http-post webhook.

LLBot's ``OB11HttpPost`` does **not** send an ``Authorization`` header. When a
token is configured on its http-post connection it signs the raw JSON body and
sends the digest as:

    x-signature: sha1=<HMAC-SHA1(raw_body, token) hex>

(see ``OB11HttpPost.emitEvent`` in llbot.js). Verification therefore has to run
against the exact bytes received — never against a re-serialised copy.

Kept dependency-free so it can be unit tested without importing ``main``
(importing ``main`` starts the scheduler and other services).
"""
from __future__ import annotations

import hashlib
import hmac


def expected_signature(token: str, raw_body: bytes) -> str:
    """The `x-signature` value LLBot would send for this body."""
    digest = hmac.new(token.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    return f"sha1={digest}"


def signature_ok(token: str | None, raw_body: bytes, supplied: str | None) -> bool:
    """True when ``supplied`` is a valid signature for ``raw_body``.

    When no token is configured the webhook is treated as open (the caller is
    expected to warn loudly about this at startup) so that upgrading cannot
    silently take a running bot offline.
    """
    if not token:
        return True
    if not supplied:
        return False
    return hmac.compare_digest(supplied.strip(), expected_signature(token, raw_body))
