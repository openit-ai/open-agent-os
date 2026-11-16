"""Task-level multi-LLM routing for the Company edition (G13).

The database (`005_llm_routing.sql`) is the policy source of truth. This module
applies the same rule in the request path so a provider failure can fall back
to the standard model without changing the owner/assistant context, and a
usage-billing model is never called without an approved positive cap.

Secrets are intentionally absent: callers pass a callable that resolves the
provider credential from the Company EnvironmentFile.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class RoutingError(Exception):
    """Base router error."""


class RouteUnavailable(RoutingError):
    """No route or no billable/reachable model can serve the task."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BillingError(RoutingError):
    """A usage-billing model is missing approval or a positive cap."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    name: str
    tier: str
    billing_mode: str
    monthly_cap: float = 0.0
    month_spend: float = 0.0
    approved: bool = False

    def usable(self) -> tuple[bool, str]:
        if self.tier not in ("standard", "premium"):
            return False, "invalid_tier"
        if self.billing_mode == "flat":
            return True, "flat_billing"
        if self.billing_mode != "usage":
            return False, "invalid_billing_mode"
        if not self.approved or self.monthly_cap <= 0:
            return False, "billing_not_approved"
        if self.month_spend >= self.monthly_cap:
            return False, "cap_exceeded"
        return True, "usage_approved"


@dataclass(frozen=True)
class Route:
    task: str
    primary: Model
    fallback: Model | None = None


@dataclass(frozen=True)
class Result:
    model_id: str
    provider: str
    model_name: str
    context: Mapping[str, Any]
    output: Any
    fallback_used: bool = False
    reason: str = "primary_available"
    attempted: tuple[str, ...] = field(default_factory=tuple)


class Router:
    """Select a model per task and execute one call with a standard fallback."""

    def __init__(self, routes: Mapping[str, Route], models: Mapping[str, Model],
                 call: Callable[[Model, Mapping[str, Any]], Any]):
        self.routes = routes
        self.models = models
        self.call = call

    def _resolve(self, task: str) -> Route:
        route = self.routes.get(task)
        if route is not None:
            return route
        fallback_route = self.routes.get("default")
        if fallback_route is None:
            raise RouteUnavailable("route_missing")
        return fallback_route

    def _model(self, model: Model) -> Model:
        return self.models.get(model.id, model)

    def run(self, task: str, context: Mapping[str, Any]) -> Result:
        route = self._resolve(task)
        primary = self._model(route.primary)
        usable, reason = primary.usable()
        if usable:
            attempts = [primary.id]
            try:
                output = self.call(primary, context)
            except Exception as error:  # provider timeout, 5xx, transport error
                fallback = route.fallback
                if fallback is None:
                    raise RoutingError(f"primary call failed: {error}") from error
                fallback = self._model(fallback)
                fallback_usable, _ = fallback.usable()
                if not fallback_usable:
                    raise RoutingError(f"primary call failed and fallback is unavailable: {error}") from error
                attempts.append(fallback.id)
                output = self.call(fallback, context)
                return Result(fallback.id, fallback.provider, fallback.name, context,
                              output, True, "provider_error", tuple(attempts))
            return Result(primary.id, primary.provider, primary.name, context,
                          output, False, "primary_available", tuple(attempts))

        fallback = route.fallback
        if fallback is None:
            raise RouteUnavailable(reason)
        fallback = self._model(fallback)
        fallback_usable, _ = fallback.usable()
        if not fallback_usable:
            raise RouteUnavailable(reason)
        output = self.call(fallback, context)
        return Result(fallback.id, fallback.provider, fallback.name, context,
                      output, True, reason, (fallback.id,))
