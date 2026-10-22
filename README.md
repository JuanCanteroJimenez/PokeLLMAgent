# Pokémon Showdown LLM Agent

A Python agent that plays **Pokémon Showdown** with a large language model. It uses **poke-env** to interact with battles, **LangChain** for structured decisions, **Langfuse** for prompts and tracing, and **PostgreSQL** for battle observations and analytics.

The agent receives a typed `BattleContext` containing the current battlefield, available actions, completed-turn history, historical matchups, and Speed estimates. Battle information is kept separate from the prompt, so different decision policies can consume the same context.

## Architecture

```mermaid
flowchart TD
    PS[Pokémon Showdown / poke-env] --> P[V4Player]
    P --> C[ContextBuilder]
    C --> BC[BattleContext]
    BC --> A[DecisionAgent / V4Agent]
    A --> R[ActionResolver]
    R --> PS
    C <--> DB[(PostgreSQL: observations & analytics)]
    P --> S[BattleSession: per-battle state]
    P --> T[Langfuse: prompts & traces]
    P --> TR[(PostgreSQL: decisions & outcomes)]
```

| Component                       | Responsibility                                                            |
| ------------------------------- | ------------------------------------------------------------------------- |
| `V4Player`                    | Coordinates battles through poke-env.                                     |
| `BattleContext`               | Typed, prompt-independent battle information.                             |
| `ContextBuilder`              | Builds a context from the current battle, replay history, and analytics.  |
| `BattleSession`               | Maintains isolated state for each battle.                                 |
| `DecisionAgent` / `V4Agent` | Selects one action using an LLM and structured output.                    |
| `ActionResolver`              | Validates the chosen move or switch and provides a fallback.              |
| PostgreSQL                      | Stores move observations, battle decisions, outcomes, and ladder results. |
| Langfuse                        | Manages prompts, LLM traces, and battle/decision observability.           |

## Project structure

```text
.
├── pokemon_agent/
│   ├── battle/
│   │   ├── models.py             # BattleContext and typed action models
│   │   ├── context.py            # Context construction
│   │   ├── session.py            # Per-battle state
│   │   ├── history.py            # Completed-turn history
│   │   ├── protocol.py           # Showdown replay parsing
│   │   ├── observations.py       # Empirical battle observations
│   │   └── speed.py              # Speed estimates
│   ├── infrastructure/
│   │   ├── analytics.py          # PostgreSQL analytics
│   │   ├── trace_repository.py   # Battle and decision persistence
│   │   └── tracing.py            # Langfuse traces and scoring
│   ├── runners/
│   │   ├── challenges.py         # Local challenge runner
│   │   └── ladder.py             # Official ranked ladder runner
│   ├── actions.py                # Legal actions and fallback
│   ├── decision.py               # DecisionAgent contract and V4Agent
│   ├── player.py                 # poke-env player/coordinator
│   └── prompts.py                # Managed and local prompts
├── tests/
├── run_challenges.py
├── run_ladder.py
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

## Requirements

- Python **3.11+**
- An **OpenAI API key** and access to a model compatible with the Responses API and structured outputs
- **PostgreSQL** (Docker setup below)
- A **Langfuse** project and API keys for managed prompts and tracing
- **Node.js and npm** only if running a local Pokémon Showdown server
- A **Pokémon Showdown account** only for the official ranked ladder

## Installation

Clone the repository and install the Python dependencies:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY

python -m venv .venv
source .venv/bin/activate       # macOS / Linux


python -m pip install -r requirements.txt
```

Create your local configuration file:

```bash
cp .env.example .env
# Windows PowerShell: Copy-Item .env.example .env
```

## PostgreSQL setup

The simplest development setup is PostgreSQL 16 in Docker:

```bash
docker run --name pokemon-agent-postgres \
  -e POSTGRES_USER=pokemon \
  -e POSTGRES_PASSWORD=pokemon \
  -e POSTGRES_DB=pokemon_analytics \
  -p 127.0.0.1:5433:5432 \
  -v pokemon-agent-db:/var/lib/postgresql/data \
  -d postgres:16
```

Confirm that PostgreSQL is running:

```bash
docker exec pokemon-agent-postgres pg_isready -U pokemon -d pokemon_analytics
```

Configure the connections in `.env`:

```dotenv
POKEMON_ANALYTICS_DB_URL=postgresql://pokemon:pokemon@localhost:5433/pokemon_analytics
POKEMON_TRACING_DB_URL=postgresql://pokemon:pokemon@localhost:5433/pokemon_analytics
```

## Langfuse setup

1. Create a project in [Langfuse](https://langfuse.com/).
2. Generate the project's public and secret API keys.
3. Add the credentials to `.env`:

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-YOUR_KEY
LANGFUSE_SECRET_KEY=sk-lf-YOUR_KEY
LANGFUSE_BASE_URL=https://cloud.langfuse.com

POKEMON_PROMPT_SOURCE=langfuse
POKEMON_PROMPT_LABEL=production
POKEMON_PROMPT_STRICT=1
POKEMON_PROMPT_CACHE_TTL_SECONDS=60
```

Set `LANGFUSE_BASE_URL` to your Langfuse deployment (`https://cloud.langfuse.com` for EU Cloud, `https://us.cloud.langfuse.com` for US Cloud, or your self-hosted URL).

Seed the decision prompt in Langfuse:

```bash
python -m pokemon_agent.prompts list
python -m pokemon_agent.prompts sync
```

The prompt is named **`pokemon/v4/decision`**. `sync` creates it if it is missing; it does not overwrite an existing prompt. Manage subsequent prompt edits and the `production` label in the Langfuse UI.

With `POKEMON_PROMPT_STRICT=1`, a missing or inaccessible managed prompt raises an error. For local prompt rendering, set `POKEMON_PROMPT_SOURCE=local`; Langfuse can still be used for tracing. The ranked runner requires a remote prompt by default; set `POKEMON_LADDER_REQUIRE_REMOTE_PROMPT=0` when intentionally running with a local prompt.

## Pokémon Showdown and poke-env

The project uses [poke-env](https://poke-env.readthedocs.io/en/latest/) as the Python interface to Pokémon Showdown. You can run a local server for development or connect to the official server for ranked play.

### Local Showdown server

Install [Node.js](https://nodejs.org/) and npm. In a separate directory, clone and start the Showdown server:

```bash
git clone https://github.com/smogon/pokemon-showdown.git
cd pokemon-showdown
npm install
cp config/config-example.js config/config.js
node pokemon-showdown start --no-security
```

Leave the server running. The default poke-env local endpoint is `ws://localhost:8000/showdown/websocket`.

> `--no-security` is intended for local development only. Do not expose this server to the public internet.

Return to the Python project in another terminal, activate the virtual environment, and start the agent:

```bash
python run_challenges.py
```

The agent displays its Showdown username and waits for **one human challenge**. Challenge that username on your local Showdown server using the configured format (`gen9randombattle` by default).

### Official ranked ladder

Add an official Showdown account to `.env`:

```dotenv
SHOWDOWN_USERNAME=YOUR_SHOWDOWN_USERNAME
SHOWDOWN_PASSWORD=YOUR_SHOWDOWN_PASSWORD
```

Then run:

```bash
python run_ladder.py
```

The ladder runner connects to the official Showdown server and plays the configured number of matches. Results are written to `ladder_runs/<run_id>/` as JSON/CSV, with PostgreSQL persistence when the database is available.
