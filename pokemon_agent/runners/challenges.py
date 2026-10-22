"""Run one human challenge with the exact same V4 decision pipeline."""
from __future__ import annotations
import asyncio
import os
from dotenv import load_dotenv
from ..player import V4Player


async def main() -> None:
    """Connect to Showdown, accept one challenge and clean up afterward."""
    load_dotenv()
    player = V4Player(
        model=os.getenv('POKEMON_MODEL', 'gpt-6.1-sol'),
        reasoning_effort=os.getenv('POKEMON_REASONING_EFFORT', 'high'),
        max_concurrent_battles=1,
        battle_format=os.getenv('POKEMON_FORMAT', 'gen9randombattle')
    )
    try:
        await player.ps_client.wait_for_login()
        print(f'Bot username: {player.username}')
        print('Waiting for one human challenge...')
        await player.accept_challenges(None, n_challenges=1)
        print(f'Wins: {player.n_won_battles}')
        print(f'Finished battles: {player.n_finished_battles}')
    finally:
        await player.ps_client.stop_listening()
        await asyncio.to_thread(player.close_resources)


if __name__ == '__main__':
    asyncio.run(main())
