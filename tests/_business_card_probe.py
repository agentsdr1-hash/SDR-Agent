"""
Standalone probe (run as its own interpreter process, not imported by
pytest) for app/services/business_card.py, same reasoning as
_ai_provider_probe.py's docstring: app.db.DB_PATH is fixed at import
time, and a fresh process keeps this isolated from pytest's own.

ai_provider._client() is patched to return a small fake object shaped
like the bits of the SDK this app actually calls, so this exercises the
real request-building/response-parsing code in business_card.py with no
network call or real API key.

Prints one JSON object to stdout with one key per scenario.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db  # noqa: E402
from app.services import settings as app_settings  # noqa: E402
from app.integrations import ai_provider  # noqa: E402
from app.services import business_card  # noqa: E402


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    def create(self, **kwargs):
        self._last_kwargs = kwargs
        if self._exc:
            raise self._exc
        return _FakeMessage(self._text)


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self.messages = _FakeMessages(text, exc)


FAKE_IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def main():
    init_db(seed_customers=False)
    results = {}

    # --- Scenario 1: not configured -- raises with a clear message,
    # doesn't attempt any request.
    try:
        business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/jpeg")
        results["not_configured_raises"] = False
    except business_card.BusinessCardScanError as e:
        results["not_configured_raises"] = True
        results["not_configured_message_mentions_admin"] = "Admin" in str(e)

    app_settings.set_setting("anthropic_api_key", "fake-test-key")

    # --- Scenario 2: unsupported media type -- rejected before any request.
    try:
        business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/tiff")
        results["bad_media_type_raises"] = False
    except business_card.BusinessCardScanError:
        results["bad_media_type_raises"] = True

    # --- Scenario 3: clean JSON response -- parsed straight through.
    clean_json = json.dumps({
        "first_name": "Ahmed", "last_name": "Rashid", "email": "ahmed@falconsteel.ae",
        "company": "Falcon Steel", "phone": "+971-50-1234567", "title": "Procurement Manager",
    })
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text=clean_json)):
        fields = business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/jpeg")
        results["clean_json_parsed"] = fields

    # --- Scenario 4: markdown-fenced JSON (```json ... ```) -- still parses.
    fenced_json = "```json\n" + clean_json + "\n```"
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text=fenced_json)):
        fields2 = business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/jpeg")
        results["fenced_json_parsed_email"] = fields2["email"]

    # --- Scenario 5: missing fields on the card come back as None, not
    # dropped or defaulted to empty string.
    partial_json = json.dumps({"first_name": "Sara", "last_name": None, "email": None,
                                "company": "Coastal Rebar", "phone": None, "title": None})
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text=partial_json)):
        fields3 = business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/png")
        results["partial_card_missing_fields_are_none"] = fields3["email"] is None and fields3["last_name"] is None
        results["partial_card_first_name_kept"] = fields3["first_name"] == "Sara"

    # --- Scenario 6: garbage (non-JSON) response -- raises a clear error,
    # doesn't crash or silently return junk.
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text="Sorry, I can't read this image clearly.")):
        try:
            business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/jpeg")
            results["garbage_response_raises"] = False
        except business_card.BusinessCardScanError:
            results["garbage_response_raises"] = True

    # --- Scenario 7: the underlying Claude request fails (network/auth) --
    # surfaces as BusinessCardScanError, not the raw AIRequestError.
    with patch.object(ai_provider, "_client", return_value=_FakeClient(exc=RuntimeError("network down"))):
        try:
            business_card.scan_business_card(FAKE_IMAGE_BYTES, "image/jpeg")
            results["request_failure_raises_scan_error"] = False
        except business_card.BusinessCardScanError:
            results["request_failure_raises_scan_error"] = True
        except Exception:
            results["request_failure_raises_scan_error"] = False

    print(json.dumps(results))


if __name__ == "__main__":
    main()
