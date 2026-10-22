"""Pokémon Showdown ranked ladder runner for AgentBaseV4.

Uses the managed Langfuse prompts and the existing AgentBaseV4 tracing.
Each run is persisted to local JSON/CSV and, when available, PostgreSQL.

Run: python run_ladder.py
Configuration: .env (see ladder_v4.env.example)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from poke_env import AccountConfiguration, ShowdownServerConfiguration

# Uses the standalone V4Player and its independent context infrastructure.
from ..player import V4Player

LOG = logging.getLogger("ladder_v4")


def now_utc() -> str:
    """Return the current UTC time for reproducible ladder run records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_bool(name: str, default: bool) -> bool:
    """Read an optional boolean feature flag from environment variables."""
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def showdown_id(text: str) -> str:
    """Normalize an account or format name to a Showdown identifier."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def number(value: Any) -> float | None:
    """Parse a finite numeric value from an external API response."""
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        if not (float("-inf") < result < float("inf")):
            return None
        return result
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    """Parse an optional integer without accepting invalid values."""
    candidate = number(value)
    return int(candidate) if candidate is not None else None


def _fetch_json(url: str, timeout: float = 8.0) -> Any:
    """Fetch a public Showdown API JSON response with an HTTP timeout."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PokemonAgentLadderRun/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def profile_elo(payload: Any, battle_format: str) -> float | None:
    """Official Showdown user API: ratings may be an array or dictionary."""
    if not isinstance(payload, dict):
        return None
    ratings = payload.get("ratings", [])
    rows: list[Any]
    if isinstance(ratings, list):
        rows = ratings
    elif isinstance(ratings, dict):
        rows = [dict(value, formatid=key) for key, value in ratings.items()
                if isinstance(value, dict)]
    else:
        rows = []
    target = showdown_id(battle_format)
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = row.get("formatid", row.get("format", row.get("id", "")))
        if showdown_id(str(current)) == target:
            return number(row.get("elo"))
    return None


def leaderboard_rank(payload: Any, username: str) -> int | None:
    """Rank only if the user appears in the *published* leaderboard."""
    rows = payload
    if isinstance(rows, dict):
        for key in ("toplist", "ladder", "rankings", "entries", "users"):
            if isinstance(rows.get(key), list):
                rows = rows[key]
                break
    if not isinstance(rows, list):
        return None
    target = showdown_id(username)
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        user = row.get("userid", row.get("username", row.get("user", row.get("name", ""))))
        if showdown_id(str(user)) != target:
            continue
        return integer(row.get("rank")) or index
    return None


async def public_ladder_snapshot(
    username: str, battle_format: str, *, include_rank: bool = True,
) -> dict[str, Any]:
    """Public API is advisory, and failures never interrupt battling."""
    userid = showdown_id(username)
    fmt = showdown_id(battle_format)
    base = "https://pokemonshowdown.com"
    calls = [asyncio.to_thread(_fetch_json, f"{base}/users/{userid}.json")]
    if include_rank:
        calls.append(asyncio.to_thread(_fetch_json, f"{base}/ladder/{fmt}.json"))
    responses = await asyncio.gather(*calls, return_exceptions=True)
    data: dict[str, Any] = {"elo": None, "rank": None, "profile_ok": False, "rank_checked": False}
    profile = responses[0]
    if isinstance(profile, BaseException):
        LOG.warning("Showdown profile request failed: %s", profile)
    else:
        data["profile_ok"] = True
        data["elo"] = profile_elo(profile, battle_format)
    if include_rank:
        ranks = responses[1]
        if isinstance(ranks, BaseException):
            LOG.warning("Showdown leaderboard request failed: %s", ranks)
        else:
            data["rank_checked"] = True
            data["rank"] = leaderboard_rank(ranks, username)
    return data


async def check_langfuse_prompt(label: str, fixed_versions: dict[str, int]) -> int:
    """Fail before rated games if the selected V4 prompt is not accessible."""
    from langfuse import get_client
    options: dict[str, Any] = {
        "type": "chat", "cache_ttl_seconds": 0, "max_retries": 0,
    }
    name = "pokemon/v4/decision"
    if name in fixed_versions:
        options["version"] = fixed_versions[name]
    else:
        options["label"] = label
    response = await asyncio.to_thread(get_client().get_prompt, name, **options)
    if not isinstance(response.prompt, list):
        raise ValueError(f"Langfuse prompt {name} must be a Chat prompt")
    return int(response.version)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Write run state via a temporary file and an atomic rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


CSV_FIELDS = [
    "battle_number", "battle_id", "result", "elo", "battle_reported_elo",
    "public_api_elo", "published_rank", "opponent_rating", "finished_at",
    "prompt_versions",
]


def save_local(directory: Path, run: dict[str, Any]) -> None:
    """Write run state and per-battle CSV without losing the previous files."""
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "run.json", run)
    target = directory / "battles.csv"
    tmp = directory / "battles.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in run["battles"]:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False)
                             if key == "prompt_versions" else row.get(key)
                             for key in CSV_FIELDS})
    tmp.replace(target)


class LadderRepository:
    """Creates independent run/game tables in existing Pokémon analytics DB."""

    def __init__(self, dsn: str | None):
        """Initialize configuration and state for this component."""
        self.dsn = dsn
        self.enabled = bool(dsn)

    def save(self, run: dict[str, Any], new_game: dict[str, Any] | None = None) -> bool:
        """Upsert ladder run and finished-game rows into PostgreSQL."""
        if not self.enabled:
            return False
        try:
            import psycopg
            from psycopg.types.json import Jsonb
            with psycopg.connect(self.dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS agent_ladder_runs (
                            run_id UUID PRIMARY KEY,
                            agent_id TEXT NOT NULL,
                            showdown_username TEXT NOT NULL,
                            model TEXT NOT NULL,
                            reasoning_effort TEXT NOT NULL,
                            battle_format TEXT NOT NULL,
                            prompt_label TEXT,
                            prompt_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
                            target_battles INTEGER NOT NULL,
                            played INTEGER NOT NULL DEFAULT 0,
                            wins INTEGER NOT NULL DEFAULT 0,
                            losses INTEGER NOT NULL DEFAULT 0,
                            ties INTEGER NOT NULL DEFAULT 0,
                            initial_elo DOUBLE PRECISION,
                            current_elo DOUBLE PRECISION,
                            min_elo DOUBLE PRECISION,
                            max_elo DOUBLE PRECISION,
                            initial_rank INTEGER,
                            current_rank INTEGER,
                            best_rank INTEGER,
                            worst_rank INTEGER,
                            status TEXT NOT NULL,
                            config JSONB NOT NULL DEFAULT '{}'::jsonb,
                            started_at TIMESTAMPTZ NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            completed_at TIMESTAMPTZ,
                            error TEXT
                        )
                    """)
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS agent_ladder_games (
                            run_id UUID NOT NULL REFERENCES agent_ladder_runs(run_id),
                            battle_number INTEGER NOT NULL,
                            battle_id TEXT NOT NULL,
                            result TEXT NOT NULL,
                            elo DOUBLE PRECISION,
                            battle_reported_elo DOUBLE PRECISION,
                            public_api_elo DOUBLE PRECISION,
                            published_rank INTEGER,
                            opponent_rating DOUBLE PRECISION,
                            prompt_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
                            finished_at TIMESTAMPTZ NOT NULL,
                            PRIMARY KEY (run_id, battle_id),
                            UNIQUE (run_id, battle_number)
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO agent_ladder_runs (
                            run_id, agent_id, showdown_username, model, reasoning_effort,
                            battle_format, prompt_label, prompt_versions,
                            target_battles, played, wins, losses, ties,
                            initial_elo, current_elo, min_elo, max_elo,
                            initial_rank, current_rank, best_rank, worst_rank,
                            status, config, started_at, completed_at, error
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                        )
                        ON CONFLICT (run_id) DO UPDATE SET
                            prompt_versions=EXCLUDED.prompt_versions,
                            played=EXCLUDED.played, wins=EXCLUDED.wins,
                            losses=EXCLUDED.losses, ties=EXCLUDED.ties,
                            initial_elo=EXCLUDED.initial_elo,
                            current_elo=EXCLUDED.current_elo,
                            min_elo=EXCLUDED.min_elo, max_elo=EXCLUDED.max_elo,
                            initial_rank=EXCLUDED.initial_rank,
                            current_rank=EXCLUDED.current_rank,
                            best_rank=EXCLUDED.best_rank, worst_rank=EXCLUDED.worst_rank,
                            status=EXCLUDED.status, config=EXCLUDED.config,
                            completed_at=EXCLUDED.completed_at,
                            error=EXCLUDED.error, updated_at=NOW()
                    """, (
                        run["run_id"], run["agent_id"], run["username"], run["model"],
                        run["reasoning_effort"], run["battle_format"],
                        run["prompt_label"], Jsonb(run["prompt_versions"]),
                        run["target_battles"], run["played"], run["wins"],
                        run["losses"], run["ties"], run["initial_elo"],
                        run["current_elo"], run["min_elo"], run["max_elo"],
                        run["initial_rank"], run["current_rank"], run["best_rank"],
                        run["worst_rank"], run["status"], Jsonb(run["config"]),
                        run["started_at"], run["completed_at"], run["error"],
                    ))
                    if new_game is not None:
                        cursor.execute("""
                            INSERT INTO agent_ladder_games (
                                run_id, battle_number, battle_id, result, elo,
                                battle_reported_elo, public_api_elo, published_rank,
                                opponent_rating, prompt_versions, finished_at
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (run_id, battle_id) DO NOTHING
                        """, (
                            run["run_id"], new_game["battle_number"], new_game["battle_id"],
                            new_game["result"], new_game["elo"],
                            new_game["battle_reported_elo"], new_game["public_api_elo"],
                            new_game["published_rank"], new_game["opponent_rating"],
                            Jsonb(new_game["prompt_versions"]), new_game["finished_at"],
                        ))
            return True
        except Exception:
            LOG.exception("PostgreSQL ladder-run save failed (local files still saved)")
            return False


class LadderTrackedAgentV4(V4Player):
    """Do not modify V4 decisions; only emit a finished-battle event."""

    def __init__(self, *args: Any, **kwargs: Any):
        """Initialize configuration and state for this component."""
        self._ladder_main_loop: asyncio.AbstractEventLoop | None = None
        self._ladder_events: asyncio.Queue[dict[str, Any] | None] | None = None
        super().__init__(*args, **kwargs)

    def configure_monitor(self, loop: asyncio.AbstractEventLoop, events: asyncio.Queue) -> None:
        """Register the receiving loop and queue for completed battle events."""
        self._ladder_main_loop = loop
        self._ladder_events = events

    def _battle_finished_callback(self, battle: Any) -> None:
        # Collect versions before AgentBase finalizes and discards this battle.
        """Publish a finished-battle event after V4 has persisted its trace."""
        prompt_versions: dict[str, dict[str, Any]] = {}
        tracking = getattr(self, "_tracked_battles", {}).get(getattr(battle, "battle_tag", ""))
        if tracking is not None:
            for pending in tracking.pending.values():
                versions = pending.intention.get("prompt_versions", {})
                if isinstance(versions, dict):
                    for name, selected in versions.items():
                        if isinstance(selected, dict):
                            prompt_versions[str(name)] = selected

        # Always allow the existing V3/V3.5/V4 persistence + Langfuse callback.
        try:
            super()._battle_finished_callback(battle)
        finally:
            if self._ladder_main_loop is None or self._ladder_events is None:
                return
            result = ("win" if bool(getattr(battle, "won", False))
                      else "loss" if bool(getattr(battle, "lost", False))
                      else "tie" if bool(getattr(battle, "finished", False))
                      else "unknown")
            event = {
                "battle_id": str(getattr(battle, "battle_tag", "")),
                "result": result,
                "battle_reported_elo": number(getattr(battle, "rating", None)),
                "opponent_rating": number(getattr(battle, "opponent_rating", None)),
                "finished_at": now_utc(),
                "prompt_versions": prompt_versions,
            }
            try:
                # poke-env may execute callbacks in a different asyncio loop/thread.
                self._ladder_main_loop.call_soon_threadsafe(self._ladder_events.put_nowait, event)
            except RuntimeError:
                LOG.exception("Ladder monitor is no longer available")


def apply_elo(run: dict[str, Any], elo: float | None) -> None:
    """Update the Elo boundaries in the current ladder run."""
    if elo is None:
        return
    run["current_elo"] = elo
    run["max_elo"] = elo if run["max_elo"] is None else max(run["max_elo"], elo)
    run["min_elo"] = elo if run["min_elo"] is None else min(run["min_elo"], elo)


def apply_rank(run: dict[str, Any], rank: int | None) -> None:
    # 'None' means not on the published board, not an infinite/worst rank.
    """Update published leaderboard rank records when available."""
    run["current_rank"] = rank
    if rank is None:
        return
    run["best_rank"] = rank if run["best_rank"] is None else min(run["best_rank"], rank)
    run["worst_rank"] = rank if run["worst_rank"] is None else max(run["worst_rank"], rank)


def short(value: Any, *, digits: int = 0) -> str:
    """Return a compact, readable display value for the CLI."""
    if value is None:
        return "--"
    return f"{value:,.{digits}f}" if isinstance(value, (int, float)) else str(value)


def show_progress(run: dict[str, Any], *, prefix: str) -> None:
    """Print the current ladder run counters and ranking snapshot."""
    elapsed = int(time.monotonic() - run["_start_monotonic"])
    print(
        f"[{prefix}] Games {run['played']}/{run['target_battles']}  "
        f"W {run['wins']} / L {run['losses']} / T {run['ties']}  "
        f"Win rate {100 * run['wins'] / max(1, run['played']):.1f}%  "
        f"Elo {short(run['current_elo'])} "
        f"(min {short(run['min_elo'])}, max {short(run['max_elo'])})  "
        f"Rank {short(run['current_rank'])} "
        f"(best {short(run['best_rank'])})  "
        f"Elapsed {elapsed // 60}m{elapsed % 60:02d}s",
        flush=True,
    )


async def monitor(
    events: asyncio.Queue[dict[str, Any] | None],
    player: LadderTrackedAgentV4,
    run: dict[str, Any],
    directory: Path,
    repository: LadderRepository,
    *,
    heartbeat: int,
    rank_every: int,
    rating_delay: float,
) -> None:
    """Consume finished-battle events, save results and refresh ladder metrics."""
    seen: set[str] = set()
    while True:
        try:
            item = await asyncio.wait_for(events.get(), timeout=heartbeat)
        except asyncio.TimeoutError:
            show_progress(run, prefix="LIVE")
            continue
        if item is None:
            break
        if item["battle_id"] in seen:
            continue
        seen.add(item["battle_id"])
        if item["result"] not in {"win", "loss", "tie"}:
            LOG.warning("Battle %s ended with unknown result; counting as tie? No, skipping.", item["battle_id"])
            continue

        # A brief delay allows Showdown's rating service to publish the update.
        if rating_delay > 0:
            await asyncio.sleep(rating_delay)
        next_number = run["played"] + 1
        snapshot = await public_ladder_snapshot(
            run["username"], run["battle_format"],
            include_rank=(next_number % rank_every == 0),
        )
        # A per-battle rating (when available) is more closely tied to that
        # exact battle than a possibly stale website snapshot.
        elo = item["battle_reported_elo"] if item["battle_reported_elo"] is not None else snapshot["elo"]
        apply_elo(run, elo)
        if snapshot["rank_checked"]:
            apply_rank(run, snapshot["rank"])
        record = {
            "battle_number": next_number,
            **item,
            "elo": elo,
            "public_api_elo": snapshot["elo"],
            "published_rank": run["current_rank"] if snapshot["rank_checked"] else None,
        }
        run["battles"].append(record)
        run["played"] = next_number
        run[{"win": "wins", "loss": "losses", "tie": "ties"}[item["result"]]] += 1
        run["prompt_versions"].update(item["prompt_versions"])
        public = {k: v for k, v in run.items() if not k.startswith("_")}
        save_local(directory, public)
        await asyncio.to_thread(repository.save, public, record)
        show_progress(run, prefix=f"BATTLE {next_number}: {item['result'].upper()}")
        print(f"         Battle ID: {item['battle_id']}", flush=True)


async def main() -> None:
    """Run the complete ladder experiment and reliably persist its outcome."""
    load_dotenv()
    username = os.environ["SHOWDOWN_USERNAME"].strip()
    password = os.environ["SHOWDOWN_PASSWORD"]
    model = os.getenv("POKEMON_LADDER_MODEL", os.getenv("POKEMON_TEST_MODEL", "gpt-6-luna"))
    battle_format = os.getenv("POKEMON_LADDER_FORMAT", "gen9randombattle")
    reasoning_effort = os.getenv("POKEMON_LADDER_REASONING_EFFORT", "high")
    service_tier = os.getenv("POKEMON_LADDER_SERVICE_TIER", "default")
    label = os.getenv("POKEMON_LADDER_PROMPT_LABEL", "latest")
    fixed_versions = json.loads(os.getenv("POKEMON_LADDER_PROMPT_VERSIONS", "{}"))
    if not isinstance(fixed_versions, dict):
        raise ValueError("POKEMON_LADDER_PROMPT_VERSIONS must be a JSON object")
    fixed_versions = {key: int(value) for key, value in fixed_versions.items()}
    agent_id = os.getenv("POKEMON_LADDER_AGENT_ID", f"ladder-v4-{showdown_id(username)}")
    n_games = int(os.getenv("POKEMON_LADDER_GAMES", "20"))
    concurrent = int(os.getenv("POKEMON_LADDER_CONCURRENT", "1"))
    heartbeat = max(5, int(os.getenv("POKEMON_LADDER_HEARTBEAT_SECONDS", "30")))
    rank_every = max(1, int(os.getenv("POKEMON_LADDER_RANK_EVERY_BATTLES", "1")))
    rating_delay = max(0.0, float(os.getenv("POKEMON_LADDER_RATING_DELAY_SECONDS", "2")))
    if n_games < 1 or concurrent < 1:
        raise ValueError("POKEMON_LADDER_GAMES and POKEMON_LADDER_CONCURRENT must be >= 1")
    run_id = str(uuid4())
    directory = Path(os.getenv("POKEMON_LADDER_OUTPUT_DIR", "ladder_runs")) / run_id
    dsn = (
        os.getenv("POKEMON_LADDER_DB_URL")
        or os.getenv("POKEMON_TRACING_DB_URL")
        or os.getenv("POKEMON_ANALYTICS_DB_URL")
        or "postgresql://pokemon:pokemon@localhost:5433/pokemon_analytics"
    )
    repo = LadderRepository(dsn)
    run = {
        "run_id": run_id,
        "agent_id": agent_id,
        "username": username,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "battle_format": battle_format,
        "prompt_name": "pokemon/v4/decision",
        "prompt_label": label,
        "prompt_version_overrides": fixed_versions,
        "prompt_versions": {},
        "target_battles": n_games,
        "played": 0,
        "wins": 0,
        "losses": 0,
        "ties": 0,
        "initial_elo": None,
        "current_elo": None,
        "min_elo": None,
        "max_elo": None,
        "initial_rank": None,
        "current_rank": None,
        "best_rank": None,
        "worst_rank": None,
        "status": "running",
        "started_at": now_utc(),
        "completed_at": None,
        "error": None,
        "config": {
            "max_concurrent_battles": concurrent,
            "save_replays": env_bool("POKEMON_LADDER_SAVE_REPLAYS", True),
            "start_timer_on_battle_start": env_bool("POKEMON_LADDER_START_TIMER", True),
            "langfuse_prompt_strict": env_bool("POKEMON_PROMPT_STRICT", False),
            "remote_prompt_preflight": env_bool("POKEMON_LADDER_REQUIRE_REMOTE_PROMPT", True),
            "rank_every_n_battles": rank_every,
        },
        "battles": [],
        "_start_monotonic": time.monotonic(),
    }

    # Never persist passwords, API keys, or DSN credentials.
    def public_run() -> dict[str, Any]:
        """Exclude internal runtime fields from saved and public run snapshots."""
        return {k: v for k, v in run.items() if not k.startswith("_")}

    initial = await public_ladder_snapshot(username, battle_format)
    run["initial_elo"] = initial["elo"]
    apply_elo(run, initial["elo"])
    if initial["rank_checked"]:
        run["initial_rank"] = initial["rank"]
        apply_rank(run, initial["rank"])
    directory.mkdir(parents=True, exist_ok=True)
    save_local(directory, public_run())
    await asyncio.to_thread(repo.save, public_run())

    if env_bool("POKEMON_LADDER_REQUIRE_REMOTE_PROMPT", True):
        if os.getenv("POKEMON_PROMPT_SOURCE", "langfuse").lower() == "local":
            raise ValueError(
                "Ranked prompt preflight requires Langfuse; remove "
                "POKEMON_PROMPT_SOURCE=local or disable POKEMON_LADDER_REQUIRE_REMOTE_PROMPT"
            )
        try:
            validated_version = await check_langfuse_prompt(label, fixed_versions)
        except Exception as exc:
            run["status"] = "failed"
            run["error"] = f"Prompt preflight failed: {type(exc).__name__}: {exc}"
            run["completed_at"] = now_utc()
            save_local(directory, public_run())
            await asyncio.to_thread(repo.save, public_run())
            raise RuntimeError(
                "The V4 prompt is missing or inaccessible in Langfuse; "
                "refusing to start ranked battles. Run 'python -m pokemon_agent.prompts sync' "
                "and verify pokemon/v4/decision has the requested label/version."
            ) from exc
        print(f"Langfuse V4 prompt validated (version {validated_version})", flush=True)

    print(f"Run ID: {run_id}", flush=True)
    print(f"Account: {username} | Agent: {agent_id} | Model: {model}", flush=True)
    print(f"Format: {battle_format} | Prompt: pokemon/v4/decision @ {label}", flush=True)
    print(f"Saving run to: {directory.resolve()}", flush=True)
    print("A published leaderboard rank is shown only when available; '--' is not rank zero.", flush=True)
    show_progress(run, prefix="START")

    player: LadderTrackedAgentV4 | None = None
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    monitor_task: asyncio.Task | None = None
    try:
        player = LadderTrackedAgentV4(
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            prompt_label=label,
            prompt_versions=fixed_versions,
            agent_id=agent_id,
            account_configuration=AccountConfiguration(username, password),
            server_configuration=ShowdownServerConfiguration,
            battle_format=battle_format,
            max_concurrent_battles=concurrent,
            save_replays=run["config"]["save_replays"],
            start_timer_on_battle_start=run["config"]["start_timer_on_battle_start"],
        )
        player.configure_monitor(asyncio.get_running_loop(), queue)
        monitor_task = asyncio.create_task(
            monitor(queue, player, run, directory, repo, heartbeat=heartbeat,
                    rank_every=rank_every, rating_delay=rating_delay),
            name="ladder-run-monitor",
        )
        await player.ladder(n_games=n_games)
        run["status"] = "completed"
    except (KeyboardInterrupt, asyncio.CancelledError):
        run["status"] = "interrupted"
        raise
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = f"{type(exc).__name__}: {exc}"
        LOG.exception("Ladder run aborted")
        raise
    finally:
        if monitor_task is not None:
            # Drain battle-completion events before writing final totals.
            await asyncio.sleep(0.05)
            queue.put_nowait(None)
            await monitor_task
        run["completed_at"] = now_utc()
        save_local(directory, public_run())
        await asyncio.to_thread(repo.save, public_run())
        show_progress(run, prefix=run["status"].upper())
        if player is not None:
            try:
                await player.ps_client.stop_listening()
            except Exception:
                LOG.exception("Could not stop Showdown websocket cleanly")
            try:
                await asyncio.to_thread(player.langfuse.flush)
            except Exception:
                LOG.exception("Langfuse flush failed")
            tracing_repo = getattr(player, "_trace_repo", None)
            if tracing_repo is not None:
                await asyncio.to_thread(tracing_repo.close)
            await asyncio.to_thread(player.analytics.close)
        print(f"Results saved in {directory.resolve()}", flush=True)


if __name__ == "__main__":
    load_dotenv()

    log_level = os.getenv(
        "POKEMON_LADDER_LOG_LEVEL", "ERROR"
    ).upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.ERROR),
        format="%(levelname)s: %(message)s",
    )

    # Silence verbose third-party libraries.
    for name in (
        "poke_env",
        "httpx",
        "httpcore",
        "openai",
        "langfuse",
        "langchain",
        "websockets",
        "urllib3",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)

    asyncio.run(main())