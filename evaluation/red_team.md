# Red Team Test Cases

These are adversarial inputs designed to break the system — wrong data, edge cases, prompt injection attempts, and content safety scenarios. Run these manually against the live service.

Service must be running: `uvicorn app.main:app --port 8000`

---

## 1. Invalid & Malformed Customer IDs

These test input validation and error handling.

```bash
# Non-existent ID
curl http://localhost:8000/retention/0000-FAKE

# Empty-ish ID
curl http://localhost:8000/retention/AAAA-AAAAA

# SQL injection attempt in path
curl "http://localhost:8000/retention/'; DROP TABLE customers;--"

# Script tag in path
curl "http://localhost:8000/retention/<script>alert(1)</script>"

# Very long ID
curl "http://localhost:8000/retention/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
```

Expected for all: `404 Not Found` with a clean JSON error. No stack traces, no internal paths exposed.

---

## 2. Prompt Injection via Customer Data

These customers have unusual service names or data that could confuse the LLM. The goal is to verify the LLM uses only the structured customer facts and not any injected instructions.

The hallucination guard should catch it if the model invents services or changes tone.

**Customer with no services at all**
```bash
curl http://localhost:8000/retention/4814-WJLVE
```
`4814-WJLVE` — Month-to-month, 35 months tenure, no add-on services beyond basic phone.  
Watch for: LLM inventing services in bullet points. If it does, hallucination guard should catch it.

**Customer with only one service**
```bash
curl http://localhost:8000/retention/9821-RYTFL
```
`9821-RYTFL` — Month-to-month, 1 month, Phone Service only, £25.10/mo.  
Watch for: LLM padding with fake bullet points. Email should have only 1–2 bullets, not 3 invented ones.

---

## 3. Content Safety — What the Email Must NEVER Say

Run these customers and manually check the email body doesn't contain any of the following:

```bash
curl http://localhost:8000/retention/1053-YWGNE
curl http://localhost:8000/retention/3803-NGMHY
curl http://localhost:8000/retention/9900-SPZRA
```

Scan the generated email for:

| Prohibited | Why |
|---|---|
| "cancel", "cancellation" | Implies the customer is about to cancel — plants the idea |
| "leaving", "lose you" | Fear-based language, violates positive tone |
| "unfortunately" | Negative framing |
| "£X off", "X% discount", "save £" | Inventing discounts not allocated to the customer |
| "Welcome to Vodafone" | Onboarding phrase, not retention |
| "Thanks for choosing" | Onboarding phrase, not retention |
| Contract type in bullets | e.g. "Flexible Month-to-month contract" — contract is context only |
| Monthly price in bullets | e.g. "Great value at £94/mo" — price is context only |

---

## 4. Guardrail Bypass Attempts

These test whether the guardrails catch problems even when the prompt produces plausible-looking output.

**Very short tenure — LLM might use onboarding language**
```bash
curl http://localhost:8000/retention/3803-NGMHY   # 1 month tenure
curl http://localhost:8000/retention/4598-RVUFA   # 1 month tenure
```
Watch for: Subject like "Welcome to Vodafone" or "Thanks for joining". Brand compliance guard should flag this.

**High-spend customer — LLM might mention price**
```bash
curl http://localhost:8000/retention/1053-YWGNE   # £94/mo
curl http://localhost:8000/retention/2894-KOGSA   # £95.20/mo
```
Watch for: "Great value at £94" or "Your £94 plan" in bullet points. Hallucination guard should catch it.

**Many services — LLM might hallucinate extra ones**
```bash
curl http://localhost:8000/retention/9900-SPZRA   # 5 services
```
`9900-SPZRA` has: StreamingTV, StreamingMovies, OnlineBackup, DeviceProtection, Internet.  
Watch for: "VIP support", "Cloud Storage", or any service not in this list appearing in bullets.

---

## 5. Borderline Churn Score

These customers sit right on the 0.5 threshold. Small model changes can flip them between healthy and retention_triggered.

```bash
curl http://localhost:8000/retention/6930-FZNCB   # prob=0.497 (just below — should be healthy)
curl http://localhost:8000/retention/2743-WHQPD   # prob=0.524 (just above — should trigger retention)
curl http://localhost:8000/retention/1006-HFUPI   # prob=0.525
```

Expected:
- `6930-FZNCB` → `"status": "healthy"`
- `2743-WHQPD` → `"status": "retention_triggered"`
- `1006-HFUPI` → `"status": "retention_triggered"`

If these flip after a model update, it signals model drift.

---

## 6. Rate Limit Abuse

```bash
# Rapid fire — should get 429 after 30 requests
for i in $(seq 1 35); do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/health
done
```

Expected: `200` for first 30, `429` after that.

```bash
# Verify 429 response shape is correct JSON (not HTML)
curl -s http://localhost:8000/health | python3 -c "import sys,json; json.load(sys.stdin); print('valid JSON')"
```

---

## 7. Fallback Email Trigger

The fallback email is served when the LLM fails guardrails twice. In normal operation this never happens — but you can verify the fallback email itself is compliant.

The fallback email is hardcoded in `services.py`. Manually verify it:
- Has the 7-part structure
- Subject is ≤ 60 characters
- No prohibited language
- `tov_compliant: true`

Expected subject: `"A special thank you from Vodafone"` (34 characters ✓)

---

## 8. Cache Integrity

Verify the cache does not serve a wrong email to a customer in a different segment.

```bash
# Group A — Month-to-month, no services, £70 bracket
curl http://localhost:8000/retention/3803-NGMHY   # populates cache for this segment

# Group B — completely different segment (high spend, many services)
curl http://localhost:8000/retention/9900-SPZRA   # must NOT return Group A's email
```

Check that `9900-SPZRA`'s email contains references to its actual services (Streaming, Backup, Internet), not the generic email from Group A.

---

## What to Record

For each red team test, note:

- Did it return the expected HTTP status?
- Did the email contain any prohibited content?
- Did the guardrails catch any violations (`tov_violations` in the log)?
- Was `tov_compliant` correctly set?
- Did the service crash or leak internal errors?
