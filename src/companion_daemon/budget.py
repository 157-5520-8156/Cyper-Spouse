from dataclasses import dataclass
from datetime import UTC, datetime

from companion_daemon.db import CompanionStore


# GPT Image 2 standard pricing as checked on 2026-07-13.  This is a
# deliberately conservative local planning estimate, not an invoice: the API
# may return a different token count for a particular edit request.
_USD_TO_CNY = 7.2
_IMAGE_OUTPUT_USD: dict[tuple[str, str], float] = {
    ("1024x1024", "low"): 0.006,
    ("1024x1024", "medium"): 0.053,
    ("1024x1024", "high"): 0.211,
    ("1024x1536", "low"): 0.005,
    ("1024x1536", "medium"): 0.041,
    ("1024x1536", "high"): 0.165,
    ("1536x1024", "low"): 0.005,
    ("1536x1024", "medium"): 0.041,
    ("1536x1024", "high"): 0.165,
}
# A portrait, high-fidelity reference is approximately 6,563 image tokens.
# GPT Image 2 image inputs are billed at $8/M tokens.  Reference images are
# the dominant non-output cost in our identity-preserving edit workflow.
_PORTRAIT_REFERENCE_INPUT_USD = 6_563 * 8 / 1_000_000


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ModelBudgetReservation:
    allowed: bool
    reason: str
    reservation_id: str


@dataclass(frozen=True)
class UsageEstimate:
    kind: str
    cny: float


def image_render_estimate(
    *,
    reference_count: int,
    size: str = "1024x1536",
    quality: str = "medium",
    attempts: int = 1,
) -> UsageEstimate:
    """Return a worst-case local estimate for one planned identity render.

    The planner owns this estimate so callers cannot accidentally budget a
    three-reference portrait as if it were a bare low-quality square render.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    output_usd = _IMAGE_OUTPUT_USD.get((size, quality))
    if output_usd is None:
        raise ValueError(f"unsupported image render plan: {size} / {quality}")
    input_usd = max(0, reference_count) * _PORTRAIT_REFERENCE_INPUT_USD
    return UsageEstimate(
        "image_generation",
        round((output_usd + input_usd) * _USD_TO_CNY * attempts, 4),
    )


class BudgetGate:
    def __init__(
        self,
        store: CompanionStore,
        *,
        monthly_budget_cny: float,
        daily_budget_cny: float,
        soft_daily_budget_cny: float,
        monthly_image_limit: int,
        monthly_vision_limit: int,
        monthly_audio_limit: int,
    ):
        self.store = store
        self.monthly_budget_cny = monthly_budget_cny
        self.daily_budget_cny = daily_budget_cny
        self.soft_daily_budget_cny = soft_daily_budget_cny
        self.monthly_image_limit = monthly_image_limit
        self.monthly_vision_limit = monthly_vision_limit
        self.monthly_audio_limit = monthly_audio_limit

    def check(self, estimate: UsageEstimate, *, automatic: bool) -> BudgetDecision:
        now = datetime.now(UTC)
        daily = self.store.usage_total("day", now)
        monthly = self.store.usage_total("month", now)
        kind_monthly = self.store.usage_count(estimate.kind, "month", now)

        if monthly + estimate.cny > self.monthly_budget_cny:
            return BudgetDecision(False, "monthly_budget_exceeded")
        if daily + estimate.cny > self.daily_budget_cny:
            return BudgetDecision(False, "daily_budget_exceeded")
        if automatic and daily + estimate.cny > self.soft_daily_budget_cny:
            return BudgetDecision(False, "soft_daily_budget_requires_manual")
        if estimate.kind == "image_generation" and kind_monthly >= self.monthly_image_limit:
            return BudgetDecision(False, "monthly_image_limit_exceeded")
        if estimate.kind == "vision" and kind_monthly >= self.monthly_vision_limit:
            return BudgetDecision(False, "monthly_vision_limit_exceeded")
        if estimate.kind == "transcription" and kind_monthly >= self.monthly_audio_limit:
            return BudgetDecision(False, "monthly_audio_limit_exceeded")
        return BudgetDecision(True, "ok")

    def record(self, estimate: UsageEstimate, *, note: str = "") -> None:
        self.store.record_usage(estimate.kind, estimate.cny, note=note)

    def remaining_model_budget_cny(
        self, *, automatic: bool, now: datetime | None = None
    ) -> float:
        """Return remaining spend after actual token-priced model usage.

        Legacy fixed-cost usage and provider token usage live in separate
        ledgers, so both are included without recording a model call twice.
        """
        observed_at = now or datetime.now(UTC)
        daily_fixed = self.store.usage_total("day", observed_at)
        monthly_fixed = self.store.usage_total("month", observed_at)
        daily_model = float(
            self.store.model_usage_report("day", observed_at)["total"][
                "estimated_cost_cny"
            ]
        )
        monthly_model = float(
            self.store.model_usage_report("month", observed_at)["total"][
                "estimated_cost_cny"
            ]
        )
        limits = [
            self.daily_budget_cny
            - daily_fixed
            - daily_model
            - self.store.pending_model_budget_reservation_total("day", observed_at),
            self.monthly_budget_cny
            - monthly_fixed
            - monthly_model
            - self.store.pending_model_budget_reservation_total("month", observed_at),
        ]
        if automatic:
            limits.append(
                self.soft_daily_budget_cny
                - daily_fixed
                - daily_model
                - self.store.pending_model_budget_reservation_total("day", observed_at)
            )
        return max(0.0, min(limits))

    def reserve_model_call(
        self,
        *,
        reservation_id: str,
        estimated_cny: float,
        automatic: bool,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> ModelBudgetReservation:
        """Reserve a priced provider call before it reaches the network."""
        allowed, reason = self.store.reserve_model_budget(
            reservation_id=reservation_id,
            estimated_cny=estimated_cny,
            automatic=automatic,
            monthly_budget_cny=self.monthly_budget_cny,
            daily_budget_cny=self.daily_budget_cny,
            soft_daily_budget_cny=self.soft_daily_budget_cny,
            now=now or datetime.now(UTC),
            lease_seconds=lease_seconds,
        )
        return ModelBudgetReservation(allowed, reason, reservation_id)

    def start_model_call(
        self,
        reservation_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        """Durably mark a preflight reservation immediately before provider I/O."""
        return self.store.start_model_budget_reservation(
            reservation_id,
            now=now or datetime.now(UTC),
            lease_seconds=lease_seconds,
        )

    def finalize_model_call(
        self,
        reservation_id: str,
        *,
        request_emitted: bool,
        usage_persisted: bool,
    ) -> None:
        """Release only calls proven not to have reached the provider."""
        self.store.finalize_model_budget_reservation(
            reservation_id,
            request_emitted=request_emitted,
            usage_persisted=usage_persisted,
        )

    def release_model_call(self, reservation_id: str) -> None:
        self.store.release_model_budget_reservation(reservation_id)


ESTIMATES = {
    "vision": UsageEstimate("vision", 0.03),
    "transcription": UsageEstimate("transcription", 0.05),
    # Compatibility default for callers that have not yet built a render plan.
    "image_generation": image_render_estimate(reference_count=2),
    "memory_maintenance": UsageEstimate("memory_maintenance", 0.02),
    "afterthought": UsageEstimate("afterthought", 0.01),
    "proactive_decision": UsageEstimate("proactive_decision", 0.01),
    "life_event": UsageEstimate("life_event", 0.02),
}
