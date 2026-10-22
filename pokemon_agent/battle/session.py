"""Explicit per-battle mutable state; no ContextVars or implicit properties."""
from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BattleSession:
    """Mutable checkpoints used only by one battle's coordinator.

    This is intentionally not part of BattleContext: it contains execution
    state (prompt pinning, pending strategies, persistence cursors), not facts
    that should influence agent decisions directly.
    """
    battle_id: str
    decision_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    turn_history: deque[str] = field(default_factory=lambda: deque(maxlen=60))
    history_checkpoint: int = 0
    observation_checkpoint: int = 0
    strategy_by_turn: dict[int, str] = field(default_factory=dict)
    prompt_state: dict[str, Any] = field(default_factory=dict)
