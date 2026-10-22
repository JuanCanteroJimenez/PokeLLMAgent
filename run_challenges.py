"""Accept one human challenge: `python run_challenges.py`."""
import asyncio
from pokemon_agent.runners.challenges import main


if __name__ == '__main__':
    asyncio.run(main())
