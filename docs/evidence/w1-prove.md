# Week 1 — Prove evidence

Recorded 2026-09-24. Each claim links to the run that showed it. The tests named in
§2.3 re-prove the gitleaks detection rule and the docs lint rules on every CI run; the
pre-commit hook and the CI wiring themselves are proven by the transcript in §2.1 and
the runs linked in §2.2. This page stays true after GitHub deletes the logs — public
repos keep them for at most 90 days.

## 1. CI green on the first PR

- [PR #1](https://github.com/thanghoangnguyen-kms/shopstream/pull/1): `lint`, `test`, `secrets` and `pr-title` all passed ([ci run](https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/35998233092), [pr-title run](https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/35998232983)).
- [PR #2](https://github.com/thanghoangnguyen-kms/shopstream/pull/2): the same four checks passed on the first run ([ci run](https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/36016340365), [pr-title run](https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/36016340310)). The PR description records the ADAPT §6 proof and the critique score.

## 2. The secret gate

### 2.1 The local hook blocks the commit

```text
gitleaks (staged changes)................................................Failed
- hook id: gitleaks
- exit code: 1

  Finding:     ...HOPSTREAM_API_TOKEN=REDACTED
  Secret:      REDACTED
  RuleID:      shopstream-token
  Entropy:     4.040109
  File:        planted-secret.env
  Line:        1
  Fingerprint: planted-secret.env:shopstream-token:1

  10:08PM INF 0 commits scanned.
  10:08PM INF scanned ~68 bytes (68 bytes) in 74.1ms
  10:08PM WRN leaks found: 1
```

### 2.2 CI fails the planted-secret PR, and the ruleset blocks the merge

- [PR #3](https://github.com/thanghoangnguyen-kms/shopstream/pull/3), closed without merging. `secrets` failed ([run](https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/36017988948/job/107695342178)); the other three checks passed; merge state `BLOCKED`.

```text
.tools/bin/gitleaks git --config .gitleaks.toml --redact --no-banner --verbose .
Finding:     ...HOPSTREAM_API_TOKEN=REDACTED
Secret:      REDACTED
RuleID:      shopstream-token
Entropy:     4.040109
File:        planted-secret.env
Line:        1
Commit:      69890aab3b3d1819a4313c209a7add1bd4c3dd61
Author:      thanghoangnguyen-kms
Date:        2026-09-24T15:08:28Z
Fingerprint: 69890aab3b3d1819a4313c209a7add1bd4c3dd61:planted-secret.env:shopstream-token:1
Link:        https://github.com/thanghoangnguyen-kms/shopstream/blob/69890aab3b3d1819a4313c209a7add1bd4c3dd61/planted-secret.env#L1
4 commits scanned.
scanned ~328042 bytes (328.04 KB) in 214ms
leaks found: 1
error: recipe `secrets-scan` failed on line 40 with exit code 1
```

### 2.3 Re-proved on every run

These two tests re-prove the gitleaks detection rule and the docs lint rules — not the
pre-commit hook or the CI wiring, which §2.1 and §2.2 above already prove — on every
`just check`.

- `tests/test_secret_gate.py::test_planted_token_is_caught` plants a fresh token in a temp dir, and gitleaks must exit 1 with rule `shopstream-token`.
- `tests/test_docs_integrity.py::test_check_goes_red_on_mutation` breaks each docs rule in a temp tree, and each check must report it.

## 3. Enforcement as applied

```json
{"enforcement":"active","name":"main","required_checks":["lint","test","secrets","pr-title"],"rules":["deletion","non_fast_forward","required_linear_history","pull_request","required_status_checks"]}
```

```text
allowed_actions=selected sha_pinning_required=true
```
