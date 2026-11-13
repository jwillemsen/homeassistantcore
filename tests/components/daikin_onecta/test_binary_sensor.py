"""Tests for Daikin binary sensors."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from daikin_onecta.models import Characteristic

from homeassistant.components.daikin_onecta.binary_sensor import (
    async_setup_entry as async_setup_binary_sensors,
)


async def test_unknown_characteristic_is_skipped() -> None:
    """Do not create untranslated binary sensors for unknown values."""
    management_point = SimpleNamespace(
        embedded_id="point",
        scalar_characteristics=lambda: {
            "futureCharacteristic": Characteristic(value=True)
        },
    )
    device = SimpleNamespace(
        device=SimpleNamespace(management_points=[management_point])
    )
    config_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(data={"gateway": device})
    )
    async_add_entities = MagicMock()

    await async_setup_binary_sensors(MagicMock(), config_entry, async_add_entities)

    async_add_entities.assert_called_once_with([])
