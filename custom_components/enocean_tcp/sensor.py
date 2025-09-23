from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_FRAME

_LOGGER = logging.getLogger(__name__)


class EnOceanTCPFramesReceived(SensorEntity):
    _attr_should_poll = False
    _attr_name = "EnOcean Frames Received"
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "frames"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_frames_received"
        self._count = 0
        self._unsub = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EnOcean TCP Bridge",
        )

    async def async_added_to_hass(self) -> None:
        self._unsub = self.hass.bus.async_listen(EVENT_FRAME, self._on_frame)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def native_value(self):
        return self._count

    @callback
    def _on_frame(self, event) -> None:
        self._count += 1
        self.async_write_ha_state()


class EnOceanTCPLastFrame(SensorEntity):
    _attr_should_poll = False
    _attr_name = "EnOcean Last Frame"
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_frame"
        self._ts: datetime | None = None
        self._last: dict | None = None
        self._unsub = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EnOcean TCP Bridge",
        )

    async def async_added_to_hass(self) -> None:
        self._unsub = self.hass.bus.async_listen(EVENT_FRAME, self._on_frame)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def native_value(self):
        return self._ts.isoformat() if self._ts else None

    @property
    def extra_state_attributes(self):
        return self._last or {}

    @callback
    def _on_frame(self, event) -> None:
        d = event.data
        self._ts = datetime.now(timezone.utc)
        self._last = {
            "packet_type": d.get("packet_type"),
            "rorg": d.get("rorg"),
            "sender_id": d.get("sender_id"),
            "status": d.get("status"),
            "data_hex": d.get("data_hex"),
            "opt_hex": d.get("opt_hex"),
            "raw": d.get("raw"),
        }
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [EnOceanTCPFramesReceived(hass, entry), EnOceanTCPLastFrame(hass, entry)]
    )