"""Request and response models.

Pydantic validates at the edge, so every layer beneath can assume its input
is already the right shape and type. Anything arriving over HTTP is
untrusted: wrong types, missing fields, hostile values.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DecisionRequest(BaseModel):
    # extra="forbid": an unexpected field is a caller BUG, and silently
    # ignoring it means a typo in "card_number" produces a decision made
    # without the card. Failing loudly is kinder than deciding blind.
    model_config = {"extra": "forbid"}

    merchant_code: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=128)

    # Money as an integer in minor units. A float here eventually produces a
    # payment of 12499.999999 fils.
    amount_minor: int = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    # The raw card number is ACCEPTED but never stored, never logged, and
    # never returned. It is hashed with a secret salt the moment it reaches
    # the service layer, and only the BIN and last four survive.
    card_number: str | None = Field(default=None, max_length=25)
    card_bin: str | None = Field(default=None, max_length=8)

    email: str | None = Field(default=None, max_length=320)
    device_fingerprint: str | None = Field(default=None, max_length=128)
    ip_address: str | None = Field(default=None, max_length=45)
    account_id: str | None = Field(default=None, max_length=128)

    ip_country: str | None = Field(default=None, min_length=2, max_length=2)
    billing_country: str | None = Field(default=None, min_length=2, max_length=2)
    shipping_country: str | None = Field(default=None, min_length=2, max_length=2)

    avs_match: Literal["FULL", "PARTIAL", "NONE", "UNAVAILABLE"] | None = None
    cvv_match: bool | None = None
    three_ds_status: Literal["AUTHENTICATED", "ATTEMPTED", "FAILED", "NOT_USED"] | None = None

    is_card_present: bool = False
    channel: Literal["WEB", "MOBILE", "API", "POS"] = "WEB"
    occurred_at: datetime | None = None

    @field_validator("card_number")
    @classmethod
    def card_number_must_be_digits(cls, v: str | None) -> str | None:
        if v is None:
            return v
        digits = "".join(c for c in v if c.isdigit())
        if not 12 <= len(digits) <= 19:
            raise ValueError("card_number must contain 12 to 19 digits")
        return v

    def redacted(self) -> dict[str, Any]:
        """A form safe to log or echo. Used in error paths."""
        data = self.model_dump()
        for key in ("card_number", "email", "ip_address",
                    "device_fingerprint", "account_id"):
            if data.get(key):
                data[key] = "[redacted]"
        return data


class DecisionResponse(BaseModel):
    decision_id: UUID
    transaction_id: UUID
    decision: Literal["APPROVE", "CHALLENGE", "REVIEW", "DECLINE"]
    score: int
    triggered_rules: list[dict[str, Any]]
    features: dict[str, Any]
    latency_ms: int
    # True when this request was a retry and the ORIGINAL answer was
    # returned. The caller needs to know it is not a fresh evaluation.
    idempotent_replay: bool


class LabelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    merchant_code: str
    external_id: str
    label: Literal["FRAUD", "LEGITIMATE", "DISPUTED_NON_FRAUD"]
    source: Literal[
        "CHARGEBACK", "MANUAL_REVIEW", "ISSUER_REPORT",
        "REFUND_REQUEST", "ASSUMED_GOOD",
    ]
    reason_code: str | None = Field(default=None, max_length=20)
    labelled_at: datetime | None = None


class ReviewResolveRequest(BaseModel):
    model_config = {"extra": "forbid"}

    disposition: Literal["APPROVE", "DECLINE"]
    analyst_note: str | None = Field(default=None, max_length=2000)
    assigned_to: str = Field(min_length=1, max_length=128)


class ListEntryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    list_type: Literal["ALLOW", "DENY", "WATCH"]
    entity_type: Literal["CARD", "EMAIL", "DEVICE", "IP", "ACCOUNT", "PHONE"]
    # The raw value; hashed before storage exactly as in the decision path.
    value: str = Field(min_length=1, max_length=512)
    merchant_code: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    added_by: str = Field(min_length=1, max_length=128)
    expires_at: datetime | None = None
