"""Build an immutable, prompt-independent view of the current battle."""
from __future__ import annotations
from typing import Any
from poke_env.battle import Battle
from .history import HistoryTracker
from .models import (
    AgentDecision, BattleContext, BattleMetadata, BattleSnapshot, LegalActions,
    MatchupAnalytics, MoveOption, PokemonSnapshot, SpeedAnalysis, SwitchOption,
    TurnHistory,
)
from .session import BattleSession
from .speed import SpeedEstimator
from ..infrastructure.analytics import AnalyticsRepository, HistoricalAnalytics
from .observations import ObservationExtractor


class ContextBuilder:
    """Merge Showdown state, completed history, V3 evidence, and V4 Speed."""

    def __init__(self, history: HistoryTracker, repository: AnalyticsRepository,
                 extractor: ObservationExtractor):
        """Receive data services; store no mutable per-battle context on this object."""
        self.history = history
        self.repository = repository
        self.extractor = extractor

    @staticmethod
    def _pokemon(pokemon: Any) -> PokemonSnapshot | None:
        """Project observable Pokémon properties to the same fields as baseline V4."""
        if pokemon is None:
            return None
        return PokemonSnapshot(
            species=pokemon.species,
            types=[str(t) for t in pokemon.types if t is not None],
            hp_percentage=pokemon.current_hp_fraction * 100,
            status=pokemon.status.name if pokemon.status is not None else None,
            boosts=dict(pokemon.boosts),
        )

    @staticmethod
    def _switch(pokemon: Any) -> SwitchOption:
        """Capture an eligible switch with its health and status."""
        return SwitchOption(
            species=pokemon.species,
            hp_percentage=pokemon.current_hp_fraction * 100,
            status=pokemon.status.name if pokemon.status is not None else None,
        )

    @staticmethod
    def _move(move: Any) -> MoveOption:
        """Capture a move exposed by poke-env's list of available actions."""
        return MoveOption(
            id=move.id, type=str(move.type), base_power=move.base_power,
            accuracy=move.accuracy, current_pp=move.current_pp,
            max_pp=move.max_pp, category=move.category.name,
        )

    def build(self, battle: Battle, session: BattleSession) -> BattleContext:
        """Build the same information available to V4, before invoking any LLM.

        History and empirical observations are checkpointed through N-1;
        matchup evidence and estimated Speed are computed on the live N state.
        The resulting model can be JSON-serialized and given to any policy.
        """
        self.history.checkpoint(battle, session)
        our_pokemon = getattr(battle, 'active_pokemon', None)
        their_pokemon = getattr(battle, 'opponent_active_pokemon', None)
        ours = self._pokemon(our_pokemon)
        theirs = self._pokemon(their_pokemon)
        own_speed = SpeedEstimator._v4_speed_info(our_pokemon, battle, ours=True) if ours else None
        foe_speed = SpeedEstimator._v4_speed_info(their_pokemon, battle, ours=False) if theirs else None

        if own_speed is not None and foe_speed is not None:
            own_value = own_speed.get('known_effective_speed')
            low, high = foe_speed.get('known_effective_min'), foe_speed.get('known_effective_max')
            if own_value is None or low is None or high is None:
                comparison = 'UNKNOWN'
            elif own_value > high:
                comparison = 'OURS_FASTER_THAN_ENTIRE_MODELLED_RANGE'
            elif own_value < low:
                comparison = 'OURS_SLOWER_THAN_ENTIRE_MODELLED_RANGE'
            else:
                comparison = 'RANGES_OVERLAP_OR_TIE'
            own_speed['comparison_to_opponent'] = comparison
            own_speed['trick_room_active'] = SpeedEstimator._v4_contains(getattr(battle, 'fields', {}), 'trickroom')
            own_speed['comparison_caveat'] = (
                'Speed only; move priority, Trick Room, hidden items, '
                'unrevealed abilities and other effects can change turn order.'
            )

        if ours is not None:
            ours = ours.model_copy(update={'speed': own_speed})
        if theirs is not None:
            theirs = theirs.model_copy(update={'speed': foe_speed})

        summary, evidence = HistoricalAnalytics(self.repository, self.extractor).build(battle)
        return BattleContext(
            battle=BattleMetadata(
                battle_id=battle.battle_tag,
                turn=int(battle.turn or 0),
                generation=int(getattr(battle, 'gen', 9)),
                battle_format=getattr(battle, 'format', None),
            ),
            state=BattleSnapshot(
                active_pokemon=ours,
                opponent_active_pokemon=theirs,
                known_opponent_team=[self._pokemon(p) for p in battle.opponent_team.values()],
                weather=[str(x) for x in battle.weather],
                fields=[str(x) for x in battle.fields],
                side_conditions={str(k): v for k, v in battle.side_conditions.items()},
                opponent_side_conditions={str(k): v for k, v in battle.opponent_side_conditions.items()},
            ),
            legal_actions=LegalActions(
                moves=[self._move(m) for m in battle.available_moves],
                switches=[self._switch(p) for p in battle.available_switches],
                can_terastallize=bool(battle.can_tera),
            ),
            history=TurnHistory(
                completed_turns=tuple(session.turn_history),
                rendered=self.history.text(session),
            ),
            analytics=MatchupAnalytics(summary=summary, evidence=evidence),
            speed=SpeedAnalysis(ours=own_speed, opponent=foe_speed),
        )
