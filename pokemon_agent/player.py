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




class AgentBase(Player):
    """
    Baseline LLM Pokémon agent.

    Uses only the current battle state and previous selected action.

    Excludes:
    - Completed-turn history tracking
    - Historical matchup analytics
    - PostgreSQL persistence
    - V4 Speed estimation
    - V4 managed prompts and strategic memory

    Preserves:
    - Structured LLM decisions
    - Legal move/switch validation
    - Terastallization
    - Random fallback
    - Langfuse tracing and LLM callbacks
    """

    def __init__(
        self,
        model="gpt-6-luna",
        reasoning_effort="high",
        service_tier="default",
        temperature=None,
        *args,
        **kwargs,
    ):
        from typing import Literal
        from pydantic import create_model

        configure_file_logging()

        # -------------------------------
        # Langfuse
        # -------------------------------
        self.langfuse = get_client()
        self.langfuse_handler = CallbackHandler()

        # -------------------------------
        # LLM
        # -------------------------------
        self.llm = ChatOpenAI(
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            temperature=temperature,
        )

        # Baseline schema: no V4 strategy/rationale requirements.
        self._decision_schema = create_model(
            "BaselineMoveDecision",
            action=(Literal["move", "switch"], ...),
            target=(str, ...),
            terastallize=(bool, False),
        )

        # Reuse the repository's move/switch resolver.
        self.actions = ActionResolver(self, self.langfuse)

        # Keep state isolated across simultaneous battles.
        # This is NOT a completed-turn history tracker.
        self._battle_states = {}

        # Compatibility with the original AgentBase interface.
        self.battle_state = None
        self._current_battle_tag = None

        self._setup_agent()

        # Initialize poke-env only after dependencies exist.
        super().__init__(*args, **kwargs)

    def _setup_agent(self):
        """Optional extension point for baseline subclasses."""
        pass

    def update_battle_state(self, battle: Battle) -> dict:
        """
        Build the original baseline battle state.

        Reuse ContextBuilder's existing snapshot helpers without
        calling ContextBuilder.build(), which would introduce
        V4 history tracking, analytics and Speed estimation.
        """
        tag = battle.battle_tag
        previous = self._battle_states.get(tag, {})

        active = ContextBuilder._pokemon(battle.active_pokemon)
        opponent = ContextBuilder._pokemon(
            battle.opponent_active_pokemon
        )

        state = {
            "active_pokemon": (
                active.prompt_dict() if active else None
            ),
            "opponent_active_pokemon": (
                opponent.prompt_dict() if opponent else None
            ),
            "available_moves": [
                ContextBuilder._move(move).model_dump(mode="json")
                for move in battle.available_moves
            ],
            "available_switches": [
                ContextBuilder._switch(pokemon).model_dump(
                    mode="json"
                )
                for pokemon in battle.available_switches
            ],
            "known_opponent_team": [
                ContextBuilder._pokemon(pokemon).prompt_dict()
                for pokemon in battle.opponent_team.values()
            ],
            "weather": [
                str(weather) for weather in battle.weather
            ],
            "fields": [
                str(field) for field in battle.fields
            ],
            "side_conditions": {
                str(condition): value
                for condition, value
                in battle.side_conditions.items()
            },
            "opponent_side_conditions": {
                str(condition): value
                for condition, value
                in battle.opponent_side_conditions.items()
            },
            "can_terastallize": bool(battle.can_tera),
            "last_move": previous.get("last_move"),
        }

        self._battle_states[tag] = state
        self._current_battle_tag = tag
        self.battle_state = state

        return state

    async def decide_next_move(self, battle_state: dict) -> dict:
        """
        Original one-shot baseline decision policy.

        No replay history, analytics, empirical evidence,
        managed V4 prompts or strategy memory.
        """
        import json

        system_prompt = """
        You are a Pokémon battle agent.

        Choose exactly one legal action for the current turn.

        You must:
        - Consider both active Pokémon.
        - Consider HP, status, stat boosts and types.
        - Consider weather, terrain and side conditions.
        - Consider the currently known opponent team.
        - Only use moves listed in available_moves.
        - Only switch to Pokémon in available_switches.
        - Never invent moves or Pokémon.
        - Only Terastallize when it is permitted.

        If action is "move", target must be the move ID.

        If action is "switch", target must be the Pokémon
        species from available_switches.

        Return exactly one action.
        """.strip()

        battle_prompt = (
            "CURRENT BATTLE STATE\n\n"
            + json.dumps(
                battle_state,
                indent=2,
                ensure_ascii=False,
            )
            + "\n\nChoose the best next action."
        )

        decision_model = self.llm.with_structured_output(
            self._decision_schema
        )

        decision = await decision_model.ainvoke(
            [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": battle_prompt,
                },
            ],
            config={
                "callbacks": [self.langfuse_handler]
            },
        )

        return decision.model_dump(mode="json")

    async def choose_move(self, battle: Battle):
        """
        Execute one baseline decision with Langfuse tracing.

        Every decision includes:
        - Current battle state
        - LLM decision
        - Legal action resolution
        - Submitted order or fallback
        """
        agent_type = type(self).__name__
        agent_name = self.username
        tag = battle.battle_tag
        turn = int(battle.turn or 0)

        with self.langfuse.start_as_current_observation(
            as_type="agent",
            name=f"choose-move-{agent_type}-{agent_name}",
            input={
                "battle_tag": tag,
                "turn": turn,
                "agent": agent_name,
            },
        ) as agent_span:

            with propagate_attributes(
                session_id=tag,
                tags=[
                    "pokemon-showdown",
                    "poke-env",
                    f"agent-type:{agent_type}",
                    f"agent:{agent_name}",
                    "baseline",
                ],
                metadata={
                    "battle_tag": tag,
                    "turn": turn,
                    "agent_type": agent_type,
                    "agent_name": agent_name,
                },
            ):
                try:
                    # -------------------------------
                    # 1. Current battle state
                    # -------------------------------
                    with self.langfuse.start_as_current_observation(
                        as_type="span",
                        name="update-battle-state",
                    ) as span:

                        state = self.update_battle_state(battle)

                        span.update(
                            output=json_safe(state)
                        )

                    # -------------------------------
                    # 2. LLM decision
                    # -------------------------------
                    with self.langfuse.start_as_current_observation(
                        as_type="span",
                        name="decide-next-move",
                        input=json_safe(state),
                    ) as span:

                        result = await self.decide_next_move(state)

                        span.update(
                            output=json_safe(result)
                        )

                    # -------------------------------
                    # 3. Resolve legal action
                    # -------------------------------
                    data = (
                        result.model_dump()
                        if hasattr(result, "model_dump")
                        else result
                    )

                    proposal = self._decision_schema.model_validate(
                        data
                    )

                    # ActionResolver already provides:
                    # - resolve-move / resolve-switch spans
                    # - target normalization
                    # - legality validation
                    # - Terastallization checks
                    # - random fallback
                    order, submitted, fallback = (
                        self.actions.resolve(battle, proposal)
                    )

                    # Remember only the submitted action.
                    # No replay history is maintained.
                    state["last_move"] = submitted

                    # -------------------------------
                    # 4. Langfuse final output
                    # -------------------------------
                    agent_span.update(
                        output={
                            "proposed_action": json_safe(data),
                            "submitted_action": json_safe(submitted),
                            "fallback_reason": fallback,
                        }
                    )

                    agent_span.score(
                        name="random_fallback_used",
                        value=float(bool(fallback)),
                        data_type="BOOLEAN",
                    )

                    return order

                except Exception as exc:
                    # -------------------------------
                    # 5. Unexpected-error fallback
                    # -------------------------------
                    logger.exception(
                        "Baseline decision failed: "
                        "battle=%s turn=%s",
                        tag,
                        turn,
                    )

                    fallback = self.choose_random_move(battle)

                    if tag in self._battle_states:
                        self._battle_states[tag]["last_move"] = {
                            "action": "fallback_random"
                        }

                    agent_span.update(
                        output={
                            "action": "fallback_random",
                            "reason": "decision_exception",
                            "error": (
                                f"{type(exc).__name__}: {exc}"
                            ),
                        }
                    )

                    agent_span.score(
                        name="random_fallback_used",
                        value=1.0,
                        data_type="BOOLEAN",
                    )

                    return fallback

    def _battle_finished_callback(
        self, battle: Battle
    ) -> None:
        """
        Release baseline state after battle completion.

        No history persistence or PostgreSQL finalization.
        """
        if not isinstance(battle, Battle):
            return

        self._battle_states.pop(battle.battle_tag, None)

        if self._current_battle_tag == battle.battle_tag:
            self._current_battle_tag = None
            self.battle_state = None

    def close_resources(self) -> None:
        """Flush pending Langfuse observations at shutdown."""
        self.langfuse.flush()
