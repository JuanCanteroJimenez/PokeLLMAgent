"""Launch the V4 ladder runner: `python run_ladder.py`."""
import asyncio
import logging
from dotenv import load_dotenv
from pokemon_agent.runners.ladder import main


if __name__ == '__main__':
    load_dotenv()
    logging.basicConfig(level=logging.ERROR, format='%(levelname)s: %(message)s')
    asyncio.run(main())
