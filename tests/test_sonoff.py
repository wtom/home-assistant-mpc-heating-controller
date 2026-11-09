"""Real HA service targets and delayed TRVZB closing behaviour."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from heating_controller.const import DOMAIN
from heating_controller.profiles import async_get_profiles, resolve_trv, TrvResolveError
from test_coordinator import (
    ENTRY_DATA,
    TRV_EMITTER,
    _coordinator,
    _trvs,
    _seed_entities,
    _register_fake_climate_set_temperature,
    register_trv_device,
)

OPEN = "number.renamed_open"
CLOSE = "number.renamed_close"
CLIMATE = "climate.heizung_wohnzimmer"
PROPERTIES = (
    ("climate", "climate", ""),
    ("number", "valve_opening_degree", "_open"),
    ("number", "valve_closing_degree", "_close"),
    ("sensor", "valve_opening_limit_voltage", "_voltage"),
    ("number", "external_temperature_input", "_external"),
)


async def bind_sonoff(hass):
    device_id = register_trv_device(hass, properties=PROPERTIES, model_id="TRVZB")
    dr.async_get(hass).async_update_device(device_id, manufacturer="SONOFF")
    registry = er.async_get(hass)
    registry.async_update_entity("number.heizung_wohnzimmer_open", new_entity_id=OPEN)
    registry.async_update_entity("number.heizung_wohnzimmer_close", new_entity_id=CLOSE)
    return device_id


async def test_sonoff_profile_binds_only_the_correct_device_numbers(hass):
    device_id = await bind_sonoff(hass)
    register_trv_device(hass, ieee="other", object_id="other", properties=PROPERTIES)
    trv = resolve_trv(
        hass, await async_get_profiles(hass), {"device_id": device_id, **TRV_EMITTER}
    )
    assert trv.valve_opening_entity == OPEN
    assert trv.valve_closing_entity == CLOSE
    assert trv.climate_entity == CLIMATE
    # Commands must never masquerade as measured positions or train a curve.
    assert trv.valve_position_entity is None
    assert OPEN in trv.entity_ids and CLOSE in trv.entity_ids


@pytest.mark.parametrize("problem", ["missing", "disabled", "ambiguous"])
async def test_sonoff_requires_both_unambiguous_enabled_numbers(hass, problem):
    device_id = await bind_sonoff(hass)
    registry = er.async_get(hass)
    if problem == "missing":
        registry.async_remove(CLOSE)
    elif problem == "disabled":
        registry.async_update_entity(CLOSE, disabled_by=er.RegistryEntryDisabler.USER)
    else:
        registry.async_get_or_create(
            "number",
            "mqtt",
            "duplicate_valve_closing_degree_zigbee2mqtt",
            device_id=device_id,
        )
    with pytest.raises(TrvResolveError, match="opening and closing"):
        resolve_trv(hass, await async_get_profiles(hass), {"device_id": device_id})


@pytest.fixture
def valve(hass, monkeypatch):
    calls, timers = [], []
    trv = _trvs(valve_opening_entity=OPEN, valve_closing_entity=CLOSE)[0]
    coordinator = _coordinator(
        hass, MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA), [trv]
    )
    hass.states.async_set(CLIMATE, "heat", {"temperature": 35})
    hass.states.async_set(OPEN, "20", {"min": 0, "max": 100, "step": 1})
    hass.states.async_set(CLOSE, "80", {"min": 0, "max": 100, "step": 1})

    async def handler(call):
        calls.append((call.data["entity_id"], call.data["value"]))

    hass.services.async_register("number", "set_value", handler)

    def later(hass, delay, callback):
        assert delay == 5
        item = {"callback": callback, "cancelled": False}
        timers.append(item)

        def cancel():
            item["cancelled"] = True

        return cancel

    monkeypatch.setattr("heating_controller.coordinator.async_call_later", later)
    return coordinator, trv, calls, timers


async def set_valve(valve, value):
    coordinator, trv, *_ = valve
    async with coordinator._trv_write_lock:
        await coordinator._async_set_valve(trv, value)


@pytest.mark.parametrize(
    "target,expected",
    [
        (17, [(OPEN, 30), (CLOSE, 70), (OPEN, 17), (CLOSE, 83)]),
        (10, [(OPEN, 10), (CLOSE, 90)]),
        (0, [(OPEN, 0), (CLOSE, 100)]),
        (25, [(OPEN, 25), (CLOSE, 75)]),
        (20, []),
        (-10, [(OPEN, 0), (CLOSE, 100)]),
        (110, [(OPEN, 100), (CLOSE, 0)]),
    ],
)
async def test_motor_commands(valve, target, expected):
    await set_valve(valve, target)
    if valve[3]:
        await valve[3][0]["callback"](None)
    assert valve[2] == expected


async def test_repeated_target_does_not_postpone_the_final_write(valve):
    await set_valve(valve, 17)
    await set_valve(valve, 17)
    assert len(valve[3]) == 1
    assert not valve[3][0]["cancelled"]
    await valve[3][0]["callback"](None)
    assert valve[2][-2:] == [(OPEN, 17), (CLOSE, 83)]


@pytest.mark.parametrize("replacement", [0, 16, 35])
async def test_new_target_supersedes_pending_write_without_another_bump(
    valve, replacement
):
    await set_valve(valve, 17)
    await set_valve(valve, replacement)
    assert valve[3][0]["cancelled"]
    before = list(valve[2])
    await valve[3][0]["callback"](None)  # already dispatched timer is stale too
    assert valve[2] == before
    assert valve[2][-2:] == [(OPEN, replacement), (CLOSE, 100 - replacement)]
    assert len(valve[3]) == 1


async def test_unload_cancels_delayed_target(valve):
    await set_valve(valve, 17)
    valve[0].async_unload()
    assert valve[3][0]["cancelled"]
    await valve[3][0]["callback"](None)
    assert valve[2] == [(OPEN, 30), (CLOSE, 70)]


async def test_upper_limit_caps_bump(hass, valve):
    hass.states.async_set(OPEN, "95", {"max": 100})
    hass.states.async_set(CLOSE, "5", {"max": 100})
    await set_valve(valve, 92)
    await valve[3][0]["callback"](None)
    assert valve[2] == [(OPEN, 100), (CLOSE, 0), (OPEN, 92), (CLOSE, 8)]


async def test_unavailable_number_never_writes_other_entities(hass, valve):
    hass.states.async_set(CLOSE, "unavailable")
    with pytest.raises(HomeAssistantError):
        await set_valve(valve, 17)
    assert not valve[2]


async def test_partial_failure_retries_both_numbers(hass, valve):
    async def failing(call):
        if call.data["entity_id"] == CLOSE:
            raise HomeAssistantError("offline")

    hass.services.async_register("number", "set_value", failing)
    with pytest.raises(HomeAssistantError):
        await set_valve(valve, 40)
    assert CLIMATE not in valve[0]._valve_commands
    calls = []

    async def working(call):
        calls.append((call.data["entity_id"], call.data["value"]))

    hass.services.async_register("number", "set_value", working)
    await set_valve(valve, 40)
    assert calls == [(OPEN, 40), (CLOSE, 60)]


async def test_recompute_sends_mpc_demand_to_numbers_not_temperature_mapping(
    hass, valve
):
    coordinator, trv, calls, _ = valve
    _seed_entities(hass)
    hass.states.async_set(OPEN, "0")
    hass.states.async_set(CLOSE, "100")
    climate_calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    assert coordinator.last_result is not None
    target = round(coordinator.last_result.demand_pct)
    assert calls[-2:] == [(OPEN, target), (CLOSE, 100 - target)]
    assert climate_calls[-1].data["temperature"] == trv.max_target_temperature_c
    assert not coordinator.mpc.valve_curve_learners
    coordinator.async_unload()


async def test_fallback_releases_limits_and_cancels_pending_target(hass, valve):
    climate_calls = _register_fake_climate_set_temperature(hass)
    await set_valve(valve, 17)
    await valve[0]._async_apply_outdoor_fallback(21, False)
    assert valve[2][-2:] == [(OPEN, 100), (CLOSE, 0)]
    assert climate_calls[-1].data["temperature"] == 21
    assert valve[3][0]["cancelled"]
    await valve[3][0]["callback"](None)
    assert valve[2][-2:] == [(OPEN, 100), (CLOSE, 0)]


async def test_delayed_and_new_write_are_serialized(hass, valve):
    await set_valve(valve, 17)
    async with valve[0]._trv_write_lock:
        stale = asyncio.create_task(valve[3][0]["callback"](None))
        await asyncio.sleep(0)
        await valve[0]._async_set_valve(valve[1], 0)
    await stale
    assert valve[2][-2:] == [(OPEN, 0), (CLOSE, 100)]
