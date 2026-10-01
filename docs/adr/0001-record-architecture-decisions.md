# 1. Record architecture decisions

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

We need a lightweight way to capture *why* we chose a particular approach,
not just *what* we chose. Code shows the "what"; ADRs show the "why".

## Decision

We will keep Architecture Decision Records (ADRs) as short markdown files
in `docs/adr/`, numbered sequentially, one file per decision.

## Consequences

- Every non-trivial technical decision gets a short, dated note.
- Future contributors (including future us) can see the reasoning.
- No tooling required; plain markdown, diffable in git.
