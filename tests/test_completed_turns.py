"""Integration-level history checkpoint tests; require installed runtime packages."""
from types import SimpleNamespace
import pytest

pytest.importorskip('poke_env')
pytest.importorskip('psycopg')

from pokemon_agent.battle.history import HistoryTracker
from pokemon_agent.battle.session import BattleSession


class StubParser:
    """Simulate a replay that can report only turns whose outcomes are complete."""

    def _extract_turn_showdown_log(self, battle, turn):
        """Produce a completed replay log, otherwise signal incomplete data."""
        return f'Turn {turn}\nA Pokémon used a move!' if turn <= battle.complete_through else None


class StubExtractor:
    """Simulate an idempotent move observation per completed turn."""

    def _extract_turn_move_observations(self, battle, turn):
        """Return no observation until the turn has finished in the replay."""
        return [{'turn': turn}] if turn <= battle.complete_through else None


class StubRepository:
    """Capture DB insert requests without opening a database."""

    def __init__(self):
        """Remember all successfully persisted turn numbers."""
        self.inserted = []

    def _insert_move_observations(self, rows):
        """Record the turn as if the idempotent insert succeeded."""
        self.inserted.extend(row['turn'] for row in rows)
        return True


def test_only_completed_turns_enter_next_decision():
    """Turn N is absent until N+1, and the prior intent is appended afterwards."""
    repo = StubRepository()
    tracker = HistoryTracker(StubParser(), StubExtractor(), repo)
    session = BattleSession('battle-1')
    session.strategy_by_turn[2] = 'Keep Pikachu alive to win the endgame.'
    battle = SimpleNamespace(turn=2, complete_through=1)
    tracker.checkpoint(battle, session)
    assert session.history_checkpoint == 1
    assert session.observation_checkpoint == 1
    assert 'Turn 2' not in tracker.text(session)
    battle.turn = 3
    battle.complete_through = 2
    tracker.checkpoint(battle, session)
    assert 'AGENT\'S INTENDED STRATEGY FOR TURN 2' in tracker.text(session)
    assert session.strategy_by_turn == {}
    assert repo.inserted == [1, 2]


def test_final_turn_is_persisted_without_next_decision():
    """A game ending after turn 3 must still persist turn 3 observations."""
    repo = StubRepository()
    tracker = HistoryTracker(StubParser(), StubExtractor(), repo)
    session = BattleSession('battle-2')
    battle = SimpleNamespace(turn=3, complete_through=3)
    tracker.checkpoint(battle, session)
    assert repo.inserted == [1, 2]
    tracker.persist_final_turns(battle, session)
    assert repo.inserted == [1, 2, 3]
    assert session.observation_checkpoint == 3
