"""V4 visible-information Speed estimation, unchanged from the baseline."""
from __future__ import annotations
from fractions import Fraction
from typing import Any

class SpeedEstimator:
    """Compute exact own Speed and conservative opponent ranges."""

    @staticmethod
    def _v4_key(value: Any) -> str:
        """Normalize poke-env enums (e.g. SideCondition.TAILWIND)."""
        if value is None:
            return ""
        text = getattr(value, "name", None) or str(value)
        return "".join(char for char in str(text).lower() if char.isalnum())

    @classmethod
    def _v4_contains(cls, items: Any, wanted: str) -> bool:
        """Works for dicts (poke-env conditions) and sets/lists."""
        if items is None:
            return False
        keys = items.keys() if isinstance(items, dict) else items
        return any(cls._v4_key(key) == wanted for key in keys)

    @classmethod
    def _v4_opponent_raw_range(cls, pokemon: Any, generation: int) -> tuple[int, int] | None:
        """Broad legal raw-Speed extremes using the Gen 3+ stat formula."""
        if generation < 3:
            return None
        try:
            base = int(pokemon.base_stats["spe"])
            level = int(pokemon.level)
            if not 1 <= level <= 100 or base < 1:
                return None

            # Speed (Gen 3+): floor((floor((2*base + IV + EV//4)*L/100)+5)*nature)
            lowest_neutral = (2 * base * level // 100) + 5
            highest_neutral = ((2 * base + 31 + 252 // 4) * level // 100) + 5
            minimum = max(1, lowest_neutral * 9 // 10)   # -Speed nature
            maximum = max(1, highest_neutral * 11 // 10) # +Speed nature
            return minimum, maximum
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _v4_modifiers(cls, pokemon: Any, battle: Any, *, ours: bool) -> tuple[Fraction, list[str]]:
        """Compute relevant visible modifiers; ignore unrevealed items/abilities."""
        mult = Fraction(1, 1)
        applied: list[str] = []

        boosts = getattr(pokemon, "boosts", {}) or {}
        stage = max(-6, min(6, int(boosts.get("spe", 0) or 0)))
        if stage > 0:
            mult *= Fraction(2 + stage, 2)
        elif stage < 0:
            mult *= Fraction(2, 2 - stage)
        applied.append(f"Speed stage {stage:+d}")

        side = battle.side_conditions if ours else battle.opponent_side_conditions
        if cls._v4_contains(side, "tailwind"):
            mult *= 2
            applied.append("Tailwind ×2")

        status = cls._v4_key(getattr(pokemon, "status", None))
        ability = cls._v4_key(getattr(pokemon, "ability", None))
        effects = getattr(pokemon, "effects", {}) or {}
        generation = int(getattr(battle, "gen", 9))

        # Known ability effects: only if the ability is visible/known.
        # Neutralizing Gas / Gastro Acid may suppress relevant abilities.
        ability_suppressed = (
            cls._v4_contains(getattr(battle, "fields", {}), "neutralizinggas")
            or cls._v4_contains(effects, "gastroacid")
        )

        # Quick Feet bypasses paralysis's Speed penalty while active.
        quick_feet_active = (
            ability == "quickfeet" and not ability_suppressed
            and status not in ("", "fnt")
        )
        if status == "par" and not quick_feet_active:
            ratio = Fraction(1, 2) if generation >= 7 else Fraction(1, 4)
            mult *= ratio
            applied.append(f"Paralysis ×{ratio}")
        if ability and not ability_suppressed:
            weather = {cls._v4_key(x) for x in getattr(battle, "weather", {})}
            fields = {cls._v4_key(x) for x in getattr(battle, "fields", {})}

            activated = (
                (ability == "swiftswim" and bool(weather & {"raindance", "primordialsea"}))
                or (ability == "chlorophyll" and bool(weather & {"sunnyday", "desolateland"}))
                or (ability == "sandrush" and "sandstorm" in weather)
                or (ability == "slushrush" and bool(weather & {"hail", "snow", "snowscape"}))
                or (ability == "surgesurfer" and "electricterrain" in fields)
            )
            if activated:
                mult *= 2
                applied.append(f"{ability} activated ×2")

            if quick_feet_active:
                mult *= Fraction(3, 2)
                applied.append("Quick Feet ×1.5 (ignores paralysis penalty)")

            if ability == "unburden" and cls._v4_contains(effects, "unburden"):
                mult *= 2
                applied.append("Unburden activated ×2")

            if ability in ("protosynthesis", "quarkdrive"):
                key = "protosynthesisspe" if ability == "protosynthesis" else "quarkdrivespe"
                if cls._v4_contains(effects, key):
                    mult *= Fraction(3, 2)
                    applied.append(f"{ability} Speed activation ×1.5")

        # Apply items only when revealed. Our own item comes from the
        # Showdown private request; opponent items are often unknown.
        item = cls._v4_key(getattr(pokemon, "item", None))
        if item == "choicescarf":
            mult *= Fraction(3, 2)
            applied.append("Revealed Choice Scarf ×1.5")
        elif item in {
            "ironball", "machobrace", "poweranklet", "powerband",
            "powerbelt", "powerbracer", "powerlens", "powerweight",
        }:
            mult *= Fraction(1, 2)
            applied.append(f"Known {item} ×0.5")
        elif item == "quickpowder" and cls._v4_key(getattr(pokemon, "species", "")) == "ditto":
            if not getattr(pokemon, "transformed", False):
                mult *= 2
                applied.append("Quick Powder Ditto ×2")

        return mult, applied

    @classmethod
    def _v4_speed_info(cls, pokemon: Any, battle: Any, *, ours: bool) -> dict[str, Any]:
        """Raw Speed, visible-modifier estimate and uncertainty disclaimer."""
        multiplier, factors = cls._v4_modifiers(pokemon, battle, ours=ours)
        info: dict[str, Any] = {
            "speed_stage": int((getattr(pokemon, "boosts", {}) or {}).get("spe", 0) or 0),
            "status": cls._v4_key(getattr(pokemon, "status", None)) or None,
            "known_modifiers": factors,
            "known_multiplier": str(multiplier),
            "item": cls._v4_key(getattr(pokemon, "item", None)) or "unknown",
            "ability": cls._v4_key(getattr(pokemon, "ability", None)) or "unknown",
        }

        if ours:
            raw = (getattr(pokemon, "stats", {}) or {}).get("spe")
            if raw is None:
                info.update({"raw_exact": None, "known_effective_speed": None,
                             "note": "Own raw Speed not present in the current Showdown request."})
            else:
                raw = int(raw)
                info.update({"raw_exact": raw,
                             "known_effective_speed": raw * multiplier.numerator // multiplier.denominator,
                             "note": "Exact RAW Speed; effective Speed is an estimate using visible factors."})
        else:
            possible = cls._v4_opponent_raw_range(pokemon, int(battle.gen))
            if possible is None:
                info.update({"raw_min": None, "raw_max": None,
                             "known_effective_min": None, "known_effective_max": None,
                             "note": "Raw opponent Speed range unavailable."})
            else:
                low, high = possible
                info.update({"raw_min": low, "raw_max": high,
                             "known_effective_min": low * multiplier.numerator // multiplier.denominator,
                             "known_effective_max": high * multiplier.numerator // multiplier.denominator,
                             "note": "Broad bound under the displayed known modifiers, not accounting for hidden items/abilities or unmodelled effects."})
        return info

