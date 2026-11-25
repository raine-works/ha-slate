"""Current limiter for the edge bar.

A PoE PD has a hard power budget (802.3af: 12.95 W at the PD input). A 48-LED
WS2812 bar at full white draws ~2.9 A at 5 V (~14 W) and would brown out the
whole panel. The limiter scales each frame so its *estimated* draw never exceeds
the configured budget. Estimation runs on post-gamma PWM duty, because that is
what the LED driver actually sinks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PowerBudget:
    """The bar's current budget and the per-LED figures its estimate uses (all in mA)."""

    max_current_ma: float = 600.0
    ma_per_channel: float = 20.0  # full-scale current of one colour channel
    quiescent_ma_per_led: float = 1.0  # driver IC idle draw, always present

    def estimate_ma(self, frame: list[tuple[int, int, int]]) -> float:
        """Estimated draw in mA of ``frame`` (post-gamma 0–255 RGB per LED), idle draw included."""
        duty = sum(r + g + b for r, g, b in frame) / 255.0
        return duty * self.ma_per_channel + self.quiescent_ma_per_led * len(frame)

    def limit(self, frame: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
        """Return ``frame`` scaled down uniformly to fit the budget, or unchanged if it already does.

        If even the idle draw exceeds the budget, the result is all off.
        """
        quiescent = self.quiescent_ma_per_led * len(frame)
        active = self.estimate_ma(frame) - quiescent
        headroom = self.max_current_ma - quiescent
        if active <= headroom or active <= 0:
            return frame
        k = max(headroom, 0.0) / active
        return [(int(r * k), int(g * k), int(b * k)) for r, g, b in frame]
