"""Proxy pool with health tracking, quarantine, and rotation.

No live proxy infrastructure is assumed or required: with zero configured
proxies the pool simply returns `None` for every acquisition (direct
connection), which is what every existing test exercises. Operators supply
real proxies via `ProxyPool(configs=[...])`.
"""

from dataclasses import dataclass, field
from enum import Enum
from itertools import cycle


class ProxyHealthState(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    QUARANTINED = "quarantined"


@dataclass
class ProxyConfig:
    proxy_id: str
    url: str


@dataclass
class _ProxyState:
    config: ProxyConfig
    health: ProxyHealthState = ProxyHealthState.HEALTHY
    consecutive_failures: int = 0


@dataclass
class ProxyPool:
    configs: list[ProxyConfig] = field(default_factory=list)
    failure_threshold: int = 3

    def __post_init__(self) -> None:
        self._states: dict[str, _ProxyState] = {c.proxy_id: _ProxyState(c) for c in self.configs}
        self._rotation = cycle(self._states.keys()) if self._states else None

    def acquire(self) -> ProxyConfig | None:
        """Return the next healthy proxy to use, or None (direct connection)
        if no proxies are configured or all are quarantined.
        """
        if not self._states:
            return None
        for _ in range(len(self._states)):
            proxy_id = next(self._rotation)
            state = self._states[proxy_id]
            if state.health != ProxyHealthState.QUARANTINED:
                return state.config
        return None

    def report_success(self, proxy_id: str) -> bool:
        """Record a success; returns True exactly when this call is the one
        that brought the proxy back to HEALTHY from UNHEALTHY/QUARANTINED
        (a real recovery), so the caller can emit a "proxy recovered" event
        once rather than on every already-healthy success.
        """
        state = self._states.get(proxy_id)
        if state is None:
            return False
        recovered = state.health != ProxyHealthState.HEALTHY
        state.consecutive_failures = 0
        state.health = ProxyHealthState.HEALTHY
        return recovered

    def report_failure(self, proxy_id: str) -> ProxyHealthState:
        """Record a failure; returns the proxy's health after this failure so
        the caller can emit an "important event" exactly on the transition
        into UNHEALTHY/QUARANTINED, not on every failure.
        """
        state = self._states.get(proxy_id)
        if state is None:
            return ProxyHealthState.HEALTHY
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            state.health = ProxyHealthState.QUARANTINED
        else:
            state.health = ProxyHealthState.UNHEALTHY
        return state.health

    def health_of(self, proxy_id: str) -> ProxyHealthState | None:
        state = self._states.get(proxy_id)
        return state.health if state else None

    def healthy_count(self) -> int:
        return sum(1 for s in self._states.values() if s.health != ProxyHealthState.QUARANTINED)

    def quarantined_count(self) -> int:
        return sum(1 for s in self._states.values() if s.health == ProxyHealthState.QUARANTINED)
