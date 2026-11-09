import pytest
import voluptuous as vol
from heating_controller.profiles import (
    StatusMatch,
    TrvResolveError,
    async_get_profiles,
    device_selector_filters,
    load_profiles,
    resolve_trv,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import TRV_EMITTER, register_trv_device

ALL_Z2M_PROPERTIES = (
    ("climate", "climate", ""),
    ("sensor", "pi_heating_demand", "_pi_heating_demand"),
    ("switch", "switch_heat_available", "_heat_available"),
    ("sensor", "adaptation_run_status", "_adaptation_run_status"),
    ("binary_sensor", "mounted_mode_active", "_mounted_mode_active"),
)


def test_every_shipped_profile_is_valid() -> None:
    profiles = {profile.key: profile for profile in load_profiles()}

    assert set(profiles) == {
        "danfoss_ally_zha",
        "danfoss_ally_zigbee2mqtt",
        "sonoff_trvzb_zigbee2mqtt",
    }
    z2m = profiles["danfoss_ally_zigbee2mqtt"]
    assert z2m.model_ids == {"014G2461"}
    assert z2m.max_target_temperature_c == 35.0


def test_the_device_picker_offers_only_profiled_models() -> None:
    filters = device_selector_filters(load_profiles())

    assert {
        "integration": "mqtt",
        "manufacturer": "Danfoss",
        "model_id": "014G2461",
    } in filters
    assert {
        "integration": "zha",
        "manufacturer": "Danfoss",
        "model": "eTRV0100",
    } in filters


async def test_a_zigbee2mqtt_ally_binds_all_its_entities(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass, properties=ALL_Z2M_PROPERTIES)

    trv = resolve_trv(
        hass, await async_get_profiles(hass), {"device_id": device_id, **TRV_EMITTER}
    )

    assert trv.climate_entity == "climate.heizung_wohnzimmer"
    assert trv.valve_position_entity == "sensor.heizung_wohnzimmer_pi_heating_demand"
    assert trv.heat_available_entity == "switch.heizung_wohnzimmer_heat_available"
    assert (
        trv.adaptation_status_entity
        == "sensor.heizung_wohnzimmer_adaptation_run_status"
    )
    assert (
        trv.mounted_mode_entity
        == "binary_sensor.heizung_wohnzimmer_mounted_mode_active"
    )
    assert trv.adaptation_ready.matches(State("sensor.a", "found"))
    assert not trv.adaptation_ready.matches(State("sensor.a", "none"))
    assert trv.emitter == TRV_EMITTER
    assert (trv.min_target_temperature_c, trv.target_temperature_step_c) == (5.0, 0.5)


async def test_renamed_entities_are_still_found(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass, properties=ALL_Z2M_PROPERTIES)
    er.async_get(hass).async_update_entity(
        "sensor.heizung_wohnzimmer_pi_heating_demand",
        new_entity_id="sensor.ventil_wohnzimmer",
    )

    trv = resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})

    assert trv.valve_position_entity == "sensor.ventil_wohnzimmer"


async def test_a_missing_optional_entity_is_left_out(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass, properties=(("climate", "climate", ""),))

    trv = resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})

    assert trv.valve_position_entity is None
    assert trv.heat_available_entity is None
    assert trv.adaptation_status_entity is None
    assert trv.adaptation_busy is None
    assert trv.adaptation_ready is None
    assert trv.mounted_mode_entity is None


async def test_a_zha_ally_binds_by_its_unique_ids(hass: HomeAssistant) -> None:
    zha_entry = MockConfigEntry(domain="zha")
    zha_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=zha_entry.entry_id,
        identifiers={("zha", "00:01")},
        manufacturer="Danfoss",
        model="eTRV0100",
        name="Heizung Bad",
    )
    registry = er.async_get(hass)
    for domain, unique_id, object_id in (
        ("climate", "00:01-1", "heizung_bad"),
        ("sensor", "00:01-1-513-pi_heating_demand", "heizung_bad_pi"),
        ("switch", "00:01-1-513-heat_available", "heizung_bad_heat"),
        ("sensor", "00:01-1-513-adaptation_run_status", "heizung_bad_adaptation"),
        ("sensor", "00:01-1-513-load_estimate", "heizung_bad_load"),
        ("binary_sensor", "00:01-1-513-mounted_mode_active", "heizung_bad_mounted"),
    ):
        registry.async_get_or_create(
            domain,
            "zha",
            unique_id,
            suggested_object_id=object_id,
            config_entry=zha_entry,
            device_id=device.id,
        )

    trv = resolve_trv(hass, await async_get_profiles(hass), {"device_id": device.id})

    assert trv.climate_entity == "climate.heizung_bad"
    assert trv.valve_position_entity == "sensor.heizung_bad_pi"
    assert trv.heat_available_entity == "switch.heizung_bad_heat"
    assert trv.adaptation_status_entity == "sensor.heizung_bad_adaptation"
    assert trv.adaptation_busy.matches(
        State("sensor.heizung_bad_adaptation", "something", {"In_progress": True})
    )
    assert trv.adaptation_ready.matches(
        State("sensor.a", "something", {"Valve_characteristic_found": True})
    )
    assert trv.mounted_mode_entity == "binary_sensor.heizung_bad_mounted"


@pytest.mark.parametrize(
    ("model_id", "reason"),
    [("unknown-model", "unsupported_device"), (None, "device_missing")],
)
async def test_an_unknown_device_is_refused(
    hass: HomeAssistant, model_id: str | None, reason: str
) -> None:
    device_id = (
        register_trv_device(hass, model_id=model_id) if model_id else "no-such-device"
    )

    with pytest.raises(TrvResolveError) as err:
        resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})

    assert err.value.reason == reason


async def test_a_device_without_its_climate_entity_is_refused(
    hass: HomeAssistant,
) -> None:
    device_id = register_trv_device(
        hass, properties=(("sensor", "pi_heating_demand", "_pi_heating_demand"),)
    )

    with pytest.raises(TrvResolveError) as err:
        resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})

    assert err.value.reason == "climate_missing"


def test_status_match_by_state_or_attribute() -> None:
    by_state = StatusMatch(states=frozenset({"in_progress", "lost_in_progress"}))
    by_attribute = StatusMatch(attributes={"In_progress": True})

    assert by_state.matches(State("sensor.a", "lost_in_progress"))
    assert not by_state.matches(State("sensor.a", "found"))
    assert not by_state.matches(None)
    assert by_attribute.matches(State("sensor.a", "something", {"In_progress": True}))
    assert not by_attribute.matches(
        State("sensor.a", "something", {"In_progress": False})
    )


DANFOSS_SEED_POINTS = ((0.1, 5.0), (0.6, 45.0), (1.5, 55.0), (2.5, 90.0), (3.0, 100.0))

_MINIMAL_PROFILE = """\
name: Test TRV
match:
  integration: mqtt
  manufacturer: Test
  models: [T1]
target_temperature: {min: 5, max: 30, step: 0.5}
entities:
  climate: {domain: climate}
"""


@pytest.mark.parametrize("key", ["danfoss_ally_zha", "danfoss_ally_zigbee2mqtt"])
def test_profile_reads_the_valve_characteristic_seed(key: str) -> None:
    profiles = {profile.key: profile for profile in load_profiles()}

    assert profiles[key].valve_characteristic_seed.points == DANFOSS_SEED_POINTS


def test_profile_without_seed_is_valid(tmp_path) -> None:
    (tmp_path / "test.yaml").write_text(_MINIMAL_PROFILE)

    [profile] = load_profiles(tmp_path)

    assert profile.valve_characteristic_seed is None


@pytest.mark.parametrize(
    "seed",
    [
        "[[0.5, 50], [1.0, 40]]",
        "[[1.0, 10], [0.5, 50]]",
        "[[0.5, 10]]",
        "[[0.5, 10, 3], [1.0, 50]]",
    ],
)
def test_profile_rejects_a_falling_seed(tmp_path, seed: str) -> None:
    (tmp_path / "test.yaml").write_text(
        _MINIMAL_PROFILE + f"valve_characteristic_seed: {seed}\n"
    )

    with pytest.raises(vol.Invalid):
        load_profiles(tmp_path)


async def test_the_seed_reaches_the_trv_binding(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass, properties=ALL_Z2M_PROPERTIES)

    trv = resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})

    assert trv.valve_characteristic_seed.points == DANFOSS_SEED_POINTS
