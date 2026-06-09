# Proactive Retention Engine

A FastAPI service that identifies high-risk telecom customers using a churn prediction model and automatically generates personalised retention emails using an LLM. Built as a proof of concept for Vodafone.

---

## What It Does

1. You give it a customer ID
2. It runs churn prediction — if the customer is low risk, it returns immediately
3. If high risk, it generates a personalised retention email using Llama 3.3 (70B)
4. The email is validated by two LLM guardrails (brand compliance + hallucination check)
5. Returns the email along with churn probability, cache status, and latency

---

## Project Structure

```
retention_engine/
├── app/
│   ├── config.py       # Settings and Vodafone tone-of-voice rules
│   ├── models.py       # Request/response schemas
│   ├── services.py     # Churn predictor, email generator, semantic cache
│   └── main.py         # FastAPI app and endpoints
├── data/
│   ├── churn_model.pkl
│   └── Vodafone_Customer_Database.csv
├── evaluation/
│   ├── promptfoo.yaml      # Automated eval config
│   ├── golden_dataset.yaml # Ground-truth test cases
│   └── red_team.md         # Adversarial test scenarios
├── .env
└── requirements.txt
```

---

## Quick Setup

**1. Create a virtual environment**
```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Create a `.env` file**
```bash
GROQ_API_KEY=your_groq_api_key_here
MODEL_CSV_PATH=data/Vodafone_Customer_Database.csv
CHURN_MODEL_PATH=data/churn_model.pkl
LLM_MODEL_NAME=llama-3.3-70b-versatile
GUARD_MODEL_NAME=llama-3.1-8b-instant
RATE_LIMIT=30
```

Get a free Groq API key at [console.groq.com](https://console.groq.com).

**3. Start the server**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API docs at `http://localhost:8000/docs`

---

## Quick Test

Once the server is running, open a new terminal and try these:

```bash
# Health check
curl http://localhost:8000/health

# Low-risk customer — returns instantly, no email
curl http://localhost:8000/retention/2824-AHFTR

# High-risk customer — generates a retention email
curl http://localhost:8000/retention/3803-NGMHY
```

See `TEST_SCENARIOS.md` for the full list of test cases with customer IDs for every scenario (cache hit, rate limiting, 404, etc.).

---

## Run with Docker

```bash
# From inside retention_engine/
docker build -t retention-engine .
docker run -p 8000:8000 --env-file .env retention-engine
```

---

## Evaluation

The `evaluation/` folder contains automated tests that verify email quality, content safety, and guardrail correctness.

### Install promptfoo

```bash
npm install -g promptfoo
```

### Run the eval

Make sure the server is running first, then:

```bash
export $(grep -v '^#' .env | xargs) && promptfoo eval --config evaluation/promptfoo.yaml
```

Expected result: **8/8 passing**

### What gets tested

| Test Type | What It Checks |
|---|---|
| `javascript` | `tov_compliant: true`, correct routing (healthy vs retention) |
| `contains-any` | Customer's actual services appear in the email |
| `not-contains` | No prohibited language (threatening phrases, invented discounts, contract details) |
| `llm-rubric` | Email tone is warm, loyalty language matches tenure, no hallucinated services |

### View results

```bash
promptfoo view
```

Opens a browser with a detailed results table for each test case.

### Red team testing

`evaluation/red_team.md` has manual adversarial test cases — invalid IDs, prompt injection attempts, borderline churn scores, cache integrity checks, and rate limit abuse. Run these manually against the live service.

---

## Key Features

**Semantic Caching** — Emails are cached by customer segment (contract + services + spend bracket), not by individual ID. Customers with the same profile share one generated email, cutting LLM calls significantly.

**Two-Layer Guardrails** — Every email passes through a brand compliance check and a hallucination check before being returned. If it fails, the LLM retries with the violation feedback. If it fails again, a pre-approved fallback email is served.

**Rate Limiting** — 30 requests per minute per IP. Configurable via `RATE_LIMIT` in `.env`.

**Structured Logging** — Every request logs one JSON line with churn probability, latency, cache status, token usage, and compliance result.

---

## Tech Stack

- **FastAPI** — API framework
- **LangGraph** — Stateful email generation with retry and fallback logic
- **LangChain + Groq** — LLM calls (Llama 3.3 70B for generation, Llama 3.1 8B for guardrails)
- **scikit-learn** — Churn prediction model
- **slowapi** — Rate limiting
- **pydantic-settings** — Config management
- **promptfoo** — Automated LLM evaluation
