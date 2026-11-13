"""Tests for Daikin Onecta water heater entities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.daikin_onecta.device import DaikinOnectaDevice
from homeassistant.components.daikin_onecta.water_heater import DaikinWaterTank
from homeassistant.components.water_heater import (
    DOMAIN as WATER_HEATER_DOMAIN,
    SERVICE_SET_TEMPERATURE as WATER_HEATER_SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_OFF as WATER_HEATER_SERVICE_TURN_OFF,
    SERVICE_TURN_ON as WATER_HEATER_SERVICE_TURN_ON,
    STATE_HEAT_PUMP,
    STATE_OFF,
    STATE_PERFORMANCE,
    WaterHeaterEntityFeature,
    WaterHeaterServiceArgument,
    WaterHeaterStateAttribute,
)
from homeassistant.const import ATTR_ENTITY_ID, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .platform_helpers import _execute_typed_command
from .test_climate_snapshots import _async_setup_fixture, _load_gateway_devices


@pytest.mark.parametrize("power_settable", [False, True])
def test_water_heater_features_follow_power_capability(power_settable: bool) -> None:
    """Only advertise power and operation controls for a writable characteristic."""
    device = DaikinOnectaDevice(_load_gateway_devices("holidaymode")[0])
    point = device.device.management_points_by_type("domesticHotWaterFlowThrough")[0]
    point.on_off_mode.settable = power_settable
    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)

    expected = (
        WaterHeaterEntityFeature.ON_OFF | WaterHeaterEntityFeature.OPERATION_MODE
        if power_settable
        else WaterHeaterEntityFeature(0)
    )
    assert entity.supported_features == expected
    assert entity.operation_list == (["off", STATE_HEAT_PUMP] if power_settable else [])


@pytest.mark.parametrize(
    "method", ["async_turn_on", "async_turn_off", "async_set_operation_mode"]
)
async def test_water_heater_rejects_read_only_power(method: str) -> None:
    """Read-only water-heater actions fail without spending a cloud request."""
    device = DaikinOnectaDevice(_load_gateway_devices("holidaymode")[0])
    point = device.device.management_points_by_type("domesticHotWaterFlowThrough")[0]
    point.on_off_mode.value = "off" if method == "async_turn_on" else "on"
    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)
    entity._async_execute_hot_water_command = AsyncMock()

    with pytest.raises(HomeAssistantError):
        await getattr(entity, method)(
            *(["off"] if method == "async_set_operation_mode" else [])
        )

    entity._async_execute_hot_water_command.assert_not_awaited()


@pytest.mark.parametrize("power", ["on", "off"])
def test_water_heater_read_only_power_keeps_writable_boost(power: str) -> None:
    """Allow writable boost while powered on without advertising power control."""
    device = DaikinOnectaDevice(_load_gateway_devices("altherma_boost")[0])
    point = device.device.management_points_by_type("domesticHotWaterTank")[0]
    point.on_off_mode.settable = False
    point.on_off_mode.value = power
    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)

    assert not entity.supported_features & WaterHeaterEntityFeature.ON_OFF
    assert bool(
        entity.supported_features & WaterHeaterEntityFeature.OPERATION_MODE
    ) == (power == "on")
    assert entity.operation_list == (
        [STATE_HEAT_PUMP, STATE_PERFORMANCE] if power == "on" else []
    )


def test_water_heater_handles_missing_temperature_values() -> None:
    """Use HA defaults when Daikin omits temperature bounds and target."""
    device = DaikinOnectaDevice(_load_gateway_devices("altherma_boost")[0])
    point = device.device.management_points_by_type("domesticHotWaterTank")[0]
    assert point.domestic_hot_water is not None
    assert point.domestic_hot_water.temperature is not None
    point.domestic_hot_water.temperature.value = None
    point.domestic_hot_water.temperature.min_value = None
    point.domestic_hot_water.temperature.max_value = None

    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)

    assert entity.target_temperature is None
    assert entity.min_temp == super(DaikinWaterTank, entity).min_temp
    assert entity.max_temp == super(DaikinWaterTank, entity).max_temp


@pytest.mark.parametrize(
    ("setpoint_unit", "sensory_unit", "expected_unit"),
    [
        ("°F", None, UnitOfTemperature.FAHRENHEIT),
        (None, "°F", UnitOfTemperature.FAHRENHEIT),
        (None, None, UnitOfTemperature.CELSIUS),
    ],
)
def test_water_heater_uses_daikin_temperature_unit(
    setpoint_unit: str | None,
    sensory_unit: str | None,
    expected_unit: UnitOfTemperature,
) -> None:
    """Use the reported setpoint or tank-sensor unit, defaulting to Celsius."""
    device = DaikinOnectaDevice(_load_gateway_devices("altherma_boost")[0])
    point = device.device.management_points_by_type("domesticHotWaterTank")[0]
    setpoint = point.domestic_hot_water.temperature
    tank_temperature = point.sensory_characteristic("tankTemperature")
    assert setpoint is not None
    assert tank_temperature is not None
    setpoint.unit = setpoint_unit
    tank_temperature.unit = sensory_unit

    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)

    assert entity.native_temperature_unit == expected_unit


def test_water_heater_uses_standard_target_temperature_step() -> None:
    """Expose the Daikin setpoint increment through the standard capability."""
    device = DaikinOnectaDevice(_load_gateway_devices("altherma_boost")[0])
    point = device.device.management_points_by_type("domesticHotWaterTank")[0]
    assert point.domestic_hot_water is not None
    assert point.domestic_hot_water.temperature is not None

    entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)

    assert entity.target_temperature_step == float(
        point.domestic_hot_water.temperature.step_value
    )
    assert entity.extra_state_attributes is None


@pytest.mark.parametrize(
    ("operation", "temperature_settable", "translation_key"),
    [
        (STATE_OFF, True, "water_heater_off"),
        (STATE_HEAT_PUMP, False, "water_heater_temperature_not_settable"),
        (STATE_HEAT_PUMP, None, "water_heater_temperature_not_settable"),
    ],
)
async def test_water_heater_rejects_unavailable_temperature_control(
    operation: str,
    temperature_settable: bool | None,
    translation_key: str,
) -> None:
    """Reject temperature writes when the tank cannot accept them."""
    if temperature_settable is None:
        entity = object.__new__(DaikinWaterTank)
        entity._device = MagicMock(
            name="Tank",
            management_point=MagicMock(
                return_value=SimpleNamespace(
                    domestic_hot_water=SimpleNamespace(temperature=None)
                )
            ),
        )
        entity._embedded_id = "tank"
    else:
        device = DaikinOnectaDevice(_load_gateway_devices("altherma_boost")[0])
        point = device.device.management_points_by_type("domesticHotWaterTank")[0]
        assert point.domestic_hot_water is not None
        assert point.domestic_hot_water.temperature is not None
        point.domestic_hot_water.temperature.settable = temperature_settable
        entity = DaikinWaterTank(device, MagicMock(), point.embedded_id)
    entity._attr_current_operation = operation
    entity._async_execute_hot_water_command = AsyncMock()

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_tank_temperature(50)

    assert err.value.translation_key == translation_key
    entity._async_execute_hot_water_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("current_operation", "supported_features", "method", "translation_key"),
    [
        (
            STATE_OFF,
            WaterHeaterEntityFeature(0),
            "async_turn_on",
            "water_heater_on_off_unavailable",
        ),
        (
            STATE_HEAT_PUMP,
            WaterHeaterEntityFeature(0),
            "async_turn_off",
            "water_heater_on_off_unavailable",
        ),
    ],
)
async def test_water_heater_rejects_unavailable_power_control(
    current_operation: str,
    supported_features: WaterHeaterEntityFeature,
    method: str,
    translation_key: str,
) -> None:
    """Reject turning a water heater on or off without a writable power control."""
    entity = object.__new__(DaikinWaterTank)
    object.__setattr__(entity, "_device", MagicMock(name="Device"))
    object.__setattr__(entity, "_attr_current_operation", current_operation)
    object.__setattr__(entity, "_attr_supported_features", supported_features)

    with pytest.raises(ServiceValidationError) as err:
        await getattr(entity, method)()

    assert err.value.translation_key == translation_key


async def test_water_heater_rejects_unadvertised_operation_mode() -> None:
    """Reject an operation mode that the heater does not advertise."""
    entity = object.__new__(DaikinWaterTank)
    object.__setattr__(entity, "_device", MagicMock(name="Device"))
    entity.get_operation_list = MagicMock(return_value=[STATE_OFF])

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_operation_mode(STATE_HEAT_PUMP)

    assert err.value.translation_key == "water_heater_operation_mode_unavailable"


async def test_water_heater_turn_off_updates_cached_state(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A successful water-heater command updates the cached operation mode."""
    await _async_setup_fixture(hass, config_entry, "altherma_boost")
    config_entry.runtime_data.async_execute_command = _execute_typed_command(
        config_entry
    )
    listener = MagicMock()
    remove_listener = config_entry.runtime_data.async_add_listener(listener)

    await hass.services.async_call(
        WATER_HEATER_DOMAIN,
        WATER_HEATER_SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "water_heater.altherma_domestichotwatertank"},
        blocking=True,
    )

    state = hass.states.get("water_heater.altherma_domestichotwatertank")
    assert state is not None
    assert state.attributes[WaterHeaterStateAttribute.OPERATION_MODE] == "off"

    await hass.services.async_call(
        WATER_HEATER_DOMAIN,
        WATER_HEATER_SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: state.entity_id},
        blocking=True,
    )
    state = hass.states.get(state.entity_id)
    assert state is not None
    assert (
        state.attributes[WaterHeaterStateAttribute.OPERATION_MODE] == STATE_PERFORMANCE
    )
    assert listener.call_count == 2
    remove_listener()


async def test_water_heater_temperature_updates_typed_cache(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A successful temperature write updates the shared typed model."""
    await _async_setup_fixture(hass, config_entry, "altherma_boost")
    config_entry.runtime_data.async_execute_command = _execute_typed_command(
        config_entry
    )
    listener = MagicMock()
    remove_listener = config_entry.runtime_data.async_add_listener(listener)

    await hass.services.async_call(
        WATER_HEATER_DOMAIN,
        WATER_HEATER_SERVICE_SET_TEMPERATURE,
        {
            ATTR_ENTITY_ID: "water_heater.altherma_domestichotwatertank",
            WaterHeaterServiceArgument.TEMPERATURE: 50,
        },
        blocking=True,
    )

    management_point = next(
        point
        for device in config_entry.runtime_data.data.values()
        for point in device.device.management_points
        if point.management_point_type == "domesticHotWaterTank"
    )
    assert management_point.domestic_hot_water is not None
    assert management_point.domestic_hot_water.temperature is not None
    assert management_point.domestic_hot_water.temperature.value == 50
    listener.assert_called_once()
    remove_listener()


async def test_water_heater_publishes_successful_partial_operation_write() -> None:
    """Keep the successful power write visible if powerful mode later fails."""
    entity = object.__new__(DaikinWaterTank)
    power = SimpleNamespace(value="off", settable=True)
    powerful_mode = SimpleNamespace(value="off", settable=True)
    hot_water = SimpleNamespace(
        power=power,
        powerful_mode=powerful_mode,
        temperature=None,
        current_temperature=None,
    )
    entity._device = MagicMock(
        name="Tank",
        management_point=MagicMock(
            return_value=SimpleNamespace(
                domestic_hot_water=hot_water,
                on_off_mode=power,
                sensory_characteristic=MagicMock(return_value=None),
            )
        ),
    )
    entity._embedded_id = "tank"
    entity._attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    entity._attr_current_operation = "off"
    entity._attr_operation_list = ["off", STATE_HEAT_PUMP, STATE_PERFORMANCE]
    entity._async_execute_hot_water_command = AsyncMock(
        side_effect=[None, HomeAssistantError]
    )
    entity.coordinator = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_set_operation_mode(STATE_PERFORMANCE)

    assert power.value == "on"
    assert entity.current_operation == STATE_HEAT_PUMP
    entity.coordinator.async_update_listeners.assert_called_once()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_turn_on", ()),
        ("async_turn_off", ()),
        ("async_set_operation_mode", (STATE_HEAT_PUMP,)),
        ("async_set_tank_temperature", (50,)),
    ],
)
async def test_water_heater_rejects_unknown_power_state(
    method: str, args: tuple[object, ...]
) -> None:
    """Reject water-heater commands when Daikin omits the current power value."""
    entity = object.__new__(DaikinWaterTank)
    object.__setattr__(entity, "_device", MagicMock(name="Device"))
    object.__setattr__(entity, "_attr_current_operation", None)
    entity.get_operation_list = MagicMock(
        return_value=[STATE_OFF, STATE_HEAT_PUMP, STATE_PERFORMANCE]
    )
    entity._async_execute_hot_water_command = AsyncMock()

    with pytest.raises(ServiceValidationError) as err:
        await getattr(entity, method)(*args)

    assert err.value.translation_key == "water_heater_on_off_unavailable"
    entity._async_execute_hot_water_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("current", "requested", "expected"),
    [
        (STATE_HEAT_PUMP, STATE_OFF, ("off", "")),
        (STATE_OFF, STATE_PERFORMANCE, ("on", "on")),
        (STATE_PERFORMANCE, STATE_HEAT_PUMP, ("", "off")),
        (STATE_HEAT_PUMP, STATE_HEAT_PUMP, ("", "")),
    ],
)
def test_water_heater_requested_modes(
    current: str, requested: str, expected: tuple[str, str]
) -> None:
    """Calculate only the native power and boost changes required."""
    entity = object.__new__(DaikinWaterTank)
    entity._attr_current_operation = current

    assert entity._requested_modes(requested) == expected


async def test_water_heater_publishes_successful_powerful_mode_write() -> None:
    """Publish a successful boost change to sibling entities."""
    entity = object.__new__(DaikinWaterTank)
    powerful_mode = SimpleNamespace(value="off", settable=True)
    hot_water = SimpleNamespace(
        power=SimpleNamespace(value="on", settable=True),
        powerful_mode=powerful_mode,
    )
    point = SimpleNamespace(domestic_hot_water=hot_water)
    entity._device = SimpleNamespace(management_point=lambda _: point)
    entity._embedded_id = "tank"
    entity._attr_current_operation = STATE_HEAT_PUMP
    entity._attr_operation_list = [STATE_HEAT_PUMP, STATE_PERFORMANCE]
    entity._async_execute_hot_water_command = AsyncMock()
    entity._publish_write_update = MagicMock()

    await entity.async_set_operation_mode(STATE_PERFORMANCE)

    assert powerful_mode.value == "on"
    entity._publish_write_update.assert_called_once_with()
