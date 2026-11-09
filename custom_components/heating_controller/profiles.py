"""Device profiles: which entities of a supported TRV the controller uses.

A TRV is configured by its device. The profile matching the device names the
entities the controller reads and writes, found by domain and unique_id, so
renaming an entity breaks nothing. One YAML file per supported model and
integration lives in device_profiles/.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml

from .const import CONF_TRV_DEVICE_ID, DOMAIN
from .controller.mpc.models.valve import ValveCharacteristic

_LOGGER = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent / "device_profiles"
DATA_PROFILES = f"{DOMAIN}_device_profiles"

ROLE_CLIMATE = "climate"
ROLE_VALVE_POSITION = "valve_position"
ROLE_VALVE_OPENING = "valve_opening"
ROLE_VALVE_CLOSING = "valve_closing"
ROLE_HEAT_AVAILABLE = "heat_available"
ROLE_ADAPTATION_STATUS = "adaptation_status"
ROLE_MOUNTED_MODE = "mounted_mode"

_ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required("domain"): str,
        vol.Optional("unique_id_suffix"): str,
    }
)
_STATUS_MATCH_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("states", default=[]): [vol.Coerce(str)],
            vol.Optional("attributes", default={}): {str: object},
        }
    ),
    vol.Any(
        vol.Schema({vol.Required("states"): vol.Length(min=1)}, extra=vol.ALLOW_EXTRA),
        vol.Schema(
            {vol.Required("attributes"): vol.Length(min=1)}, extra=vol.ALLOW_EXTRA
        ),
        msg="a status match needs states or attributes",
    ),
)


def _ascending(points: list[list[float]]) -> list[list[float]]:
    """Offsets strictly rising, openings never falling."""
    for (offset_low, opening_low), (offset_high, opening_high) in pairwise(points):
        if offset_high <= offset_low or opening_high < opening_low:
            raise vol.Invalid(
                "valve_characteristic_seed must rise in offset and opening"
            )
    return points


def _to_characteristic(points: list[list[float]]) -> ValveCharacteristic:
    return ValveCharacteristic(
        points=tuple((offset_c, opening_pct) for offset_c, opening_pct in points)
    )


_VALVE_CHARACTERISTIC_SCHEMA = vol.All(
    [vol.All([vol.Coerce(float)], vol.Length(min=2, max=2))],
    vol.Length(min=2),
    _ascending,
    _to_characteristic,
)
PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("match"): vol.All(
            {
                vol.Required("integration"): str,
                vol.Required("manufacturer"): str,
                vol.Optional("models", default=[]): [vol.Coerce(str)],
                vol.Optional("model_ids", default=[]): [vol.Coerce(str)],
            },
            vol.Any(
                vol.Schema(
                    {vol.Required("models"): vol.Length(min=1)}, extra=vol.ALLOW_EXTRA
                ),
                vol.Schema(
                    {vol.Required("model_ids"): vol.Length(min=1)},
                    extra=vol.ALLOW_EXTRA,
                ),
                msg="match needs models or model_ids",
            ),
        ),
        vol.Required("target_temperature"): {
            vol.Required("min"): vol.Coerce(float),
            vol.Required("max"): vol.Coerce(float),
            vol.Required("step"): vol.Coerce(float),
        },
        vol.Required("entities"): {
            vol.Required(ROLE_CLIMATE): _ENTITY_SCHEMA,
            vol.Optional(ROLE_VALVE_POSITION): _ENTITY_SCHEMA,
            vol.Optional(ROLE_VALVE_OPENING): _ENTITY_SCHEMA,
            vol.Optional(ROLE_VALVE_CLOSING): _ENTITY_SCHEMA,
            vol.Optional(ROLE_HEAT_AVAILABLE): _ENTITY_SCHEMA,
            vol.Optional(ROLE_ADAPTATION_STATUS): _ENTITY_SCHEMA.extend(
                {
                    vol.Required("busy"): _STATUS_MATCH_SCHEMA,
                    vol.Optional("ready"): _STATUS_MATCH_SCHEMA,
                }
            ),
            # A binary sensor that is on while the TRV is in mounting mode.
            vol.Optional(ROLE_MOUNTED_MODE): _ENTITY_SCHEMA,
        },
        # [[offset °C, opening %], ...]: where the valve curve each TRV learns
        # starts from. Optional; without it a TRV learns from scratch.
        vol.Optional("valve_characteristic_seed"): _VALVE_CHARACTERISTIC_SCHEMA,
    }
)


class TrvResolveError(Exception):
    """A configured TRV cannot be bound to its entities."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class EntityMatch:
    domain: str
    unique_id_suffix: str | None = None

    def matches(self, entry: er.RegistryEntry) -> bool:
        return entry.domain == self.domain and (
            self.unique_id_suffix is None
            or entry.unique_id.endswith(self.unique_id_suffix)
        )


@dataclass(frozen=True)
class StatusMatch:
    """Adaptation statuses that mean one thing: a run going on, or calibrated."""

    states: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, state: State | None) -> bool:
        if state is None:
            return False
        if state.state in self.states:
            return True
        return any(
            state.attributes.get(name) == value
            for name, value in self.attributes.items()
        )


@dataclass(frozen=True)
class DeviceProfile:
    key: str
    name: str
    integration: str
    manufacturer: str
    models: frozenset[str]
    model_ids: frozenset[str]
    min_target_temperature_c: float
    max_target_temperature_c: float
    target_temperature_step_c: float
    entities: Mapping[str, EntityMatch]
    adaptation_busy: StatusMatch | None = None
    adaptation_ready: StatusMatch | None = None
    valve_characteristic_seed: ValveCharacteristic | None = None

    def matches_device(self, device: dr.DeviceEntry, integrations: set[str]) -> bool:
        return (
            self.integration in integrations
            and device.manufacturer == self.manufacturer
            and (device.model in self.models or device.model_id in self.model_ids)
        )


@dataclass(frozen=True)
class TrvBinding:
    """A configured TRV with the entities its device profile resolved."""

    climate_entity: str
    min_target_temperature_c: float
    max_target_temperature_c: float
    target_temperature_step_c: float
    # The radiator settings stored for this TRV (emitter type, dimensions).
    emitter: Mapping[str, Any] = field(default_factory=dict)
    valve_opening_entity: str | None = None
    valve_closing_entity: str | None = None
    valve_position_entity: str | None = None
    heat_available_entity: str | None = None
    adaptation_status_entity: str | None = None
    adaptation_busy: StatusMatch | None = None
    adaptation_ready: StatusMatch | None = None
    mounted_mode_entity: str | None = None
    valve_characteristic_seed: ValveCharacteristic | None = None

    @property
    def entity_ids(self) -> list[str]:
        return [
            entity_id
            for entity_id in (
                self.climate_entity,
                self.valve_position_entity,
                self.valve_opening_entity,
                self.valve_closing_entity,
                self.heat_available_entity,
                self.adaptation_status_entity,
                self.mounted_mode_entity,
            )
            if entity_id
        ]


def _status_match(config: dict[str, Any]) -> StatusMatch:
    return StatusMatch(
        states=frozenset(config["states"]), attributes=dict(config["attributes"])
    )


def _profile_from_config(key: str, config: dict[str, Any]) -> DeviceProfile:
    entities = config["entities"]
    adaptation = entities.get(ROLE_ADAPTATION_STATUS)
    return DeviceProfile(
        key=key,
        name=config["name"],
        integration=config["match"]["integration"],
        manufacturer=config["match"]["manufacturer"],
        models=frozenset(config["match"]["models"]),
        model_ids=frozenset(config["match"]["model_ids"]),
        min_target_temperature_c=config["target_temperature"]["min"],
        max_target_temperature_c=config["target_temperature"]["max"],
        target_temperature_step_c=config["target_temperature"]["step"],
        entities={
            role: EntityMatch(
                domain=match["domain"],
                unique_id_suffix=match.get("unique_id_suffix"),
            )
            for role, match in entities.items()
        },
        adaptation_busy=_status_match(adaptation["busy"]) if adaptation else None,
        adaptation_ready=(
            _status_match(adaptation["ready"])
            if adaptation and "ready" in adaptation
            else None
        ),
        valve_characteristic_seed=config.get("valve_characteristic_seed"),
    )


def load_profiles(directory: Path = PROFILES_DIR) -> tuple[DeviceProfile, ...]:
    """Read and validate every profile; blocking, run in the executor."""
    return tuple(
        _profile_from_config(path.stem, PROFILE_SCHEMA(load_yaml(path)))
        for path in sorted(directory.glob("*.yaml"))
    )


async def async_get_profiles(hass: HomeAssistant) -> tuple[DeviceProfile, ...]:
    profiles = hass.data.get(DATA_PROFILES)
    if profiles is None:
        profiles = await hass.async_add_executor_job(load_profiles)
        hass.data[DATA_PROFILES] = profiles
    return profiles


def device_selector_filters(
    profiles: tuple[DeviceProfile, ...],
) -> list[dict[str, str]]:
    """Device selector filters offering only devices a profile covers."""
    filters: list[dict[str, str]] = []
    for profile in profiles:
        base = {
            "integration": profile.integration,
            "manufacturer": profile.manufacturer,
        }
        filters.extend({**base, "model": model} for model in sorted(profile.models))
        filters.extend(
            {**base, "model_id": model_id} for model_id in sorted(profile.model_ids)
        )
    return filters


def find_profile(
    hass: HomeAssistant, profiles: tuple[DeviceProfile, ...], device: dr.DeviceEntry
) -> DeviceProfile | None:
    # Newer cores tie a device to a single config entry and deprecate the set.
    entry_ids = (
        [device.config_entry_id]
        if hasattr(device, "config_entry_id")
        else device.config_entries
    )
    integrations = {
        config_entry.domain
        for entry_id in entry_ids
        if (config_entry := hass.config_entries.async_get_entry(entry_id)) is not None
    }
    return next(
        (
            profile
            for profile in profiles
            if profile.matches_device(device, integrations)
        ),
        None,
    )


def _device_label(device: dr.DeviceEntry) -> str:
    return (
        f"{device.name_by_user or device.name} "
        f"({device.manufacturer} {device.model_id or device.model})"
    )


def resolve_trv(
    hass: HomeAssistant,
    profiles: tuple[DeviceProfile, ...],
    trv: Mapping[str, Any],
) -> TrvBinding:
    """Bind a configured TRV to the entities its device profile names.

    The climate entity is required. Any other entity the profile names but
    the device lacks (disabled, not exposed) is left out, and the controller
    runs without what it provides.
    """
    device_id = trv[CONF_TRV_DEVICE_ID]
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise TrvResolveError("device_missing", f"Device {device_id} does not exist")
    profile = find_profile(hass, profiles, device)
    if profile is None:
        raise TrvResolveError(
            "unsupported_device", f"{_device_label(device)} has no device profile"
        )

    registry_entries = er.async_entries_for_device(er.async_get(hass), device_id)
    entity_ids: dict[str, str] = {}
    for role, match in profile.entities.items():
        candidates = [
            entry.entity_id for entry in registry_entries if match.matches(entry)
        ]
        if len(candidates) == 1:
            entity_ids[role] = candidates[0]
        elif role != ROLE_CLIMATE:
            _LOGGER.warning(
                "%s: no single enabled entity for %s, running without it",
                _device_label(device),
                role,
            )

    if ROLE_CLIMATE not in entity_ids:
        raise TrvResolveError(
            "climate_missing",
            f"{_device_label(device)} has no single enabled climate entity "
            f"matching profile {profile.name}",
        )

    if (
        ROLE_VALVE_OPENING in profile.entities or ROLE_VALVE_CLOSING in profile.entities
    ) and not all(
        role in entity_ids for role in (ROLE_VALVE_OPENING, ROLE_VALVE_CLOSING)
    ):
        raise TrvResolveError(
            "valve_control_missing",
            f"{_device_label(device)} needs enabled opening and closing number entities",
        )

    return TrvBinding(
        climate_entity=entity_ids[ROLE_CLIMATE],
        min_target_temperature_c=profile.min_target_temperature_c,
        max_target_temperature_c=profile.max_target_temperature_c,
        target_temperature_step_c=profile.target_temperature_step_c,
        emitter={key: value for key, value in trv.items() if key != CONF_TRV_DEVICE_ID},
        valve_position_entity=entity_ids.get(ROLE_VALVE_POSITION),
        valve_opening_entity=entity_ids.get(ROLE_VALVE_OPENING),
        valve_closing_entity=entity_ids.get(ROLE_VALVE_CLOSING),
        heat_available_entity=entity_ids.get(ROLE_HEAT_AVAILABLE),
        adaptation_status_entity=entity_ids.get(ROLE_ADAPTATION_STATUS),
        adaptation_busy=(
            profile.adaptation_busy if ROLE_ADAPTATION_STATUS in entity_ids else None
        ),
        adaptation_ready=(
            profile.adaptation_ready if ROLE_ADAPTATION_STATUS in entity_ids else None
        ),
        mounted_mode_entity=entity_ids.get(ROLE_MOUNTED_MODE),
        valve_characteristic_seed=profile.valve_characteristic_seed,
    )
