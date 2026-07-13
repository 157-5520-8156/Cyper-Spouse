from dataclasses import dataclass
from datetime import UTC, datetime

from companion_daemon.db import CompanionStore


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
        )
        return ModelBudgetReservation(allowed, reason, reservation_id)

    def release_model_call(self, reservation_id: str) -> None:
        self.store.release_model_budget_reservation(reservation_id)


ESTIMATES = {
    "vision": UsageEstimate("vision", 0.03),
    "transcription": UsageEstimate("transcription", 0.05),
    "image_generation": UsageEstimate("image_generation", 0.35),
    "memory_maintenance": UsageEstimate("memory_maintenance", 0.02),
    "afterthought": UsageEstimate("afterthought", 0.01),
    "proactive_decision": UsageEstimate("proactive_decision", 0.01),
    "life_event": UsageEstimate("life_event", 0.02),
}
