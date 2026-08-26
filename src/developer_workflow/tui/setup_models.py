"""Secret-free presentation models for the runtime setup wizard."""

from __future__ import annotations

from dataclasses import dataclass

from ..setup_validation import SetupStep, ValidationStatus


TUI_SETUP_STEPS = (
    SetupStep.PROFILE,
    SetupStep.ONES,
    SetupStep.REPOSITORIES,
    SetupStep.PROVIDER,
    SetupStep.PRIVATE_PATHS,
    SetupStep.REVIEW,
)

_STEP_LABELS = {
    SetupStep.PROFILE: "Workflow profile",
    SetupStep.ONES: "ONES",
    SetupStep.REPOSITORIES: "Repositories",
    SetupStep.PROVIDER: "Git provider",
    SetupStep.PRIVATE_PATHS: "Private paths",
    SetupStep.REVIEW: "Review",
}

_STATUS_LABELS = {
    ValidationStatus.NOT_CONFIGURED: "Not configured",
    ValidationStatus.PENDING: "Testing",
    ValidationStatus.PASSED: "Passed",
    ValidationStatus.FAILED: "Failed",
}


@dataclass(frozen=True, slots=True)
class SetupStepView:
    """A deliberately value-free setup summary safe for any renderable."""

    step: SetupStep
    label: str
    status: ValidationStatus
    summary: tuple[str, ...]
    can_test: bool
    can_continue: bool


def build_setup_step_view(
    state: object,
    step: SetupStep,
    *,
    steps: tuple[SetupStep, ...] = TUI_SETUP_STEPS,
) -> SetupStepView:
    """Project controller state without copying user-controlled values."""

    results = tuple(getattr(state, "results", ()))
    by_step = {
        result.step: result
        for result in results
        if getattr(result, "step", None) in _STEP_LABELS
        and getattr(result, "status", None) in _STATUS_LABELS
    }
    result = by_step.get(step)
    status = (
        result.status if result is not None else ValidationStatus.NOT_CONFIGURED
    )
    order = steps
    if step is SetupStep.REVIEW:
        prior = order[: order.index(step)]
        prior_passed = all(
            candidate in by_step
            and by_step[candidate].status is ValidationStatus.PASSED
            for candidate in prior
        )
    else:
        # Each visible configuration surface is independently editable. Remote
        # credentials, Git mappings, and private roots can be prepared
        # before the operator decides to complete the full runtime review.
        prior_passed = True
    if step is SetupStep.REVIEW:
        can_test = False
        can_continue = prior_passed and bool(
            getattr(state, "review_confirmed", False)
        )
    else:
        can_test = prior_passed and status is not ValidationStatus.PENDING
        can_continue = prior_passed and status is ValidationStatus.PASSED
    summary = (_STATUS_LABELS[status],)
    if step is SetupStep.REPOSITORIES:
        count = getattr(state, "repository_count", 0)
        group_count = getattr(state, "repository_group_count", 0)
        if type(count) is int and type(group_count) is int:
            summary = (
                f"{max(0, count)} repositories",
                f"{max(0, group_count)} groups",
                _STATUS_LABELS[status],
            )
    return SetupStepView(
        step=step,
        label=_STEP_LABELS[step],
        status=status,
        summary=summary,
        can_test=can_test,
        can_continue=can_continue,
    )


def setup_step_label(step: SetupStep) -> str:
    return _STEP_LABELS[step]


__all__ = [
    "SetupStepView",
    "TUI_SETUP_STEPS",
    "build_setup_step_view",
    "setup_step_label",
]
