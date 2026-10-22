"""Replay-to-SQL observation extraction (V3 mechanics used by V4)."""
from __future__ import annotations
import hashlib
from copy import deepcopy
from typing import Any
from poke_env.battle import Battle, Move
from poke_env.data import GenData, to_id_str
from ..utils import logger, normalize_name
from .protocol import ShowdownParser

class ObservationExtractor:
    """Extract move observations independently of database persistence."""

    BOOST_STATS = ('atk', 'def', 'spa', 'spd', 'spe', 'accuracy', 'evasion')
    BOOST_LABELS = {'atk': 'Attack', 'def': 'Defense', 'spa': 'Special Attack', 'spd': 'Special Defense', 'spe': 'Speed', 'accuracy': 'Accuracy', 'evasion': 'Evasion'}

    def __init__(self, parser: ShowdownParser) -> None:
        """Reuse one stateless protocol parser for event metadata."""
        self.parser = parser

    @classmethod
    def _zero_boosts(cls) -> dict[str, int]:
        """Return the initial zero stages for all tracked stats."""
        return {stat: 0 for stat in cls.BOOST_STATS}

    @staticmethod
    def _species_id(species: str | None) -> str | None:
        """Normalize a species to a stable database ID."""
        if not species:
            return None
        return to_id_str(species)

    @staticmethod
    def _normalize_type_name(type_value: Any) -> str:
        """Normalize elemental type names for analytics."""
        if type_value is None:
            return ''
        if hasattr(type_value, 'name'):
            return str(type_value.name).lower()
        value = str(type_value)
        if '.' in value:
            value = value.rsplit('.', 1)[-1]
        return to_id_str(value)

    @staticmethod
    def _clean_effect_name(value: str | None) -> str | None:
        """Normalize an effect name from Showdown protocol text."""
        if not value:
            return None
        cleaned = value.strip()
        for prefix in ('move:', 'ability:', 'item:'):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        return cleaned

    def _base_types_for_species(self, species: str | None, generation: int) -> list[str]:
        """Read base typing from the generation-specific species dex."""
        species_id = self._species_id(species)
        if not species_id:
            return []
        try:
            entry = GenData.from_gen(generation).pokedex[species_id]
            return sorted({to_id_str(type_name) for type_name in entry.get('types', []) if type_name})
        except Exception:
            logger.debug('Could not resolve types for species %s', species, exc_info=True)
            return []

    def _move_metadata(self, move_name: str, generation: int) -> dict[str, Any]:
        """Resolve move type, category and power from the generation dex."""
        move_id = normalize_name(move_name)
        try:
            move_id = Move.retrieve_id(move_name)
            move = Move(move_id=move_id, raw_id=move_name, gen=generation)
            move_type = self._normalize_type_name(move.type) if move.type is not None else None
            move_category = move.category.name.lower() if move.category is not None else None
            return {'move_id': move.id, 'move_name': move_name, 'move_type': move_type, 'move_category': move_category, 'base_power': move.base_power}
        except Exception:
            logger.debug('Could not resolve move metadata for %s', move_name, exc_info=True)
            return {'move_id': move_id, 'move_name': move_name, 'move_type': None, 'move_category': None, 'base_power': None}

    def _make_observation_key(self, battle: Battle, turn: int, event_index: int, actor: str, move_id: str) -> str:
        """Build a deterministic ID for idempotent observation writes."""
        raw_key = f'{battle.battle_tag}|{turn}|{event_index}|{actor}|{move_id}'
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    def _extract_turn_move_observations(self, battle: Battle, target_turn: int) -> list[dict[str, Any]] | None:
        """Extract empirical move records from a completed replay turn."""
        replay_data = getattr(battle, '_replay_data', None)
        if replay_data is None:
            return None
        generation = battle.gen
        battle_format = battle.format
        hp_by_slot: dict[str, float] = {}
        status_by_slot: dict[str, str | None] = {}
        species_by_slot: dict[str, str] = {}
        base_types_by_slot: dict[str, list[str]] = {}
        effective_types_by_slot: dict[str, list[str]] = {}
        boosts_by_slot: dict[str, dict[str, int]] = {}
        tera_by_slot: dict[str, str | None] = {}
        tera_by_pokemon: dict[tuple[str, str], str] = {}
        weather: str | None = None
        terrain: str | None = None
        side_conditions: dict[str, dict[str, int]] = {'agent': {}, 'opponent': {}}
        current_turn: int | None = None
        found_target_turn = False
        observations: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        move_order = 0

        def finalize_pending():
            """Turn the current in-progress move into one complete observation."""
            nonlocal pending
            if pending is None:
                return
            move_category = pending.get('move_category')
            hp_before = pending.get('defender_hp_before')
            hp_after = pending.get('defender_hp_after')
            if pending.get('defender_species') is not None and move_category != 'status':
                if hp_before is not None and hp_after is not None:
                    pending['damage_percentage'] = round(max(0.0, hp_before - hp_after), 2)
                elif pending.get('missed') or pending.get('immune'):
                    pending['damage_percentage'] = 0.0
                else:
                    pending['damage_percentage'] = 0.0
                if pending.get('immune'):
                    pending['effectiveness'] = 'immune'
                elif pending.get('effectiveness') is None:
                    pending['effectiveness'] = 'neutral'
            else:
                pending['damage_percentage'] = None
            observations.append(pending)
            pending = None

        def current_side_conditions(side: str | None) -> dict[str, int]:
            """Retrieve the latest known side-condition state for an actor."""
            if side not in side_conditions:
                return {}
            return deepcopy(side_conditions[side])

        def start_move_observation(raw_event: list[Any], event_index: int) -> dict[str, Any] | None:
            """Begin recording metadata for an observed Showdown move."""
            nonlocal move_order
            if len(raw_event) < 4:
                return None
            actor = raw_event[2]
            move_name = raw_event[3]
            target = raw_event[4] if len(raw_event) > 4 else None
            attacker_slot = self.parser._slot_from_actor(actor)
            defender_slot = self.parser._slot_from_actor(target)
            attacker_side = self.parser._side_from_reference(battle, actor)
            defender_side = self.parser._side_from_reference(battle, target)
            attacker_species = species_by_slot.get(attacker_slot) if attacker_slot else None
            if attacker_species is None:
                attacker_species = self._species_id(self.parser._pokemon_from_actor(actor))
            defender_species = species_by_slot.get(defender_slot) if defender_slot else None
            metadata = self._move_metadata(move_name, generation)
            move_order += 1
            attacker_boosts = deepcopy(boosts_by_slot.get(attacker_slot, self._zero_boosts()))
            if defender_slot is not None:
                defender_boosts = deepcopy(boosts_by_slot.get(defender_slot, self._zero_boosts()))
            else:
                defender_boosts = None
            observation = {'observation_key': self._make_observation_key(battle, target_turn, event_index, actor, metadata['move_id']), 'battle_id': battle.battle_tag, 'turn': target_turn, 'generation': generation, 'format': battle_format, 'attacker_side': attacker_side, 'attacker_species': attacker_species or self._species_id(self.parser._pokemon_from_actor(actor)) or 'unknown', 'attacker_types': list(effective_types_by_slot.get(attacker_slot, [])), 'attacker_hp': hp_by_slot.get(attacker_slot), 'attacker_status': status_by_slot.get(attacker_slot), 'attacker_boosts': attacker_boosts, 'attacker_tera': tera_by_slot.get(attacker_slot), **metadata, 'defender_species': defender_species, 'defender_types': list(effective_types_by_slot.get(defender_slot, [])) if defender_slot else [], 'defender_hp_before': hp_by_slot.get(defender_slot) if defender_slot else None, 'defender_hp_after': hp_by_slot.get(defender_slot) if defender_slot else None, 'defender_status': status_by_slot.get(defender_slot) if defender_slot else None, 'defender_boosts': defender_boosts, 'defender_tera': tera_by_slot.get(defender_slot) if defender_slot else None, 'damage_percentage': None, 'effectiveness': None, 'critical_hit': False, 'missed': False, 'immune': False, 'fainted': False, 'attacker_go_first': None, 'weather': weather, 'terrain': terrain, 'attacker_side_conditions': current_side_conditions(attacker_side), 'defender_side_conditions': current_side_conditions(defender_side), '_attacker_slot': attacker_slot, '_defender_slot': defender_slot, '_move_order': move_order}
            return observation
        for event_index, raw_event in enumerate(replay_data):
            if len(raw_event) < 2:
                continue
            event_type = raw_event[1]
            if event_type == 'turn':
                if len(raw_event) < 3:
                    continue
                try:
                    new_turn = int(raw_event[2])
                except (TypeError, ValueError):
                    continue
                if found_target_turn and new_turn > target_turn:
                    finalize_pending()
                    break
                current_turn = new_turn
                if current_turn == target_turn:
                    found_target_turn = True
                continue
            in_target_turn = current_turn == target_turn
            if event_type == 'move':
                if in_target_turn:
                    finalize_pending()
                    pending = start_move_observation(raw_event, event_index)
                continue
            if in_target_turn and pending is not None:
                defender_slot = pending.get('_defender_slot')
                attacker_slot = pending.get('_attacker_slot')
                if event_type == '-damage':
                    if len(raw_event) >= 4:
                        actor = raw_event[2]
                        slot = self.parser._slot_from_actor(actor)
                        hp_after, _ = self.parser._parse_hp_status(raw_event[3])
                        tags = self.parser._extract_protocol_tags(raw_event[4:])
                        cause = tags.get('from')
                        current_species = species_by_slot.get(slot) if slot else None
                        if cause is None:
                            direct_damage = True
                        elif str(cause).lower().startswith('move:'):
                            caused_move = normalize_name(self._clean_effect_name(str(cause)) or '')
                            direct_damage = caused_move == pending.get('move_id')
                        else:
                            direct_damage = False
                        if slot == defender_slot and current_species == pending.get('defender_species') and direct_damage and (hp_after is not None):
                            pending['defender_hp_after'] = hp_after
                elif event_type == '-supereffective':
                    if len(raw_event) >= 3:
                        slot = self.parser._slot_from_actor(raw_event[2])
                        if slot == defender_slot:
                            pending['effectiveness'] = 'super_effective'
                elif event_type == '-resisted':
                    if len(raw_event) >= 3:
                        slot = self.parser._slot_from_actor(raw_event[2])
                        if slot == defender_slot:
                            pending['effectiveness'] = 'not_very_effective'
                elif event_type == '-immune':
                    if len(raw_event) >= 3:
                        slot = self.parser._slot_from_actor(raw_event[2])
                        if slot == defender_slot:
                            pending['immune'] = True
                            pending['effectiveness'] = 'immune'
                elif event_type == '-crit':
                    if len(raw_event) >= 3:
                        slot = self.parser._slot_from_actor(raw_event[2])
                        if slot == defender_slot:
                            pending['critical_hit'] = True
                elif event_type == '-miss':
                    source = raw_event[2] if len(raw_event) > 2 else None
                    if self.parser._slot_from_actor(source) == attacker_slot:
                        pending['missed'] = True
                elif event_type == 'faint':
                    if len(raw_event) >= 3:
                        slot = self.parser._slot_from_actor(raw_event[2])
                        if slot == defender_slot:
                            pending['fainted'] = True
                            pending['defender_hp_after'] = 0.0
                if event_type in {'switch', 'drag'}:
                    finalize_pending()
            if event_type in {'switch', 'drag'}:
                if len(raw_event) < 4:
                    continue
                actor = raw_event[2]
                details = raw_event[3]
                hp_status = raw_event[4] if len(raw_event) > 4 else None
                slot = self.parser._slot_from_actor(actor)
                if slot is None:
                    continue
                side = self.parser._side_from_reference(battle, actor)
                species = self.parser._species_from_details(details)
                species_id = self._species_id(species) or self._species_id(self.parser._pokemon_from_actor(actor)) or 'unknown'
                hp_percentage, status = self.parser._parse_hp_status(hp_status)
                base_types = self._base_types_for_species(species, generation)
                stored_tera = tera_by_pokemon.get((side or 'unknown', species_id))
                species_by_slot[slot] = species_id
                base_types_by_slot[slot] = base_types
                if stored_tera:
                    effective_types_by_slot[slot] = [stored_tera]
                else:
                    effective_types_by_slot[slot] = list(base_types)
                tera_by_slot[slot] = stored_tera
                hp_by_slot[slot] = hp_percentage if hp_percentage is not None else hp_by_slot.get(slot, 100.0)
                status_by_slot[slot] = None if status == 'fnt' else status
                boosts_by_slot[slot] = self._zero_boosts()
                continue
            if event_type in {'-damage', '-heal'}:
                if len(raw_event) >= 4:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    hp_after, status = self.parser._parse_hp_status(raw_event[3])
                    if slot is not None and hp_after is not None:
                        hp_by_slot[slot] = hp_after
                    if slot is not None and status and (status != 'fnt'):
                        status_by_slot[slot] = status
                continue
            if event_type == '-sethp':
                index = 2
                while index + 1 < len(raw_event):
                    actor = raw_event[index]
                    hp_status = raw_event[index + 1]
                    if not isinstance(actor, str) or actor.startswith('['):
                        break
                    slot = self.parser._slot_from_actor(actor)
                    hp_after, status = self.parser._parse_hp_status(hp_status)
                    if slot is not None and hp_after is not None:
                        hp_by_slot[slot] = hp_after
                    if slot is not None and status and (status != 'fnt'):
                        status_by_slot[slot] = status
                    index += 2
                continue
            if event_type == 'faint':
                if len(raw_event) >= 3:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        hp_by_slot[slot] = 0.0
                continue
            if event_type == '-status':
                if len(raw_event) >= 4:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        status_by_slot[slot] = raw_event[3]
                continue
            if event_type == '-curestatus':
                if len(raw_event) >= 3:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        status_by_slot[slot] = None
                continue
            if event_type == '-cureteam':
                side_ref = raw_event[2] if len(raw_event) > 2 else None
                cured_side = self.parser._side_from_reference(battle, side_ref)
                for slot in list(status_by_slot):
                    slot_side = self.parser._side_from_reference(battle, slot)
                    if slot_side == cured_side:
                        status_by_slot[slot] = None
                continue
            if event_type in {'-boost', '-unboost', '-setboost'}:
                if len(raw_event) < 5:
                    continue
                slot = self.parser._slot_from_actor(raw_event[2])
                stat = raw_event[3]
                if slot is None or stat not in self.BOOST_STATS:
                    continue
                boosts = boosts_by_slot.setdefault(slot, self._zero_boosts())
                try:
                    amount = int(raw_event[4])
                except (TypeError, ValueError):
                    continue
                if event_type == '-boost':
                    boosts[stat] = min(6, boosts.get(stat, 0) + amount)
                elif event_type == '-unboost':
                    boosts[stat] = max(-6, boosts.get(stat, 0) - amount)
                else:
                    boosts[stat] = max(-6, min(6, amount))
                continue
            if event_type == '-clearboost':
                if len(raw_event) >= 3:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        boosts_by_slot[slot] = self._zero_boosts()
                continue
            if event_type == '-clearnegativeboost':
                if len(raw_event) >= 3:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        boosts = boosts_by_slot.setdefault(slot, self._zero_boosts())
                        for stat in self.BOOST_STATS:
                            if boosts[stat] < 0:
                                boosts[stat] = 0
                continue
            if event_type == '-invertboost':
                if len(raw_event) >= 3:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    if slot is not None:
                        boosts = boosts_by_slot.setdefault(slot, self._zero_boosts())
                        for stat in self.BOOST_STATS:
                            boosts[stat] = max(-6, min(6, -boosts[stat]))
                continue
            if event_type == '-clearallboost':
                for slot in list(boosts_by_slot):
                    boosts_by_slot[slot] = self._zero_boosts()
                continue
            if event_type == '-terastallize':
                if len(raw_event) < 4:
                    continue
                actor = raw_event[2]
                slot = self.parser._slot_from_actor(actor)
                if slot is None:
                    continue
                side = self.parser._side_from_reference(battle, actor)
                tera_type = self._normalize_type_name(raw_event[3])
                species_id = species_by_slot.get(slot)
                tera_by_slot[slot] = tera_type
                effective_types_by_slot[slot] = [tera_type]
                if species_id:
                    tera_by_pokemon[side or 'unknown', species_id] = tera_type
                continue
            if event_type in {'detailschange', '-formechange'}:
                if len(raw_event) < 4:
                    continue
                actor = raw_event[2]
                slot = self.parser._slot_from_actor(actor)
                if slot is None:
                    continue
                new_species = self.parser._species_from_details(raw_event[3])
                species_id = self._species_id(new_species)
                if species_id:
                    species_by_slot[slot] = species_id
                    base_types = self._base_types_for_species(new_species, generation)
                    base_types_by_slot[slot] = base_types
                    if tera_by_slot.get(slot):
                        effective_types_by_slot[slot] = [tera_by_slot[slot]]
                    else:
                        effective_types_by_slot[slot] = list(base_types)
                continue
            if event_type == '-start':
                if len(raw_event) >= 4:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    effect = str(raw_event[3]).lower()
                    if slot is not None and effect == 'typechange' and (len(raw_event) >= 5) and (not tera_by_slot.get(slot)):
                        raw_types = str(raw_event[4]).split('/')
                        effective_types_by_slot[slot] = sorted({self._normalize_type_name(type_name) for type_name in raw_types if type_name})
                    elif slot is not None and effect == 'typeadd' and (len(raw_event) >= 5) and (not tera_by_slot.get(slot)):
                        added_type = self._normalize_type_name(raw_event[4])
                        current_types = set(effective_types_by_slot.get(slot, []))
                        current_types.add(added_type)
                        effective_types_by_slot[slot] = sorted(current_types)
                continue
            if event_type == '-end':
                if len(raw_event) >= 4:
                    slot = self.parser._slot_from_actor(raw_event[2])
                    effect = str(raw_event[3]).lower()
                    if slot is not None and effect in {'typechange', 'typeadd'} and (not tera_by_slot.get(slot)):
                        effective_types_by_slot[slot] = list(base_types_by_slot.get(slot, []))
                continue
            if event_type == '-weather':
                if len(raw_event) >= 3:
                    new_weather = str(raw_event[2])
                    if new_weather.lower() in {'none', 'null'}:
                        weather = None
                    else:
                        weather = self._clean_effect_name(new_weather)
                continue
            if event_type in {'-fieldstart', '-fieldend'}:
                if len(raw_event) >= 3:
                    field_name = self._clean_effect_name(str(raw_event[2]))
                    if field_name and 'terrain' in field_name.lower():
                        if event_type == '-fieldstart':
                            terrain = field_name
                        elif terrain == field_name:
                            terrain = None
                continue
            if event_type in {'-sidestart', '-sideend'}:
                if len(raw_event) < 4:
                    continue
                side = self.parser._side_from_reference(battle, raw_event[2])
                condition = self._clean_effect_name(str(raw_event[3]))
                if side not in side_conditions or not condition:
                    continue
                if event_type == '-sidestart':
                    side_conditions[side][condition] = side_conditions[side].get(condition, 0) + 1
                else:
                    side_conditions[side].pop(condition, None)
                continue
        if found_target_turn:
            finalize_pending()
        if not found_target_turn:
            return None
        agent_orders = [observation['_move_order'] for observation in observations if observation['attacker_side'] == 'agent']
        opponent_orders = [observation['_move_order'] for observation in observations if observation['attacker_side'] == 'opponent']
        for observation in observations:
            if observation['attacker_side'] == 'agent' and opponent_orders:
                observation['attacker_go_first'] = observation['_move_order'] < min(opponent_orders)
            elif observation['attacker_side'] == 'opponent' and agent_orders:
                observation['attacker_go_first'] = observation['_move_order'] < min(agent_orders)
            else:
                observation['attacker_go_first'] = None
            observation.pop('_attacker_slot', None)
            observation.pop('_defender_slot', None)
            observation.pop('_move_order', None)
        return observations

