"""The V4 policy: one context in, one structured action out.

This file intentionally contains no Showdown, database, history, or trace code.
A new policy can implement the same `decide` interface without changing runtime.
"""
from __future__ import annotations
from typing import Protocol
import json
from .battle.models import AgentDecision, BattleContext
from .battle.session import BattleSession
from .prompts import PokemonPromptManager


class DecisionAgent(Protocol):
    """Interface for any future agent architecture (one-shot, tools, planning)."""

    async def decide(self, context: BattleContext, session: BattleSession) -> AgentDecision:
        """Choose exactly one intended action from the given typed context."""
        ...


class V4Agent:
    """Keep the original V4 prompt, model settings, and strategic response schema."""

    def __init__(self, llm, prompts: PokemonPromptManager, callbacks: list):
        """Inject the configured model, managed prompts and Langfuse callback."""
        self.llm = llm
        self.prompts = prompts
        self.callbacks = callbacks

    @staticmethod
    def prompt_variables(context: BattleContext) -> dict[str, str]:
        """Render V4 fields only here, never while building battle information."""
        state = context.to_v4_prompt_state()
        variables = {key: str(value) for key, value in state.items()}
        variables['battle_state_json'] = json.dumps(state, indent=2, ensure_ascii=False)
        return variables

    async def decide(self, context: BattleContext, session: BattleSession) -> AgentDecision:
        """Invoke the original Langfuse V4 prompt with structured V3.5 output.

        Only the high-level strategy is saved for next turn; actions are never
        inserted into replay history before Showdown confirms them.
        """
        model = self.llm.with_structured_output(AgentDecision)
        decision = await self.prompts.structured(
            'pokemon/v4/decision', self.prompt_variables(context),
            session.prompt_state, model, callbacks=self.callbacks,
        )
        if context.battle.turn is not None:
            session.strategy_by_turn[context.battle.turn] = decision.rationale.strip()
        return decision
