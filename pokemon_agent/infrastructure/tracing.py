"""Langfuse battle/turn lifecycle with durable PostgreSQL trace persistence.

Each decision has its own long-lived observation, ending only when its actual
Showdown outcome becomes available or when the battle finishes as unknown.
"""
from __future__ import annotations
from typing import Any

from poke_env.battle import Battle
from .trace_repository import BattleTraceRepository, BattleTracking, PendingTurn
from ..battle.history import HistoryTracker
from ..battle.session import BattleSession
from ..utils import json_safe, logger


class BattleTracing:
    """Own root traces, pending decisions, evidence scores, and turn outcomes."""

    def __init__(self, *, agent_id: str, client: Any, repository: BattleTraceRepository,
                 history: HistoryTracker):
        """Configure tracing once; allocate independent records per battle ID."""
        self.agent_id = agent_id
        self.client = client
        self.repository = repository
        self.history = history
        self.tracked: dict[str, BattleTracking] = {}

    def begin(self, battle: Battle, *, username: str, agent_type: str) -> BattleTracking:
        """Open one stable trace per battle and insert its durable root record."""
        battle_id = battle.battle_tag
        if battle_id in self.tracked:
            return self.tracked[battle_id]
        trace_id = self.client.create_trace_id(seed=f'pokemon:{self.agent_id}:{battle_id}')
        root = self.client.start_observation(
            trace_context={'trace_id': trace_id}, as_type='agent',
            name=f'battle-{agent_type}',
            input={'agent_id': self.agent_id, 'battle_id': battle_id,
                   'battle_format': getattr(battle, 'format', None)},
            metadata={'agent_id': self.agent_id, 'battle_id': battle_id,
                      'agent_name': username},
        )
        tracked = BattleTracking(battle_id=battle_id, root=root, trace_id=trace_id)
        self.tracked[battle_id] = tracked
        self.repository.begin_battle(
            agent_id=self.agent_id, battle_id=battle_id, trace_id=trace_id,
            agent_type=agent_type, agent_name=username,
            battle_format=getattr(battle, 'format', None),
        )
        return tracked

    def new_decision(self, tracked: BattleTracking, *, turn: int) -> PendingTurn:
        """Allocate an independent observation, including for repeated same-turn requests."""
        from uuid import uuid4
        index = tracked.decision_counts.get(turn, 0) + 1
        tracked.decision_counts[turn] = index
        span = tracked.root.start_observation(
            as_type='agent', name=f'turn-{turn}-decision-{index}',
            input={'agent_id': self.agent_id, 'battle_id': tracked.battle_id,
                   'turn': turn, 'decision_index': index},
        )
        pending = PendingTurn(turn=turn, decision_index=index,
                              decision_id=uuid4().hex, span=span)
        tracked.pending[pending.decision_id] = pending
        return pending

    @staticmethod
    def _actor_side(battle: Battle, actor: Any) -> str | None:
        """Determine whether a Showdown actor belongs to our side or the opponent."""
        if not isinstance(actor, str) or not getattr(battle, 'player_role', None):
            return None
        role = actor.split(':', 1)[0].strip()[:2]
        if role == battle.player_role:
            return 'agent'
        if role in ('p1', 'p2'):
            return 'opponent'
        return None

    def _turn_observed_outcome(self, battle: Battle, turn: int, *, final: bool = False) -> dict | None:
        """Summarize a real, completed replay turn; never invent unobserved results."""
        events = getattr(battle, '_replay_data', None)
        if not events:
            return None
        found = finished = False
        selected = []
        current_turn = None
        for raw in events:
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                continue
            typ = raw[1]
            if typ == 'turn' and len(raw) > 2:
                try:
                    current_turn = int(raw[2])
                except (ValueError, TypeError):
                    continue
                if current_turn == turn:
                    found = True
                elif found and current_turn > turn:
                    finished = True
                    break
                continue
            if found and current_turn == turn:
                if typ in ('win', 'tie'):
                    finished = True
                selected.append(raw)
        if not found or (not final and not finished) or not selected:
            return None
        ours_fainted: list[str] = []
        theirs_fainted: list[str] = []
        ours_moves: list[str] = []
        theirs_moves: list[str] = []
        switches: list[dict] = []
        for item in selected:
            typ = item[1]
            actor = item[2] if len(item) > 2 else None
            side = self._actor_side(battle, actor)
            if typ == 'faint' and side in ('agent', 'opponent'):
                (ours_fainted if side == 'agent' else theirs_fainted).append(actor)
            elif typ == 'move' and len(item) > 3 and side in ('agent', 'opponent'):
                (ours_moves if side == 'agent' else theirs_moves).append(str(item[3]))
            elif typ in ('switch', 'drag') and side:
                switches.append({'side': side, 'actor': actor, 'kind': typ,
                                 'details': item[3] if len(item) > 3 else None})
        return {
            'status': 'resolved', 'turn': turn,
            'our_fainted': bool(ours_fainted), 'opponent_fainted': bool(theirs_fainted),
            'our_fainted_actors': ours_fainted, 'opponent_fainted_actors': theirs_fainted,
            'our_observed_moves': ours_moves, 'opponent_observed_moves': theirs_moves,
            'switches': switches, 'event_count': len(selected),
            'outcome_source': 'poke-env Battle._replay_data',
        }

    @staticmethod
    def record_evidence_scores(span: Any, evidence: dict) -> None:
        """Emit the existing categorical evidence quality and numeric sample scores."""
        for direction in ('offense', 'defense'):
            item = evidence.get(direction)
            if not isinstance(item, dict):
                continue
            evidence_type = item.get('type', 'UNAVAILABLE')
            span.score(name=f'{direction}_evidence_type', value=evidence_type,
                       data_type='CATEGORICAL')
            if evidence_type in ('EXACT_MATCHUP', 'DEFENDER_TYPE_FALLBACK', 'NO_EVIDENCE'):
                span.score(name=f'{direction}_distinct_moves',
                           value=float(item.get('distinct_moves', 0)), data_type='NUMERIC')
                span.score(name=f'{direction}_observed_uses',
                           value=float(item.get('total_uses', 0)), data_type='NUMERIC')

    def save_decision(self, tracked: BattleTracking, pending: PendingTurn) -> None:
        """Persist one pending turn and its submitted action before returning the order."""
        self.repository.save_decision(
            agent_id=self.agent_id, battle_id=tracked.battle_id,
            pending=pending, trace_id=tracked.trace_id,
        )

    def finalize_completed_turns(self, battle: Battle, session: BattleSession,
                                 *, final: bool = False) -> None:
        """Resolve earlier decisions against replay evidence, or mark unknown at end."""
        tracked = self.tracked.get(battle.battle_tag)
        if tracked is None:
            return
        now_turn = getattr(battle, 'turn', None)
        for pending in sorted(tracked.pending.values(), key=lambda p: (p.turn, p.decision_index)):
            if pending.completed:
                continue
            if not final and (now_turn is None or pending.turn >= now_turn):
                continue
            outcome = self._turn_observed_outcome(battle, pending.turn, final=final)
            if outcome is None and not final:
                continue
            if outcome is None:
                outcome = {'status': 'unknown', 'turn': pending.turn,
                           'outcome_source': 'missing_or_incomplete_replay'}
            showdown_log = self.history.completed_log(battle, pending.turn, session)
            if showdown_log is not None:
                outcome['showdown_log'] = showdown_log
            pending.completed = True
            pending.span.update(
                output={'intention': json_safe(pending.intention),
                        'observed_outcome': json_safe(outcome)},
                metadata={'agent_id': self.agent_id, 'battle_id': battle.battle_tag,
                          'turn': pending.turn, 'decision_index': pending.decision_index,
                          'evidence': json_safe(pending.evidence),
                          'random_fallback': pending.fallback},
            )
            if outcome.get('status') == 'resolved':
                pending.span.score(name='our_pokemon_fainted_in_turn',
                                   value=float(outcome['our_fainted']), data_type='BOOLEAN')
                pending.span.score(name='opponent_pokemon_fainted_in_turn',
                                   value=float(outcome['opponent_fainted']), data_type='BOOLEAN')
            self.repository.save_outcome(
                agent_id=self.agent_id, battle_id=battle.battle_tag,
                pending=pending, outcome=outcome,
            )
            pending.span.end()
            tracked.all_turns.append({
                'turn': pending.turn, 'decision_index': pending.decision_index,
                'decision_id': pending.decision_id, 'intention': json_safe(pending.intention),
                'observed_outcome': json_safe(outcome),
            })

    def finish(self, battle: Battle, session: BattleSession) -> None:
        """Finish a battle root trace, score win/fallbacks, and persist final result."""
        tracked = self.tracked.get(battle.battle_tag)
        if tracked is None or tracked.finished:
            return
        try:
            self.finalize_completed_turns(battle, session, final=True)
            if getattr(battle, 'won', False):
                result = 'win'
            elif getattr(battle, 'lost', False):
                result = 'loss'
            else:
                result = 'tie' if getattr(battle, 'finished', False) else 'unknown'
            turns = sorted(tracked.all_turns,
                           key=lambda record: (record['turn'], record['decision_index']))
            n = len(tracked.pending)
            fallbacks = sum(1 for p in tracked.pending.values() if p.fallback)
            pct = 100.0 * fallbacks / n if n else 0.0
            prompt_versions = {}
            for decision in tracked.pending.values():
                for name, info in decision.intention.get('prompt_versions', {}).items():
                    key = (name, info.get('version'), info.get('source'))
                    prompt_versions[str(key)] = info
            tracked.root.update(
                output={'result': result, 'total_decisions': n,
                        'random_fallbacks': fallbacks, 'random_fallback_pct': pct,
                        'turns': turns, 'prompt_versions': list(prompt_versions.values())},
                metadata={'agent_id': self.agent_id, 'battle_id': battle.battle_tag,
                          'result': result, 'total_decisions': n,
                          'random_fallbacks': fallbacks,
                          'prompt_versions': list(prompt_versions.values())},
            )
            tracked.root.score(name='battle_result', value=result, data_type='CATEGORICAL')
            if result in ('win', 'loss'):
                tracked.root.score(name='battle_win', value=float(result == 'win'),
                                   data_type='BOOLEAN')
            tracked.root.score(name='random_fallback_percent', value=pct,
                               data_type='NUMERIC')
            self.repository.finish_battle(
                agent_id=self.agent_id, battle_id=battle.battle_tag,
                result=result, count=n, fallbacks=fallbacks,
                metadata={'turns': len(turns),
                          'prompt_versions': list(prompt_versions.values())},
            )
            tracked.finished = True
        except Exception:
            logger.exception('Failed to finalize battle trace %s', battle.battle_tag)
        finally:
            tracked.root.end()
            self.tracked.pop(battle.battle_tag, None)
