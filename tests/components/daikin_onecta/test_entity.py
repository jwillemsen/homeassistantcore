"""Tests for the Daikin Onecta base entity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.daikin_onecta.const import DOMAIN
from homeassistant.components.daikin_onecta.entity import (
    DaikinEntity,
    DaikinManagementPointEntity,
    DaikinOnectaEntity,
    _add_management_point_metadata,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo


@pytest.mark.parametrize(
    ("mac_address", "embedded_id", "management_point", "expected"),
    [
        (None, None, None, {}),
        (
            "00:11:22:33:44:55",
            None,
            None,
            {"connections": {(CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")}},
        ),
        (
            None,
            "gateway",
            None,
            {},
        ),
        (
            "00:11:22:33:44:55",
            "gateway",
            SimpleNamespace(model="Model", serial="Serial", version="Version"),
            {
                "connections": {(CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")},
                "model": "Model",
                "serial_number": "Serial",
                "sw_version": "Version",
            },
        ),
        (
            None,
            "gateway",
            SimpleNamespace(model=None, serial=None, version=None),
            {},
        ),
    ],
)
def test_device_info(
    mac_address: str | None,
    embedded_id: str | None,
    management_point: SimpleNamespace | None,
    expected: dict[str, object],
) -> None:
    """Build device registry metadata only from available gateway data."""
    entity = object.__new__(DaikinOnectaEntity)
    device = MagicMock()
    device.id = "gateway-id"
    device.name = "Gateway name"
    get_management_point = MagicMock(return_value=management_point)
    device.device = SimpleNamespace(
        mac_address=mac_address,
        device_model="Gateway model",
        gateway_embedded_id=embedded_id,
        management_point=get_management_point,
    )
    device.management_point = get_management_point
    entity._device = device

    device_info = entity.device_info

    assert device_info["identifiers"] == {(DOMAIN, "gateway-id")}
    assert device_info["manufacturer"] == "Daikin"
    assert device_info["model_id"] == "Gateway model"
    assert device_info["name"] == "Gateway name"
    for key, value in expected.items():
        assert device_info[key] == value
    for key in {"model", "serial_number", "sw_version"} - expected.keys():
        assert key not in device_info
    if embedded_id is None:
        device.management_point.assert_not_called()
    else:
        device.management_point.assert_called_with(embedded_id)


def test_add_management_point_metadata_handles_missing_point() -> None:
    """Leave gateway metadata unchanged when the point disappeared."""
    info = DeviceInfo(identifiers={(DOMAIN, "gateway-id")})
    device = MagicMock()
    device.management_point.return_value = None

    _add_management_point_metadata(info, device, "point")

    assert info == DeviceInfo(identifiers={(DOMAIN, "gateway-id")})
    device.management_point.assert_called_once_with("point")


async def test_execute_command_raises_when_command_is_rejected() -> None:
    """Raise the translated HA error when the coordinator rejects a write."""
    entity = object.__new__(DaikinEntity)
    entity._device = SimpleNamespace(name="Gateway")
    entity.coordinator = SimpleNamespace(
        async_execute_command=AsyncMock(return_value=False)
    )

    with pytest.raises(HomeAssistantError) as err:
        await entity._async_execute_command(AsyncMock(), "command_failed")

    assert err.value.translation_key == "command_failed"


def test_management_point_device_info_falls_back_to_gateway() -> None:
    """Use gateway registry information when the management point has no type."""
    entity = object.__new__(DaikinManagementPointEntity)
    entity._management_point_type = None
    entity._device = SimpleNamespace(
        id="gateway-id",
        name="Gateway",
        device=SimpleNamespace(
            mac_address=None,
            device_model="Gateway model",
            gateway_embedded_id=None,
        ),
    )

    info = entity.device_info

    assert info["identifiers"] == {(DOMAIN, "gateway-id")}
    assert info["name"] == "Gateway"
