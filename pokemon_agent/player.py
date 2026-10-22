"""One thin poke-env adapter, composed from independent V4 services.

There are no version-to-version agent subclasses. Context construction,
policy selection, action validation, persistence and tracing are separable.
"""
from __future__ import annotations
import os
from uuid import uuid4
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from poke_env.battle import Battle
from poke_env.player import Player

from .actions import ActionResolver
from .battle.context import ContextBuilder
from .battle.history import HistoryTracker
from .battle.observations import ObservationExtractor
from .battle.protocol import ShowdownParser
from .battle.session import BattleSession
from .decision import DecisionAgent, V4Agent
from .infrastructure.analytics import AnalyticsRepository
from .infrastructure.tracing import BattleTracing
from .infrastructure.trace_repository import BattleTraceRepository
from .prompts import PokemonPromptManager
from .utils import configure_file_logging, json_safe, logger

load_dotenv()

DEFAULT_ANALYTICS_DSN = 'postgresql://pokemon:pokemon@localhost:5433/pokemon_analytics'


class V4Player(Player):
    """Run any `DecisionAgent` against Showdown using the unchanged V4 data stack."""

    def __init__(self, model: str | None = None, reasoning_effort: str = 'high',
                 service_tier: str = 'fast', temperature: float | None = None,
                 *, agent_id: str | None = None, analytics_db_url: str | None = None,
                 tracing_db_url: str | None = None, prompt_label: str | None = None,
                 prompt_versions: dict[str, int] | None = None,
                 prompt_strict: bool | None = None,
                 decision_agent: DecisionAgent | None = None, **kwargs: Any):
        """Assemble dependencies once, then allow poke-env to start its client.

        Set `decision_agent` for a future planning/tool-using architecture.
        The default uses the original V4 LLM and managed Langfuse prompt.
        """
        configure_file_logging()
        self.agent_id = agent_id or str(uuid4())
        db_dsn = analytics_db_url or os.getenv('POKEMON_ANALYTICS_DB_URL') or DEFAULT_ANALYTICS_DSN
        trace_dsn = (tracing_db_url or os.getenv('POKEMON_TRACING_DB_URL')
                     or os.getenv('POKEMON_ANALYTICS_DB_URL') or DEFAULT_ANALYTICS_DSN)
        self.sessions: dict[str, BattleSession] = {}
        self.langfuse = get_client()
        self.langfuse_handler = CallbackHandler()
        self.prompts = PokemonPromptManager(
            self.langfuse, label=prompt_label, versions=prompt_versions, strict=prompt_strict,
        )
        self.llm = ChatOpenAI(
            model=model, service_tier=service_tier, temperature=temperature,
            use_responses_api=True, output_version='responses/v1',
            reasoning={'effort': reasoning_effort, 'summary': 'auto'},
        )
        self.analytics = AnalyticsRepository(db_dsn)
        self.parser = ShowdownParser()
        self.observations = ObservationExtractor(self.parser)
        self.history = HistoryTracker(self.parser, self.observations, self.analytics)
        self.context_builder = ContextBuilder(self.history, self.analytics, self.observations)
        self._trace_repo = BattleTraceRepository(trace_dsn)
        self.tracing = BattleTracing(
            agent_id=self.agent_id, client=self.langfuse,
            repository=self._trace_repo, history=self.history,
        )
        self._tracked_battles = self.tracing.tracked  # compatibility for ladder monitor
        self.decision_agent = decision_agent or V4Agent(
            self.llm, self.prompts, callbacks=[self.langfuse_handler],
        )
        self.actions = ActionResolver(self, self.langfuse)
        super().__init__(**kwargs)

    def _session(self, battle: Battle) -> BattleSession:
        """Create or reuse a battle-ID-keyed session; never mix separate battles."""
        tag = battle.battle_tag
        if tag not in self.sessions:
            self.sessions[tag] = BattleSession(battle_id=tag)
        return self.sessions[tag]

    async def choose_move(self, battle: Battle):
        """Build context, obtain an action, validate it, and persist the decision.

        The observation for this call remains pending after the order returns.
        It is resolved using actual replay events at the next completed turn.
        A failure anywhere in the pipeline uses the V4 random fallback.
        """
        session = self._session(battle)
        # Multiple battles have independent sessions, while reentrant
        # requests for the SAME battle are serialized to avoid state races.
        async with session.decision_lock:
            tracked = self.tracing.begin(
                battle, username=self.username, agent_type=self.__class__.__name__,
            )
            turn = int(battle.turn or 0)
            pending = self.tracing.new_decision(tracked, turn=turn)
            session.prompt_state['_prompt_usage_current_decision'] = {}
            with pending.span.start_as_current_observation(
                as_type='span', name='execute-turn-decision',
            ):
                try:
                    with propagate_attributes(
                        user_id=self.agent_id, session_id=battle.battle_tag,
                        tags=['pokemon-showdown', 'poke-env',
                              f'agent-type:{self.__class__.__name__}'],
                        metadata={'agent_id': self.agent_id,
                                  'battle_id': battle.battle_tag,
                                  'turn': turn, 'decision_index': pending.decision_index},
                    ):
                        with self.langfuse.start_as_current_observation(
                            as_type='span', name='update-battle-state',
                        ) as span:
                            context = self.context_builder.build(battle, session)
                            span.update(output=json_safe(context.to_v4_prompt_state()))
                        self.tracing.finalize_completed_turns(battle, session)
                        pending.context = json_safe(context.to_v4_prompt_state())
                        pending.evidence = json_safe(context.analytics.evidence)
                        with self.langfuse.start_as_current_observation(
                            as_type='span', name='decide-next-move',
                            input=pending.context,
                        ) as span:
                            decision = await self.decision_agent.decide(context, session)
                            span.update(output=json_safe(decision))
                        pending.intention = {
                            'proposed_action': {
                                'action': decision.action,
                                'target': decision.target,
                                'terastallize': decision.terastallize,
                            },
                            'rationale': session.strategy_by_turn.get(turn),
                            'prompt_versions': json_safe(
                                session.prompt_state.get('_prompt_usage_current_decision', {})
                            ),
                        }
                        order, submitted, fallback = self.actions.resolve(battle, decision)
                        pending.intention['submitted_action'] = submitted
                        pending.fallback = bool(fallback)
                        pending.fallback_reason = fallback
                        self.tracing.record_evidence_scores(pending.span, pending.evidence)
                        pending.span.score(name='random_fallback_used',
                                           value=float(pending.fallback), data_type='BOOLEAN')
                        if fallback:
                            pending.span.score(name='fallback_reason', value=fallback,
                                               data_type='CATEGORICAL')
                        self.tracing.save_decision(tracked, pending)
                        logger.debug('Battle=%s turn=%s context=%s', battle.battle_tag,
                                     turn, pending.context)
                        return order
                except Exception as exc:
                    logger.exception('Decision failed for battle=%s turn=%s',
                                     battle.battle_tag, turn)
                    pending.fallback = True
                    pending.fallback_reason = 'decision_exception'
                    pending.intention = {
                        'error': f'{type(exc).__name__}: {exc}',
                        'submitted_action': {'action': 'fallback_random'},
                        'prompt_versions': json_safe(
                            session.prompt_state.get('_prompt_usage_current_decision', {})
                        ),
                    }
                    pending.span.score(name='random_fallback_used', value=1.0,
                                       data_type='BOOLEAN')
                    pending.span.score(name='fallback_reason', value='decision_exception',
                                       data_type='CATEGORICAL')
                    self.tracing.save_decision(tracked, pending)
                    return self.choose_random_move(battle)

    def _battle_finished_callback(self, battle: Battle) -> None:
        """Flush the final observations, resolve pending turns, and release state."""
        if not isinstance(battle, Battle):
            return
        session = self.sessions.get(battle.battle_tag)
        if session is None:
            return
        try:
            self.history.persist_final_turns(battle, session)
        except Exception:
            logger.exception('Error while persisting final battle observations.')
        try:
            self.tracing.finish(battle, session)
        finally:
            self.sessions.pop(battle.battle_tag, None)

    def close_resources(self) -> None:
        """Close PostgreSQL connections and flush Langfuse at process shutdown."""
        self.analytics.close()
        self._trace_repo.close()
        self.langfuse.flush()
