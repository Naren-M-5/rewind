# Verification — REWIND v0.2

## Observed results

- **57 automated tests passed** on Windows with Python 3.12 and pytest 8.4.2 after UI v4 installation.
- The unchanged six-case live evaluator passed **6/6** with Ollama 0.40.10 and `qwen3:8b` using structured, non-thinking output.
- All three bounded PayPal Sandbox scenarios passed. Redacted reports are stored in `docs/sandbox-evidence/`.
- The combined three-task replay passed schema and sensitive-marker checks. Its server exposes task/evidence reads and no mutation routes.
- JavaScript syntax and Python compilation checks pass for the release candidate.

## PayPal Sandbox evidence

| Scenario | Observed result |
| --- | --- |
| Capture response lost | Fault proxy recorded upstream HTTP 201 and dropped the downstream socket; reconciliation recovered one capture; duplicate task payment was blocked. |
| Result delivery lost | One capture; committed artifact; AI recommended redelivery; original artifact and matching buyer receipt were recorded. |
| Terminal merchant failure | One capture; no artifact; AI recommended refund; PayPal Sandbox returned a refund ID and confirmed refunded state. |

The captured payment, order, capture, and refund objects are PayPal Sandbox objects. Merchant work, buyer delivery, and injected faults are controlled local demo services. The reports prove those bounded runs, not every possible provider or model failure.

## What the automated suite establishes

The suite covers concurrent retries, restart recovery, lost capture/refund responses, stale operation windows, amount and task-binding mismatches, pending/partial refunds, policy overrides, AI vetoes, fabricated citations, conflicting merchant evidence, fresh-diagnosis requirements, token protection, and webhook deduplication/signature-handler behavior.

Process-level tests send real HTTP requests between a merchant, buyer inbox, fault proxy, and local provider stub. They confirm a socket closes after a successful upstream capture, a retry observes one capture, redelivery gets a matching receipt, a lost buyer acknowledgement is reconciled, changed artifact bytes are rejected, and service authentication is enforced. STUB identifiers are never presented as PayPal objects.

Hosted-AI protocol tests verify request formats, schema validation, refusals, incomplete output, and sanitized errors. The separate six-case evaluation measures live model behavior on recovered payment, delivery gap, terminal failure, contradictory evidence, prompt injection, and pending payment.

## Explicitly incomplete

| Check | Status |
| --- | --- |
| Real PayPal webhook delivery | Handler tests pass; live delivery remains unverified. |
| Process restart during a real Sandbox uncertain capture/refund | Emulator tests pass; broader Sandbox restart spike remains unverified. |
| Public GitHub repository and remote CI | Published; GitHub Actions passes on Python 3.11 and 3.12. |
| Hosted public replay | Local read-only replay is verified; hosting is optional and incomplete. |

One Starlette TestClient deprecation warning appears; all 57 tests pass. These results are development and bounded Sandbox evidence, not a production-readiness or live-money claim.
