"""Launch pairwise V4/Random/Max Damage matches: `python run_cross_validation.py`."""
import asyncio
import logging

from dotenv import load_dotenv
from pokemon_agent.runners.cross_validation import main


if __name__ == '__main__':
    load_dotenv()
    logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s')
    asyncio.run(main())
