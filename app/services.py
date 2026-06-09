import asyncio
import hashlib
import logging
import time
from functools import lru_cache
from typing import Optional, TypedDict

import joblib
import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from app.config import MODEL_VERSION, TOV_RULES, settings
from app.models import ChurnAssessment, CustomerProfile, EmailPayload

logger = logging.getLogger("retention_engine")

_FEW_SHOT = """
EXAMPLE OF A FULLY COMPLIANT RETENTION EMAIL (follow this structure exactly):
---
Subject: Your Vodafone benefits, just for you

Hi there,

We have noticed you have been with us for a while now, and we just want to say — thank you!

Here is what you are already enjoying, with even more waiting for you:
• Lightning-fast Fibre broadband keeping you seamlessly connected 24/7
• Streaming TV and Movies, all in one brilliant entertainment experience
• Dedicated Tech Support whenever you need a helping hand

Explore your exclusive member benefits today.

Thank you for being a valued Vodafone customer. We look forward to continuing to serve you.

Best regards, Vodafone Customer Care Team
---

IMPORTANT FORMATTING RULE: Every section must be separated by a blank line — including between the closing sentence and the signature.
"""

_SYSTEM_PROMPT = TOV_RULES + _FEW_SHOT

_FALLBACK_EMAIL = EmailPayload(
    subject="A special thank you from Vodafone",
    body=(
        "Hi there,\n\n"
        "We noticed you have been with us for a while, and we want to say a heartfelt thank you!\n\n"
        "Here is what is waiting for you as a valued member of our community:\n"
        "• Exclusive upgrade options tailored for our most loyal customers\n"
        "• Priority access to our latest features and services\n"
        "• Dedicated customer care, whenever you need it\n\n"
        "Explore your exclusive member benefits today.\n\n"
        "Thank you for being a valued Vodafone customer. "
        "We look forward to continuing to serve you.\n\n"
        "Best regards, Vodafone Customer Care Team"
    ),
    personalisation_signals=["loyal customer"],
    tov_compliant=True,
)


class _EmailOutput(BaseModel):
    subject: str = Field(description="Subject line — friendly, 60 characters or fewer")
    body: str = Field(description="Full email body following the required structure")
    personalisation_signals: str = Field(
        description="Comma-separated personalisation signals, e.g. '5-year tenure, Streaming TV subscriber'"
    )


_BRAND_COMPLIANCE_PROMPT = """You are a Vodafone brand compliance reviewer for customer retention emails.

Review the email against Vodafone's official tone-of-voice and structural guidelines:

TONE (all four must be present):
- Friendly and Approachable: warm, conversational, no jargon
- Clear and Concise: short sentences, bullet points
- Positive and Reassuring: benefits-led, never negative
- Professional and Trustworthy: respectful and accurate

STRUCTURE (7-part — all must be present in order):
1. Subject line: friendly, enticing, under 60 characters
2. Greeting: "Hi there,"
3. Introduction: one sentence acknowledging loyalty / thanking the customer
4. Body: 2–3 bullet points on specific service value
5. Call to action: exactly one, clear and compelling
6. Closing: warm and appreciative
7. Signature: "Best regards, Vodafone Customer Care Team"

ADDITIONAL RULES:
- Must never imply the account is at risk, service may be cut, or contract is ending
- Must not promise specific monetary discounts or offers not explicitly provided
- Must feel personalised to the customer's services, not generic
- Subject must NOT use "Welcome" or "Thanks for choosing" — this is a retention email, not onboarding
- Bullet points must highlight the value of specific SERVICES only — never contract type, price, or account details
- Each section (greeting, introduction, body, CTA, closing) must be separated by a blank line

Respond in EXACTLY this format and nothing else:
RESULT: PASS
or:
RESULT: FAIL
VIOLATIONS: specific violation 1; specific violation 2"""

_HALLUCINATION_GUARD_PROMPT = """You are checking a Vodafone retention email for factual accuracy.

Verified customer facts (only these are known):
{customer_facts}

Check the email for claims NOT supported by the facts above. Watch for:
- Specific discount amounts or financial offers (none have been allocated to this customer)
- Bullet points describing services or features the customer does NOT have in their active services list
- Loyalty duration claims contradicting the actual tenure
- Generic filler bullet points that invent benefits not tied to a real service the customer has
- Feature promises not grounded in the customer's actual subscription

Respond in EXACTLY this format and nothing else:
RESULT: PASS
or:
RESULT: FAIL
VIOLATIONS: specific hallucinated claim 1; specific hallucinated claim 2"""


class _GenState(TypedDict):
    customer: CustomerProfile
    current_prompt: str
    email: Optional[EmailPayload]
    violations: list[str]
    attempt: int
    tokens_used: int


def _format_customer_facts(customer: CustomerProfile) -> str:
    tenure_years = customer.tenure_months // 12
    tenure_str = (
        f"{tenure_years} year{'s' if tenure_years != 1 else ''}"
        if tenure_years > 0
        else f"{customer.tenure_months} month{'s' if customer.tenure_months != 1 else ''}"
    )
    return (
        f"- Tenure: {tenure_str} with Vodafone\n"
        f"- Active services: {', '.join(customer.services) if customer.services else 'basic plan'}\n"
        f"- Contract type: {customer.contract_type}\n"
        f"- Monthly spend: £{customer.monthly_charges:.2f}\n"
        f"- No specific discounts or promotional offers are allocated to this customer"
    )


def _build_generation_prompt(customer: CustomerProfile) -> str:
    tenure_years = customer.tenure_months // 12
    tenure_str = (
        f"{tenure_years} year{'s' if tenure_years != 1 else ''}"
        if tenure_years > 0
        else f"{customer.tenure_months} month{'s' if customer.tenure_months != 1 else ''}"
    )
    services_str = ", ".join(customer.services) if customer.services else "basic plan"
    return (
        f"Write a RETENTION email for this existing Vodafone customer.\n\n"
        f"Customer context (for personalisation only):\n"
        f"- Tenure: {tenure_str} with Vodafone\n"
        f"- Active services: {services_str}\n"
        f"- Contract: {customer.contract_type} (context only — do NOT include contract type in bullet points)\n"
        f"- Monthly spend: £{customer.monthly_charges:.2f} (context only — do NOT mention the price)\n\n"
        f"Instructions:\n"
        f"- Subject: focus on EXCLUSIVE BENEFITS or APPRECIATION — "
        f"example style: 'Your exclusive Vodafone benefits await' or 'A thank you from Vodafone, just for you'\n"
        f"- Never use 'Welcome' or 'Thanks for choosing' — those are onboarding phrases, not retention\n"
        f"- Bullet points must ONLY highlight the value of their ACTIVE SERVICES listed above\n"
        f"- Write 2–3 bullets maximum — group related services into one bullet when there are many\n"
        f"  e.g. 'Streaming TV and Movies for endless entertainment' covers two services in one bullet\n"
        f"- Do not invent bullets for services not in the active services list\n"
        f"- Do not mention contract type, price, or account details in bullet points\n"
        f"- Each section must be on its own line with a blank line between greeting, introduction, body, CTA, closing\n"
        f"- The longer the tenure, the stronger the loyalty language in the introduction"
    )


class ChurnPredictor:
    def __init__(self, model_path: str):
        self._pipeline = joblib.load(model_path)
        logger.info("churn model loaded from %s", model_path)

    def predict(self, customer: CustomerProfile) -> ChurnAssessment:
        features = pd.DataFrame([{
            "SeniorCitizen": customer.senior_citizen,
            "Partner": customer.partner,
            "Dependents": customer.dependents,
            "tenure": customer.tenure_months,
            "PhoneService": customer.phone_service,
            "PaperlessBilling": customer.paperless_billing,
            "MonthlyCharges": customer.monthly_charges,
            "TotalCharges": customer.total_charges,
            "MultipleLines": customer.multiple_lines,
            "InternetService": customer.internet_service,
            "OnlineSecurity": customer.online_security,
            "OnlineBackup": customer.online_backup,
            "DeviceProtection": customer.device_protection,
            "TechSupport": customer.tech_support,
            "StreamingTV": customer.streaming_tv,
            "StreamingMovies": customer.streaming_movies,
            "Contract": customer.contract_type,
            "PaymentMethod": customer.payment_method,
        }])
        churn_prob = float(self._pipeline.predict_proba(features)[0][1])
        return ChurnAssessment(
            customer_id=customer.customer_id,
            churn_probability=round(churn_prob, 4),
            risk_level="high" if churn_prob >= 0.5 else "low",
            model_version=MODEL_VERSION,
        )


@lru_cache(maxsize=None)
def get_churn_predictor() -> ChurnPredictor:
    return ChurnPredictor(settings.CHURN_MODEL_PATH)


class SemanticCache:
    def __init__(self):
        self._store: dict[str, tuple[EmailPayload, float]] = {}
        self._ttl = 3600

    def make_key(self, customer: CustomerProfile) -> str:
        charge_bucket = round(customer.monthly_charges / 10) * 10
        raw = f"{customer.contract_type}|{'|'.join(sorted(customer.services))}|{charge_bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, key: str) -> EmailPayload | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return payload

    def set(self, key: str, payload: EmailPayload) -> None:
        self._store[key] = (payload, time.monotonic() + self._ttl)


class EmailGenerator:
    def __init__(self, llm: ChatGroq):
        structured_llm = llm.with_structured_output(_EmailOutput, include_raw=True)
        judge_llm = ChatGroq(model=settings.GUARD_MODEL_NAME, api_key=settings.GROQ_API_KEY)
        self._graph = self._build_graph(structured_llm, judge_llm)

    @staticmethod
    def _parse_guardrail_response(text: str) -> list[str]:
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        result_line = next((l for l in lines if l.upper().startswith("RESULT:")), "")
        if "PASS" in result_line.upper():
            return []
        violations_line = next((l for l in lines if l.upper().startswith("VIOLATIONS:")), "")
        raw = violations_line.split(":", 1)[1].strip() if ":" in violations_line else text
        return [v.strip() for v in raw.split(";") if v.strip()]

    @staticmethod
    def _build_graph(structured_llm, judge_llm):
        async def generate_node(state: _GenState) -> dict:
            messages = [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=state["current_prompt"]),
            ]
            result = await structured_llm.ainvoke(messages)
            raw_msg = result.get("raw")
            parsed: Optional[_EmailOutput] = result.get("parsed")

            tokens = 0
            if raw_msg and raw_msg.usage_metadata:
                tokens = raw_msg.usage_metadata.get("total_tokens", 0)

            if parsed is None or result.get("parsing_error"):
                logger.warning("llm_parse_error: %s", result.get("parsing_error"))
                return {"email": None, "tokens_used": state["tokens_used"] + tokens}

            signals = [s.strip() for s in parsed.personalisation_signals.split(",") if s.strip()]
            email = EmailPayload(
                subject=parsed.subject[:60],
                body=parsed.body,
                personalisation_signals=signals,
                tov_compliant=True,
            )
            return {"email": email, "tokens_used": state["tokens_used"] + tokens}

        async def guardrail_node(state: _GenState) -> dict:
            email = state.get("email")
            if email is None:
                return {"violations": ["email_generation_failed"], "attempt": state["attempt"] + 1}

            violations = []
            if len(email.subject) > 60:
                violations.append("subject exceeds 60 characters")

            brand_response, hallucination_response = await asyncio.gather(
                judge_llm.ainvoke([
                    SystemMessage(content=_BRAND_COMPLIANCE_PROMPT),
                    HumanMessage(content=f"Subject: {email.subject}\n\nBody:\n{email.body}"),
                ]),
                judge_llm.ainvoke([
                    SystemMessage(content=_HALLUCINATION_GUARD_PROMPT.format(
                        customer_facts=_format_customer_facts(state["customer"])
                    )),
                    HumanMessage(content=f"Subject: {email.subject}\n\nBody:\n{email.body}"),
                ]),
            )

            violations.extend(EmailGenerator._parse_guardrail_response(brand_response.content))
            violations.extend(EmailGenerator._parse_guardrail_response(hallucination_response.content))

            email.tov_compliant = len(violations) == 0
            return {"email": email, "violations": violations, "attempt": state["attempt"] + 1}

        def prepare_retry_node(state: _GenState) -> dict:
            viol_str = "; ".join(state["violations"])
            return {
                "current_prompt": (
                    state["current_prompt"]
                    + f"\n\nYour previous draft failed guardrail checks: [{viol_str}]. "
                    "Fix every issue listed before responding."
                )
            }

        def fallback_node(state: _GenState) -> dict:
            logger.warning(
                "guardrail_fallback customer=%s violations=%s",
                state["customer"].customer_id,
                state.get("violations", []),
            )
            return {"email": _FALLBACK_EMAIL, "violations": []}

        def route_after_guardrail(state: _GenState) -> str:
            if not state["violations"]:
                return "done"
            if state["attempt"] < 2:
                return "retry"
            return "fallback"

        workflow = StateGraph(_GenState)
        workflow.add_node("generate", generate_node)
        workflow.add_node("guardrail", guardrail_node)
        workflow.add_node("prepare_retry", prepare_retry_node)
        workflow.add_node("apply_fallback", fallback_node)

        workflow.set_entry_point("generate")
        workflow.add_edge("generate", "guardrail")
        workflow.add_conditional_edges(
            "guardrail",
            route_after_guardrail,
            {"done": END, "retry": "prepare_retry", "fallback": "apply_fallback"},
        )
        workflow.add_edge("prepare_retry", "generate")
        workflow.add_edge("apply_fallback", END)

        return workflow.compile()

    async def generate(self, customer: CustomerProfile) -> tuple[EmailPayload, int, list[str]]:
        final_state = await self._graph.ainvoke({
            "customer": customer,
            "current_prompt": _build_generation_prompt(customer),
            "email": None,
            "violations": [],
            "attempt": 0,
            "tokens_used": 0,
        })
        email = final_state.get("email") or _FALLBACK_EMAIL
        return email, final_state["tokens_used"], final_state["violations"]
