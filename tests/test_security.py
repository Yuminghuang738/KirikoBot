"""Security-relevant behaviour: webhook signatures and output escaping."""
from __future__ import annotations

import logging
import os

import pytest

from conftest import APP_DIR
from webhook_auth import expected_signature, signature_ok

PROJECT_DIR = os.path.dirname(APP_DIR)


class TestAuthExemptions:
    """The healthcheck must not sit behind the dashboard password.

    Enabling dashboard auth made `curl -f /` return 401, which marked the
    container unhealthy — the compose healthcheck and the auth exemption have
    to stay in sync.
    """

    def test_healthz_is_exempt(self):
        import dashboard_auth

        assert "healthz" in dashboard_auth._WEBHOOK_ENDPOINTS

    def test_webhook_is_exempt(self):
        import dashboard_auth

        assert "receive" in dashboard_auth._WEBHOOK_ENDPOINTS

    def test_main_exposes_the_health_route(self):
        src = open(os.path.join(APP_DIR, "main.py"), encoding="utf-8").read()
        assert '@app.route("/healthz")' in src

    def test_compose_healthcheck_hits_the_exempt_endpoint(self):
        compose = open(os.path.join(PROJECT_DIR, "docker-compose.yml"), encoding="utf-8").read()
        assert "/healthz" in compose
        assert "http://localhost:5000/healthz" in compose


class TestWebhookSignature:
    BODY = b'{"post_type":"message","user_id":123}'

    def test_accepts_valid_signature(self):
        sig = expected_signature("s3cret", self.BODY)
        assert sig.startswith("sha1=")
        assert signature_ok("s3cret", self.BODY, sig)

    def test_rejects_tampered_body(self):
        sig = expected_signature("s3cret", self.BODY)
        assert not signature_ok("s3cret", self.BODY + b" ", sig)

    def test_rejects_wrong_token(self):
        sig = expected_signature("s3cret", self.BODY)
        assert not signature_ok("other", self.BODY, sig)

    def test_rejects_missing_signature(self):
        assert not signature_ok("s3cret", self.BODY, None)
        assert not signature_ok("s3cret", self.BODY, "")

    def test_open_when_no_token_configured(self):
        # Backwards compatibility: an unconfigured token must not take a
        # running bot offline (main logs a loud warning instead).
        assert signature_ok(None, self.BODY, None)
        assert signature_ok("", self.BODY, "garbage")

    def test_signature_is_over_raw_bytes(self):
        pretty = b'{"a": 1}'
        compact = b'{"a":1}'
        assert expected_signature("k", pretty) != expected_signature("k", compact)


class TestLogEscaping:
    """Log messages carry QQ nicknames and message bodies into the dashboard."""

    @pytest.mark.parametrize("level,name", [
        (logging.INFO, "robot_server"),
        (logging.WARNING, "main"),
        (logging.ERROR, "main"),
        (logging.INFO, "think"),
    ])
    def test_display_escapes_html(self, level, name):
        from log_stream import SSELogHandler

        handler = SSELogHandler()
        record = logging.LogRecord(
            name, level, __file__, 1,
            "user=<img src=x onerror=alert(1)>", None, None,
        )
        handler.emit(record)
        entry = handler.read()[0]

        assert "<img" not in entry["display"]
        assert "onerror=alert(1)>" not in entry["display"]
        assert "&lt;img" in entry["display"]
        # The raw message is still available for non-HTML consumers.
        assert "<img" in entry["msg"]
