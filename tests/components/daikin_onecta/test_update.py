"""Tests for Daikin Onecta firmware update entities."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from daikin_onecta.models import Characteristic, GatewayDevice
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE as CLIMATE_SERVICE_SET_TEMPERATURE,
    ClimateServiceArgument,
)
from homeassistant.components.daikin_onecta.device import DaikinOnectaDevice
from homeassistant.components.daikin_onecta.update import DaikinFirmwareUpdateEntity
from homeassistant.components.update import (
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
    UpdateEntityFeature,
    UpdateEntityStateAttribute,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .platform_helpers import _async_fire_coordinator_update, _execute_typed_command
from .test_climate_snapshots import _async_setup_fixture, _load_gateway_devices


async def test_firmware_install_executes_command(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The update service executes the typed firmware-install command."""
    await _async_setup_fixture(hass, config_entry, "dx4_firmwareavailable")
    config_entry.runtime_data.async_execute_command = _execute_typed_command(
        config_entry
    )

    await hass.services.async_call(
        UPDATE_DOMAIN,
        SERVICE_INSTALL,
        {ATTR_ENTITY_ID: "update.living_room_gateway_firmware_update"},
        blocking=True,
    )

    config_entry.runtime_data.async_execute_command.assert_awaited_once()
    state = hass.states.get("update.living_room_gateway_firmware_update")
    assert state is not None
    assert state.attributes[UpdateEntityStateAttribute.IN_PROGRESS] is True

    coordinator = config_entry.runtime_data
    coordinator.async_update_listeners()
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is True
    )

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        CLIMATE_SERVICE_SET_TEMPERATURE,
        {
            ATTR_ENTITY_ID: "climate.living_room_room_temperature",
            ClimateServiceArgument.TEMPERATURE: 25,
        },
        blocking=True,
    )
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is True
    )

    # A few fresh polls without status or version evidence may still be stale.
    gateways = _load_gateway_devices("dx4_firmwareavailable")
    coordinator._async_get_cloud_devices = AsyncMock(return_value=gateways)
    await _async_fire_coordinator_update(hass, coordinator, freezer)
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is True
    )
    await _async_fire_coordinator_update(hass, coordinator, freezer)
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is True
    )
    await _async_fire_coordinator_update(hass, coordinator, freezer)
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is False
    )

    gateway_point = gateways[0].management_point(gateways[0].gateway_embedded_id)
    gateway_point.firmware_update_status = Characteristic(value="in-progress")
    await _async_fire_coordinator_update(hass, coordinator, freezer)
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is True
    )

    gateway_point.firmware_update_status = None
    gateway_point.firmware_version = Characteristic(value="new-version")
    gateway_point.software_version = Characteristic(value="new-version")
    await _async_fire_coordinator_update(hass, coordinator, freezer)
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.IN_PROGRESS
        ]
        is False
    )
    assert (
        hass.states.get(state.entity_id).attributes[
            UpdateEntityStateAttribute.INSTALLED_VERSION
        ]
        == "new-version"
    )


async def test_firmware_install_records_pending_state_before_waiting_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A queued poll cannot replace firmware state before install bookkeeping."""
    await _async_setup_fixture(hass, config_entry, "dx4_firmwareavailable")
    coordinator = config_entry.runtime_data
    entity_id = "update.living_room_gateway_firmware_update"
    entity = hass.data[UPDATE_DOMAIN].get_entity(entity_id)
    assert isinstance(entity, DaikinFirmwareUpdateEntity)
    requested_sequence = coordinator.cloud_update_sequence
    installed_version = entity.installed_version
    gateways = _load_gateway_devices("dx4_firmwareavailable")
    point = gateways[0].management_point(gateways[0].gateway_embedded_id)
    assert point is not None
    point.firmware_version = Characteristic(value="new-version")
    point.software_version = Characteristic(value="new-version")
    point.firmware_update_status = None
    install_started = asyncio.Event()
    finish_install = asyncio.Event()
    poll_started = asyncio.Event()
    pending_at_poll: list[tuple[int, str | None, int] | None] = []
    original_update_data = coordinator._async_update_data

    async def install(
        _gateway_id: str, _management_point_id: str, _resource: str
    ) -> None:
        install_started.set()
        await finish_install.wait()

    async def read_gateways() -> list[GatewayDevice]:
        pending_at_poll.append(entity._pending_install)
        return gateways

    async def track_update_data() -> dict[str, DaikinOnectaDevice]:
        poll_started.set()
        return await original_update_data()

    with (
        patch.object(coordinator.client, "put_management_point", side_effect=install),
        patch.object(
            coordinator.client, "get_gateway_devices", side_effect=read_gateways
        ),
        patch.object(coordinator, "_async_update_data", side_effect=track_update_data),
        # Exercise the waiting poll without relying on the post-write cooldown.
        patch(
            "homeassistant.components.daikin_onecta.coordinator._POST_WRITE_COOLDOWN",
            timedelta(0),
        ),
    ):
        install_task = hass.async_create_task(
            hass.services.async_call(
                UPDATE_DOMAIN,
                SERVICE_INSTALL,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        )
        await install_started.wait()
        freezer.tick(coordinator.update_interval)
        async_fire_time_changed(hass)
        await poll_started.wait()
        finish_install.set()
        await install_task
        await hass.async_block_till_done()

    assert pending_at_poll == [
        (requested_sequence, installed_version, requested_sequence + 3)
    ]
    assert coordinator.cloud_update_sequence == requested_sequence + 1
    assert entity._pending_install is None
    state = hass.states.get(entity_id)
    assert state is not None
    assert (
        state.attributes[UpdateEntityStateAttribute.INSTALLED_VERSION] == "new-version"
    )
    assert state.attributes[UpdateEntityStateAttribute.IN_PROGRESS] is False


async def test_failed_firmware_install_clears_pending_state() -> None:
    """A rejected install does not leave optimistic progress behind."""
    entity = object.__new__(DaikinFirmwareUpdateEntity)
    entity._device = SimpleNamespace(id="gateway")
    entity._embedded_id = "gateway"
    entity._is_update_supported = True
    entity._firmware_id = "firmware-id"
    entity._attr_installed_version = "1.0"
    entity._attr_supported_features = UpdateEntityFeature.INSTALL
    entity._pending_install = None
    entity.coordinator = SimpleNamespace(cloud_update_sequence=2)
    entity._async_execute_command = AsyncMock(side_effect=HomeAssistantError)
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_install(None, False)

    assert entity._pending_install is None
    assert entity.supported_features is UpdateEntityFeature.INSTALL


@pytest.mark.parametrize("update_supported", [False, True])
async def test_firmware_install_rejects_unavailable_firmware(
    update_supported: bool,
) -> None:
    """Do not issue a cloud command without a usable firmware offer."""
    entity = object.__new__(DaikinFirmwareUpdateEntity)
    entity._device = MagicMock(name="Device")
    entity._is_update_supported = update_supported
    entity._firmware_id = None
    entity._async_execute_command = AsyncMock()

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_install(None, False)

    assert err.value.translation_key == "firmware_install_unavailable"
    entity._async_execute_command.assert_not_awaited()


def test_firmware_state_clears_when_firmware_disappears() -> None:
    """Clear stale update state when a gateway stops reporting firmware data."""
    entity = object.__new__(DaikinFirmwareUpdateEntity)
    entity._pending_install = (1, "1.0", 4)
    entity._attr_installed_version = "1.0"
    entity._attr_latest_version = "2.0"
    entity._attr_release_url = "https://example.invalid/release"
    entity._attr_release_summary = "Update"
    entity._firmware_id = "firmware-id"
    entity._attr_in_progress = True
    entity._is_update_supported = True
    entity._attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    entity._attr_extra_state_attributes = {"firmware_update_type": "major"}

    entity._update_from_management_point(SimpleNamespace(firmware=None))

    assert entity._pending_install is None
    assert entity.installed_version is None
    assert entity.latest_version is None
    assert entity.release_url is None
    assert entity.release_summary is None
    assert entity.in_progress is False
    assert not entity._is_update_supported
    assert entity.supported_features == UpdateEntityFeature(0)
    assert entity.extra_state_attributes == {}
