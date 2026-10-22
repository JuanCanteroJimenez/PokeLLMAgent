"""Completed replay checkpointing shared by all decision policies."""
from __future__ import annotations
from poke_env.battle import Battle
from .observations import ObservationExtractor
from .protocol import ShowdownParser
from .session import BattleSession
from ..infrastructure.analytics import AnalyticsRepository
from ..utils import logger


class HistoryTracker:
    """Advance history and DB checkpoints independently and only when complete."""

    def __init__(self, parser: ShowdownParser, observations: ObservationExtractor,
                 repository: AnalyticsRepository):
        """Receive stateless parsing services and the existing analytics repository."""
        self.parser = parser
        self.observations = observations
        self.repository = repository

    def extract_turn_log(self, battle: Battle, turn: int, session: BattleSession) -> str | None:
        """Add the prior strategy *only after* Showdown's turn is complete.

        The log contains actual events and the separately labelled intent.
        Intent cannot leak into the context for the turn it was chosen on.
        """
        log = self.parser._extract_turn_showdown_log(battle, turn)
        if log is None:
            return None
        strategy = session.strategy_by_turn.pop(turn, None)
        if strategy:
            log += (f"\n\nAGENT'S INTENDED STRATEGY FOR TURN {turn} "
                    "(chosen before the turn; the Showdown log above is what actually happened):\n"
                    f"{strategy}")
        return log

    def checkpoint(self, battle: Battle, session: BattleSession) -> None:
        """Record turns up to N-1, stopping if a log or DB write is unavailable.

        The original V4 independently advanced replay-history and analytics
        cursors, retrying failed database writes on a subsequent request.
        """
        current = getattr(battle, "turn", None)
        if current is None or current <= 1:
            return
        for turn in range(session.history_checkpoint + 1, current):
            log = self.extract_turn_log(battle, turn, session)
            if log is None:
                break
            session.turn_history.append(log)
            session.history_checkpoint = turn
        for turn in range(session.observation_checkpoint + 1, current):
            observations = self.observations._extract_turn_move_observations(battle, turn)
            if observations is None or not self.repository._insert_move_observations(observations):
                break
            session.observation_checkpoint = turn

    def persist_final_turns(self, battle: Battle, session: BattleSession) -> None:
        """Flush completed empirical observations, including the last battle turn."""
        final_turn = getattr(battle, "turn", None)
        if final_turn is None:
            return
        for turn in range(session.observation_checkpoint + 1, final_turn + 1):
            observations = self.observations._extract_turn_move_observations(battle, turn)
            if observations is None or not self.repository._insert_move_observations(observations):
                break
            session.observation_checkpoint = turn

    @staticmethod
    def text(session: BattleSession, limit: int = 50) -> str:
        """Return up to 50 completed replay turns, most recent first, as V4 did."""
        selected = list(session.turn_history)[-limit:]
        return "\n\n".join(reversed(selected)) if selected else "No completed turns yet."

    def completed_log(self, battle: Battle, turn: int, session: BattleSession) -> str | None:
        """Retrieve a checkpointed log, or reconstruct one for trace finalization."""
        prefix = f"Turn {turn}\n"
        for entry in session.turn_history:
            if entry.startswith(prefix):
                return entry
        return self.extract_turn_log(battle, turn, session)
