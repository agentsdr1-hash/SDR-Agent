"""
Business card scanning -- Claude vision, reusing the same connector as
kb_qa.py's AI reply drafting (app/integrations/ai_provider.py). Attach a
photo of a business card on the Import tab; Claude reads it and returns
structured contact fields for review, exactly like a single CSV row would
look before validation -- nothing is written to the database here.

Two-step flow, mirroring prospect_import.py's own two-step shape
(import -> validate):
  1. scan_business_card() -- image in, extracted fields out. No DB write.
  2. Once the person reviews/edits the fields in the UI and confirms,
     prospect_import.create_prospect_from_scan() writes them as a
     one-row import batch and the *existing* /prospects/validate/{batch_id}
     endpoint takes it from there -- same downstream pipeline as any
     other prospect, no separate code path to keep in sync.

Nothing here is stored unreviewed: OCR can misread a card (a smudged
digit in a phone number, a similar-looking name), so this is treated the
same as an AI-drafted reply -- a suggestion a human confirms, not
something that lands in the database on its own.
"""
import json
import re

from app.integrations import ai_provider

ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_SYSTEM_PROMPT = (
    "You extract contact details from a photo of a business card. Respond with ONLY a single "
    "JSON object, no markdown formatting, no explanation -- just the raw JSON -- with exactly "
    "these keys: first_name, last_name, email, company, phone, title. Use null (not an empty "
    "string) for any field that isn't visible or isn't present on the card. Never guess or "
    "invent a value that isn't actually printed on the card -- e.g. don't fabricate an email "
    "address from a name and company domain if no email is printed."
)
_USER_PROMPT = "Extract the contact details from this business card image."

_FIELDS = ("first_name", "last_name", "email", "company", "phone", "title")


class BusinessCardScanError(Exception):
    pass


def _parse_json_response(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise BusinessCardScanError(
            "Could not read a clear response from Claude for this image -- try a clearer, "
            "well-lit photo of the card."
        )
    if not isinstance(data, dict):
        raise BusinessCardScanError("Unexpected response reading this card -- try a different photo.")
    return data


def scan_business_card(image_bytes: bytes, media_type: str) -> dict:
    """Returns {first_name, last_name, email, company, phone, title}, any
    of which may be None. Raises BusinessCardScanError (never
    AIRequestError directly) for every failure mode -- not configured,
    unsupported image type, Claude request failure, or an unparseable
    response -- so the router has one exception type to handle."""
    if not ai_provider.is_configured():
        raise BusinessCardScanError(
            "Claude is not configured. Add an Anthropic API key from the Admin tab first "
            "(AI Brain — Claude connector)."
        )
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise BusinessCardScanError(
            f"Unsupported image type '{media_type}'. Use a JPEG, PNG, WEBP, or GIF photo."
        )
    try:
        raw = ai_provider.vision_request(_SYSTEM_PROMPT, _USER_PROMPT, image_bytes, media_type)
    except ai_provider.AIRequestError as e:
        raise BusinessCardScanError(str(e))

    data = _parse_json_response(raw)
    return {field: (data.get(field) or None) for field in _FIELDS}
