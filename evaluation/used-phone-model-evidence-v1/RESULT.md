# Used-phone Model Evidence V1 Pilot Result

## Result

```text
DETERMINISTIC_REFERENCE_ROUTING_ACCEPT
STRUCTURED_MODEL_FACTS: HOLD
USER_FACING_INTEGRATION: HOLD
MULTI_AGENT: HOLD
PRODUCTION_DEFAULT: UNCHANGED
```

## Measured

| Check | Result |
|---|---:|
| Runtime parser parity | 439 / 439 |
| Frozen registry binding | 24 / 24 |
| Official source routing agreement | 24 / 24 |
| Independent controlled-test routing agreement | 24 / 24 |
| Physical condition fail-closed | 24 / 24 |
| Ambiguous/unresolved exact-reference leaks | 0 / 119 |
| Network calls | 0 |
| Model calls | 0 |

## Interpretation

The deterministic shadow service can preserve the frozen V2 title/source audit
and route seller-stated model claims to URL-only reference locators. It does not
extract source facts and never upgrades a model locator into physical listing
identity, battery, repair, originality, or device-performance evidence.

This is an EvidenceService/Workflow result, not a ResearchAgent or Multi-Agent
effectiveness result.
