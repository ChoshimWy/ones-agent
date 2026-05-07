# AnalysisResult acceptance rules

This document defines the minimum acceptance gate for the canonical `AnalysisResult` contract in `src/contracts.py`. It exists to keep M4 analysis output and M5 execution decisions deterministic.

These rules are intentionally stricter than the current markdown-only analyzer in `src/llm/analyzer.py`. If the analyzer cannot support a claim with enough evidence, it must return a valid `AnalysisResult` that sets `insufficient_evidence=True` instead of filling empty fields with confident prose.

## Scope and authority

- `src/contracts.py` is the canonical schema authority.
- `docs/ones_defect_refactor_boundaries.md` defines stage ownership.
- This document defines the quality gate for outputs that cross from M4 analysis into any later decision point.
- M5 execution and branch creation must treat this document as blocking policy, not advisory guidance.

## Canonical fields in scope

These rules apply to the current `AnalysisResult` fields:

- `defect_id`
- `project`
- `repo_resolution`
- `analysis_summary`
- `root_cause`
- `evidence`
- `confidence`
- `impacted_files`
- `fix_suggestions`
- `insufficient_evidence`
- `rendered_markdown`

## 1. Minimum fields for a complete AnalysisResult

An `AnalysisResult` is complete only when all rules below are satisfied.

### 1.1 Required identity and target fields

- `defect_id` must be a non-empty string.
- `project.id` or `project.name` must be non-empty.
- `repo_resolution` must be present.
- `repo_resolution.selected_repo.repo_url` or `repo_resolution.selected_repo.repo_name` must be non-empty.
- `repo_resolution.selected_branch` must be non-empty.

If any field in this section is missing, the result is incomplete and execution is blocked.

### 1.2 Required reasoning fields for an actionable result

For an actionable result, all fields below must be present and non-empty:

- `analysis_summary`, at least 20 non-whitespace characters.
- `root_cause`, at least 20 non-whitespace characters.
- `evidence`, meeting the thresholds in section 2.
- `impacted_files`, at least 1 path.
- `fix_suggestions`, at least 1 item meeting section 5.
- `rendered_markdown`, containing a readable rendering of the same conclusion, not extra unsupported claims.

### 1.3 Allowed shape for a non-actionable result

If the analyzer cannot satisfy section 1.2, it may still return a complete but non-actionable result only when all conditions below hold:

- `insufficient_evidence=True`
- `analysis_summary` explains what was checked and why the result is blocked.
- `root_cause` is empty or explicitly states that root cause is unconfirmed.
- `evidence` contains every evidence item that was actually gathered, even if limited.
- `fix_suggestions` is empty, or only contains investigation-only next steps that are clearly marked non-executable.
- `confidence <= 0.49`
- `rendered_markdown` matches the blocked state.

## 2. Acceptable evidence types and minimum evidence threshold

### 2.1 Acceptable evidence types

Each `EvidenceReference` must point to an observable input, not a guess. Acceptable evidence includes:

- Repository file evidence, `kind="file"`, with `file_path` and a relevant `snippet`.
- Repository line-range evidence, `kind="file"`, with `file_path`, `start_line`, and `end_line`.
- Codebase search result evidence, where `description` explains the search hit and `source` names the search method.
- Defect record evidence, where `source="ones"` or equivalent and `description` ties the defect text to the claim being made.
- Repo resolution evidence, where `description` explains why the selected repo or branch is relevant.

### 2.2 Unacceptable evidence types

The following do not count toward the threshold:

- Restating the defect title or description without linking it to code or repo context.
- Repeating model reasoning without a quoted snippet, file path, or explicit source.
- Generic statements like "this is probably in the auth module" with no repository reference.
- Evidence entries with empty `file_path`, empty `description`, and empty `source`.
- Evidence that cites a file path but no matching snippet or line range when the claim depends on code details.

### 2.3 Minimum evidence threshold for an actionable result

An actionable result must have at least 2 acceptable evidence items, and all conditions below must hold:

1. At least 1 evidence item must reference repository code through `file_path`.
2. At least 1 evidence item must support the claimed root cause directly, not only background context.
3. Across all evidence items, there must be at least 2 distinct support points from this set:
   - defect description or metadata
   - repo resolution rationale
   - code snippet or line-range observation
   - cross-file consistency check
4. At least 1 impacted file must appear in either `evidence.file_path` or a `fix_suggestions[].impacted_files` entry.

If the analyzer only has defect text and no code evidence, it must mark `insufficient_evidence=True`.

## 3. Insufficient-evidence criteria

`insufficient_evidence` must be set to `True` when any condition below is true:

- No `repo_resolution` is available.
- The selected repo is known, but no code context could be retrieved.
- Fewer than 2 acceptable evidence items were collected.
- No evidence item directly supports the claimed root cause.
- `root_cause` is speculative, contradictory, or copied from the defect wording with no added proof.
- `impacted_files` lists files that are not backed by evidence.
- `fix_suggestions` require code changes, but no file-level evidence exists.
- `confidence >= 0.5` cannot be justified by the gathered evidence.

When `insufficient_evidence=True`, the result must say what is missing. Examples include a missing repository, missing file content, conflicting signals, or no file that explains the reported behavior.

## 4. Confidence usage rules

`AnalysisResult.confidence` is a gating field, not decorative metadata.

### 4.1 Numeric meaning

- `0.00` to `0.49`: blocked confidence. The result cannot drive execution.
- `0.50` to `0.74`: partial confidence. The result may be useful for human review, but branch creation stays blocked.
- `0.75` to `1.00`: actionable confidence. Branch creation may proceed only if all other acceptance rules pass.

### 4.2 Confidence constraints

- `confidence` must stay within `0.0` to `1.0`.
- `confidence >= 0.75` requires the actionable evidence threshold in section 2.3 and at least 1 concrete fix suggestion.
- `confidence >= 0.50` requires at least 1 repository-backed evidence item.
- `confidence <= 0.49` is mandatory whenever `insufficient_evidence=True`.
- Confidence must drop when the result depends on a single file guess, incomplete code context, or unresolved alternative explanations.

### 4.3 Confidence must not be used this way

- Do not map fluent prose to high confidence.
- Do not assign `confidence >= 0.75` from defect text alone.
- Do not use a high-confidence repo resolution score as a substitute for root-cause evidence.

## 5. Fix suggestion quality bar

Each `FixSuggestion` in an actionable result must meet all rules below:

- `title` must be non-empty and specific enough to distinguish the suggestion from other options.
- `description` must explain the intended code or behavior change.
- `impacted_files` must contain at least 1 file path.
- `steps` must contain at least 1 concrete implementation step.
- `risk_level` must be one of `low`, `medium`, or `high`.

### 5.1 Additional quality rules for actionable suggestions

- At least 1 `impacted_files` entry must overlap with `AnalysisResult.impacted_files`.
- Steps must describe edits or checks a developer can perform. Examples: update condition ordering, add null guard, adjust parser branch, add regression test.
- Suggestions must align with the stated `root_cause`.
- Suggestions must not introduce branch, commit, push, or PR instructions. Those belong to M5 or later.

### 5.2 Suggestions that fail acceptance

Reject suggestions like these:

- "Fix the bug in the relevant module"
- "Review the code and improve stability"
- "Update logic as needed"
- any suggestion with empty `impacted_files`
- any suggestion that proposes execution work while `insufficient_evidence=True`

## 6. Rules that block execution and branch creation

M5 must not create a branch, and must not build an `ExecutionRequest` from analysis alone, when any of these are true:

- `insufficient_evidence=True`
- `confidence < 0.75`
- `repo_resolution is None`
- `repo_resolution.selected_repo` is unresolved
- `analysis_summary` is empty
- `root_cause` is empty or marked unconfirmed
- `evidence` fails section 2.3
- `fix_suggestions` is empty
- `impacted_files` is empty
- `fix_suggestions[].impacted_files` does not overlap with `AnalysisResult.impacted_files`

Execution is also blocked when the rendered markdown says the result is tentative, but the structured fields claim it is ready. In that case, the structured result is invalid and must be corrected before any M5 handoff.

## 7. Consistency rules across fields

The structured and rendered outputs must agree.

- `rendered_markdown` must summarize the same `root_cause`, `evidence`, and `fix_suggestions` carried in the structured fields.
- `impacted_files` must be the union or a subset of file paths justified by evidence and suggestions. It must not introduce unrelated paths.
- If `root_cause` names a component or file, at least 1 evidence item must support that claim.
- If multiple fix suggestions are present, they must not assume mutually exclusive root causes unless the result is explicitly blocked for ambiguity.

## 8. Positive and negative examples

### 8.1 Acceptable actionable result

Accept:

- `defect_id` present
- `repo_resolution` resolved to a repo and branch
- `analysis_summary` explains that a null response path skips a guard in `src/api/user_service.py`
- `root_cause` states that missing null handling in `src/api/user_service.py` causes the crash
- `evidence` includes:
  - ONES defect description describing the null-response crash
  - file evidence from `src/api/user_service.py:48-63` showing unchecked access
  - file evidence from `tests/test_user_service.py` showing the missing regression case
- `impacted_files` includes both files above
- `fix_suggestions` proposes adding the null guard and a regression test
- `confidence=0.82`
- `insufficient_evidence=False`

Why it passes: the root cause is tied to concrete code, the impacted files are justified, and the suggested fix matches the evidence.

### 8.2 Acceptable blocked result

Accept as blocked only:

- repo resolved correctly
- defect text suggests a permission issue
- no repository file or snippet confirms the failing path
- `analysis_summary` says the likely area is auth middleware, but current evidence is not enough to confirm root cause
- `root_cause` is empty or explicitly unconfirmed
- `evidence` contains the defect text and repo-resolution rationale only
- `fix_suggestions` is empty or limited to investigation steps such as collecting stack traces or locating the failing endpoint handler
- `confidence=0.34`
- `insufficient_evidence=True`

Why it passes: it is honest about the gap and prevents fabricated certainty.

### 8.3 Unacceptable result

Reject:

- `analysis_summary` says the bug is definitely in payment retry logic
- `root_cause` names `src/payments/retry.py`
- `evidence` contains only the defect description and a generic sentence with no file snippet
- `impacted_files` lists 4 files with no support
- `fix_suggestions` says "update retry handling" with no steps
- `confidence=0.91`
- `insufficient_evidence=False`

Why it fails: high confidence is unsupported, file claims are unproven, and the fix suggestion is too vague for execution.

## 9. Implementation checklist for M4 and M5

M4 analysis should treat this checklist as the minimum pass condition before returning an actionable result:

1. Populate every required `AnalysisResult` field from `src/contracts.py`.
2. Validate evidence count and evidence quality.
3. Downgrade confidence when proof is thin or conflicting.
4. Set `insufficient_evidence=True` instead of inventing a root cause.
5. Reject fix suggestions that cannot be traced to impacted files.
6. Keep `rendered_markdown` aligned with the structured result.

M5 execution should refuse to proceed unless sections 1 through 7 all pass.
