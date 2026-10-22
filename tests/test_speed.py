"""Deterministic tests for the retained V4 speed estimator."""
from types import SimpleNamespace
from pokemon_agent.battle.speed import SpeedEstimator


def pokemon(*, speed=120, base_speed=100, level=80, stage=0,
            item=None, ability=None, status=None):
    """Construct only the observable attributes read by V4's Speed formulas."""
    return SimpleNamespace(stats={'spe': speed}, base_stats={'spe': base_speed},
        level=level, boosts={'spe': stage}, item=item, ability=ability,
        status=status, species='Pikachu', effects={})


def battle(*, tailwind=False):
    """Construct visible battlefield modifiers without any Showdown client."""
    return SimpleNamespace(gen=9, weather={}, fields={},
        side_conditions={'Tailwind': 1} if tailwind else {},
        opponent_side_conditions={})


def test_own_speed_with_stages_scarf_and_tailwind():
    """Only known modifiers are applied to the exact privately known Speed."""
    info = SpeedEstimator._v4_speed_info(
        pokemon(speed=120, stage=1, item='Choice Scarf'),
        battle(tailwind=True), ours=True,
    )
    assert info['raw_exact'] == 120
    assert info['known_multiplier'] == '9/2'
    assert info['known_effective_speed'] == 540
    assert info['item'] == 'choicescarf'


def test_opponent_speed_is_range_not_asserted_exact():
    """V4 must retain uncertainty about unrevealed opponent stats and items."""
    info = SpeedEstimator._v4_speed_info(pokemon(base_speed=100, level=80),
                                        battle(), ours=False)
    assert info['raw_min'] == 148
    assert info['raw_max'] == 264
    assert info['known_effective_min'] == 148
    assert info['known_effective_max'] == 264
    assert 'hidden items/abilities' in info['note']


def test_quick_feet_prevents_paralysis_penalty():
    """Observed Quick Feet bypasses the standard paralysis Speed reduction."""
    info = SpeedEstimator._v4_speed_info(
        pokemon(status='par', ability='Quick Feet'), battle(), ours=True)
    assert info['known_multiplier'] == '3/2'
    assert info['known_effective_speed'] == 180
