"""Typed, prompt-independent V4 battle information.

`BattleContext` is the public contract between the battle runtime and any
future agent. `to_v4_prompt_state()` is the only compatibility bridge to the
original V4 Langfuse prompt; agents never manipulate internal poke-env objects.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    """Reject attribute reassignment after constructing one context snapshot."""
    model_config = ConfigDict(frozen=True)


class PokemonSnapshot(FrozenModel):
    """Public or privately known fields for one currently observed Pokémon."""
    species: str
    types: list[str]
    hp_percentage: float
    status: str | None
    boosts: dict[str, int]
    speed: dict[str, Any] | None = None

    def prompt_dict(self) -> dict[str, Any]:
        """Preserve the pre-refactor Pokémon shape, without inserting null Speed."""
        result = self.model_dump(mode="json")
        if self.speed is None:
            result.pop("speed")
        return result


class MoveOption(FrozenModel):
    """A move that Showdown currently permits the player to submit."""
    id: str
    type: str
    base_power: int
    accuracy: float
    current_pp: int
    max_pp: int
    category: str


class SwitchOption(FrozenModel):
    """An available bench Pokémon and its currently known status."""
    species: str
    hp_percentage: float
    status: str | None


class BattleMetadata(FrozenModel):
    """Stable battle identity and the turn being decided."""
    battle_id: str
    turn: int
    generation: int
    battle_format: str | None = None


class BattleSnapshot(FrozenModel):
    """Current observable field and team state, independent of prompting."""
    active_pokemon: PokemonSnapshot | None
    opponent_active_pokemon: PokemonSnapshot | None
    known_opponent_team: list[PokemonSnapshot]
    weather: list[str]
    fields: list[str]
    side_conditions: dict[str, int]
    opponent_side_conditions: dict[str, int]


class LegalActions(FrozenModel):
    """Authoritative moves, switches, and tera eligibility for this request."""
    moves: list[MoveOption]
    switches: list[SwitchOption]
    can_terastallize: bool


class TurnHistory(FrozenModel):
    """Only completed replay turns; stored chronologically, rendered newest first."""
    completed_turns: tuple[str, ...]
    rendered: str


class MatchupAnalytics(FrozenModel):
    """Historical empirical evidence separate from its legacy text rendering."""
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SpeedAnalysis(FrozenModel):
    """V4 speed estimates and limits of knowledge; never implies turn priority."""
    ours: dict[str, Any] | None = None
    opponent: dict[str, Any] | None = None


class BattleContext(FrozenModel):
    """Complete, serializable input contract for V4 and future decision agents."""
    battle: BattleMetadata
    state: BattleSnapshot
    legal_actions: LegalActions
    history: TurnHistory
    analytics: MatchupAnalytics
    speed: SpeedAnalysis

    def to_v4_prompt_state(self) -> dict[str, Any]:
        """Project the typed context to the *exact field structure* V4 expects.

        No battle ID, strategy objects, trace records, or structured analytics
        are silently added to the old prompt. Formatting changes belong in
        a future decision policy, not in the context builder.
        """
        s = self.state
        return {
            "active_pokemon": s.active_pokemon.prompt_dict() if s.active_pokemon else None,
            "opponent_active_pokemon": s.opponent_active_pokemon.prompt_dict() if s.opponent_active_pokemon else None,
            "available_moves": [move.model_dump(mode="json") for move in self.legal_actions.moves],
            "available_switches": [switch.model_dump(mode="json") for switch in self.legal_actions.switches],
            "weather": s.weather,
            "fields": s.fields,
            "side_conditions": s.side_conditions,
            "opponent_side_conditions": s.opponent_side_conditions,
            "known_opponent_team": [pokemon.prompt_dict() for pokemon in s.known_opponent_team],
            "last_move": self.history.rendered,
            "historical_analytics": self.analytics.summary,
        }


class AgentDecision(FrozenModel):
    """The existing structured V3.5/V4 response contract, including strategy."""
    action: Literal["move", "switch"]
    rationale: str = Field(
        min_length=20,
        description=(
            "In 2-5 concise sentences explain the OVERALL STRATEGY, not just "
            "why this individual move is good. Identify the main win condition "
            "or defensive objective, how this turn's action advances the plan, "
            "what you expect next, and when you would revise the plan. "
            "Clearly distinguish observations from assumptions."
        ),
    )
    target: str
    terastallize: bool = False
