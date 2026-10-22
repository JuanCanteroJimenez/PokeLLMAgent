"""Action validation and fallback are tested without a Showdown connection."""
from contextlib import contextmanager
from types import SimpleNamespace
from pokemon_agent.actions import ActionResolver
from pokemon_agent.battle.models import AgentDecision


class FakeTracer:
    """Provide a minimal Langfuse span context manager."""

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        """Capture resolution metadata without calling Langfuse."""
        class Span:
            """Minimal recorder for the resolve span."""
            def update(self, output):
                """Accept the recorded result."""
                self.result = output
        yield Span()


class FakePlayer:
    """Provide create-order and random fallback methods."""

    def create_order(self, selected, **kwargs):
        """Return a non-network action tuple."""
        return selected, kwargs

    def choose_random_move(self, battle):
        """Identify random recovery distinctly."""
        return SimpleNamespace(message='/choose default')


def make_battle(can_tera=True):
    """Prepare fake available actions with canonical and display move names."""
    return SimpleNamespace(
        available_moves=[SimpleNamespace(id='thunderbolt', name='Thunderbolt')],
        available_switches=[SimpleNamespace(species='Great Tusk')],
        can_tera=can_tera,
    )


def decision(action, target, tera=False):
    """Make a valid V4 decision for action-resolver tests."""
    return AgentDecision(action=action, target=target, terastallize=tera,
                         rationale='This action improves positioning for the endgame.')


def test_moves_and_tera_are_validated():
    """Terastallization is enabled only when Showdown allows it."""
    r = ActionResolver(FakePlayer(), FakeTracer())
    order, action, reason = r.resolve(make_battle(can_tera=False),
                                      decision('move', 'Thunderbolt', True))
    assert reason is None
    assert action == {'action': 'move', 'target': 'thunderbolt', 'terastallize': False}
    assert order[1] == {'terastallize': False}


def test_legal_switch_and_random_fallback():
    """Unknown switch targets fall back with the original reason code."""
    r = ActionResolver(FakePlayer(), FakeTracer())
    _, action, reason = r.resolve(make_battle(), decision('switch', 'Great-Tusk'))
    assert reason is None and action['target'] == 'Great Tusk'
    order, action, reason = r.resolve(make_battle(), decision('switch', 'Snorlax'))
    assert reason == 'invalid_switch'
    assert action['action'] == 'fallback_random'
    assert order.message == '/choose default'
