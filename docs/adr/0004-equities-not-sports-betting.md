# 4. Domain: equities, not sports betting

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The initial project sketch considered sports betting data (via The Odds API).
This raised several concerns:

- Legal ambiguity in some jurisdictions (sports betting is illegal in Indonesia).
- Reputational risk for a public portfolio: "is this a gambling project?".
- Free-tier Terms of Service explicitly prohibit commercial use.
- Data requires an API key, weakening reproducibility.

US equity daily data via `yfinance` does not have these problems:

- Public, free, no API key, well-documented coverage.
- Universally recognized domain for quantitative analysis.
- Reproducible by any reviewer: `git clone` -> `make ingest` -> same raw data.

## Decision

Use **US equity daily bars**, with a subset of ~30 large-cap S&P 500 tickers
plus SPY as benchmark.

## Consequences

- Reproducibility is trivial: no API key, no rate limit negotiation.
- Benchmarks are standard (buy-and-hold SPY, momentum) and universally understood.
- Signals are weak; the model will likely not beat buy-and-hold on risk-adjusted
  terms. This is reported honestly and framed as the expected outcome, not as
  a failure of the pipeline.
- Universe construction must handle survivorship bias explicitly (see ADR 0005).
