"""Pure Showdown replay decoding, extracted without changing V4 event semantics.

The parser has no database, model, or battle-session dependencies. Its long
normalization and narration tables are deliberately retained for parity.
"""
from __future__ import annotations
from typing import Any
from poke_env.battle import Battle

class ShowdownParser:
    """Decode replay events and render only completed turns into readable history."""

    @staticmethod
    def _slot_from_actor(
        actor: str | None,
    ) -> str | None:
        """
        Examples:

            p1a: Gallade
                ->
            p1a

            p2a: Gyarados
                ->
            p2a
        """

        if not actor:
            return None

        if ":" in actor:
            return actor.split(":", 1)[0].strip()

        return actor.strip()

    @staticmethod
    def _pokemon_from_actor(
        actor: str | None,
    ) -> str | None:
        """
        Examples:

            p1a: Gallade
                ->
            Gallade

            p2a: Gyarados
                ->
            Gyarados
        """

        if not actor:
            return None

        if ":" in actor:
            return actor.split(":", 1)[1].strip()

        return actor.strip()

    @staticmethod
    def _species_from_details(
        details: str | None,
    ) -> str | None:
        """
        Example:

            Landorus-Therian, L80, M
                ->
            Landorus-Therian
        """

        if not details:
            return None

        return details.split(",")[0].strip()

    def _side_from_reference(
        self,
        battle: Battle,
        reference: str | None,
    ) -> str | None:
        """
        Convert Showdown's p1 / p2 into:

            agent
            opponent
        """

        if not reference:
            return None

        reference = reference.strip()

        player_role = battle.player_role

        if player_role not in {"p1", "p2"}:
            return None

        opponent_role = (
            "p2"
            if player_role == "p1"
            else "p1"
        )

        if reference.startswith(player_role):
            return "agent"

        if reference.startswith(opponent_role):
            return "opponent"

        return None

    def _side_display(
        self,
        side: str | None,
    ) -> str:
        """
        Human-readable side name for the Showdown log.
        """

        if side == "agent":
            return "Agent"

        if side == "opponent":
            return "Opponent"

        return "Unknown"

    @staticmethod
    def _pretty_stat(
        stat: str | None,
    ) -> str:
        """
        Convert protocol stat names into readable names.
        """

        names = {
            "atk": "Attack",
            "def": "Defense",
            "spa": "Special Attack",
            "spd": "Special Defense",
            "spe": "Speed",
            "accuracy": "Accuracy",
            "evasion": "Evasion",
        }

        if stat is None:
            return "stat"

        return names.get(
            stat.lower(),
            stat,
        )

    @staticmethod
    def _parse_hp_status(
        hp_status: str | None,
    ) -> tuple[float | None, str | None]:
        """
        Parse Showdown HP strings.

        Examples:

            75/100
                ->
            (75.0, None)

            150/300 par
                ->
            (50.0, "par")

            0 fnt
                ->
            (0.0, "fnt")

            0/100 fnt
                ->
            (0.0, "fnt")
        """

        if not hp_status:
            return None, None

        parts = hp_status.strip().split()

        if not parts:
            return None, None

        hp_part = parts[0]

        status = (
            parts[1]
            if len(parts) > 1
            else None
        )

        # Some faint messages can use simply:
        #
        # 0 fnt
        if hp_part == "0":
            return 0.0, status

        if "/" not in hp_part:
            return None, status

        try:
            current_text, maximum_text = (
                hp_part.split("/", 1)
            )

            current = float(current_text)
            maximum = float(maximum_text)

            if maximum <= 0:
                return None, status

            percentage = (
                current / maximum
            ) * 100.0

            return round(
                percentage,
                2,
            ), status

        except (
            TypeError,
            ValueError,
            ZeroDivisionError,
        ):
            return None, status

    @staticmethod
    def _format_percentage(
        value: float | None,
    ) -> str:
        """
        Make percentages easier to read.

        75.0 -> "75"
        24.37 -> "24.37"
        """

        if value is None:
            return "unknown"

        rounded = round(value, 2)

        if rounded.is_integer():
            return str(int(rounded))

        return str(rounded)

    @staticmethod
    def _extract_protocol_tags(
        values: list[Any],
    ) -> dict[str, Any]:
        """
        Extract Showdown protocol annotations.

        Examples:

            [from] ability: Intimidate
            [of] p2a: Gyarados
            [silent]
        """

        tags: dict[str, Any] = {}

        for value in values:

            if not isinstance(value, str):
                continue

            if value.startswith("[from]"):

                tags["from"] = (
                    value[
                        len("[from]"):
                    ].strip()
                )

            elif value.startswith("[of]"):

                tags["of"] = (
                    value[
                        len("[of]"):
                    ].strip()
                )

            elif value.startswith("["):

                tag = value.strip(
                    "[]"
                ).strip()

                if tag:
                    tags[tag] = True

        return tags

    def _normalize_protocol_event(
        self,
        battle: Battle,
        event: list[Any],
        hp_by_slot: dict[str, float],
        active_by_slot: dict[str, str],
        fainted_slots: set[str],
    ) -> dict[str, Any] | list[dict[str, Any]] | None:

        """Translate an individual Showdown protocol event into normalized battle facts."""
        if len(event) < 2:
            return None

        event_type = event[1]


        # ========================================================
        # MOVE
        # ========================================================

        if event_type == "move":

            if len(event) < 4:
                return None

            actor = event[2]
            move = event[3]

            target = (
                event[4]
                if len(event) > 4
                else None
            )

            return {
                "type": "move",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "move": move,

                "target": self._pokemon_from_actor(
                    target
                ),

                "target_side": self._side_from_reference(
                    battle,
                    target,
                ),
            }


        # ========================================================
        # SWITCH / DRAG
        # ========================================================

        if event_type in {
            "switch",
            "drag",
        }:

            if len(event) < 4:
                return None

            actor = event[2]
            details = event[3]

            hp_status = (
                event[4]
                if len(event) > 4
                else None
            )

            slot = self._slot_from_actor(
                actor
            )

            previous_pokemon = (
                active_by_slot.get(slot)
                if slot is not None
                else None
            )

            pokemon = (
                self._pokemon_from_actor(
                    actor
                )
                or self._species_from_details(
                    details
                )
            )

            hp_percentage, status = (
                self._parse_hp_status(
                    hp_status
                )
            )

            # If that side's previous Pokémon fainted,
            # this is a replacement, not a voluntary switch.
            is_faint_replacement = (
                slot in fainted_slots
                if slot is not None
                else False
            )

            if slot is not None:

                active_by_slot[slot] = pokemon

                if hp_percentage is not None:
                    hp_by_slot[slot] = (
                        hp_percentage
                    )

                if is_faint_replacement:
                    fainted_slots.discard(slot)

            return {
                "type": (
                    "forced_switch"
                    if event_type == "drag"
                    else "switch"
                ),

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "from": previous_pokemon,
                "to": pokemon,

                "hp_percentage": hp_percentage,
                "status": status,

                "faint_replacement":
                    is_faint_replacement,
            }


        # ========================================================
        # DAMAGE
        # ========================================================

        if event_type == "-damage":

            if len(event) < 4:
                return None

            actor = event[2]
            hp_status = event[3]

            slot = self._slot_from_actor(
                actor
            )

            hp_before = (
                hp_by_slot.get(slot)
                if slot is not None
                else None
            )

            hp_after, status = (
                self._parse_hp_status(
                    hp_status
                )
            )

            hp_lost = None

            if (
                hp_before is not None
                and hp_after is not None
            ):

                hp_lost = max(
                    0.0,
                    hp_before - hp_after,
                )

                hp_lost = round(
                    hp_lost,
                    2,
                )

            if (
                slot is not None
                and hp_after is not None
            ):
                hp_by_slot[slot] = hp_after

            tags = self._extract_protocol_tags(
                event[4:]
            )

            source_reference = tags.get(
                "of"
            )

            return {
                "type": "damage",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "hp_before": hp_before,
                "hp_after": hp_after,
                "hp_lost": hp_lost,

                "status": status,

                "cause": tags.get("from"),

                "source": (
                    self._pokemon_from_actor(
                        source_reference
                    )
                    if source_reference
                    else None
                ),
            }


        # ========================================================
        # HEALING
        # ========================================================

        if event_type == "-heal":

            if len(event) < 4:
                return None

            actor = event[2]
            hp_status = event[3]

            slot = self._slot_from_actor(
                actor
            )

            hp_before = (
                hp_by_slot.get(slot)
                if slot is not None
                else None
            )

            hp_after, status = (
                self._parse_hp_status(
                    hp_status
                )
            )

            hp_healed = None

            if (
                hp_before is not None
                and hp_after is not None
            ):

                hp_healed = max(
                    0.0,
                    hp_after - hp_before,
                )

                hp_healed = round(
                    hp_healed,
                    2,
                )

            if (
                slot is not None
                and hp_after is not None
            ):
                hp_by_slot[slot] = hp_after

            tags = self._extract_protocol_tags(
                event[4:]
            )

            return {
                "type": "heal",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "hp_before": hp_before,
                "hp_after": hp_after,
                "hp_healed": hp_healed,

                "status": status,

                "cause": tags.get("from"),
            }


        # ========================================================
        # SET HP
        #
        # Used by things such as Pain Split.
        # ========================================================

        if event_type == "-sethp":

            results = []

            index = 2

            while index + 1 < len(event):

                actor = event[index]
                hp_status = event[index + 1]

                if not isinstance(
                    actor,
                    str,
                ):
                    break

                if actor.startswith("["):
                    break

                slot = self._slot_from_actor(
                    actor
                )

                hp_before = (
                    hp_by_slot.get(slot)
                    if slot is not None
                    else None
                )

                hp_after, status = (
                    self._parse_hp_status(
                        hp_status
                    )
                )

                if (
                    slot is not None
                    and hp_after is not None
                ):
                    hp_by_slot[slot] = hp_after

                results.append({
                    "type": "set_hp",

                    "side":
                        self._side_from_reference(
                            battle,
                            actor,
                        ),

                    "pokemon":
                        self._pokemon_from_actor(
                            actor
                        ),

                    "hp_before": hp_before,
                    "hp_after": hp_after,

                    "status": status,
                })

                index += 2

            return results or None


        # ========================================================
        # FAINT
        # ========================================================

        if event_type == "faint":

            if len(event) < 3:
                return None

            actor = event[2]

            slot = self._slot_from_actor(
                actor
            )

            if slot is not None:
                hp_by_slot[slot] = 0.0
                fainted_slots.add(slot)

            return {
                "type": "faint",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),
            }


        # ========================================================
        # STAT BOOST
        # ========================================================

        if event_type == "-boost":

            if len(event) < 5:
                return None

            actor = event[2]

            try:
                stages = int(event[4])

            except (
                ValueError,
                TypeError,
            ):
                stages = event[4]

            tags = self._extract_protocol_tags(
                event[5:]
            )

            return {
                "type": "boost",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "stat": event[3],
                "stages": stages,

                "cause": tags.get("from"),
            }


        # ========================================================
        # STAT DROP
        # ========================================================

        if event_type == "-unboost":

            if len(event) < 5:
                return None

            actor = event[2]

            try:
                stages = int(event[4])

            except (
                ValueError,
                TypeError,
            ):
                stages = event[4]

            tags = self._extract_protocol_tags(
                event[5:]
            )

            return {
                "type": "stat_drop",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "stat": event[3],
                "stages": stages,

                "cause": tags.get("from"),
            }


        # ========================================================
        # SET BOOST
        # ========================================================

        if event_type == "-setboost":

            if len(event) < 5:
                return None

            actor = event[2]

            return {
                "type": "set_boost",

                "side": self._side_from_reference(
                    battle,
                    actor,
                ),

                "pokemon": self._pokemon_from_actor(
                    actor
                ),

                "stat": event[3],
                "stages": event[4],
            }


        # ========================================================
        # BOOST CLEARING
        # ========================================================

        if event_type in {
            "-clearboost",
            "-clearnegativeboost",
            "-invertboost",
        }:

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": event_type.lstrip("-"),

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        if event_type == "-clearallboost":

            return {
                "type": "clear_all_boosts"
            }


        # ========================================================
        # SUPER EFFECTIVE
        # ========================================================

        if event_type == "-supereffective":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "super_effective",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        # ========================================================
        # NOT VERY EFFECTIVE
        # ========================================================

        if event_type == "-resisted":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type":
                    "not_very_effective",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        # ========================================================
        # IMMUNITY
        # ========================================================

        if event_type == "-immune":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "immune",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        # ========================================================
        # CRITICAL HIT
        # ========================================================

        if event_type == "-crit":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "critical_hit",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        # ========================================================
        # MISS
        # ========================================================

        if event_type == "-miss":

            source = (
                event[2]
                if len(event) > 2
                else None
            )

            target = (
                event[3]
                if len(event) > 3
                else None
            )

            return {
                "type": "miss",

                "side":
                    self._side_from_reference(
                        battle,
                        source,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        source
                    ),

                "target":
                    self._pokemon_from_actor(
                        target
                    ),
            }


        # ========================================================
        # FAILED MOVE
        # ========================================================

        if event_type == "-fail":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "fail",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),
            }


        # ========================================================
        # CANNOT MOVE
        # ========================================================

        if event_type == "cant":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "cant_move",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "reason": (
                    event[3]
                    if len(event) > 3
                    else None
                ),

                "move": (
                    event[4]
                    if len(event) > 4
                    else None
                ),
            }


        # ========================================================
        # TERASTALLIZATION
        # ========================================================

        if event_type == "-terastallize":

            if len(event) < 4:
                return None

            actor = event[2]

            return {
                "type": "terastallize",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "tera_type": event[3],
            }


        # ========================================================
        # STATUS
        # ========================================================

        if event_type == "-status":

            if len(event) < 4:
                return None

            actor = event[2]

            tags = self._extract_protocol_tags(
                event[4:]
            )

            return {
                "type": "status",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "status": event[3],

                "cause": tags.get("from"),
            }


        # ========================================================
        # STATUS CURED
        # ========================================================

        if event_type == "-curestatus":

            if len(event) < 4:
                return None

            actor = event[2]

            return {
                "type": "status_cured",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "status": event[3],
            }


        # ========================================================
        # ABILITY
        # ========================================================

        if event_type == "-ability":

            if len(event) < 4:
                return None

            actor = event[2]

            return {
                "type": "ability",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "ability": event[3],
            }


        # ========================================================
        # ITEM
        # ========================================================

        if event_type in {
            "-item",
            "-enditem",
        }:

            if len(event) < 4:
                return None

            actor = event[2]

            return {
                "type": (
                    "item"
                    if event_type == "-item"
                    else "item_lost"
                ),

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "item": event[3],
            }


        # ========================================================
        # VOLATILE EFFECT
        # ========================================================

        if event_type in {
            "-start",
            "-end",
        }:

            if len(event) < 4:
                return None

            actor = event[2]

            return {
                "type": (
                    "effect_started"
                    if event_type == "-start"
                    else "effect_ended"
                ),

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "effect": event[3],
            }


        # ========================================================
        # ACTIVATION
        # ========================================================

        if event_type == "-activate":

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "activate",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "effect": (
                    event[3]
                    if len(event) > 3
                    else None
                ),
            }


        # ========================================================
        # WEATHER
        # ========================================================

        if event_type == "-weather":

            weather = (
                event[2]
                if len(event) > 2
                else None
            )

            tags = self._extract_protocol_tags(
                event[3:]
            )

            return {
                "type": "weather",
                "weather": weather,
                "cause": tags.get("from"),
            }


        # ========================================================
        # FIELD / TERRAIN
        # ========================================================

        if event_type in {
            "-fieldstart",
            "-fieldend",
        }:

            return {
                "type": (
                    "field_started"
                    if event_type == "-fieldstart"
                    else "field_ended"
                ),

                "field": (
                    event[2]
                    if len(event) > 2
                    else None
                ),
            }


        # ========================================================
        # SIDE CONDITION
        # ========================================================

        if event_type in {
            "-sidestart",
            "-sideend",
        }:

            side_reference = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": (
                    "side_condition_started"
                    if event_type == "-sidestart"
                    else "side_condition_ended"
                ),

                "side":
                    self._side_from_reference(
                        battle,
                        side_reference,
                    ),

                "condition": (
                    event[3]
                    if len(event) > 3
                    else None
                ),
            }


        # ========================================================
        # FORME CHANGE
        # ========================================================

        if event_type in {
            "detailschange",
            "-formechange",
        }:

            actor = (
                event[2]
                if len(event) > 2
                else None
            )

            return {
                "type": "forme_change",

                "side":
                    self._side_from_reference(
                        battle,
                        actor,
                    ),

                "pokemon":
                    self._pokemon_from_actor(
                        actor
                    ),

                "forme":
                    self._species_from_details(
                        event[3]
                        if len(event) > 3
                        else None
                    ),
            }


        # ========================================================
        # IGNORE NON-BATTLE INFORMATION
        # ========================================================

        ignored_events = {
            "",
            "turn",
            "upkeep",
            "t",
            "request",
            "inactive",
            "inactiveoff",
            "sentchoice",
            "choice",
            "debug",
            "rule",
            "gametype",
            "player",
            "teamsize",
            "gen",
            "tier",
            "rated",
            "seed",
            "clearpoke",
            "poke",
            "teampreview",
            "start",
            "win",
            "tie",
        }

        if event_type in ignored_events:
            return None

        # Other uncommon events are intentionally not sent to the
        # model instead of dumping raw protocol / JSON.
        return None

    def _event_to_showdown_lines(
        self,
        event: dict[str, Any],
    ) -> list[str]:
        """
        Convert one temporary structured event into readable Showdown
        history.

        Structured data disappears after this step.
        """

        event_type = event.get("type")

        side = self._side_display(
            event.get("side")
        )

        pokemon = event.get(
            "pokemon"
        )

        lines: list[str] = []


        # ========================================================
        # MOVE
        # ========================================================

        if event_type == "move":

            lines.append(
                f"{side}'s {pokemon} used "
                f"{event.get('move')}!"
            )

            return lines


        # ========================================================
        # SWITCH
        # ========================================================

        if event_type == "switch":

            switched_from = event.get(
                "from"
            )

            switched_to = event.get(
                "to"
            )

            if event.get(
                "faint_replacement"
            ):
                lines.append(
                    f"{side} sent out "
                    f"{switched_to}!"
                )

                return lines

            if switched_from:
                lines.append(
                    f"{side} withdrew "
                    f"{switched_from}!"
                )

            lines.append(
                f"{side} sent out "
                f"{switched_to}!"
            )

            return lines


        # ========================================================
        # FORCED SWITCH
        # ========================================================

        if event_type == "forced_switch":

            switched_from = event.get(
                "from"
            )

            switched_to = event.get(
                "to"
            )

            if switched_from:
                lines.append(
                    f"{side}'s "
                    f"{switched_from} was forced out!"
                )

            lines.append(
                f"{side} sent out "
                f"{switched_to}!"
            )

            return lines


        # ========================================================
        # DAMAGE
        # ========================================================

        if event_type == "damage":

            hp_lost = event.get(
                "hp_lost"
            )

            hp_after = event.get(
                "hp_after"
            )

            cause = event.get(
                "cause"
            )

            if hp_lost is not None:

                percentage = (
                    self._format_percentage(
                        hp_lost
                    )
                )

                lines.append(
                    f"{side}'s {pokemon} lost "
                    f"{percentage}% of its health!"
                )

            elif hp_after is not None:

                percentage = (
                    self._format_percentage(
                        hp_after
                    )
                )

                lines.append(
                    f"{side}'s {pokemon} took damage "
                    f"and is now at {percentage}% HP!"
                )

            else:

                lines.append(
                    f"{side}'s {pokemon} took damage!"
                )

            if cause:

                lines.append(
                    f"Damage source: {cause}."
                )

            return lines


        # ========================================================
        # HEAL
        # ========================================================

        if event_type == "heal":

            healed = event.get(
                "hp_healed"
            )

            hp_after = event.get(
                "hp_after"
            )

            cause = event.get(
                "cause"
            )

            if healed is not None:

                healed_text = (
                    self._format_percentage(
                        healed
                    )
                )

                lines.append(
                    f"{side}'s {pokemon} restored "
                    f"{healed_text}% of its health!"
                )

            else:

                lines.append(
                    f"{side}'s {pokemon} restored health!"
                )

            if hp_after is not None:

                after_text = (
                    self._format_percentage(
                        hp_after
                    )
                )

                lines.append(
                    f"{side}'s {pokemon} is now at "
                    f"{after_text}% HP."
                )

            if cause:

                lines.append(
                    f"Healing source: {cause}."
                )

            return lines


        # ========================================================
        # SET HP
        # ========================================================

        if event_type == "set_hp":

            hp_before = event.get(
                "hp_before"
            )

            hp_after = event.get(
                "hp_after"
            )

            if hp_after is None:
                return lines

            after_text = (
                self._format_percentage(
                    hp_after
                )
            )

            if hp_before is None:

                lines.append(
                    f"{side}'s {pokemon}'s HP became "
                    f"{after_text}%."
                )

                return lines

            before_text = (
                self._format_percentage(
                    hp_before
                )
            )

            lines.append(
                f"{side}'s {pokemon}'s HP changed "
                f"from {before_text}% to {after_text}%."
            )

            return lines


        # ========================================================
        # FAINT
        # ========================================================

        if event_type == "faint":

            lines.append(
                f"{side}'s {pokemon} fainted!"
            )

            return lines


        # ========================================================
        # EFFECTIVENESS
        # ========================================================

        if event_type == "super_effective":

            lines.append(
                "It's super effective!"
            )

            return lines


        if event_type == "not_very_effective":

            lines.append(
                "It's not very effective..."
            )

            return lines


        if event_type == "immune":

            lines.append(
                f"It doesn't affect "
                f"{side}'s {pokemon}..."
            )

            return lines


        # ========================================================
        # CRITICAL HIT
        # ========================================================

        if event_type == "critical_hit":

            lines.append(
                "A critical hit!"
            )

            return lines


        # ========================================================
        # MISS
        # ========================================================

        if event_type == "miss":

            target = event.get(
                "target"
            )

            if target:

                lines.append(
                    f"{side}'s {pokemon}'s attack "
                    f"missed {target}!"
                )

            else:

                lines.append(
                    f"{side}'s {pokemon}'s attack missed!"
                )

            return lines


        # ========================================================
        # BOOST
        # ========================================================

        if event_type == "boost":

            stat = self._pretty_stat(
                event.get("stat")
            )

            stages = event.get(
                "stages"
            )

            cause = event.get(
                "cause"
            )

            if stages == 1:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} rose!"
                )

            elif stages == 2:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} rose sharply!"
                )

            else:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} rose by "
                    f"{stages} stages!"
                )

            if cause:

                lines.append(
                    f"Boost source: {cause}."
                )

            return lines


        # ========================================================
        # STAT DROP
        # ========================================================

        if event_type == "stat_drop":

            stat = self._pretty_stat(
                event.get("stat")
            )

            stages = event.get(
                "stages"
            )

            cause = event.get(
                "cause"
            )

            if stages == 1:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} fell!"
                )

            elif stages == 2:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} harshly fell!"
                )

            else:

                lines.append(
                    f"{side}'s {pokemon}'s "
                    f"{stat} fell by "
                    f"{stages} stages!"
                )

            if cause:

                lines.append(
                    f"Stat change source: {cause}."
                )

            return lines


        # ========================================================
        # SET BOOST
        # ========================================================

        if event_type == "set_boost":

            stat = self._pretty_stat(
                event.get("stat")
            )

            lines.append(
                f"{side}'s {pokemon}'s "
                f"{stat} was set to "
                f"{event.get('stages')} stages."
            )

            return lines


        # ========================================================
        # STATUS
        # ========================================================

        if event_type == "status":

            lines.append(
                f"{side}'s {pokemon} became "
                f"{event.get('status')}!"
            )

            return lines


        if event_type == "status_cured":

            lines.append(
                f"{side}'s {pokemon} was cured of "
                f"{event.get('status')}!"
            )

            return lines


        # ========================================================
        # CANNOT MOVE
        # ========================================================

        if event_type == "cant_move":

            reason = event.get(
                "reason"
            )

            move = event.get(
                "move"
            )

            if move:

                lines.append(
                    f"{side}'s {pokemon} could not use "
                    f"{move}! Reason: {reason}."
                )

            else:

                lines.append(
                    f"{side}'s {pokemon} could not move! "
                    f"Reason: {reason}."
                )

            return lines


        # ========================================================
        # FAILED ACTION
        # ========================================================

        if event_type == "fail":

            lines.append(
                f"{side}'s {pokemon}'s action failed!"
            )

            return lines


        # ========================================================
        # TERASTALLIZATION
        # ========================================================

        if event_type == "terastallize":

            lines.append(
                f"{side}'s {pokemon} Terastallized "
                f"into the {event.get('tera_type')}-type!"
            )

            return lines


        # ========================================================
        # ABILITY
        # ========================================================

        if event_type == "ability":

            lines.append(
                f"[{side}'s {pokemon}'s "
                f"{event.get('ability')}]"
            )

            return lines


        # ========================================================
        # ITEM
        # ========================================================

        if event_type == "item":

            lines.append(
                f"{side}'s {pokemon} obtained/revealed "
                f"{event.get('item')}."
            )

            return lines


        if event_type == "item_lost":

            lines.append(
                f"{side}'s {pokemon} lost/consumed "
                f"{event.get('item')}."
            )

            return lines


        # ========================================================
        # VOLATILE EFFECTS
        # ========================================================

        if event_type == "effect_started":

            lines.append(
                f"{side}'s {pokemon} gained "
                f"{event.get('effect')}."
            )

            return lines


        if event_type == "effect_ended":

            lines.append(
                f"{side}'s {pokemon}'s "
                f"{event.get('effect')} ended."
            )

            return lines


        # ========================================================
        # ACTIVATION
        # ========================================================

        if event_type == "activate":

            lines.append(
                f"{side}'s {pokemon} activated "
                f"{event.get('effect')}."
            )

            return lines


        # ========================================================
        # WEATHER
        # ========================================================

        if event_type == "weather":

            weather = event.get(
                "weather"
            )

            lines.append(
                f"Weather: {weather}."
            )

            return lines


        # ========================================================
        # FIELD
        # ========================================================

        if event_type == "field_started":

            lines.append(
                f"{event.get('field')} started."
            )

            return lines


        if event_type == "field_ended":

            lines.append(
                f"{event.get('field')} ended."
            )

            return lines


        # ========================================================
        # SIDE CONDITIONS
        # ========================================================

        if event_type == "side_condition_started":

            lines.append(
                f"{side}'s side gained "
                f"{event.get('condition')}."
            )

            return lines


        if event_type == "side_condition_ended":

            lines.append(
                f"{side}'s "
                f"{event.get('condition')} ended."
            )

            return lines


        # ========================================================
        # FORME CHANGE
        # ========================================================

        if event_type == "forme_change":

            lines.append(
                f"{side}'s {pokemon} changed forme "
                f"into {event.get('forme')}."
            )

            return lines


        return lines

    def _extract_turn_showdown_log(
        self,
        battle: Battle,
        target_turn: int,
    ) -> str | None:
        """
        Reconstruct one completed turn directly from _replay_data.

        Earlier protocol events are replayed internally so we can keep
        track of HP and active Pokémon.

        ONLY target_turn is converted to text and retained.

        The temporary structured dictionaries never leave this method.
        """

        replay_data = getattr(
            battle,
            "_replay_data",
            None,
        )

        if replay_data is None:
            return None

        # State required to correctly calculate HP differences.
        hp_by_slot: dict[str, float] = {}

        active_by_slot: dict[str, str] = {}

        fainted_slots: set[str] = set()

        current_turn: int | None = None

        found_target_turn = False

        turn_lines: list[str] = []


        for raw_event in replay_data:

            if len(raw_event) < 2:
                continue

            event_type = raw_event[1]


            # ----------------------------------------------------
            # TURN MARKER
            # ----------------------------------------------------

            if event_type == "turn":

                if len(raw_event) < 3:
                    continue

                try:
                    new_turn = int(
                        raw_event[2]
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                # The next turn has begun, therefore target_turn
                # is fully complete.
                if (
                    found_target_turn
                    and new_turn > target_turn
                ):
                    break

                current_turn = new_turn

                if current_turn == target_turn:
                    found_target_turn = True

                continue


            # ----------------------------------------------------
            # Process EVERY event before and during target_turn.
            #
            # Earlier events are needed for:
            #
            # - HP before damage
            # - active Pokémon
            # - switch detection
            # ----------------------------------------------------

            normalized = (
                self._normalize_protocol_event(
                    battle=battle,
                    event=raw_event,
                    hp_by_slot=hp_by_slot,
                    active_by_slot=active_by_slot,
                    fainted_slots=fainted_slots,
                )
            )


            # We need prior events for state tracking,
            # but only target_turn is written to memory.
            if current_turn != target_turn:
                continue


            if normalized is None:
                continue


            normalized_events = (
                normalized
                if isinstance(
                    normalized,
                    list,
                )
                else [normalized]
            )


            for normalized_event in normalized_events:

                lines = (
                    self._event_to_showdown_lines(
                        normalized_event
                    )
                )

                turn_lines.extend(
                    lines
                )


        if not found_target_turn:
            return None


        # --------------------------------------------------------
        # BUILD TEXT-ONLY CHECKPOINT
        # --------------------------------------------------------

        if not turn_lines:

            return (
                f"Turn {target_turn}\n\n"
                "No relevant battle events recorded."
            )


        return (
            f"Turn {target_turn}\n\n"
            + "\n".join(turn_lines)
        )

