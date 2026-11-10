# PayPal sandbox verification

The resumable core runner completed all three PayPal Sandbox scenarios on Windows on November 2 and November 6, 2026. Redacted reports are preserved in `docs/sandbox-evidence/`. The broader webhook and restart checks below remain separate acceptance items.

## Required local configuration

- Sandbox REST app belonging to a business seller.
- Personal sandbox buyer distinct from seller.
- `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `REWIND_API_TOKEN` in local `.env`.
- `REWIND_PROVIDER=sandbox`, `REWIND_DB=rewind-sandbox.db`.
- For webhooks: reachable HTTPS origin, sandbox webhook registration, and correct `PAYPAL_WEBHOOK_ID`.
- For live AI: configured hosted model/key, or a running Ollama model. See README.
- Start all four services with `python -m rewind.devstack` (without `--rehearsal`).

## Runnable core verification

Use the Python executable from your virtual environment. In another terminal:

```bash
python -m rewind.verify_sandbox start --scenario true_failure --report local-runs/refund.json
```

Approve the printed URL yourself with the personal sandbox buyer, then:

```bash
python -m rewind.verify_sandbox resume --report local-runs/refund.json
```

Repeat start/approval/resume for `capture_timeout` with `local-runs/capture-timeout.json` and `delivery_loss` with `local-runs/delivery-loss.json`. Each run records its own task and provider IDs. Resume preserves the original task; do not delete a report to retry an uncertain payment. Check the PayPal sandbox dashboard against these IDs. The runner requires real live AI before completing recovery; it reports incomplete status when inference or recovery fails.

For the timeout run, the proxy records a successful upstream response and closes the socket before the app receives it. A second capture attempt must reconcile the original capture, with capture count exactly one. This is controlled fault injection, not a real PayPal outage.

The committed reports and judge replay are redacted and contain no credentials. The core runner does not exercise webhooks or all restart/refund-loss cases below.

## Spike acceptance checks

| Check | Expected evidence | Status |
| --- | --- | --- |
| Authentication | Token obtained without browser-visible secret | Verified during three bounded core runs |
| Create + buyer approve order | Real sandbox order ID and APPROVED state | Verified during three bounded core runs |
| Normal capture | Capture ID, USD 50.00, COMPLETED | Verified during three bounded core runs |
| Drop response + retry | First capture remains; no new capture/order for task | Verified: proxy recorded upstream 201 then dropped socket; capture count remained one |
| Restart after uncertainty | Same DB and task recover original capture | Unverified |
| Result loss | Same artifact hash redelivered, no refund | Verified in sandbox core run |
| Terminal failure | Refund ID and confirmed refund status | Verified in sandbox core run |
| Lost refund response | Reconciliation detects actual refund; no new request ID | Emulator test only |
| Real webhook | Verified event stored and provider GET confirms state | Unverified |
| Duplicate webhook | One stored event, safe repeated reconciliation | Automated handler test only |
| Live hosted AI or Ollama | Valid diagnosis, citations, contradiction classification | Verified with Ollama `qwen3:8b`; six-case evaluator passed 6/6 |

Record timestamps and non-secret sandbox object IDs in a separate verification note. Never publish credentials or buyer personal details.

## Disputes decision

Official documentation provides sandbox-only dispute creation/status/settlement methods, but creating a dispute requires buyer credentials and the `DISPUTE_CREATE` scope. The docs instruct developers to contact a PayPal account manager to enable that setting.

Therefore disputes are **stretch scope**, not a core dependency. This build exports a local evidence package only. Do not advertise automatic dispute filing or a guaranteed refund entitlement.

If scope becomes available, spike it separately and add tests before integrating it into the main demo. Otherwise retain the three scenarios and local evidence export.

Reference: https://developer.paypal.com/platforms/disputes/integrate-disputes/

