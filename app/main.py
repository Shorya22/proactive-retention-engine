import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_groq import ChatGroq
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import MODEL_VERSION, settings
from app.models import (
    CustomerProfile,
    HealthyResponse,
    RequestLog,
    RetentionResponse,
)
from app.services import EmailGenerator, SemanticCache, get_churn_predictor

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
logger = logging.getLogger("retention_engine")

limiter = Limiter(key_func=get_remote_address)


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": "rate limit exceeded", "detail": "30 requests/minute max"},
    )


def map_row_to_profile(row: pd.Series) -> dict:
    services = []
    if int(row["PhoneService"]) == 1:
        services.append("Phone Service")
    if str(row["MultipleLines"]) == "Yes":
        services.append("Multiple Lines")
    if str(row["InternetService"]) not in ("No", "No internet service"):
        services.append(f"Internet ({row['InternetService']})")
    for col, label in [
        ("OnlineSecurity", "Online Security"),
        ("OnlineBackup", "Online Backup"),
        ("DeviceProtection", "Device Protection"),
        ("TechSupport", "Tech Support"),
        ("StreamingTV", "Streaming TV"),
        ("StreamingMovies", "Streaming Movies"),
    ]:
        if str(row[col]) == "Yes":
            services.append(label)

    try:
        total_charges = float(row["TotalCharges"]) if pd.notna(row["TotalCharges"]) else 0.0
    except (ValueError, TypeError):
        total_charges = 0.0

    return {
        "customer_id": str(row["customerID"]),
        "tenure_months": int(row["tenure"]),
        "monthly_charges": float(row["MonthlyCharges"]),
        "services": services,
        "contract_type": str(row["Contract"]),
        "payment_method": str(row["PaymentMethod"]),
        "senior_citizen": int(row["SeniorCitizen"]),
        "partner": int(row["Partner"]),
        "dependents": int(row["Dependents"]),
        "phone_service": int(row["PhoneService"]),
        "multiple_lines": str(row["MultipleLines"]),
        "internet_service": str(row["InternetService"]),
        "online_security": str(row["OnlineSecurity"]),
        "online_backup": str(row["OnlineBackup"]),
        "device_protection": str(row["DeviceProtection"]),
        "tech_support": str(row["TechSupport"]),
        "streaming_tv": str(row["StreamingTV"]),
        "streaming_movies": str(row["StreamingMovies"]),
        "paperless_billing": int(row["PaperlessBilling"]),
        "total_charges": total_charges,
    }


def load_customer_db(csv_path: str) -> dict[str, CustomerProfile]:
    df = pd.read_csv(csv_path)
    df["TotalCharges"] = pd.to_numeric(df["TotalCharges"], errors="coerce").fillna(0.0)
    return {
        row["customerID"]: CustomerProfile(**map_row_to_profile(row))
        for _, row in df.iterrows()
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_churn_predictor()
    llm = ChatGroq(model=settings.LLM_MODEL_NAME, api_key=settings.GROQ_API_KEY)
    app.state.email_generator = EmailGenerator(llm)
    app.state.cache = SemanticCache()
    app.state.customer_db = load_customer_db(settings.MODEL_CSV_PATH)
    logger.info("retention engine ready")
    yield
    logger.info("llm client closed cleanly")


app = FastAPI(
    title="Proactive Retention Engine",
    description="Telecom churn prediction and personalised retention email generation",
    version=MODEL_VERSION,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def get_email_generator(request: Request) -> EmailGenerator:
    return request.app.state.email_generator


def get_cache(request: Request) -> SemanticCache:
    return request.app.state.cache


def get_customer_db(request: Request) -> dict[str, CustomerProfile]:
    return request.app.state.customer_db


def _log_request(log: RequestLog) -> None:
    logger.info(json.dumps(log.model_dump()))


@app.get("/health")
async def health():
    return {"status": "ok", "model_version": MODEL_VERSION}


@app.get("/retention/{customer_id}", response_model=RetentionResponse | HealthyResponse)
@limiter.limit(f"{settings.RATE_LIMIT}/minute")
async def assess_customer(
    customer_id: str,
    request: Request,
    predictor=Depends(get_churn_predictor),
    generator: EmailGenerator = Depends(get_email_generator),
    cache: SemanticCache = Depends(get_cache),
    customer_db: dict = Depends(get_customer_db),
):
    started_at = time.monotonic()

    customer = customer_db.get(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=f"customer {customer_id} not found")

    assessment = predictor.predict(customer)

    if assessment.risk_level == "low":
        return HealthyResponse(
            status="healthy",
            churn_probability=assessment.churn_probability,
            message="customer is not at risk — no action required",
        )

    cache_key = cache.make_key(customer)
    cached = cache.get(cache_key)
    cache_hit = cached is not None

    if cached:
        email = cached
        tokens_used = 0
        tov_violations: list[str] = []
    else:
        email, tokens_used, tov_violations = await generator.generate(customer)
        cache.set(cache_key, email)

    latency_ms = (time.monotonic() - started_at) * 1000

    _log_request(RequestLog(
        timestamp=datetime.utcnow().isoformat(),
        customer_id=customer_id,
        churn_probability=assessment.churn_probability,
        risk_level="high",
        cache_hit=cache_hit,
        llm_tokens_used=tokens_used,
        latency_ms=round(latency_ms, 2),
        tov_compliant=email.tov_compliant,
        tov_violations=tov_violations,
        model_version=MODEL_VERSION,
    ))

    return RetentionResponse(
        status="retention_triggered",
        churn_probability=assessment.churn_probability,
        email=email,
        cache_hit=cache_hit,
        latency_ms=round(latency_ms, 2),
    )
