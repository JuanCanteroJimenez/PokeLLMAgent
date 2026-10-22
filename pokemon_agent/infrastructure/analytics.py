"""Existing move-observation PostgreSQL schema and historical match-up evidence.

SQL and aggregation semantics are retained. A lock protects the shared
synchronous psycopg connection across battle/callback threads.
"""
from __future__ import annotations
import json
import psycopg
from psycopg.rows import dict_row
from threading import RLock
from typing import Any
from poke_env.battle import Battle
from ..utils import logger
from ..battle.observations import ObservationExtractor


def synchronized(method):
    """Serialize calls sharing one psycopg connection; permit nested repo calls."""
    from functools import wraps
    @wraps(method)
    def guarded(self, *args, **kwargs):
        """Apply the repository reentrant connection lock around a SQL operation."""
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


class AnalyticsRepository:
    """Own PostgreSQL observation writes and aggregate reads without LLM coupling."""

    def __init__(self, dsn: str):
        """Remember the analytics DSN; connect lazily on first read/write."""
        self.analytics_db_url = dsn
        self._analytics_conn = None
        self._analytics_schema_ready = False
        self._lock = RLock()

    def close(self) -> None:
        """Close the shared observation connection at shutdown."""
        with self._lock:
            if self._analytics_conn is not None:
                self._analytics_conn.close()
                self._analytics_conn = None
                self._analytics_schema_ready = False

    @synchronized
    def _get_analytics_connection(self):
        """Get the database connection and ensure the existing observations schema."""
        try:
            if self._analytics_conn is None or self._analytics_conn.closed:
                self._analytics_conn = psycopg.connect(self.analytics_db_url, autocommit=True, row_factory=dict_row)
                self._analytics_schema_ready = False
            if not self._analytics_schema_ready:
                self._ensure_analytics_schema()
            return self._analytics_conn
        except Exception:
            logger.exception('Could not connect to Pokémon analytics PostgreSQL database.')
            return None

    @synchronized
    def _ensure_analytics_schema(self):
        """Create the V3 move-observation table when absent."""
        conn = self._analytics_conn
        if conn is None:
            return
        schema_sql = '\n        CREATE TABLE IF NOT EXISTS move_observations (\n            observation_key TEXT PRIMARY KEY,\n\n            battle_id TEXT NOT NULL,\n            turn INTEGER NOT NULL,\n            generation INTEGER NOT NULL,\n            "format" TEXT,\n\n            attacker_side TEXT,\n            attacker_species TEXT NOT NULL,\n            attacker_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],\n            attacker_hp DOUBLE PRECISION,\n            attacker_status TEXT,\n\n            attacker_boosts_attack SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_defense SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_special_attack SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_special_defense SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_speed SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_accuracy SMALLINT NOT NULL DEFAULT 0,\n            attacker_boosts_evasion SMALLINT NOT NULL DEFAULT 0,\n\n            attacker_tera TEXT,\n\n            move_id TEXT NOT NULL,\n            move_name TEXT NOT NULL,\n            move_type TEXT,\n            move_category TEXT,\n            base_power INTEGER,\n\n            defender_species TEXT,\n            defender_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],\n            defender_hp_before DOUBLE PRECISION,\n            defender_hp_after DOUBLE PRECISION,\n            defender_status TEXT,\n\n            defender_boosts_attack SMALLINT,\n            defender_boosts_defense SMALLINT,\n            defender_boosts_special_attack SMALLINT,\n            defender_boosts_special_defense SMALLINT,\n            defender_boosts_speed SMALLINT,\n            defender_boosts_accuracy SMALLINT,\n            defender_boosts_evasion SMALLINT,\n\n            defender_tera TEXT,\n\n            damage_percentage DOUBLE PRECISION,\n            effectiveness TEXT,\n            critical_hit BOOLEAN NOT NULL DEFAULT FALSE,\n            missed BOOLEAN NOT NULL DEFAULT FALSE,\n            immune BOOLEAN NOT NULL DEFAULT FALSE,\n            fainted BOOLEAN NOT NULL DEFAULT FALSE,\n\n            attacker_go_first BOOLEAN,\n\n            weather TEXT,\n            terrain TEXT,\n\n            attacker_side_conditions JSONB NOT NULL DEFAULT \'{}\'::JSONB,\n            defender_side_conditions JSONB NOT NULL DEFAULT \'{}\'::JSONB,\n\n            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n        );\n\n        CREATE INDEX IF NOT EXISTS idx_move_observations_exact_matchup\n            ON move_observations (\n                attacker_species,\n                defender_species,\n                move_id\n            );\n\n        CREATE INDEX IF NOT EXISTS idx_move_observations_type_matchup\n            ON move_observations (\n                attacker_species,\n                defender_types,\n                move_id\n            );\n\n        CREATE INDEX IF NOT EXISTS idx_move_observations_battle_turn\n            ON move_observations (\n                battle_id,\n                turn\n            );\n        '
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        self._analytics_schema_ready = True

    def _observation_to_db_row(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Convert a parsed move observation to the existing SQL row schema."""
        attacker_boosts = observation['attacker_boosts']
        defender_boosts = observation.get('defender_boosts')
        row = {'observation_key': observation['observation_key'], 'battle_id': observation['battle_id'], 'turn': observation['turn'], 'generation': observation['generation'], 'format': observation['format'], 'attacker_side': observation['attacker_side'], 'attacker_species': observation['attacker_species'], 'attacker_types': observation['attacker_types'], 'attacker_hp': observation['attacker_hp'], 'attacker_status': observation['attacker_status'], 'attacker_boosts_attack': attacker_boosts.get('atk', 0), 'attacker_boosts_defense': attacker_boosts.get('def', 0), 'attacker_boosts_special_attack': attacker_boosts.get('spa', 0), 'attacker_boosts_special_defense': attacker_boosts.get('spd', 0), 'attacker_boosts_speed': attacker_boosts.get('spe', 0), 'attacker_boosts_accuracy': attacker_boosts.get('accuracy', 0), 'attacker_boosts_evasion': attacker_boosts.get('evasion', 0), 'attacker_tera': observation['attacker_tera'], 'move_id': observation['move_id'], 'move_name': observation['move_name'], 'move_type': observation['move_type'], 'move_category': observation['move_category'], 'base_power': observation['base_power'], 'defender_species': observation['defender_species'], 'defender_types': observation['defender_types'], 'defender_hp_before': observation['defender_hp_before'], 'defender_hp_after': observation['defender_hp_after'], 'defender_status': observation['defender_status'], 'defender_boosts_attack': None if defender_boosts is None else defender_boosts.get('atk', 0), 'defender_boosts_defense': None if defender_boosts is None else defender_boosts.get('def', 0), 'defender_boosts_special_attack': None if defender_boosts is None else defender_boosts.get('spa', 0), 'defender_boosts_special_defense': None if defender_boosts is None else defender_boosts.get('spd', 0), 'defender_boosts_speed': None if defender_boosts is None else defender_boosts.get('spe', 0), 'defender_boosts_accuracy': None if defender_boosts is None else defender_boosts.get('accuracy', 0), 'defender_boosts_evasion': None if defender_boosts is None else defender_boosts.get('evasion', 0), 'defender_tera': observation['defender_tera'], 'damage_percentage': observation['damage_percentage'], 'effectiveness': observation['effectiveness'], 'critical_hit': observation['critical_hit'], 'missed': observation['missed'], 'immune': observation['immune'], 'fainted': observation['fainted'], 'attacker_go_first': observation['attacker_go_first'], 'weather': observation['weather'], 'terrain': observation['terrain'], 'attacker_side_conditions_json': json.dumps(observation['attacker_side_conditions'], sort_keys=True), 'defender_side_conditions_json': json.dumps(observation['defender_side_conditions'], sort_keys=True)}
        return row

    @synchronized
    def _insert_move_observations(self, observations: list[dict[str, Any]]) -> bool:
        """Idempotently store empirical move observations."""
        if not observations:
            return True
        conn = self._get_analytics_connection()
        if conn is None:
            return False
        insert_sql = '\n        INSERT INTO move_observations (\n            observation_key,\n\n            battle_id,\n            turn,\n            generation,\n            "format",\n\n            attacker_side,\n            attacker_species,\n            attacker_types,\n            attacker_hp,\n            attacker_status,\n\n            attacker_boosts_attack,\n            attacker_boosts_defense,\n            attacker_boosts_special_attack,\n            attacker_boosts_special_defense,\n            attacker_boosts_speed,\n            attacker_boosts_accuracy,\n            attacker_boosts_evasion,\n\n            attacker_tera,\n\n            move_id,\n            move_name,\n            move_type,\n            move_category,\n            base_power,\n\n            defender_species,\n            defender_types,\n            defender_hp_before,\n            defender_hp_after,\n            defender_status,\n\n            defender_boosts_attack,\n            defender_boosts_defense,\n            defender_boosts_special_attack,\n            defender_boosts_special_defense,\n            defender_boosts_speed,\n            defender_boosts_accuracy,\n            defender_boosts_evasion,\n\n            defender_tera,\n\n            damage_percentage,\n            effectiveness,\n            critical_hit,\n            missed,\n            immune,\n            fainted,\n\n            attacker_go_first,\n\n            weather,\n            terrain,\n\n            attacker_side_conditions,\n            defender_side_conditions\n        )\n        VALUES (\n            %(observation_key)s,\n\n            %(battle_id)s,\n            %(turn)s,\n            %(generation)s,\n            %(format)s,\n\n            %(attacker_side)s,\n            %(attacker_species)s,\n            %(attacker_types)s,\n            %(attacker_hp)s,\n            %(attacker_status)s,\n\n            %(attacker_boosts_attack)s,\n            %(attacker_boosts_defense)s,\n            %(attacker_boosts_special_attack)s,\n            %(attacker_boosts_special_defense)s,\n            %(attacker_boosts_speed)s,\n            %(attacker_boosts_accuracy)s,\n            %(attacker_boosts_evasion)s,\n\n            %(attacker_tera)s,\n\n            %(move_id)s,\n            %(move_name)s,\n            %(move_type)s,\n            %(move_category)s,\n            %(base_power)s,\n\n            %(defender_species)s,\n            %(defender_types)s,\n            %(defender_hp_before)s,\n            %(defender_hp_after)s,\n            %(defender_status)s,\n\n            %(defender_boosts_attack)s,\n            %(defender_boosts_defense)s,\n            %(defender_boosts_special_attack)s,\n            %(defender_boosts_special_defense)s,\n            %(defender_boosts_speed)s,\n            %(defender_boosts_accuracy)s,\n            %(defender_boosts_evasion)s,\n\n            %(defender_tera)s,\n\n            %(damage_percentage)s,\n            %(effectiveness)s,\n            %(critical_hit)s,\n            %(missed)s,\n            %(immune)s,\n            %(fainted)s,\n\n            %(attacker_go_first)s,\n\n            %(weather)s,\n            %(terrain)s,\n\n            %(attacker_side_conditions_json)s::JSONB,\n            %(defender_side_conditions_json)s::JSONB\n        )\n        ON CONFLICT (observation_key) DO NOTHING;\n        '
        rows = [self._observation_to_db_row(observation) for observation in observations]
        try:
            with conn.cursor() as cur:
                cur.executemany(insert_sql, rows)
            return True
        except Exception:
            logger.exception('Could not insert move observations into PostgreSQL.')
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    @synchronized
    def _query_matchup_aggregates(self, attacker_species: str, *, defender_species: str | None=None, defender_types: list[str] | None=None) -> list[dict[str, Any]] | None:
        """Query observed attacks by exact matchup or defender typing."""
        conn = self._get_analytics_connection()
        if conn is None:
            return None
        if defender_species is not None:
            where_clause = '\n                attacker_species = %s\n                AND defender_species = %s\n            '
            params = (attacker_species, defender_species)
        elif defender_types is not None:
            where_clause = '\n                attacker_species = %s\n                AND defender_types = %s\n            '
            params = (attacker_species, defender_types)
        else:
            raise ValueError('Either defender_species or defender_types is required.')
        query = f'\n        SELECT\n            move_id,\n            MAX(move_name) AS move_name,\n            MAX(move_type) AS move_type,\n            MAX(move_category) AS move_category,\n            MAX(base_power) AS base_power,\n\n            COUNT(*) AS uses,\n\n            COUNT(*) FILTER (\n                WHERE\n                    attacker_boosts_attack > 0\n                    OR attacker_boosts_defense > 0\n                    OR attacker_boosts_special_attack > 0\n                    OR attacker_boosts_special_defense > 0\n                    OR attacker_boosts_speed > 0\n                    OR attacker_boosts_accuracy > 0\n                    OR attacker_boosts_evasion > 0\n            ) AS attacker_boosted_uses,\n\n            BOOL_OR(attacker_boosts_attack > 0)\n                AS attacker_boosted_attack,\n            BOOL_OR(attacker_boosts_defense > 0)\n                AS attacker_boosted_defense,\n            BOOL_OR(attacker_boosts_special_attack > 0)\n                AS attacker_boosted_special_attack,\n            BOOL_OR(attacker_boosts_special_defense > 0)\n                AS attacker_boosted_special_defense,\n            BOOL_OR(attacker_boosts_speed > 0)\n                AS attacker_boosted_speed,\n            BOOL_OR(attacker_boosts_accuracy > 0)\n                AS attacker_boosted_accuracy,\n            BOOL_OR(attacker_boosts_evasion > 0)\n                AS attacker_boosted_evasion,\n\n            COUNT(*) FILTER (\n                WHERE\n                    COALESCE(defender_boosts_attack, 0) > 0\n                    OR COALESCE(defender_boosts_defense, 0) > 0\n                    OR COALESCE(defender_boosts_special_attack, 0) > 0\n                    OR COALESCE(defender_boosts_special_defense, 0) > 0\n                    OR COALESCE(defender_boosts_speed, 0) > 0\n                    OR COALESCE(defender_boosts_accuracy, 0) > 0\n                    OR COALESCE(defender_boosts_evasion, 0) > 0\n            ) AS defender_boosted_uses,\n\n            BOOL_OR(COALESCE(defender_boosts_attack, 0) > 0)\n                AS defender_boosted_attack,\n            BOOL_OR(COALESCE(defender_boosts_defense, 0) > 0)\n                AS defender_boosted_defense,\n            BOOL_OR(COALESCE(defender_boosts_special_attack, 0) > 0)\n                AS defender_boosted_special_attack,\n            BOOL_OR(COALESCE(defender_boosts_special_defense, 0) > 0)\n                AS defender_boosted_special_defense,\n            BOOL_OR(COALESCE(defender_boosts_speed, 0) > 0)\n                AS defender_boosted_speed,\n            BOOL_OR(COALESCE(defender_boosts_accuracy, 0) > 0)\n                AS defender_boosted_accuracy,\n            BOOL_OR(COALESCE(defender_boosts_evasion, 0) > 0)\n                AS defender_boosted_evasion,\n\n            COUNT(damage_percentage)\n                AS damage_samples,\n\n            MIN(damage_percentage)\n                AS damage_min,\n\n            MAX(damage_percentage)\n                AS damage_max,\n\n            PERCENTILE_CONT(0.5)\n                WITHIN GROUP (\n                    ORDER BY damage_percentage\n                )\n                FILTER (\n                    WHERE damage_percentage IS NOT NULL\n                )\n                AS damage_median,\n\n            COUNT(*) FILTER (\n                WHERE attacker_go_first IS TRUE\n            ) AS went_first_count,\n\n            COUNT(*) FILTER (\n                WHERE attacker_go_first IS NOT NULL\n            ) AS order_samples,\n\n            COUNT(*) FILTER (\n                WHERE critical_hit IS TRUE\n            ) AS critical_hits,\n\n            COUNT(*) FILTER (\n                WHERE missed IS TRUE\n            ) AS misses,\n\n            COUNT(*) FILTER (\n                WHERE immune IS TRUE\n            ) AS immunities,\n\n            COUNT(*) FILTER (\n                WHERE fainted IS TRUE\n            ) AS defender_faints\n\n        FROM move_observations\n\n        WHERE {where_clause}\n\n        GROUP BY move_id\n\n        ORDER BY\n            COUNT(*) DESC,\n            move_id ASC\n\n        LIMIT 4;\n        '
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return list(cur.fetchall())
        except Exception:
            logger.exception('Could not query historical Pokémon analytics.')
            return None



class HistoricalAnalytics:
    """Build the V4 text/evidence view without storing shared battle state."""

    BOOST_STATS = ObservationExtractor.BOOST_STATS
    BOOST_LABELS = ObservationExtractor.BOOST_LABELS

    def __init__(self, repository: AnalyticsRepository, extractor: ObservationExtractor):
        """Keep evidence local to this single context-build operation."""
        self.repository = repository
        self.extractor = extractor
        self._evidence_for_current_decision: dict[str, Any] = {}

    def build(self, battle: Battle) -> tuple[str, dict[str, Any]]:
        """Return V4's original formatted analytics plus structured evidence."""
        message = self._build_historical_analytics(battle)
        return message, self._evidence_for_current_decision

    def _current_effective_types(self, pokemon) -> list[str]:
        """Read currently effective types, including known type changes."""
        if pokemon is None:
            return []
        try:
            return sorted({self.extractor._normalize_type_name(pokemon_type) for pokemon_type in pokemon.types if pokemon_type is not None})
        except Exception:
            return []

    @staticmethod
    def _format_number(value: Any) -> str:
        """Format a numeric metric for a human-readable report."""
        if value is None:
            return 'unknown'
        value = float(value)
        rounded = round(value, 2)
        if rounded.is_integer():
            return str(int(rounded))
        return str(rounded)

    def _boosted_stats_from_row(self, row: dict[str, Any], prefix: str) -> list[str]:
        """List observed stat boosts associated with SQL aggregate rows."""
        db_names = {'atk': 'attack', 'def': 'defense', 'spa': 'special_attack', 'spd': 'special_defense', 'spe': 'speed', 'accuracy': 'accuracy', 'evasion': 'evasion'}
        stats = []
        for stat in self.BOOST_STATS:
            key = f'{prefix}_boosted_{db_names[stat]}'
            if row.get(key):
                stats.append(self.BOOST_LABELS[stat])
        return stats

    def _format_move_analytics(self, row: dict[str, Any], attacker_display: str, defender_description: str) -> str:
        """Summarize observations for one move and matchup."""
        uses = int(row.get('uses') or 0)
        attacker_boosted_uses = int(row.get('attacker_boosted_uses') or 0)
        defender_boosted_uses = int(row.get('defender_boosted_uses') or 0)
        attacker_boosted_stats = self._boosted_stats_from_row(row, 'attacker')
        defender_boosted_stats = self._boosted_stats_from_row(row, 'defender')
        lines = [f"- {row.get('move_name')}: {uses} observed use(s) against {defender_description}."]
        if attacker_boosted_stats:
            lines.append(f"  Attacker positively boosted in {attacker_boosted_uses}/{uses} uses. Boosted stats observed: {', '.join(attacker_boosted_stats)}.")
        else:
            lines.append(f'  Attacker positively boosted in 0/{uses} uses.')
        if defender_boosted_stats:
            lines.append(f"  Defender positively boosted in {defender_boosted_uses}/{uses} uses. Boosted stats observed: {', '.join(defender_boosted_stats)}.")
        else:
            lines.append(f'  Defender positively boosted in 0/{uses} uses.')
        damage_samples = int(row.get('damage_samples') or 0)
        if damage_samples > 0:
            damage_min = self._format_number(row.get('damage_min'))
            damage_max = self._format_number(row.get('damage_max'))
            damage_median = self._format_number(row.get('damage_median'))
            lines.append(f'  Direct damage samples: {damage_samples}. Observed damage: {damage_min}-{damage_max}%. Median damage: {damage_median}%.')
        else:
            lines.append('  No direct damage samples.')
        order_samples = int(row.get('order_samples') or 0)
        if order_samples > 0:
            went_first_count = int(row.get('went_first_count') or 0)
            lines.append(f'  {attacker_display} acted before the defender in {went_first_count}/{order_samples} comparable turns.')
        else:
            lines.append('  No comparable move-order samples.')
        outcomes = []
        critical_hits = int(row.get('critical_hits') or 0)
        misses = int(row.get('misses') or 0)
        immunities = int(row.get('immunities') or 0)
        defender_faints = int(row.get('defender_faints') or 0)
        if critical_hits:
            outcomes.append(f'{critical_hits} critical hit(s)')
        if misses:
            outcomes.append(f'{misses} miss(es)')
        if immunities:
            outcomes.append(f'{immunities} immunity result(s)')
        if defender_faints:
            outcomes.append(f'{defender_faints} defender faint(s)')
        if outcomes:
            lines.append('  Other outcomes: ' + ', '.join(outcomes) + '.')
        return '\n'.join(lines)

    def _direction_analytics(self, *, attacker_species: str, attacker_display: str,
                             defender_species: str, defender_display: str,
                             defender_types: list[str], direction_title: str) -> str:
        # Capture structured evidence from the SQL result itself. Langfuse
        # and PostgreSQL never have to scrape the formatted LLM prompt.
        """Create structured evidence and a textual summary for one attack direction."""
        direction = ('offense' if direction_title.startswith('OFFENSIVE')
                     else 'defense')

        def save_evidence(level, rows):
            """Retain the SQL-backed evidence quality and sample counts for tracing."""
            moves = []
            for row in (rows or []):
                moves.append({
                    'move_id': row.get('move_id'),
                    'move_name': row.get('move_name'),
                    'uses': int(row.get('uses') or 0),
                    'damage_samples': int(row.get('damage_samples') or 0),
                    'damage_min': row.get('damage_min'),
                    'damage_max': row.get('damage_max'),
                    'damage_median': row.get('damage_median'),
                })
            self._evidence_for_current_decision[direction] = {
                'type': level,
                'attacker_species': attacker_species,
                'defender_species': defender_species,
                'defender_types': list(defender_types),
                'distinct_moves': len(moves),
                'total_uses': sum(m['uses'] for m in moves),
                'moves': moves,
            }

        exact_rows = self.repository._query_matchup_aggregates(
            attacker_species, defender_species=defender_species)
        if exact_rows is None:
            save_evidence('UNAVAILABLE', None)
            return f'{direction_title}\nHistorical analytics unavailable (database connection/query failed).'
        if exact_rows:
            save_evidence('EXACT_MATCHUP', exact_rows)
            lines = [direction_title, 'Evidence level: EXACT attacker + move + defender.']
            for row in exact_rows:
                lines.append(self._format_move_analytics(row, attacker_display, defender_display))
            return '\n'.join(lines)
        type_rows = self.repository._query_matchup_aggregates(
            attacker_species, defender_types=defender_types)
        if type_rows is None:
            save_evidence('UNAVAILABLE', None)
            return f'{direction_title}\nHistorical analytics unavailable (database connection/query failed).'
        if type_rows:
            save_evidence('DEFENDER_TYPE_FALLBACK', type_rows)
            type_display = '/'.join(type_name.title() for type_name in defender_types) or 'unknown-type'
            lines = [direction_title,
                     f'Evidence level: TYPE FALLBACK. No exact {attacker_display} vs {defender_display} observations were found.',
                     f'Using historical observations against {type_display} Pokémon instead.']
            for row in type_rows:
                lines.append(self._format_move_analytics(row, attacker_display,
                                                          f'{type_display} Pokémon'))
            return '\n'.join(lines)
        save_evidence('NO_EVIDENCE', [])
        return f'{direction_title}\nNo empirical evidence.'

    def _build_historical_analytics(self, battle: Battle) -> str:
        """Build offense and defense matchup evidence for the active Pokémon."""
        self._evidence_for_current_decision = {}
        attacker = battle.active_pokemon
        defender = battle.opponent_active_pokemon
        if attacker is None or defender is None:
            self._evidence_for_current_decision = {
                side: {'type': 'UNAVAILABLE', 'distinct_moves': 0,
                       'total_uses': 0, 'moves': []}
                for side in ('offense', 'defense')
            }
            return 'Historical analytics unavailable: one or both active Pokémon are unknown.'
        attacker_display = attacker.species
        defender_display = defender.species
        attacker_species = self.extractor._species_id(attacker_display) or attacker_display
        defender_species = self.extractor._species_id(defender_display) or defender_display
        attacker_types = self._current_effective_types(attacker)
        defender_types = self._current_effective_types(defender)
        offense = self._direction_analytics(attacker_species=attacker_species, attacker_display=attacker_display, defender_species=defender_species, defender_display=defender_display, defender_types=defender_types, direction_title=f'OFFENSIVE EMPIRICAL EVIDENCE: {attacker_display} attacking {defender_display}')
        defense = self._direction_analytics(attacker_species=defender_species, attacker_display=defender_display, defender_species=attacker_species, defender_display=attacker_display, defender_types=attacker_types, direction_title=f'DEFENSIVE EMPIRICAL EVIDENCE: {defender_display} attacking {attacker_display}')
        return offense + '\n\n' + defense

