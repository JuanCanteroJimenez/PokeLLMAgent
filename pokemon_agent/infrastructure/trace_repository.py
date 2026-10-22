"""Battle/turn trace SQL repository. Retains the existing table names and schemas."""
from __future__ import annotations
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
import psycopg
from psycopg.types.json import Jsonb
from ..utils import json_safe as _json_safe, logger


@dataclass
class PendingTurn:
    """One decision waiting for a real Showdown outcome."""
    turn: int
    decision_index: int
    decision_id: str
    span: Any
    context: dict = field(default_factory=dict)
    intention: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    fallback: bool = False
    fallback_reason: str | None = None
    completed: bool = False


@dataclass
class BattleTracking:
    """Stable battle root span and its potentially multiple decisions per turn."""
    battle_id: str
    root: Any
    trace_id: str
    pending: dict[str, PendingTurn] = field(default_factory=dict)
    decision_counts: dict[int, int] = field(default_factory=dict)
    all_turns: list[dict] = field(default_factory=list)
    finished: bool = False


class BattleTraceRepository:
    """Persist completed/pending battle trace rows without modifying observation schema."""
    def __init__(self, dsn: str):
        """Init."""
        self.dsn = dsn
        self._schema_ok = False
        self._schema_lock = Lock()  # protects the shared psycopg connection
        self._connection = None

    def _run(self, operation):
        """Run."""
        try:
            # psycopg's synchronous connection is reused under a lock. This
            # avoids opening a TCP connection on every decision and is safe
            # if callbacks run in different threads in the future.
            with self._schema_lock:
                if self._connection is None or self._connection.closed:
                    self._connection = psycopg.connect(self.dsn, autocommit=True)
                    self._schema_ok = False
                connection = self._connection
                if not self._schema_ok:
                    with connection.cursor() as cursor:
                        cursor.execute('''
                                    CREATE TABLE IF NOT EXISTS agent_battles (
                                      agent_id TEXT NOT NULL,
                                      battle_id TEXT NOT NULL,
                                      trace_id TEXT NOT NULL,
                                      agent_type TEXT NOT NULL,
                                      agent_name TEXT NOT NULL,
                                      battle_format TEXT,
                                      status TEXT NOT NULL DEFAULT 'in_progress',
                                      result TEXT,
                                      total_decisions INTEGER NOT NULL DEFAULT 0,
                                      random_fallbacks INTEGER NOT NULL DEFAULT 0,
                                      started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                      finished_at TIMESTAMPTZ,
                                      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                                      PRIMARY KEY (agent_id, battle_id)
                                    )
                                ''')
                        cursor.execute('''
                                    CREATE TABLE IF NOT EXISTS agent_turns (
                                      agent_id TEXT NOT NULL,
                                      battle_id TEXT NOT NULL,
                                      turn INTEGER NOT NULL,
                                      decision_index INTEGER NOT NULL,
                                      decision_id TEXT NOT NULL,
                                      trace_id TEXT NOT NULL,
                                      observation_id TEXT,
                                      context JSONB NOT NULL DEFAULT '{}'::jsonb,
                                      intention JSONB NOT NULL DEFAULT '{}'::jsonb,
                                      evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                                      observed_outcome JSONB,
                                      random_fallback BOOLEAN NOT NULL DEFAULT FALSE,
                                      fallback_reason TEXT,
                                      status TEXT NOT NULL DEFAULT 'pending',
                                      decision_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                      resolved_at TIMESTAMPTZ,
                                      PRIMARY KEY (agent_id, battle_id, turn, decision_index),
                                      FOREIGN KEY (agent_id, battle_id)
                                        REFERENCES agent_battles(agent_id, battle_id)
                                    )
                                ''')
                        cursor.execute('''
                                  CREATE INDEX IF NOT EXISTS idx_agent_turns_battle_order
                                  ON agent_turns (agent_id, battle_id, turn, decision_index)
                                ''')
                        cursor.execute('''
                                  CREATE INDEX IF NOT EXISTS idx_agent_turns_evidence
                                  ON agent_turns USING gin (evidence)
                                ''')
                    self._schema_ok = True
                with connection.cursor() as cursor:
                    return operation(cursor)
        except Exception:
            logger.exception('Could not persist agent battle/turn trace to PostgreSQL')
            # Force a clean connection and schema check on the next attempt.
            with self._schema_lock:
                if self._connection is not None:
                    try:
                        self._connection.close()
                    except Exception:
                        pass
                self._connection = None
                self._schema_ok = False
            return None

    def close(self):
        """Close."""
        with self._schema_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._schema_ok = False

    def begin_battle(self, *, agent_id, battle_id, trace_id, agent_type, agent_name, battle_format):
        """Begin battle."""
        def execute(c):
            """Execute this repository operation using the supplied SQL cursor."""
            c.execute('''
                INSERT INTO agent_battles
                  (agent_id, battle_id, trace_id, agent_type, agent_name, battle_format)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, battle_id) DO NOTHING
            ''', (agent_id, battle_id, trace_id, agent_type, agent_name, battle_format))
        self._run(execute)

    def save_decision(self, *, agent_id, battle_id, pending, trace_id):
        """Save decision."""
        def execute(c):
            """Execute this repository operation using the supplied SQL cursor."""
            c.execute('''
                INSERT INTO agent_turns (
                  agent_id, battle_id, turn, decision_index, decision_id,
                  trace_id, observation_id, context, intention, evidence,
                  random_fallback, fallback_reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (agent_id, battle_id, turn, decision_index)
                DO UPDATE SET
                  context=EXCLUDED.context,
                  intention=EXCLUDED.intention,
                  evidence=EXCLUDED.evidence,
                  random_fallback=EXCLUDED.random_fallback,
                  fallback_reason=EXCLUDED.fallback_reason
            ''', (
                agent_id, battle_id, pending.turn, pending.decision_index,
                pending.decision_id, trace_id, pending.span.id,
                Jsonb(_json_safe(pending.context)), Jsonb(_json_safe(pending.intention)),
                Jsonb(_json_safe(pending.evidence)), pending.fallback,
                pending.fallback_reason,
            ))
        self._run(execute)

    def save_outcome(self, *, agent_id, battle_id, pending, outcome):
        """Save outcome."""
        def execute(c):
            """Execute this repository operation using the supplied SQL cursor."""
            c.execute('''
                UPDATE agent_turns
                SET observed_outcome = %s, status = %s, resolved_at = NOW()
                WHERE agent_id=%s AND battle_id=%s
                  AND turn=%s AND decision_index=%s
            ''', (
                Jsonb(_json_safe(outcome)), outcome.get('status', 'resolved'),
                agent_id, battle_id, pending.turn, pending.decision_index,
            ))
        self._run(execute)

    def finish_battle(self, *, agent_id, battle_id, result, count, fallbacks, metadata):
        """Finish battle."""
        def execute(c):
            """Execute this repository operation using the supplied SQL cursor."""
            c.execute('''
                UPDATE agent_battles
                SET status='finished', result=%s, total_decisions=%s,
                    random_fallbacks=%s, finished_at=NOW(), metadata=%s
                WHERE agent_id=%s AND battle_id=%s
            ''', (result, count, fallbacks, Jsonb(_json_safe(metadata)), agent_id, battle_id))
        self._run(execute)

