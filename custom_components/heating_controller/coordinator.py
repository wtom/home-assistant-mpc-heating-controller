from __future__ import annotations

import asyncio
import math
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_MAX_SENSOR_AGE,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_STATIONARY_RANGE,
    CONF_TARGET_BAND,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_SETPOINT_MODE,
    CONF_TRV_WIDTH_MM,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_WINDOW_SURFACES,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_STATIONARY_RANGE_C,
    HEAT_SOURCE_ACTIVE_STATE,
    LEARNING_CYCLE_INTERVAL_MINUTES,
    SENSOR_POLL_INTERVAL_MINUTES,
    Construction,
    CurveEnd,
    DesignTemperatureSystem,
    FlowSupplyStatus,
    HeatEmitterType,
    HeatMode,
    PanelRadiatorType,
    TrvSetpointMode,
)
from .controller.mpc.controller import (
    FlowGateConfig,
    MpcRateLimitConfig,
    RoomMpcController,
)
from .controller.mpc.math_helper import clamp, round_to_step
from .controller.mpc.models.gains import GainBreakdown, profile_index
from .controller.mpc.results import RoomMpcResult
from .controller.mpc.types import (
    FlowGateState,
    LearningFactors,
    RoomThermalConfig,
    TrvConfig,
)
from .controller.state import HeatingStateConfig, HeatingStateController
from .irradiance import IrradianceProvider, SunSeries
from .profiles import TrvBinding
from .store import (
    FlowGateStateStore,
    GainStore,
    LearningFactorsStore,
    ValveCurveStore,
    async_remove_observer_store,
)

_LOGGER = logging.getLogger(__name__)

LEARNING_CYCLE_INTERVAL = timedelta(minutes=LEARNING_CYCLE_INTERVAL_MINUTES)

SENSOR_POLL_INTERVAL = timedelta(minutes=SENSOR_POLL_INTERVAL_MINUTES)

TARGET_TEMPERATURE_TOLERANCE_C = 0.01
# A Danfoss Ally reports its previous setpoint once more a few seconds after a
# write. Within this window the written target counts as the TRV's target, so
# the echo neither triggers a second write nor moves a relative target.
TARGET_WRITE_SETTLE_S = 120.0
# A TRV switches between shut (minimum target) and open at most this often,
# unless frost protection is forced: every switch wakes it twice.
TRV_SWITCH_MIN_INTERVAL_S = 15 * 60
# A write aborts a TRV's adaptation run, so nothing is written to a TRV while
# its profile reports one running -- but no longer than this, so a status that
# never clears cannot leave the room uncontrolled.
# A Danfoss Ally calibration took 1 h 46 to 1 h 49 min.
ADAPTATION_MAX_HOLD_S = 3 * 3600

WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S = 30 * 60
WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S = 10 * 60
# Valve curve samples finish every few minutes per TRV; one delayed write
# bundles what finished within this time and spares the disk. HA flushes a
# pending write on shutdown.
VALVE_CURVE_SAVE_DELAY_S = 10 * 60

SUNSET_LEARNING_DELAY = timedelta(hours=2)

# After startup a configured room sensor often reports a few seconds after the
# TRVs. Computing in between falls back to the TRV average, which can sit well
# below the room sensor and reopen a restored surplus gate. Wait this long for
# the room sensor before accepting the fallback.
ROOM_SENSOR_STARTUP_GRACE_S = 5 * 60

# Right after startup the outdoor entity and the heat source may not have
# reported yet. Without the heat source state the room reads as unheated and
# would force frost protection; without the outdoor temperature it would hand
# the TRVs an absolute fallback. Either is overwritten seconds later and costs
# TRV and switch writes on every restart. So both wait until the input was
# read once, or has been missing for a full poll cycle.
STARTUP_GRACE_S = SENSOR_POLL_INTERVAL_MINUTES * 60

# The heat pump reports its flow temperature about every 10 minutes and drops
# out for a poll now and then. Learning pauses only once no reading came for
# longer than this; the control keeps the last reading meanwhile.
FLOW_TEMPERATURE_MAX_GAP_S = 16 * 60


def _build_trv_configs(trvs: list[TrvBinding]) -> list[TrvConfig]:
    return [
        TrvConfig(
            name=trv.climate_entity,
            emitter_type=HeatEmitterType(trv.emitter[CONF_TRV_EMITTER_TYPE]),
            min_target_temperature_c=trv.min_target_temperature_c,
            max_target_temperature_c=trv.max_target_temperature_c,
            target_temperature_step_c=trv.target_temperature_step_c,
            radiator_type=(
                PanelRadiatorType(trv.emitter[CONF_TRV_RADIATOR_TYPE])
                if CONF_TRV_RADIATOR_TYPE in trv.emitter
                else None
            ),
            width_mm=trv.emitter.get(CONF_TRV_WIDTH_MM),
            height_mm=trv.emitter.get(CONF_TRV_HEIGHT_MM),
            nominal_power_w=trv.emitter.get(CONF_TRV_NOMINAL_POWER_W),
            valve_position_configured=trv.valve_position_entity is not None,
            valve_characteristic_seed=trv.valve_characteristic_seed,
        )
        for trv in trvs
    ]


class HeatingRoomCoordinator:
    def __init__(
        self,
        hass: HomeAssistant,
        hub_entry: ConfigEntry,
        room_id: str,
        data: Mapping[str, Any],
        trvs: list[TrvBinding],
    ) -> None:
        """One room of the hub.

        room_id is the room's subentry id; stores, unique ids and the device
        hang on it. data holds the hub's settings merged with the room's.
        """
        self.hass = hass
        self.hub_entry = hub_entry
        self.room_id = room_id
        self.data = data
        self.room_name: str = self.data[CONF_ROOM_NAME]

        self._trvs = trvs
        self._trv_entity_ids: list[str] = [trv.climate_entity for trv in trvs]
        self._valve_position_entities: list[str | None] = [
            trv.valve_position_entity for trv in trvs
        ]
        self._adaptation_status_entities: list[str] = [
            trv.adaptation_status_entity for trv in trvs if trv.adaptation_status_entity
        ]
        self._mounted_mode_entities: list[str] = [
            trv.mounted_mode_entity for trv in trvs if trv.mounted_mode_entity
        ]

        self._heat_source_climate_entity: str | None = self.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._flow_temperature_entity: str = self.data[CONF_FLOW_TEMPERATURE_ENTITY]
        # The heat source drops out for a few polls at a time while it keeps
        # running; its last known state bridges that.
        self._last_heat_source_state: str | None = None
        # Same for the flow temperature entity, which drops out for a moment
        # whenever its template is reloaded.
        self._flow_temperature_c: float | None = None
        self._flow_threshold_entity: str | None = self.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        # Set by the hub when a flow device owns the threshold.
        self.flow_threshold_provider: Callable[[], float] | None = None
        # Set by the hub when a hot water device sees the charges.
        self.hot_water_charge_provider: Callable[[], bool] | None = None
        # Set by the hub when a flow device knows the heating curve's outdoor
        # end points: (cold, warm).
        self.curve_outdoor_provider: Callable[[], tuple[float, float]] | None = None
        self._learning_block_entity: str | None = self.data.get(
            CONF_LEARNING_BLOCK_ENTITY
        )
        self._learning_blocked = False
        # When the flow temperature last had a reading; set on startup.
        self._flow_temperature_seen_ts = 0.0
        self._learning_paused = False

        self.state = HeatingStateController(
            HeatingStateConfig(
                boost_temperature_offset_c=self.data[CONF_BOOST_TEMPERATURE_OFFSET],
                frost_protection_temperature_c=self.data[
                    CONF_FROST_PROTECTION_TEMPERATURE
                ],
                pv_boost_enabled=self.data[CONF_PV_BOOST_ENABLED],
                pv_boost_temperature_offset_c=self.data[
                    CONF_PV_BOOST_TEMPERATURE_OFFSET
                ],
            )
        )
        # Until the switches restore: house on, the room's own switch off.
        self.follows_house_comfort: bool = self.data.get(
            CONF_USE_HOUSE_COMFORT_CONDITIONS, True
        )
        self.has_room_comfort_switch: bool = self.data.get(
            CONF_ROOM_COMFORT_SWITCH, False
        )
        if self.follows_house_comfort:
            self.state.set_house_comfort(True)
        if self.has_room_comfort_switch:
            self.state.set_room_comfort(False)

        thermal_config = RoomThermalConfig(
            design_indoor_temperature_c=self.data[CONF_DESIGN_INDOOR_TEMPERATURE],
            design_outdoor_temperature_c=self.data[CONF_DESIGN_OUTDOOR_TEMPERATURE],
            design_temperature_system=DesignTemperatureSystem(
                self.data[CONF_DESIGN_TEMPERATURE_SYSTEM]
            ),
            room_heat_load_w=self.data[CONF_ROOM_HEAT_LOAD],
            flow_threshold_c=self.flow_threshold_c,
            room_area_m2=self.data[CONF_ROOM_AREA],
            construction=Construction(self.data[CONF_CONSTRUCTION]),
            window_surfaces=tuple(self.data.get(CONF_WINDOW_SURFACES) or ()),
            opaque_surfaces=tuple(self.data.get(CONF_OPAQUE_SURFACES) or ()),
        )
        rate_limit_config = MpcRateLimitConfig(
            demand_hysteresis_pct=self.data[CONF_MPC_DEMAND_HYSTERESIS_PCT],
            hold_time_s=self.data[CONF_MPC_HOLD_TIME],
            hold_override_demand_pct=self.data[CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT],
            max_demand_step_pct=self.data[CONF_MPC_MAX_DEMAND_STEP_PCT],
        )
        flow_gate_config = FlowGateConfig(
            close_surplus_c=self.data.get(
                CONF_FLOW_GATE_CLOSE_SURPLUS, DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C
            ),
            open_surplus_c=self.data.get(
                CONF_FLOW_GATE_OPEN_SURPLUS, DEFAULT_FLOW_GATE_OPEN_SURPLUS_C
            ),
            hold_time_s=self.data.get(
                CONF_FLOW_GATE_HOLD_TIME, DEFAULT_FLOW_GATE_HOLD_TIME_S
            ),
        )
        self.mpc = RoomMpcController(
            thermal_config=thermal_config,
            trvs=_build_trv_configs(trvs),
            rate_limit_config=rate_limit_config,
            max_sensor_age_s=self.data[CONF_MAX_SENSOR_AGE],
            room_sensor_configured=bool(self.data.get(CONF_ROOM_SENSOR_ENTITY)),
            target_band_c=self.data[CONF_TARGET_BAND],
            trv_setpoint_mode=TrvSetpointMode(self.data[CONF_TRV_SETPOINT_MODE]),
            flow_gate_config=flow_gate_config,
        )
        self.mpc.set_stationary_range_c(
            self.data.get(CONF_STATIONARY_RANGE, DEFAULT_STATIONARY_RANGE_C)
        )
        self.mpc.set_sun_condition(self.is_sun_condition_met)
        self.mpc.set_gain_context(self.profile_index_at, self.fresh_sun_series)

        self.store = LearningFactorsStore(hass, self.room_name, room_id)
        self.flow_gate_store = FlowGateStateStore(hass, self.room_name, room_id)
        self._persisted_flow_gate_state: FlowGateState | None = None
        self.valve_curve_store = ValveCurveStore(hass, self.room_name, room_id)
        self.gain_store = GainStore(hass, self.room_name, room_id)
        # Set by the hub when some room has sun surfaces.
        self.sun_provider: IrradianceProvider | None = None
        self.workday_entity: str | None = None
        # When the pending delayed write is due. HA's delayed save moves a
        # pending write out on every call, so samples finishing more often
        # than the delay would postpone it until shutdown.
        self._valve_curve_save_due_ts: float | None = None
        self._awaiting_room_sensor = bool(self.data.get(CONF_ROOM_SENSOR_ENTITY))

        self.blocked = False
        self.trv_active = True
        self.last_result: RoomMpcResult | None = None
        # Target writes per TRV since setup: every write wakes the TRV.
        self.trv_setpoint_writes: dict[str, int] = {}
        self._trv_write_lock = asyncio.Lock()
        self._valve_commands: dict[str, tuple[int, float]] = {}
        self._valve_pending: dict[str, tuple[int, Callable[[], None]]] = {}
        self._unloaded = False
        self._written_targets: dict[str, tuple[float, float]] = {}
        self._trv_switched_at: dict[str, float] = {}
        self._adaptation_since: dict[str, float] = {}
        self.normal_result: RoomMpcResult | None = None
        self.normal_heat_mode: HeatMode | None = None
        self.normal_target_temperature_c: float | None = None
        # Without an outdoor temperature the TRVs get the room's target as an
        # absolute setpoint and regulate on their own reading.
        self.outdoor_fallback_since_ts: float | None = None
        self.outdoor_fallback_target_c: float | None = None
        self._outdoor_seen = False
        self._started_ts = 0.0

        self._unsub: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    def set_sun_provider(
        self, provider: IrradianceProvider, workday_entity: str | None
    ) -> None:
        self.sun_provider = provider
        self.workday_entity = workday_entity

    def fresh_sun_series(self) -> SunSeries | None:
        """The hub's irradiance series, unless it is too old to use."""
        provider = self.sun_provider
        if provider is None or not provider.series.fresh(time.time()):
            return None
        return provider.series

    def is_workday(self, timestamp: float) -> bool:
        """The workday entity's state; without one Monday to Friday."""
        if self.workday_entity:
            state = self.hass.states.get(self.workday_entity)
            if state is not None and state.state in (STATE_ON, STATE_OFF):
                return state.state == STATE_ON
        return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).weekday() < 5

    def profile_index_at(self, timestamp: float) -> int:
        local = dt_util.as_local(dt_util.utc_from_timestamp(timestamp))
        return profile_index(local.hour, self.is_workday(timestamp))

    def current_gain(self) -> GainBreakdown:
        now_ts = time.time()
        gains = self.mpc.gain_model
        return gains.breakdown(
            self.profile_index_at(now_ts),
            gains.sun_regressors(self.fresh_sun_series(), now_ts),
        )

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def _notify_listeners(self) -> None:
        for update_callback in self._listeners:
            update_callback()

    async def async_setup(self) -> None:
        self._started_ts = time.time()
        factors = await self.store.async_load()
        if factors is not None:
            self.mpc.recalibrate_learning_factors(factors)
        else:
            await self._async_import_legacy_learning_factors()

        gains = await self.gain_store.async_load()
        if gains is not None:
            self.mpc.gain_model.restore(gains)

        flow_gate_state = await self.flow_gate_store.async_load()
        if flow_gate_state is not None:
            self.mpc.restore_flow_gate_state(flow_gate_state)
            self._persisted_flow_gate_state = flow_gate_state

        # The disturbance observer is gone; its file would stay behind in
        # .storage -- drop after season 2026/27.
        await async_remove_observer_store(self.hass, self.room_name, self.room_id)

        valve_curves = await self.valve_curve_store.async_load()
        if valve_curves is not None:
            self.mpc.restore_valve_curves(valve_curves)

        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            self.state.update_window_state(entity_id, self._is_on(entity_id))
        self.mpc.set_window_open(self.state.is_window_open, time.time())

        if self._learning_block_entity:
            self._learning_blocked = self._is_on(self._learning_block_entity)

        # A missing flow reading counts from startup on, like a gap.
        self._flow_temperature_seen_ts = self._started_ts
        self._refresh_temperature_inputs()
        self._update_learning_paused(time.time())

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if self._awaiting_room_sensor:
            self._unsub.append(
                async_call_later(
                    self.hass,
                    ROOM_SENSOR_STARTUP_GRACE_S,
                    self._async_handle_room_sensor_grace_expired,
                )
            )

        tracked_entities = list(self._trv_entity_ids)
        tracked_entities.extend(
            entity_id for entity_id in self._valve_position_entities if entity_id
        )
        tracked_entities.extend(self._adaptation_status_entities)
        tracked_entities.extend(self._mounted_mode_entities)
        tracked_entities.extend(self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []))
        tracked_entities.append(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        tracked_entities.append(self._flow_temperature_entity)
        if self._heat_source_climate_entity:
            tracked_entities.append(self._heat_source_climate_entity)
        if self._flow_threshold_entity:
            tracked_entities.append(self._flow_threshold_entity)
        if self._learning_block_entity:
            tracked_entities.append(self._learning_block_entity)
        if room_sensor_entity:
            tracked_entities.append(room_sensor_entity)

        self._unsub.append(
            async_track_state_change_event(
                self.hass, tracked_entities, self._async_handle_state_change
            )
        )
        self._unsub.append(
            async_track_entity_registry_updated_event(
                self.hass,
                [entity_id for trv in self._trvs for entity_id in trv.entity_ids],
                self._async_handle_trv_entity_registry_update,
            )
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_handle_learning_cycle, LEARNING_CYCLE_INTERVAL
            )
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_handle_sensor_poll, SENSOR_POLL_INTERVAL
            )
        )

        await self._async_recompute()

    def async_unload(self) -> None:
        self._unloaded = True
        for _, cancel in self._valve_pending.values():
            cancel()
        self._valve_pending.clear()
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        self.mpc.destroy()

    async def _async_import_legacy_learning_factors(self) -> None:
        slug = slugify(self.room_name)
        ua_state = self.hass.states.get(f"text.{slug}_ua_factor")
        capacity_state = self.hass.states.get(f"text.{slug}_capacity_factor")
        if ua_state is None or capacity_state is None:
            return

        try:
            factors = LearningFactors(
                ua_factor=float(ua_state.state),
                capacity_factor=float(capacity_state.state),
            )
        except ValueError:
            _LOGGER.warning(
                "Could not parse legacy learning factors for room %s", self.room_name
            )
            return

        self.mpc.recalibrate_learning_factors(factors)
        await self.store.async_save(factors)
        _LOGGER.info(
            "Imported legacy learning factors for room %s: ua_factor=%s capacity_factor=%s",
            self.room_name,
            factors.ua_factor,
            factors.capacity_factor,
        )

    def _refresh_temperature_inputs(self) -> None:
        for index, entity_id in enumerate(self._trv_entity_ids):
            state = self.hass.states.get(entity_id)
            self.mpc.set_trv_temperature(
                index, self._climate_temperature_from_state(state)
            )
            self.mpc.set_trv_target(index, self._climate_target_from_state(state))
        for index, entity_id in enumerate(self._valve_position_entities):
            if entity_id:
                self.mpc.set_valve_position(index, self._float_state(entity_id))

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if room_sensor_entity:
            room_sensor_temp_c = self._float_state(room_sensor_entity)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        self.mpc.set_outdoor_temperature(
            self._float_state(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        )
        self._update_flow_temperature(
            self.hass.states.get(self._flow_temperature_entity)
        )

    @callback
    def _async_handle_trv_entity_registry_update(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        """Bind the TRVs afresh once one of their entities is renamed or removed."""
        if event.data["action"] == "update" and "entity_id" not in event.data.get(
            "changes", {}
        ):
            return
        self.hass.config_entries.async_schedule_reload(self.hub_entry.entry_id)

    async def _async_handle_room_sensor_grace_expired(self, _now: Any) -> None:
        if not self._awaiting_room_sensor:
            return
        _LOGGER.warning(
            "Room sensor for room %s did not report within %s s after startup; "
            "falling back to TRV temperatures",
            self.room_name,
            ROOM_SENSOR_STARTUP_GRACE_S,
        )
        self._awaiting_room_sensor = False
        await self._async_recompute()

    async def _async_handle_sensor_poll(self, _now: Any) -> None:
        self._refresh_temperature_inputs()
        await self._async_recompute()

    def _update_flow_temperature(self, state: State | None) -> None:
        value = self._float_from_state(state)
        if value is not None:
            self._flow_temperature_c = value
            self._flow_temperature_seen_ts = time.time()
        self.mpc.set_flow_temperature(self._flow_temperature_c)

    @property
    def flow_temperature_missing(self) -> bool:
        """No flow reading for longer than the heat pump's usual poll gaps."""
        return time.time() - self._flow_temperature_seen_ts > FLOW_TEMPERATURE_MAX_GAP_S

    def _update_learning_paused(self, now_ts: float) -> None:
        paused = self._learning_blocked or self.flow_temperature_missing
        if paused != self._learning_paused:
            self._learning_paused = paused
            self.mpc.set_learning_paused(paused, now_ts)

    def _compute_trv_active(self) -> bool:
        if not self._heat_source_climate_entity:
            return False
        climate_state = self.hass.states.get(self._heat_source_climate_entity)
        if climate_state is not None and climate_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._last_heat_source_state = climate_state.state
        if self._last_heat_source_state != HEAT_SOURCE_ACTIVE_STATE:
            return False
        flow_temp_c = self.current_flow_temperature_c
        return flow_temp_c is not None and flow_temp_c >= self.flow_threshold_c

    def _adaptation_running(self, trv: TrvBinding, now_ts: float) -> bool:
        """Whether the TRV runs an adaptation that a write would abort."""
        if trv.adaptation_busy is None or trv.adaptation_status_entity is None:
            return False
        if not trv.adaptation_busy.matches(
            self.hass.states.get(trv.adaptation_status_entity)
        ):
            self._adaptation_since.pop(trv.climate_entity, None)
            return False
        since = self._adaptation_since.setdefault(trv.climate_entity, now_ts)
        if now_ts - since < ADAPTATION_MAX_HOLD_S:
            return True
        if since > 0:
            _LOGGER.warning(
                "%s still reports an adaptation run after %s s; writing again",
                trv.climate_entity,
                ADAPTATION_MAX_HOLD_S,
            )
            # Marks the run as warned about; it clears with the status.
            self._adaptation_since[trv.climate_entity] = -ADAPTATION_MAX_HOLD_S
        return False

    def _trv_uncalibrated(self, trv: TrvBinding) -> bool:
        """Whether the TRV is mounting or its valve is not calibrated.

        Its valve then follows the mounting or the TRV's own search, not the
        setpoint, so the room learns nothing. Unlike a running adaptation
        this does not stop writes: a TRV left uncalibrated still has to heat.
        """
        if trv.mounted_mode_entity is not None:
            mounted = self.hass.states.get(trv.mounted_mode_entity)
            if mounted is not None and mounted.state == STATE_ON:
                return True
        if trv.adaptation_ready is None or trv.adaptation_status_entity is None:
            return False
        return not trv.adaptation_ready.matches(
            self.hass.states.get(trv.adaptation_status_entity)
        )

    async def _async_apply_trv_active_switches(self, active: bool) -> None:
        desired_state = STATE_ON if active else STATE_OFF
        now_ts = time.time()
        switches_to_set = [
            trv.heat_available_entity
            for trv in self._trvs
            if trv.heat_available_entity
            and not self._adaptation_running(trv, now_ts)
            and (
                (state := self.hass.states.get(trv.heat_available_entity)) is None
                or state.state != desired_state
            )
        ]
        if not switches_to_set:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if active else "turn_off",
            {"entity_id": switches_to_set},
            blocking=False,
        )

    def _is_on(self, entity_id: str) -> bool:
        return self._bool_from_state(self.hass.states.get(entity_id))

    def _float_state(self, entity_id: str) -> float | None:
        return self._float_from_state(self.hass.states.get(entity_id))

    @staticmethod
    def _bool_from_state(state: State | None) -> bool:
        return state is not None and state.state == STATE_ON

    @staticmethod
    def _float_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _climate_temperature_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.attributes.get("current_temperature"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _climate_target_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.attributes.get("temperature"))
        except (TypeError, ValueError):
            return None

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        self.hass.async_create_task(
            self._async_process_state_change(entity_id, new_state)
        )

    async def _async_process_state_change(
        self, entity_id: str, new_state: State | None
    ) -> None:
        if entity_id in self._trv_entity_ids:
            index = self._trv_entity_ids.index(entity_id)
            self.mpc.set_trv_temperature(
                index, self._climate_temperature_from_state(new_state)
            )
            self.mpc.set_trv_target(index, self._climate_target_from_state(new_state))

        elif entity_id in self._valve_position_entities:
            self.mpc.set_valve_position(
                self._valve_position_entities.index(entity_id),
                self._float_from_state(new_state),
            )

        elif entity_id == self.data.get(CONF_ROOM_SENSOR_ENTITY):
            room_sensor_temp_c = self._float_from_state(new_state)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        elif entity_id == self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY]:
            self.mpc.set_outdoor_temperature(self._float_from_state(new_state))

        elif entity_id == self._flow_temperature_entity:
            self._update_flow_temperature(new_state)

        elif entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            was_open = self.state.is_window_open
            _, is_open = self.state.update_window_state(
                entity_id, self._bool_from_state(new_state)
            )
            self.mpc.set_window_open(is_open, time.time())
            if was_open and not is_open:
                self.mpc.suppress_learning_for_interval(
                    WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S,
                    WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S,
                )

        elif entity_id == self._learning_block_entity:
            self._learning_blocked = self._bool_from_state(new_state)

        await self._async_recompute()

    async def _async_handle_learning_cycle(self, _now: Any) -> None:
        self.mpc.run_learning_cycle()
        await self._async_persist_learning_factors()
        self._notify_listeners()

    async def _async_persist_learning_factors(self) -> None:
        persisted_factors = self.mpc.consume_persisted_learning_factors()
        if persisted_factors is not None:
            await self.store.async_save(persisted_factors)
        if self.mpc.gain_model.consume_changed():
            await self.gain_store.async_save(self.mpc.gain_model.export())

    async def async_recalibrate(
        self,
        ua_factor: float | None,
        capacity_factor: float | None,
        reset_gains: bool = False,
        reset_valve_curves: bool = False,
    ) -> None:
        """Set the factors; one left out keeps its learned value."""
        if reset_gains:
            self.mpc.gain_model.reset()
            await self.gain_store.async_save(self.mpc.gain_model.export())
            self.mpc.gain_model.consume_changed()
            _LOGGER.info("Reset learned gains for room %s", self.room_name)
        if reset_valve_curves:
            self.mpc.reset_valve_curves()
            # Replaces a pending delayed write, which would read the same.
            self._valve_curve_save_due_ts = None
            await self.valve_curve_store.async_save(self.mpc.export_valve_curves())
            _LOGGER.info("Reset learned valve curves for room %s", self.room_name)
        if ua_factor is None and capacity_factor is None:
            self._notify_listeners()
            return
        factors = LearningFactors(
            ua_factor=self.mpc.learned_ua_factor if ua_factor is None else ua_factor,
            capacity_factor=(
                self.mpc.learned_capacity_factor
                if capacity_factor is None
                else capacity_factor
            ),
        )
        self.mpc.recalibrate_learning_factors(factors)
        await self.store.async_save(factors)
        _LOGGER.info(
            "Recalibrated learning factors for room %s: ua_factor=%s capacity_factor=%s",
            self.room_name,
            factors.ua_factor,
            factors.capacity_factor,
        )
        self._notify_listeners()

    def is_sun_condition_met(self, timestamp: float) -> bool:
        moment = dt_util.utc_from_timestamp(timestamp)
        today = dt_util.as_local(moment).date()

        sunset_today = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, today)
        if sunset_today is not None and moment >= sunset_today + SUNSET_LEARNING_DELAY:
            return True

        sunrise_today = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, today)
        sunset_yesterday = get_astral_event_date(
            self.hass, SUN_EVENT_SUNSET, today - timedelta(days=1)
        )
        if sunrise_today is None or sunset_yesterday is None:
            return False
        return sunset_yesterday + SUNSET_LEARNING_DELAY <= moment < sunrise_today

    @property
    def flow_threshold_c(self) -> float:
        """The lowest flow temperature the heat source can usefully deliver.

        Read from the linked entity on every access so the limit can be tried
        out on the heat pump by changing a single helper. Without a linked
        entity, or while it reports nothing usable, the hub's fixed threshold
        keeps the room controllable instead of blocking it.
        """
        if self.flow_threshold_provider is not None:
            return self.flow_threshold_provider()
        fixed = self.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)
        if not self._flow_threshold_entity:
            return fixed
        value = self._float_state(self._flow_threshold_entity)
        if value is None:
            return fixed
        return value

    async def _async_recompute(self) -> None:
        self.mpc.set_flow_threshold_c(self.flow_threshold_c)
        if self.hot_water_charge_provider is not None:
            self.mpc.set_external_hot_water_charge(self.hot_water_charge_provider())
        desired_mode = self.state.desired_automatic_heat_mode(self.blocked)
        self.state.set_active_heat_mode(desired_mode)

        now_ts = time.time()
        self._update_learning_paused(now_ts)
        self.mpc.set_trv_adapting(
            any(
                self._adaptation_running(trv, now_ts) or self._trv_uncalibrated(trv)
                for trv in self._trvs
            )
        )
        self.trv_active = self._compute_trv_active()
        heat_source_pending = self._heat_source_pending()
        self.state.set_trv_active(self.trv_active)
        self.mpc.set_heat_available(self.trv_active)
        if self.trv_active:
            self.mpc.enable_learning()
        else:
            self.mpc.disable_learning()

        if self._awaiting_room_sensor:
            if not heat_source_pending:
                await self._async_apply_trv_active_switches(self.trv_active)
            self._notify_listeners()
            return

        # The room reads as unheated only because the heat source has not
        # reported yet: no forced frost protection, nothing to write. A real
        # reason (open window, a mode the user picked) still goes through.
        if heat_source_pending and not self.blocked and not self.state.is_window_open:
            self._notify_listeners()
            return

        self.normal_heat_mode = self.state.current_heat_mode
        normal_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(self.normal_heat_mode)
        )
        self.normal_target_temperature_c = normal_target
        self.mpc.set_current_trv_targets(
            [self._current_trv_target(entity_id) for entity_id in self._trv_entity_ids]
        )
        normal_compute = self.mpc.compute(normal_target, apply_side_effects=False)
        self.normal_result = normal_compute.result if normal_compute.valid else None

        display_mode = self.state.resolve_display_mode(self.blocked)
        actual_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(display_mode)
        )
        actual_compute = self.mpc.compute(
            actual_target,
            skip_demand_smoothing=self.state.should_force_frost_protection(
                self.blocked
            ),
        )
        forced = self.state.should_force_frost_protection(self.blocked)
        if not self.mpc.outdoor_temperature_missing:
            self._outdoor_seen = True
        if actual_compute.valid:
            self._end_outdoor_fallback()
            # Direct-valve devices use demand for the actuator; their climate
            # setpoint mirrors the room, not the temperature-to-valve mapping.
            self.last_result = replace(
                actual_compute.result,
                trv_targets=[
                    clamp(
                        round_to_step(actual_target, trv.target_temperature_step_c),
                        trv.min_target_temperature_c,
                        trv.max_target_temperature_c,
                    )
                    if trv.valve_opening_entity
                    else target
                    for trv, target in zip(
                        self._trvs, actual_compute.result.trv_targets, strict=True
                    )
                ],
            )
            await self._async_write_trv_targets(
                self.last_result.trv_targets,
                forced=forced,
                valve_demand_pct=actual_compute.result.demand_pct,
            )
        elif self.mpc.outdoor_temperature_missing and self._outdoor_fallback_due():
            await self._async_apply_outdoor_fallback(actual_target, forced)
        else:
            _LOGGER.debug(
                "MPC compute failed for room %s: %s",
                self.room_name,
                actual_compute.error,
            )

        if not heat_source_pending:
            await self._async_apply_trv_active_switches(self.trv_active)
        await self._async_persist_learning_factors()
        await self._async_persist_flow_gate_state()
        self._schedule_valve_curve_save()
        self._notify_listeners()

    async def _async_persist_flow_gate_state(self) -> None:
        flow_gate_state = self.mpc.flow_gate_state
        if flow_gate_state == self._persisted_flow_gate_state:
            return
        await self.flow_gate_store.async_save(flow_gate_state)
        self._persisted_flow_gate_state = flow_gate_state

    async def async_flush_valve_curves(self) -> None:
        """Write a pending delayed valve-curve save now.

        On unload: an awaited save replaces the pending delayed write, so it
        cannot fire after the entry is removed and recreate the file, and a
        reload keeps the samples.
        """
        if self._valve_curve_save_due_ts is None:
            return
        self._valve_curve_save_due_ts = None
        await self.valve_curve_store.async_save(self.mpc.export_valve_curves())

    def _schedule_valve_curve_save(self) -> None:
        if not self.mpc.consume_valve_curves_changed():
            return
        now_ts = time.time()
        if (
            self._valve_curve_save_due_ts is not None
            and now_ts < self._valve_curve_save_due_ts
        ):
            # The pending write reads the samples when it runs.
            return
        self.valve_curve_store.async_delay_save(
            self.mpc.export_valve_curves, VALVE_CURVE_SAVE_DELAY_S
        )
        self._valve_curve_save_due_ts = now_ts + VALVE_CURVE_SAVE_DELAY_S

    async def _async_apply_outdoor_fallback(
        self, target_temperature_c: float, forced: bool
    ) -> None:
        """Hand the room to the TRVs while the outdoor temperature is missing.

        The model cannot plan without it, and keeping the last relative target
        would leave the valves where they happened to stand. The TRVs get the
        target of the active mode as an absolute setpoint instead and regulate
        on their own reading; the MPC takes over again once the value is back.
        Learning pauses meanwhile (the controller tells the learner).
        """
        if self.outdoor_fallback_since_ts is None:
            self.outdoor_fallback_since_ts = time.time()
            _LOGGER.warning(
                "Outdoor temperature missing for room %s; TRVs hold %s °C on "
                "their own until it is back",
                self.room_name,
                target_temperature_c,
            )
        self.outdoor_fallback_target_c = target_temperature_c
        # The planned values no longer apply; showing them would pass stale
        # numbers off as current.
        self.last_result = None
        await self._async_write_trv_targets(
            [
                clamp(
                    round_to_step(target_temperature_c, trv.target_temperature_step_c),
                    trv.min_target_temperature_c,
                    trv.max_target_temperature_c,
                )
                for trv in self._trvs
            ],
            forced=forced,
        )

    def _outdoor_fallback_due(self) -> bool:
        """Whether a missing outdoor temperature is an outage, not a startup.

        Until then nothing is written, as for any other missing input;
        learning pauses all the same (the controller tells the learner).
        """
        return self._outdoor_seen or not self._in_startup_grace()

    def _in_startup_grace(self) -> bool:
        return time.time() - self._started_ts < STARTUP_GRACE_S

    def _heat_source_pending(self) -> bool:
        """The heat source has not reported since startup, within the grace.

        Its state and the flow temperature both decide whether the TRVs get
        heat; until both were seen the room is not known to be unheated.
        Without a heat source entity the room is unheated by configuration,
        nothing to wait for.
        """
        if not self._heat_source_climate_entity:
            return False
        known = (
            self._last_heat_source_state is not None
            and self._flow_temperature_c is not None
        )
        return not known and self._in_startup_grace()

    def _end_outdoor_fallback(self) -> None:
        if self.outdoor_fallback_since_ts is None:
            return
        _LOGGER.info(
            "Outdoor temperature back for room %s; the MPC takes over again",
            self.room_name,
        )
        self.outdoor_fallback_since_ts = None
        self.outdoor_fallback_target_c = None

    @property
    def outdoor_fallback_active(self) -> bool:
        return self.outdoor_fallback_since_ts is not None

    async def _async_write_trv_targets(
        self,
        trv_targets: list[float],
        forced: bool = False,
        valve_demand_pct: float | None = None,
    ) -> None:
        # State events can arrive while a blocking number service is in flight.
        async with self._trv_write_lock:
            if not self._unloaded:
                await self._async_write_trv_targets_locked(
                    trv_targets, forced, valve_demand_pct
                )

    async def _async_write_trv_targets_locked(
        self,
        trv_targets: list[float],
        forced: bool,
        valve_demand_pct: float | None,
    ) -> None:
        for trv, target_temperature_c in zip(self._trvs, trv_targets, strict=True):
            entity_id = trv.climate_entity
            # Every write wakes the TRV and can abort a running adaptation run,
            # so only send a target the TRV does not already report, none to a
            # TRV that cannot take it, and none while it adapts unless frost
            # protection is forced.
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            now_ts = time.time()
            if not forced and self._adaptation_running(trv, now_ts):
                continue
            if trv.valve_opening_entity:
                try:
                    # No MPC demand means the existing absolute-temperature
                    # fallback: release both limits for the TRV's own regulator.
                    await self._async_set_valve(
                        trv,
                        100 if valve_demand_pct is None else valve_demand_pct,
                        allow_bump=valve_demand_pct is not None,
                    )
                except HomeAssistantError:
                    _LOGGER.exception(
                        "Valve write failed for %s; retry next cycle", entity_id
                    )
                    continue
            current_target_c = self._current_trv_target(entity_id)
            if (
                current_target_c is not None
                and abs(current_target_c - target_temperature_c)
                < TARGET_TEMPERATURE_TOLERANCE_C
            ):
                continue
            min_target_c = trv.min_target_temperature_c
            switches = (
                current_target_c is None or current_target_c <= min_target_c
            ) != (target_temperature_c <= min_target_c)
            if (
                switches
                and not trv.valve_opening_entity
                and not forced
                and now_ts - self._trv_switched_at.get(entity_id, 0.0)
                < TRV_SWITCH_MIN_INTERVAL_S
            ):
                continue
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target_temperature_c},
                blocking=bool(trv.valve_opening_entity),
            )
            self.trv_setpoint_writes[entity_id] = (
                self.trv_setpoint_writes.get(entity_id, 0) + 1
            )
            self._written_targets[entity_id] = (target_temperature_c, now_ts)
            if switches:
                self._trv_switched_at[entity_id] = now_ts

    async def _async_set_valve(
        self, trv: TrvBinding, percent: float, *, allow_bump: bool = True
    ) -> None:
        """TRVZB commands, called with _trv_write_lock held.

        Small closes use +10 pp, wait 5 s, then close to the latest target.
        Already pending targets are not postponed by repeated controller ticks.
        """
        entity_id = trv.climate_entity
        opening = self.hass.states.get(trv.valve_opening_entity)
        closing = self.hass.states.get(trv.valve_closing_entity)
        if any(
            s is None or s.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
            for s in (opening, closing)
        ):
            raise HomeAssistantError(f"Valve numbers unavailable for {entity_id}")
        low = max(
            0,
            float(opening.attributes.get("min", 0)),
            100 - float(closing.attributes.get("max", 100)),
        )
        high = min(
            100,
            float(opening.attributes.get("max", 100)),
            100 - float(closing.attributes.get("min", 0)),
        )
        if not math.isfinite(percent) or low > high:
            raise HomeAssistantError(f"Invalid valve target/range for {entity_id}")
        target = int(clamp(round(percent), math.ceil(low), math.floor(high)))
        pending = self._valve_pending.get(entity_id)
        if pending is not None and pending[0] == target and allow_bump:
            return
        if pending is not None:
            pending[1]()
            self._valve_pending.pop(entity_id, None)
        written = self._valve_commands.get(entity_id)
        current = None
        if written is not None and time.time() - written[1] < TARGET_WRITE_SETTLE_S:
            current = written[0]
        else:
            open_value = self._float_from_state(opening)
            close_value = self._float_from_state(closing)
            if (
                open_value is not None
                and close_value is not None
                and abs(open_value + close_value - 100) < 0.1
            ):
                current = open_value
        if current == target:
            return
        if (
            allow_bump
            and pending is None
            and current is not None
            and 0 < current - target < 10
        ):
            bump = int(min(high, current + 10))
            if bump - target < 10:
                _LOGGER.warning(
                    "%s: upper valve limit prevents a 10 pp closing stroke", entity_id
                )
            if bump > current:
                try:
                    await self._async_write_valve_pair(trv, bump)
                except HomeAssistantError:
                    # Do not leave a partially applied bump as the last write.
                    await self._async_write_valve_pair(trv, target)
                    return
                if self._unloaded:
                    return

                async def finish(_now: Any) -> None:
                    async with self._trv_write_lock:
                        # Identity check also rejects a timer already dispatched
                        # before cancellation, or before an unload/reload.
                        if (
                            self._unloaded
                            or self._valve_pending.get(entity_id) is not scheduled
                        ):
                            return
                        self._valve_pending.pop(entity_id, None)
                        try:
                            await self._async_set_valve(trv, target, allow_bump=False)
                        except HomeAssistantError:
                            _LOGGER.exception(
                                "Delayed valve write failed for %s; retry next cycle",
                                entity_id,
                            )

                scheduled = (target, async_call_later(self.hass, 5, finish))
                self._valve_pending[entity_id] = scheduled
                return
        await self._async_write_valve_pair(trv, target)

    async def _async_write_valve_pair(self, trv: TrvBinding, percent: int) -> None:
        # Invalidate before either write: a partial failure must retry BOTH.
        self._valve_commands.pop(trv.climate_entity, None)
        for entity_id, value in (
            (trv.valve_opening_entity, percent),
            (trv.valve_closing_entity, 100 - percent),
        ):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
        self._valve_commands[trv.climate_entity] = (percent, time.time())

    def _current_trv_target(self, entity_id: str) -> float | None:
        """The TRV's target, or the one just written while its echo settles."""
        written = self._written_targets.get(entity_id)
        if written is not None and time.time() - written[1] < TARGET_WRITE_SETTLE_S:
            return written[0]
        return self._climate_target_from_state(self.hass.states.get(entity_id))

    async def async_set_manual_heat_mode(self, mode: HeatMode) -> None:
        self.state.set_active_heat_mode(mode)
        self.blocked = True
        await self._async_recompute()

    async def async_unblock(self) -> None:
        self.blocked = False
        await self._async_recompute()

    async def async_set_house_comfort(self, value: bool) -> None:
        if not self.follows_house_comfort:
            return
        self.state.set_house_comfort(value)
        await self._async_recompute()

    async def async_set_room_comfort(self, value: bool) -> None:
        self.state.set_room_comfort(value)
        await self._async_recompute()

    async def async_set_pv_boost(self, value: bool) -> None:
        self.state.set_pv_boost(value)
        await self._async_recompute()

    async def async_set_comfort_temperature(self, value: float) -> None:
        self.state.set_comfort_temperature(value)
        await self._async_recompute()

    async def async_set_eco_temperature_offset(self, value: float) -> None:
        self.state.set_eco_temperature_offset(value)
        await self._async_recompute()

    @property
    def trv_entity_ids(self) -> list[str]:
        return self._trv_entity_ids

    @property
    def trv_valve_positions(self) -> dict[str, float | None]:
        """The valve opening each TRV reports (pi_heating_demand), in percent.

        None for a TRV whose profile has no valve-position entity or whose
        entity has no number right now.
        """
        return {
            climate: self._float_state(valve) if valve else None
            for climate, valve in zip(
                self._trv_entity_ids, self._valve_position_entities, strict=True
            )
        }

    @property
    def radiators_not_heating(self) -> list[str]:
        """TRVs whose radiator gives off no heat although the valve is open."""
        return [
            entity_id
            for entity_id, not_heating in zip(
                self._trv_entity_ids, self.mpc.radiators_not_heating
            )
            if not_heating
        ]

    @property
    def current_heat_mode(self) -> HeatMode:
        return self.state.resolve_display_mode(self.blocked)

    @property
    def base_temperature_c(self) -> float:
        return self.state.determine_base_target_temperature(self.current_heat_mode)

    @property
    def is_automation_active(self) -> bool:
        return not self.blocked

    @property
    def current_flow_temperature_c(self) -> float | None:
        """The last valid flow temperature; None until one was reported."""
        return self._flow_temperature_c

    @property
    def normal_min_flow_temperature_c(self) -> float | None:
        result = self.normal_result
        return result.recommended_flow_temperature_c if result else None

    @property
    def normal_hold_flow_temperature_c(self) -> float | None:
        """The ungated hold requirement -- what the room would need at setpoint.

        Unlike normal_min_flow_temperature_c this keeps following the weather
        while the surplus gate is closed, which is what makes the gate's effect
        (and the seasonal heating limit behind it) observable.
        """
        result = self.normal_result
        return result.hold_flow_temperature_c if result else None

    @property
    def comfort_temperature_c(self) -> float:
        return self.state.determine_base_target_temperature(HeatMode.COMFORT)

    def curve_outdoor_c(self, end: CurveEnd) -> float | None:
        """Outdoor value of one end of the heat source's curve; None without one."""
        if self.curve_outdoor_provider is None:
            return None
        cold_c, warm_c = self.curve_outdoor_provider()
        return cold_c if end is CurveEnd.COLD else warm_c

    def hold_flow_at_curve_end_c(self, end: CurveEnd) -> float | None:
        """Flow that holds the comfort temperature at one end of the curve.

        Comfort, not the current mode: the curve has to carry the room when it
        is in use. Learned UA, no gains -- errs high, the safe side for a
        curve that also has to hold through the night.
        """
        outdoor_c = self.curve_outdoor_c(end)
        if outdoor_c is None:
            return None
        return self.comfort_hold_flow_c(outdoor_c)

    def comfort_hold_flow_c(self, outdoor_c: float) -> float | None:
        """Flow that holds the comfort temperature at an outdoor value."""
        return self.mpc.calculate_hold_flow_temperature_c(
            self.comfort_temperature_c, outdoor_c
        )

    @property
    def normal_gated_hold_flow_temperature_c(self) -> float | None:
        """The hold requirement after the surplus gate, without recovery.

        Depends on the weather and on whether the room coasts on stored heat,
        not on a deficit the room would recover from once heating runs -- the
        signal for deciding whether the heating season is on.
        """
        result = self.normal_result
        return result.gated_hold_flow_temperature_c if result else None

    @property
    def normal_recovery_flow_temperature_c(self) -> float | None:
        result = self.normal_result
        return result.recovery_flow_temperature_c if result else None

    @property
    def normal_recovery_flow_saturated(self) -> bool | None:
        result = self.normal_result
        return result.recovery_flow_saturated if result else None

    @property
    def normal_flow_gate_closed(self) -> bool | None:
        result = self.normal_result
        return result.flow_gate_closed if result else None

    @property
    def flow_supply_status(self) -> FlowSupplyStatus:

        required = self.normal_min_flow_temperature_c
        if required is None or required <= 0:
            if self.normal_flow_gate_closed:
                return FlowSupplyStatus.SURPLUS
            return FlowSupplyStatus.NO_REQUIREMENT

        if required < self.flow_threshold_c:
            return FlowSupplyStatus.BELOW_THRESHOLD

        if not self.trv_active:
            return FlowSupplyStatus.SOURCE_INACTIVE

        current = self.current_flow_temperature_c
        if current is not None and current >= required:
            return FlowSupplyStatus.SUFFICIENT

        return FlowSupplyStatus.UNDERSUPPLIED
