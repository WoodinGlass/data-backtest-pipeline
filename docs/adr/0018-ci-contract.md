# ADR 0018: CI Contract and Merge Blocking

- **Status:** Accepted
- **Date:** 2026-10-05
- **Related:** ADR 0008 (three-layer quality), ADR 0011 (memory),
  ADR 0015 (MLflow), ADR 0016 (Prefect), ADR 0017 (Docker)

## Context

Since M1 the project has had a CI pipeline with two jobs
(`lint-and-test`, `dbt-build`). By M8 the local development loop is
mature enough that CI is no longer "does it even run" — it is "does
it still pass after this change".

What is missing is a **contract**: which checks block a merge, how
long they may take, what coverage floor we enforce, and how the
contract is maintained when the project grows. Today merges are
possible even if a job fails (nothing stops the button on GitHub).

M9 makes the contract explicit and enforceable. It must **not**
add new pipeline semantics. Every check already runs locally; M9
moves the definition of "green" from the developer's head into
`docs/adr/0018-ci-contract.md` and `.github/workflows/ci.yml`.

## Decision

We adopt nine decisions.

### 1. Three parallel CI jobs

| Job | Purpose | Blocking? |
|---|---|---|
| `lint-and-test` | ruff, mypy, pytest + coverage floor | yes |
| `dbt-build` | dbt build against committed fixture, quality gate | yes |
| `docker-build` | `docker build` the image and smoke-import inside it | yes |

All three run in parallel; each has its own timeout. Total CI
wall-clock target: **≤ 6 minutes** on GitHub-hosted runners.

Rationale: `docker-build` is the only new job. It catches the
class of failure that "works locally" but "breaks in the image"
(missing system deps, wrong WORKDIR, entrypoint bugs). This is the
exact failure mode M8 introduced risk of.

### 2. Timeout per job

Every job declares `timeout-minutes`. Current values:

| Job | Timeout |
|---|---|
| `lint-and-test` | 15 |
| `dbt-build` | 15 |
| `docker-build` | 20 |

Rationale: GitHub's default is 360 minutes. A hung test would burn
runner time and block the queue. Hard caps force a failure, not a
hang. The values are ~3× observed normal runtime.

### 3. Coverage floor: 65% now, raise deliberately

`pytest --cov` with `--cov-fail-under=65`. This is the current
observed value minus a small buffer; not aspirational.

Rationale: a floor at the current level **prevents regressions** but
does not force busywork. We explicitly do **not** set 90%+; a
coverage number chased for its own sake produces meaningless tests.
Raising the floor is a deliberate act, documented in the ADR at that
time (see §9).

### 4. Fixture-only dbt build in CI

The `dbt-build` job runs against committed fixtures under
`tests/fixtures/raw/`, not the real raw layer. No network, no
secrets, deterministic.

Rationale: already in place since M2. Kept unchanged. The `full`
pipeline is exercised by the nightly manual workflow (future work,
not M9).

### 5. `docker-build` job details

The new job:

1. Checks out the repo.
2. Runs `docker build -t dbp-pipeline:ci .`.
3. Runs `docker run --rm dbp-pipeline:ci python -c "import ingestion, backtest, tracking, orchestration"`.
4. Runs `docker run --rm dbp-pipeline:ci python -m orchestration.cli list`.
5. Does **not** start the compose stack — building the image is the
   contract. Compose up is a user action.

Rationale: the expensive, flaky part (running the pipeline inside
Docker against real data) is out of scope. The cheap, high-signal
part (does the image build, does the package import inside it) is
in scope.

### 6. Branch protection via GitHub UI, documented here

Enforcement happens in GitHub's repository settings, not in the
repository files. The exact clicks are in `docs/runbook.md`.

The contract:

- `main` is protected.
- Direct push to `main` is disabled.
- PR required for every change, including the maintainer's own.
- All three CI jobs are **required status checks**.
- Stale reviews are dismissed on new commits.
- Conversations must be resolved before merge.

Rationale: GitHub branch protection is the only mechanism that
cannot be bypassed by a `git push --force` from a developer with
push access. Documenting the exact settings prevents the classic
"who disabled required checks and forgot" incident.

### 7. Dependabot for pip and GitHub Actions

A `.github/dependabot.yml` opens weekly PRs for:

- Python dependencies (`pip` ecosystem, grouped by extra).
- GitHub Actions (`github-actions` ecosystem).

Auto-merge is **not** enabled. Every Dependabot PR runs the full
CI. Merging is manual.

Rationale: dependencies rot silently. Dependabot surfaces it
explicitly. Auto-merge is intentionally omitted: for a project whose
selling point is reproducibility, each bump should be an explicit
decision.

### 8. Pull request template

A minimal `.github/pull_request_template.md` with four prompts:

1. What changed and why (1–3 bullets).
2. Which milestone (M1–M12) this belongs to.
3. How it was tested (`make test`, `make ci`, `make up`, ...).
4. Any ADR amendment required (yes/no + link).

Rationale: four questions force enough clarity that a reviewer
knows what they are approving. More prompts than this are ignored
in practice.

### 9. Maintenance policy

When the project's baseline changes materially (new milestone,
new test tier, new package), the coverage floor and the job set are
reviewed **in the same PR** that changes them. The ADR is amended
with a "### Amendments" section, dated, describing the change.

Rationale: an ADR that is not amended when reality changes is
worse than no ADR. The policy is light (one paragraph per change),
not a process.

## Consequences

### Positive

- Every merge to `main` was green on all three jobs.
- A broken Dockerfile fails in CI, not on the reviewer's machine.
- Coverage floor prevents silent regressions; not raised until
  there is a reason.
- Dependabot PRs are visible; nothing rots silently.
- The exact branch protection settings live in a file, not in
  the maintainer's memory.

### Negative

- CI wall-clock increases by ~3–5 minutes because of `docker-build`.
  Acceptable: PRs are infrequent, correctness matters more.
- Coverage as a single number is a blunt instrument. Mitigated by
  treating it as a floor, not a target.
- Branch protection makes the maintainer's own life slightly harder
  (no direct push to `main`). This is intentional.

### Neutral

- CI does not deploy anything. Deployment is M12 and lives outside
  this contract.
- CI does not run integration tests (`-m integration`). Those stay
  local-only until M10's monitoring work clarifies what they should
  cover in CI.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| No branch protection | Merges are possible while CI is red; "we will be careful" does not scale |
| Single monolithic CI job | Slower (no parallelism); one failure hides others |
| Coverage floor 90% | Forces meaningless tests; the number becomes the goal |
| Auto-merge Dependabot | Removes the deliberate decision point for each bump |
| Run `docker compose up` in CI | Slow, flaky, tests Compose (a runtime concern), not the image |
| `act` (local GitHub Actions runner) | Heavy dependency; local `make ci` is the local contract |
| Self-hosted runner | Ops burden not justified at this size |
| Matrix Python versions | `requires-python = ">=3.11"` is the contract; one version suffices |

## References

- GitHub docs: *Managing a branch protection rule*.
- GitHub docs: *Required status checks*.
- Dependabot docs: *Configuration options*.
- Keep a Changelog for the project's release-notes convention
  (unchanged by this ADR).
