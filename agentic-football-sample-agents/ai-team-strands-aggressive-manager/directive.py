"""StrategyDirective — the parameterized policy artifact the GK-manager issues.

The GK runs a slow, off-the-critical-path "manager" LLM that reads the match
trajectory and emits one of these compact directives. It is disseminated to the
outfield agents over A2A (see strategy_channel.py) and:

  1. injected into each outfield agent's per-tick LLM prompt as tactical guidance
     (the fast, directive-guided reactive tier), and
  2. used to re-tune the deterministic FallbackConfig so the rule-based safety
     net follows the same plan when the LLM is unavailable.

Keeping the artifact small and flat is deliberate: it must serialize to a tiny
payload (fast A2A hops) and be trivial for a small model to honour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, replace
from typing import Optional

from fallback import FallbackConfig


# Allowed enum-ish values — the manager LLM is constrained to these.
RISK_LEVELS = ("low", "medium", "high", "all_out")
TEMPO_LEVELS = ("slow", "balanced", "fast")
FOCUS_SIDES = ("left", "center", "right")


@dataclass
class StrategyDirective:
    """A compact, team-wide tactical plan.

    Fields are intentionally coarse so a small per-tick model can follow them
    and so the whole thing fits in a few hundred bytes on the wire.
    """

    # Overall stance the whole team should bias toward: 0=Balanced,1=Attack,2=Defend
    stance: int = 1
    # How hard to press the ball carrier, 0.0-1.0
    press_intensity: float = 0.85
    # Defensive/attacking line height as a fraction toward the opponent goal, 0.0-1.0.
    # 0.0 = sit on own goal line, 1.0 = camp on the opponent goal.
    line_height: float = 0.7
    # Opponent player id the team should prioritise marking, or -1 for "nobody specific".
    mark_target_id: int = -1
    # Appetite for risky actions (through balls, slide tackles, keeper pushing up).
    risk: str = "high"
    # How quickly to move the ball forward.
    tempo: str = "fast"
    # Which channel to attack through.
    focus_side: str = "center"
    # Free-text one-liner from the manager, surfaced verbatim to the players.
    notes: str = ""
    # Monotonic version stamp set by the manager so agents can tell staleness.
    version: int = 0

    # ---- serialization -------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyDirective":
        """Build a directive from a (possibly partial / untrusted) dict.

        Unknown keys are ignored and out-of-range values are clamped so a noisy
        LLM response can never produce an invalid directive.
        """
        base = cls()
        stance = _as_int(data.get("stance"), base.stance)
        return cls(
            stance=stance if stance in (0, 1, 2) else base.stance,
            press_intensity=_clamp01(_as_float(data.get("press_intensity"), base.press_intensity)),
            line_height=_clamp01(_as_float(data.get("line_height"), base.line_height)),
            mark_target_id=_as_int(data.get("mark_target_id"), base.mark_target_id),
            risk=_pick(str(data.get("risk", base.risk)).lower(), RISK_LEVELS, base.risk),
            tempo=_pick(str(data.get("tempo", base.tempo)).lower(), TEMPO_LEVELS, base.tempo),
            focus_side=_pick(str(data.get("focus_side", base.focus_side)).lower(), FOCUS_SIDES, base.focus_side),
            notes=str(data.get("notes", base.notes))[:200],
            version=_as_int(data.get("version"), base.version),
        )

    @classmethod
    def from_json(cls, text: str) -> "StrategyDirective":
        return cls.from_dict(json.loads(text))

    # ---- rendering for the reactive-tier LLM prompt --------------------

    def as_prompt_block(self) -> str:
        """Render the directive as a guidance block for a player's LLM prompt."""
        mark = "none" if self.mark_target_id < 0 else f"opponent P{self.mark_target_id}"
        stance_name = {0: "Balanced", 1: "Attack", 2: "Defend"}.get(self.stance, "Attack")
        lines = [
            "## MANAGER'S ORDERS (from the goalkeeper-manager — follow these this tick)",
            f"- Team stance: {stance_name}",
            f"- Risk appetite: {self.risk} | Tempo: {self.tempo} | Attack focus: {self.focus_side}",
            f"- Press intensity: {self.press_intensity:.2f} | Line height: {self.line_height:.2f} (0=deep, 1=high)",
            f"- Priority mark: {mark}",
        ]
        if self.notes:
            lines.append(f"- Coach note: {self.notes}")
        lines.append("Bias your single command toward these orders unless an obvious shot or clearance is available.")
        return "\n".join(lines)

    # ---- tuning the deterministic fallback -----------------------------

    def tune_config(self, cfg: FallbackConfig) -> FallbackConfig:
        """Return a copy of `cfg` re-tuned to follow this directive.

        Used so the rule-based safety net (build_fallback) obeys the same plan
        the LLM is being told to follow.
        """
        # Press harder / softer per the directive.
        press_intensity = self.press_intensity
        # Line height nudges how high the default position sits. For refs anchored on
        # my_goal, a higher line means a larger x_factor away from own goal.
        default_x_factor = cfg.default_x_factor
        if cfg.default_x_ref == "my_goal":
            # 0.0 line -> hug own goal (factor ~0.95); 1.0 line -> push out (factor ~0.35)
            default_x_factor = 0.95 - 0.6 * self.line_height
        elif cfg.default_x_ref == "opp_goal":
            # higher line -> get closer to opp goal (smaller factor toward opp goal)
            default_x_factor = max(0.2, 0.7 - 0.4 * self.line_height)

        risk_shoot = {"low": 20.0, "medium": 25.0, "high": 32.0, "all_out": 40.0}
        shoot_threshold = risk_shoot.get(self.risk, cfg.shoot_threshold)

        mark_threshold = cfg.mark_threshold
        if self.mark_target_id >= 0 and cfg.mark_threshold > 0:
            mark_threshold = max(cfg.mark_threshold, 45.0)  # widen so the mark actually triggers

        return replace(
            cfg,
            default_stance=self.stance,
            press_intensity=press_intensity,
            default_x_factor=default_x_factor,
            shoot_threshold=shoot_threshold,
            mark_threshold=mark_threshold,
        )


# Default directive used before the first manager broadcast arrives — mirrors the
# extremely-aggressive personality so the team behaves correctly from tick 0.
DEFAULT_DIRECTIVE = StrategyDirective()


# ---------------------------------------------------------------------------
# small coercion helpers — never raise on bad LLM output
# ---------------------------------------------------------------------------

def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _pick(v: str, allowed: tuple, default: str) -> str:
    return v if v in allowed else default
