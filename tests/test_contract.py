"""No external service needed: test the policy/context boundary."""
import asyncio
import json
from pokemon_agent.battle.models import (
    AgentDecision, BattleContext, BattleMetadata, BattleSnapshot,
    LegalActions, MatchupAnalytics, MoveOption, PokemonSnapshot,
    SpeedAnalysis, SwitchOption, TurnHistory,
)
from pokemon_agent.battle.session import BattleSession
from pokemon_agent.decision import V4Agent


def make_context() -> BattleContext:
    """Construct a representative V4 input without requiring poke-env."""
    ours = PokemonSnapshot(species='Pikachu', types=['ELECTRIC'],
                           hp_percentage=84, status=None, boosts={},
                           speed={'raw_exact': 200})
    opponent = PokemonSnapshot(species='Garchomp', types=['DRAGON', 'GROUND'],
                               hp_percentage=62, status=None, boosts={},
                               speed={'raw_min': 100, 'raw_max': 220})
    return BattleContext(
        battle=BattleMetadata(battle_id='battle-1', turn=7,
                              generation=9, battle_format='gen9randombattle'),
        state=BattleSnapshot(active_pokemon=ours, opponent_active_pokemon=opponent,
                             known_opponent_team=[opponent],
                             weather=['Weather.RAINDANCE'], fields=[],
                             side_conditions={}, opponent_side_conditions={}),
        legal_actions=LegalActions(moves=[MoveOption(id='thunderbolt',
                               type='ELECTRIC', base_power=90, accuracy=1.0,
                               current_pp=10, max_pp=15, category='SPECIAL')],
                               switches=[SwitchOption(species='Ditto',
                                      hp_percentage=80, status=None)], can_terastallize=True),
        history=TurnHistory(completed_turns=('Turn 5\n...', 'Turn 6\n...'),
                            rendered='Turn 6\n...\n\nTurn 5\n...'),
        analytics=MatchupAnalytics(summary='No empirical evidence.',
                    evidence={'offense': {'type': 'NO_EVIDENCE'}}),
        speed=SpeedAnalysis(ours={'raw_exact': 200},
                            opponent={'raw_min': 100, 'raw_max': 220}),
    )


def test_v4_payload_contract_has_only_legacy_keys():
    """New structured sections must not be smuggled into V4's prompt."""
    payload = make_context().to_v4_prompt_state()
    assert list(payload) == [
        'active_pokemon', 'opponent_active_pokemon', 'available_moves',
        'available_switches', 'weather', 'fields', 'side_conditions',
        'opponent_side_conditions', 'known_opponent_team', 'last_move',
        'historical_analytics',
    ]
    assert payload['active_pokemon']['speed'] == {'raw_exact': 200}
    assert payload['available_moves'][0]['id'] == 'thunderbolt'
    assert payload['last_move'].startswith('Turn 6')
    assert 'battle_id' not in payload
    assert 'evidence' not in payload
    assert json.loads(make_context().model_dump_json())['battle']['turn'] == 7


class FakeLLM:
    """A no-network structured model stub."""

    def with_structured_output(self, schema):
        """Capture the exact output schema passed to the model."""
        assert schema is AgentDecision
        return object()


class FakePromptManager:
    """Record prompt inputs and return a deterministic structured decision."""

    def __init__(self):
        """Keep a list of all calls for assertions."""
        self.calls = []

    async def structured(self, name, variables, state, model, callbacks):
        """Simulate a managed prompt completion without contacting OpenAI."""
        self.calls.append((name, variables, state, callbacks))
        return AgentDecision(action='move', target='thunderbolt',
                             rationale='Preserve the sweeper for a safe endgame position.',
                             terastallize=False)


def test_policy_uses_v4_prompt_but_saves_only_future_strategy():
    """Policy reads typed context and doesn't contaminate current replay."""
    prompt = FakePromptManager()
    agent = V4Agent(FakeLLM(), prompt, callbacks=['callback'])
    ctx = make_context()
    session = BattleSession(ctx.battle.battle_id)
    decision = asyncio.run(agent.decide(ctx, session))
    assert decision.target == 'thunderbolt'
    assert prompt.calls[0][0] == 'pokemon/v4/decision'
    variables = prompt.calls[0][1]
    assert json.loads(variables['battle_state_json']) == ctx.to_v4_prompt_state()
    assert session.strategy_by_turn == {7: decision.rationale}
    assert ctx.history.rendered == 'Turn 6\n...\n\nTurn 5\n...'
    assert list(session.turn_history) == []


def test_battle_sessions_remain_independent():
    """Prompt and strategic state for different battles never share objects."""
    a = BattleSession('a')
    b = BattleSession('b')
    a.turn_history.append('Turn 1\nAction')
    a.prompt_state['key'] = 'v4'
    assert list(b.turn_history) == []
    assert b.prompt_state == {}
