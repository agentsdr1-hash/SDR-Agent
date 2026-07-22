from pydantic import BaseModel
from typing import Optional


class ImportSummary(BaseModel):
    batch_id: str
    filename: str
    row_count: int
    columns_mapped: dict[str, str]


class ProspectRecord(BaseModel):
    id: int
    lead_number: str
    row_number: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    status: str
    validation_notes: Optional[str] = None


class ValidationSummary(BaseModel):
    batch_id: str
    total: int
    valid: int
    invalid: int
    duplicate: int
    existing_customer: int
    already_contacted: int = 0


class CampaignCreate(BaseModel):
    name: str
    send_days: Optional[str] = "Mon,Tue,Wed,Thu,Fri"
    daily_send_limit: Optional[int] = 25


class Campaign(BaseModel):
    id: int
    name: str
    status: str
    send_days: str
    daily_send_limit: int
    created_at: str
    prospect_count: int = 0


class AssignResult(BaseModel):
    campaign_id: int
    batch_id: str
    assigned: int
    skipped_already_in_campaign: int
    skipped_not_valid: int
    skipped_suppressed: int = 0


class CampaignProspect(BaseModel):
    id: int
    prospect_id: int
    lead_number: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    status: str
    subject: Optional[str] = None
    body: Optional[str] = None
    added_at: str
    approved_at: Optional[str] = None
    sent_at: Optional[str] = None
    replied_at: Optional[str] = None
    reply_subject: Optional[str] = None
    quote_requested_at: Optional[str] = None
    won_at: Optional[str] = None
    lost_at: Optional[str] = None
    deal_value: Optional[float] = None
    lost_reason: Optional[str] = None


class DraftUpdate(BaseModel):
    subject: str
    body: str


class SendResult(BaseModel):
    campaign_id: int
    attempted: int
    sent: int
    failed: int
    suppressed: int = 0
    errors: list[str] = []


class EmailStatus(BaseModel):
    configured: bool
    gmail_address: Optional[str] = None
    poll_interval_minutes: int
    last_poll_at: Optional[str] = None
    last_poll_replies_found: Optional[int] = None
    last_poll_error: Optional[str] = None


class PollResult(BaseModel):
    checked_at: str
    replies_found: int
    updated_prospects: list[str] = []


class SuppressionEntry(BaseModel):
    email: str
    reason: Optional[str] = None
    source: str
    added_at: str


class SuppressionAdd(BaseModel):
    email: str
    reason: Optional[str] = None


class WonPayload(BaseModel):
    deal_value: Optional[float] = None


class LostPayload(BaseModel):
    reason: Optional[str] = None


class SimulateReplyPayload(BaseModel):
    reply_subject: Optional[str] = None
    reply_body: Optional[str] = None
    is_opt_out: bool = False


class StockImportSummary(BaseModel):
    filename: str
    item_count: int
    category_count: int
    imported_at: str


class StockCategory(BaseModel):
    category: str
    count: int


class StockItem(BaseModel):
    id: int
    product_code: str
    product_name: str
    category: Optional[str] = None


class AuditEvent(BaseModel):
    id: int
    timestamp: str
    event_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    details: Optional[str] = None
    actor: str


class KBEntry(BaseModel):
    id: int
    question: str
    answer: str
    tags: Optional[str] = None


class KBEntryCreate(BaseModel):
    question: str
    answer: str
    tags: Optional[str] = None


class KBImportSummary(BaseModel):
    entry_count: int


class ReplyDraft(BaseModel):
    id: int
    campaign_prospect_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    subject: str
    body: str
    status: str
    confidence: Optional[str] = None
    matched_summary: Optional[str] = None
    source_reply_subject: Optional[str] = None
    source_reply_snippet: Optional[str] = None
    created_at: str
    approved_at: Optional[str] = None
    rejected_at: Optional[str] = None
    sent_at: Optional[str] = None


class ReplyDraftUpdate(BaseModel):
    subject: str
    body: str
