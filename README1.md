# Pokémon Showdown V4 — context-first refactor

A complete **V4-only** refactor of the supplied Python agent. It uses **Option B**: the battle state is a typed, serializable domain model and is independent of prompts. The `AgentBase → V2 → V3 → V3.5 → V4` inheritance chain is replaced by one `V4Player` adapter plus small services. Smogon and episodic-memory agents are excluded.

## How it works

```text
poke-env battle
      │
      ▼
V4Player (one battle coordinator; owns BattleSession by battle ID)
      │
      ├── HistoryTracker + ShowdownParser  ── completed replay turns
      ├── ObservationExtractor             ── empirical attack events
      ├── AnalyticsRepository              ── PostgreSQL move observations
      ├── HistoricalAnalytics              ── historical offense/defense evidence
      └── SpeedEstimator                   ── V4 speed estimates
      │
      ▼
BattleContext (typed, serializable, prompt-independent)
      │
      ▼
V4Agent (BattleContext → legacy V4 prompt variables → AgentDecision)
      │
      ▼
ActionResolver (legal move/switch, tera, random fallback)
      │
      ▼
poke-env order

BattleTracing + BattleTraceRepository operate across the entire battle lifecycle.
LadderTrackedAgentV4 is a tiny event-emitting adapter, NOT another policy version.
```

### Files and responsibilities

| File | Why it exists |
|---|---|
| `battle/models.py` | `BattleContext`, typed battle facts, allowed actions, observation summaries, output action contract |
| `battle/context.py` | Builds the typed context using the currently visible Showdown state |
| `battle/session.py` | Mutable, isolated state per `battle_tag`; no ContextVars |
| `battle/history.py` | Ensures history and DB observations only include completed turns; final-turn checkpoint |
| `battle/protocol.py` | The original long V2 replay event decoder and human-readable renderer, preserved for parity |
| `battle/observations.py` | The original V3 attack extraction logic, independent of SQL |
| `battle/speed.py` | The original V4 Speed rules, including unknowns and modifiers |
| `infrastructure/analytics.py` | Existing SQL schema, observation writes, historical aggregates, readable reports |
| `infrastructure/trace_repository.py` | Existing `agent_battles` and `agent_turns` SQL schemas and writes |
| `infrastructure/tracing.py` | Langfuse battle root, pending decisions, actual outcomes, evidence scores |
| `prompts.py` | Verbatim local V4 prompt and pinned remote Langfuse prompt management |
| `decision.py` | V4 policy and the extension point `DecisionAgent.decide(context, session)` |
| `actions.py` | Validate proposed orders against poke-env; fallback reasons |
| `player.py` | The only full-featured poke-env `Player` adapter |
| `runners/ladder.py` | Existing ranked ladder runner, Elo, leaderboard, local JSON/CSV, PostgreSQL |
| `runners/challenges.py` | Wait for one human challenge |

The protocol decoder is intentionally long: it supports a large variety of Showdown replay events. It was **moved, not rewritten**, to avoid changing attack/event interpretation during architectural migration.

## Installation

Python 3.11+ and a working Pokémon Showdown configuration are required.

```bash
cd pokemon_v4_refactor
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e '.[test]'
cp .env.example .env
```

Configure `OPENAI_API_KEY`, Langfuse credentials, and your existing PostgreSQL URI in `.env`. The PostgreSQL user must have appropriate CREATE/INSERT/UPDATE privileges if tables do not already exist.

### Run a human challenge

```bash
python run_challenges.py
```

### Run the ranked ladder

Configure `POKEMON_LADDER_USERNAME`, `POKEMON_LADDER_PASSWORD`, `POKEMON_LADDER_GAMES`, and the other flags from the original ladder runner, then:

```bash
python run_ladder.py
```

The ladder runner still saves local `run.json` and `battles.csv`, optionally writes `agent_ladder_runs` and `agent_ladder_games`, fetches Elo/rank where available, validates the V4 Langfuse prompt before rated games, and supports concurrent battles.

### Prompts

`pokemon/v4/decision` is the **only** managed decision prompt in the new package. The local text is copied from the supplied `prompts(1).py` without editorial changes. Label selection, version pinning per battle, strict mode and local fallback use the existing environment flags.

```bash
python -m pokemon_agent.prompts list
python -m pokemon_agent.prompts sync
```

`sync` creates an absent V4 prompt but never overwrites an existing Langfuse version. If you have a custom V4 prompt deployed, keep it in Langfuse; this refactor does not replace it.

## Contract for the next agent

```python
from pokemon_agent.battle.models import AgentDecision, BattleContext

class NewAgent:
    async def decide(self, context: BattleContext, session) -> AgentDecision:
        # Your policy can use typed context information directly.
        # The session is for execution-local prompt pins/strategy state,
        # NOT a mutable container for context fields.
        ...
```

Inject an instance via `V4Player(decision_agent=NewAgent(), ...)`. The other components, schema, tracing, and legal-action validation continue to work. The default `V4Agent` calls `context.to_v4_prompt_state()` **only at the policy boundary**, preserving the original V4 prompt variables. A future agent need not use that projection.

### Context and session

`BattleContext` groups battle identity, field/team observations, legal actions, completed history, empirical analytics (text **and** structured evidence), and V4 Speed information. It is a Pydantic model with frozen attributes and JSON serialization. As with ordinary Pydantic models, nested dictionaries and lists should be treated as read-only by convention; use `model_copy(deep=True)` to experiment with modifications safely.

`BattleSession` contains turn-history checkpoints, the pending strategy for each turn, the last successful observation DB checkpoint, per-battle prompt pins and a same-battle async lock. Battle sessions are stored under `battle_tag`, not hidden inside an inheritance mixin.

## Preserved capabilities

- **Showdown**: the same poke-env execution flow, moves, switches, tera gating and random fallback reasons.
- **History**: V2 protocol event parsing and reverse-chronological, last-50-completed-turn text in the V4 compatibility prompt; up to 60 retained internally.
- **V3 empirical analytics**: `move_observations` schema, idempotent observation keys, attack/defense aggregates, exact-match vs defender-type fallbacks, evidence quality scores.
- **V3.5 intent continuity**: rationale for turn N saved separately and appended only when the completed turn is reconstructed; not confused with the actual action.
- **V4 Speed**: raw own Speed, conservative opponent possible range, stage/weather/terrain/item/ability modifiers, confidence disclaimers and Trick Room caveat.
- **Langfuse**: stable battle trace IDs, nested turn decision spans, prompt version identities, structured evidence scores, fallback metrics, actual observed outcomes and battle scores.
- **PostgreSQL**: unchanged `move_observations`, `agent_battles`, `agent_turns`, `agent_ladder_runs`, `agent_ladder_games` tables; no migration required.
- **Ladder**: unchanged public API advisory lookups, local CSV/JSON, Elo/rank statistics and prompt preflight.

## Validation

```bash
pytest -q
python -m compileall -q pokemon_agent
```

The included tests cover structured context rendering, policy isolation, same-battle strategy staging, legal-action validation, tera gating, random fallback, and independent battle sessions. Additional replay, PostgreSQL, Langfuse and live-server verification is **still required on your actual stack** before deploying to ranked play. This environment did not have `poke-env`, Langfuse, LangChain/OpenAI or PostgreSQL packages/services installed, so no full Showdown battle or real DB/tracing integration test was performed here. The implementation preserves the relevant source mechanics but live behavioral parity has not yet been demonstrated.

## Practical extension points

- Edit strategy or introduce tools in **`decision.py`** (or add a second policy file), rather than modifying replay parsing.
- Add new information sources to **`ContextBuilder`** and give them explicit typed sections in **`BattleContext`**. Don't inject text into random history fields.
- Update prompt formatting only in the policy's `prompt_variables` function, so information does not become tied to one LLM interface.
- Prefer keeping the existing SQL schemas until you have fixture-based proof that a migration is useful.

## Known operational caveats

The V4 baseline performs synchronous PostgreSQL work in the decision path. It is retained here for compatibility, with a per-repository reentrant lock to avoid sharing a psycopg connection unsafely between callbacks. For many simultaneous battles, the next independent performance improvement would be an async connection pool—**not** a requirement for this architectural simplification.

The battle integration is designed for the poke-env interfaces used by the original supplied code. Package versions were not available to validate here; if a new poke-env release changes callback or replay internals, pin the version used by your current V4 deployment and rerun the real-battle regression suite.
