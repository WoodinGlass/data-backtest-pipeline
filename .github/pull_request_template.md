<!--
Thanks for the PR. Keep it small and focused: one topic per PR.
Delete sections that do not apply, but do not delete section 3.
-->

## What changed and why

<!-- 1–3 bullets. What is different after this PR, and what problem
     does it solve? -->

-
-

## Milestone

<!-- Which milestone (M1–M12) does this belong to? If it is a fix
     or chore, say so. -->

-

## How it was tested

<!-- Check what you actually ran. `make ci` is the local mirror of
     the GitHub Actions suite and is required for any change that
     touches Python or dbt. -->

- [ ] `make test` (unit tests pass locally)
- [ ] `make ci` (lint + tests + dbt + quality pass locally)
- [ ] `make up` (Docker build + container smoke, if Docker-related)
- [ ] Manual verification (describe below)

<!-- If you ticked "Manual verification", describe the steps. -->

## ADR amendment required?

<!-- Does this change alter a design decision documented in an ADR?
     Examples: new dependency tier, new service, new schedule,
     changed metric definition.

     If yes, link the ADR and describe the amendment in the same PR.
     If no, write "no". -->

-
