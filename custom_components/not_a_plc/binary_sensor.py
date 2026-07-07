"""Binary sensor platform: publishes coils and memory bits as HA entities.

Each coil/memory tag becomes a ``binary_sensor`` mirroring the logical truth of
that tag after every scan. Actuation of real devices (for coils with a ``writes``
binding) happens in the coordinator; this platform is the state mirror.

All bits are grouped under one "Not a PLC" service device, so entity_ids are
predictable (e.g. ``binary_sensor.not_a_plc_daylight``).
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import LadderCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one binary_sensor per coil and memory tag."""
    coordinator: LadderCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    program = coordinator.program

    tags = list(program.coil_tags()) + list(program.memory_tags())
    async_add_entities(
        LadderBitSensor(coordinator, entry.entry_id, tag) for tag in tags
    )


class LadderBitSensor(CoordinatorEntity[LadderCoordinator], BinarySensorEntity):
    """A single coil/memory bit exposed as a binary_sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LadderCoordinator, entry_id: str, tag: str) -> None:
        super().__init__(coordinator)
        self._tag = tag
        self._attr_name = tag
        self._attr_unique_id = f"{entry_id}_{tag}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Not a PLC",
            manufacturer="Not a PLC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get(self._tag, False))
