"""Compare V4, Random, and Max Damage players on a local Showdown server.

Uses poke-env's cross_evaluate to play each pairing and report win rates.
No additional framework, model, or database is introduced for evaluation.
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from poke_env import RandomPlayer, cross_evaluate
from poke_env.player import Player

from ..player import V4Player, AgentBase


class MaxDamagePlayer(Player):
    """Choose the highest-base-power move; use Terastallization when possible.

    This is the simple baseline from the poke-env quickstart, not a damage
    calculator: it does not account for type effectiveness, accuracy, or stats.
    """

    def choose_move(self, battle):
        """Return a legal Showdown order, falling back when no move is available."""
        if not battle.available_moves:
            return self.choose_random_move(battle)

        move = max(battle.available_moves, key=lambda candidate: candidate.base_power)
        return self.create_order(move, terastallize=bool(battle.can_tera))


def print_results(results: dict, players: list[Player]) -> None:
    """Print cross_evaluate's pairwise win rates as an easy-to-read table."""
    labels = {players[0].username: 'V4',
              players[1].username: 'Random',
              players[2].username: 'Max Damage',
              players[3].username: 'AgentBase',}
    width = 14
    print('\nWin rate (row player against column player):')
    print(f'{"Player":<{width}}' + ''.join(f'{name:>{width}}' for name in labels.values()))

    for player in players:
        row = results.get(player.username, {})
        cells = []
        for opponent in players:
            score = row.get(opponent.username)
            cells.append(f'{score:.1%}' if score is not None else '-')
        print(f'{labels[player.username]:<{width}}' + ''.join(f'{cell:>{width}}' for cell in cells))


async def main() -> None:
    """Run pairwise battles, print results, and close all connections."""
    load_dotenv()
    battle_format = os.getenv('POKEMON_FORMAT', 'gen9randombattle')
    n_battles = int(os.getenv('POKEMON_CROSS_VALIDATION_BATTLES', '1'))
    if n_battles < 1:
        raise ValueError('POKEMON_CROSS_VALIDATION_BATTLES must be >= 1')

    # All three players use the same local Showdown server and battle format.
    player_kwargs = {'battle_format': battle_format, 'max_concurrent_battles': 1}
    v4 = V4Player(
        model=os.getenv('POKEMON_MODEL', 'gpt-6-luna'),
        reasoning_effort=os.getenv('POKEMON_REASONING_EFFORT', 'high'),
        **player_kwargs,
    )

    base = AgentBase(
        model=os.getenv('POKEMON_MODEL', 'gpt-6-luna'),
        reasoning_effort=os.getenv('POKEMON_REASONING_EFFORT', 'high')
    )


    players = [ v4, RandomPlayer(**player_kwargs), MaxDamagePlayer(**player_kwargs), base]

    try:
        await asyncio.gather(*(player.ps_client.wait_for_login() for player in players))
        print(f'Format: {battle_format}')
        print(f'Battles per pairing: {n_battles}')
        print(f'Players: {", ".join(player.username for player in players)}')

        results = await cross_evaluate(players, n_challenges=n_battles)
        print_results(results, players)
    finally:
        # Stop listeners for *all* players even if a battle or a connection fails.
        await asyncio.gather(
            *(player.ps_client.stop_listening() for player in players),
            return_exceptions=True,
        )
        await asyncio.to_thread(v4.close_resources)
        await asyncio.to_thread(base.close_resources)


if __name__ == '__main__':
    asyncio.run(main())
