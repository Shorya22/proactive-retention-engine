from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomerProfile(BaseModel):
    customer_id: str
    tenure_months: int
    monthly_charges: float = Field(gt=0)
    services: list[str]
    contract_type: Literal["Month-to-month", "One year", "Two year"]
    payment_method: str
    # ML inference fields — not surfaced in API responses
    senior_citizen: int = 0
    partner: int = 0
    dependents: int = 0
    phone_service: int = 1
    multiple_lines: str = "No"
    internet_service: str = "No"
    online_security: str = "No"
    online_backup: str = "No"
    device_protection: str = "No"
    tech_support: str = "No"
    streaming_tv: str = "No"
    streaming_movies: str = "No"
    paperless_billing: int = 0
    total_charges: float = 0.0


class ChurnAssessment(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    customer_id: str
    churn_probability: float = Field(ge=0.0, le=1.0)
    risk_level: Literal["low", "high"]
    model_version: str


class EmailPayload(BaseModel):
    subject: str = Field(max_length=60)
    body: str
    personalisation_signals: list[str]
    tov_compliant: bool


class RetentionResponse(BaseModel):
    status: Literal["retention_triggered"]
    churn_probability: float
    email: EmailPayload
    cache_hit: bool
    latency_ms: float


class HealthyResponse(BaseModel):
    status: Literal["healthy"]
    churn_probability: float
    message: str


class RequestLog(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    timestamp: str
    customer_id: str
    churn_probability: float
    risk_level: str
    cache_hit: bool
    llm_tokens_used: int
    latency_ms: float
    tov_compliant: bool
    tov_violations: list[str]
    model_version: str
