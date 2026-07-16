"""Sensor platform: publishes REAL coil and memory tags as HA entities.

Each REAL coil/memory tag (a move/calc destination) becomes a ``sensor`` showing
its numeric value after every scan. BOOL coil/memory tags are binary sensors (see
``binary_sensor.py``); REAL ``temp`` tags stay internal (status view only).

All values are grouped under the same "Not-a-PLC" service device as the binary
sensors, so entity_ids are predictable (e.g. ``sensor.not_a_plc_level``).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import LadderCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per REAL coil and memory tag."""
    coordinator: LadderCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    program = coordinator.program

    outputs = {**program.coil_tags(), **program.memory_tags()}
    tags = [name for name, tag in outputs.items() if tag.type == "REAL"]
    async_add_entities(
        LadderRealSensor(coordinator, entry.entry_id, entry.title, tag) for tag in tags
    )


class LadderRealSensor(CoordinatorEntity[LadderCoordinator], SensorEntity):
    """A single REAL coil/memory value exposed as a sensor."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LadderCoordinator,
        entry_id: str,
        service_name: str,
        tag: str,
    ) -> None:
        super().__init__(coordinator)
        self._tag = tag
        self._attr_name = tag
        self._attr_unique_id = f"{entry_id}_{tag}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=service_name,
            manufacturer="Not-a-PLC",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float:
        data = self.coordinator.data or {}
        return float(data.get(self._tag, 0.0))
