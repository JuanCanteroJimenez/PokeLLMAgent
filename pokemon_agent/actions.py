"""Convert a structured agent proposal into a legal poke-env order."""
from __future__ import annotations
from typing import Any
from .battle.models import AgentDecision
from .utils import normalize_name


class ActionResolver:
    """Validate against current Showdown options and preserve random fallback."""

    def __init__(self, player: Any, langfuse: Any):
        """Receive the poke-env Player API and tracing client."""
        self.player = player
        self.langfuse = langfuse

    def resolve(self, battle: Any, proposal: AgentDecision) -> tuple[Any, dict, str | None]:
        """Return the order, actual submitted action, and optional fallback reason.

        The action is resolved against the latest poke-env legal options, not
        inferred solely from the context. Invalid proposals become random moves.
        """
        if proposal.action == 'move':
            with self.langfuse.start_as_current_observation(
                as_type='span', name='resolve-move', input={'target': proposal.target}
            ) as span:
                target = normalize_name(proposal.target or '')
                move = next((m for m in battle.available_moves if m.id == target), None)
                if move is None:
                    move = next((m for m in battle.available_moves
                                 if m.name.lower() == (proposal.target or '').lower()), None)
                span.update(output={'found': move is not None, 'move': getattr(move, 'id', None)})
            if move is None:
                return self._fallback(battle, 'invalid_move')
            tera = bool(proposal.terastallize) and bool(battle.can_tera)
            return (self.player.create_order(move, terastallize=tera),
                    {'action': 'move', 'target': move.id, 'terastallize': tera}, None)

        if proposal.action == 'switch':
            with self.langfuse.start_as_current_observation(
                as_type='span', name='resolve-switch', input={'target': proposal.target}
            ) as span:
                target = normalize_name(proposal.target or '')
                pokemon = next((p for p in battle.available_switches
                                if normalize_name(p.species) == target), None)
                span.update(output={'found': pokemon is not None,
                                    'pokemon': getattr(pokemon, 'species', None)})
            if pokemon is None:
                return self._fallback(battle, 'invalid_switch')
            return (self.player.create_order(pokemon),
                    {'action': 'switch', 'target': pokemon.species, 'terastallize': False}, None)

        return self._fallback(battle, 'invalid_action_type')

    def _fallback(self, battle: Any, reason: str) -> tuple[Any, dict, str]:
        """Keep the original random recovery behavior when action resolution fails."""
        order = self.player.choose_random_move(battle)
        return (order, {'action': 'fallback_random',
                        'order_message': getattr(order, 'message', None)}, reason)
