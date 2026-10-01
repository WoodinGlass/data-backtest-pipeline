# 3. Batch ingestion (not streaming)

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The upstream data updates once per trading day. Streaming would add
Kafka/Kinesis complexity for no business benefit.

## Decision

Use **batch** ingestion. One Prefect-scheduled flow per trading day.

## Consequences

- Simple failure model: retry the whole batch.
- Idempotency via `(source, ticker, trade_date, payload_hash)` is enough.
- Latency is bounded by the schedule, which is acceptable for daily bars.
- If intraday data is needed later, this is a *new* ADR, not a refactor.
