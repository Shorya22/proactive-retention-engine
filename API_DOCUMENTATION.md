# Retention Engine API

**Base URL:** `http://localhost:8000`  
**Version:** `v1.0.0`

## Quick Start

Check the service is running:

```bash
curl http://localhost:8000/health
```

Get a retention email for a customer:

```bash
curl http://localhost:8000/retention/3803-NGMHY
```

---

## Endpoints

### GET /health

Returns the service status and model version. Use this to verify the API is up.

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "ok",
  "model_version": "v1.0.0"
}
```

---

### GET /retention/{customer_id}

Main endpoint. Checks if a customer is at churn risk. If yes, generates a personalised retention email.

**Path Parameters**
- `customer_id` (string, required) — the customer to assess, e.g. `3803-NGMHY`

**Response — Low Risk**

Customer is not at risk (churn probability < 0.5). Returns immediately without LLM calls.

```json
{
  "status": "healthy",
  "churn_probability": 0.23,
  "message": "customer is not at risk — no action required"
}
```

**Response — High Risk**

Customer is at risk (churn probability ≥ 0.5). An email has been generated.

```json
{
  "status": "retention_triggered",
  "churn_probability": 0.72,
  "email": {
    "subject": "Your exclusive Vodafone benefits await",
    "body": "Hi there,\n\nWe have noticed you have been with us for a while now...",
    "personalisation_signals": ["12-year tenure", "Fibre subscriber"],
    "tov_compliant": true
  },
  "cache_hit": false,
  "latency_ms": 2485.23
}
```

Fields:
- `status` — `"retention_triggered"` if high risk
- `churn_probability` — predicted churn score (0.0–1.0)
- `email.subject` — email subject (max 60 characters)
- `email.body` — full email body
- `email.personalisation_signals` — which customer attributes informed the email
- `email.tov_compliant` — whether the email passed guardrails (true in normal operation)
- `cache_hit` — was this email retrieved from the cache?
- `latency_ms` — total response time in milliseconds

**Latency expectations:**
- Low-risk response: ~5ms
- High-risk, cache hit: ~10ms
- High-risk, cache miss: ~2500ms (LLM generation dominates)

**Example — Checking a Low-Risk Customer**

```bash
curl -X GET http://localhost:8000/retention/1234-ABCDE
```

Response (instant):
```json
{
  "status": "healthy",
  "churn_probability": 0.18,
  "message": "customer is not at risk — no action required"
}
```

**Example — Checking a High-Risk Customer (First Time)**

```bash
curl -X GET http://localhost:8000/retention/5678-XYZAB
```

Response (2.5 seconds):
```json
{
  "status": "retention_triggered",
  "churn_probability": 0.68,
  "email": {
    "subject": "A thank you from Vodafone, just for you",
    "body": "Hi there,\n\nWe noticed you have been with us for a while, and we want to say thank you!\n\nHere is what you are already enjoying:\n• High-speed Internet keeping you connected\n• Streaming TV for entertainment\n• Technical Support whenever you need help\n\nExplore your exclusive member benefits today.\n\nThank you for being a valued Vodafone customer. We look forward to continuing to serve you.\n\nBest regards, Vodafone Customer Care Team",
    "personalisation_signals": ["8-year tenure", "Internet + Streaming TV"],
    "tov_compliant": true
  },
  "cache_hit": false,
  "latency_ms": 2485.23
}
```

**Example — Same Customer Segment (Cache Hit)**

```bash
curl -X GET http://localhost:8000/retention/9999-LMNOP
```

Response (10 milliseconds):
```json
{
  "status": "retention_triggered",
  "churn_probability": 0.71,
  "email": { ... same email as above ... },
  "cache_hit": true,
  "latency_ms": 9.87
}
```

---

## Error Responses

### 404 Customer Not Found

```bash
curl -X GET http://localhost:8000/retention/unknown-id
```

```json
{
  "detail": "customer unknown-id not found"
}
```

The customer ID doesn't exist in the database.

### 429 Rate Limit Exceeded

```json
{
  "error": "rate limit exceeded",
  "detail": "30 requests/minute max"
}
```

You've hit the rate limit (30 requests per minute per IP). Back off and retry after 60 seconds.

### 500 Internal Server Error

Something went wrong. Check:
- Groq API key is valid: `echo $GROQ_API_KEY`
- Model file exists: `ls data/churn_model.pkl`
- CSV is readable: `head data/Vodafone_Customer_Database.csv`

---

## Semantic Caching

Emails are cached by customer *segment*, not by individual customer ID.

A segment is defined by:
- Contract type
- Services the customer has
- Monthly spend bracket (rounded to nearest £10)

Two customers with the same contract, same services, and spend within the same £10 bracket get the same email. This is intentional — if two customers have identical profiles, a retention email for one is personalised for the other.

**Cache TTL:** 3600 seconds (1 hour)

After 1 hour, the cached entry expires and a fresh email is generated on the next request.

In practice, with Vodafone's customer base, semantic caching reduces LLM calls by 50–100×.

---

## Structured Logging

Every request produces one JSON line on stdout:

```json
{
  "timestamp": "2026-06-08T13:24:32.245392",
  "customer_id": "3803-NGMHY",
  "churn_probability": 0.7858,
  "risk_level": "high",
  "cache_hit": false,
  "llm_tokens_used": 817,
  "latency_ms": 1168.65,
  "tov_compliant": true,
  "tov_violations": [],
  "model_version": "v1.0.0"
}
```

**Key fields:**
- `cache_hit` — true if the email came from cache
- `llm_tokens_used` — tokens used in generation + guardrails
- `latency_ms` — total response time
- `tov_compliant` — did the email pass guardrails?
- `tov_violations` — list of any guardrail violations (empty if compliant)

**Monitoring:**
- Alert if `tov_compliant: false` (never happens; signals guardrail failure)
- Track `cache_hit` rate (should be >40% in production)
- Monitor `latency_ms` p99 (should stay <3 seconds)

---

## Rate Limiting

Default: 30 requests per minute per IP.

The limit is per IP address, enforced in-process. In a multi-instance setup, add a Redis backend to make it global across all instances.



---

## Email Structure

Generated emails follow Vodafone's standard format:

```
Subject: [friendly, <60 characters]

Hi there,

[One sentence thanking them for their loyalty]

[2–3 bullets about their specific services]
• Service 1 with benefit
• Service 2 with benefit
• Service 3 with benefit

[Call to action]

[Warm closing]

Best regards, Vodafone Customer Care Team
```

Examples of good subject lines:
- "Your exclusive Vodafone benefits await"
- "A thank you from Vodafone, just for you"
- "Unlock more with your Vodafone services"

Examples of bad subject lines (avoided):
- "Welcome to Vodafone" (onboarding, not retention)
- "Thanks for choosing us" (onboarding, not retention)
- "Special offer just for you" (implies a specific offer being made)

The email respects these tone attributes:
- Friendly & Approachable
- Clear & Concise
- Positive & Reassuring
- Professional & Trustworthy

---

## Configuration

All settings come from `.env`:

```bash
GROQ_API_KEY=gsk_...
LLM_MODEL_NAME=llama-3.3-70b-versatile
GUARD_MODEL_NAME=llama-3.1-8b-instant
CHURN_MODEL_PATH=data/churn_model.pkl
MODEL_CSV_PATH=data/Vodafone_Customer_Database.csv
RATE_LIMIT=30
```

To change a setting, edit `.env` and restart the service. No code changes needed.

---

## Guardrails

Every generated email passes through two guardrail checks:

1. **Brand Compliance** — checks tone (friendly, clear, positive, professional) and structure (7-part format)
2. **Hallucination Guard** — verifies the email doesn't invent services or discounts the customer doesn't actually have

If either check fails, the LLM is asked to fix the violations and regenerate. If it still fails, a pre-approved fallback email is returned.

In normal operation, `tov_compliant` is always `true`. If you ever see `false`, the guardrails triggered and something is wrong with the generation prompt.

---

## Logs

Watch logs in real-time:

```bash
tail -f app.log
```

Filter for guardrail violations (should be empty):

```bash
tail -f app.log | jq 'select(.tov_violations | length > 0)'
```

Calculate cache hit rate:

```bash
tail -f app.log | jq -s 'map(.cache_hit) | map(select(. == true)) | length / length'
```

Find slow requests:

```bash
tail -f app.log | jq 'select(.latency_ms > 3000)'
```

---

