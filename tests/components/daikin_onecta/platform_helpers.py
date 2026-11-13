"""Helpers shared by platform tests."""

from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.daikin_onecta import OnectaDataUpdateCoordinator
from homeassistant.core import HomeAssistant


async def _async_fire_coordinator_update(
    hass: HomeAssistant,
    coordinator: OnectaDataUpdateCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Advance time until the coordinator's next scheduled update."""
    freezer.tick(coordinator.update_interval)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _execute_typed_command(config_entry: MockConfigEntry) -> AsyncMock:
    """Execute a typed command callback without making a cloud request."""
    coordinator = config_entry.runtime_data
    coordinator.client.patch_characteristic = AsyncMock()
    coordinator.client.put_management_point = AsyncMock()
    coordinator.client.install_firmware = AsyncMock()

    async def execute(command) -> bool:
        await command(coordinator.client)
        return True

    return AsyncMock(side_effect=execute)
