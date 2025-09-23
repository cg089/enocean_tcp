from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_FRAME

AUTO_OFF = 1.0  # Sekunden (nur für Taster)

# --- Fenstergriff-Mapping: DB0 (zweites Byte nach 0xF6)
WINDOW_CODES = {0xF0: "closed", 0xE0: "open", 0xD0: "tilt"}


class _BaseBS(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sender_id: str,
        name: str,
        model: str,
        device_class: BinarySensorDeviceClass | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._sender_id = sender_id
        self._attr_name = name
        self._attr_unique_id = (
            f"{entry.entry_id}_{name.lower().replace(' ', '_')}_{sender_id}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"device_{sender_id}")},
            name=f"EnOcean Device {sender_id}",
            manufacturer="EnOcean",
            model=model,
            via_device=(DOMAIN, entry.entry_id),
        )
        if device_class is not None:
            self._attr_device_class = device_class


class EnOceanTCPWindowHandleBS(_BaseBS):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sender_id: str) -> None:
        super().__init__(
            hass,
            entry,
            sender_id,
            f"Window {sender_id}",
            "F6-10 Window Handle",
            BinarySensorDeviceClass.WINDOW,
        )
        self._attr_is_on: bool | None = None  # on==open/tilt
        self._state_txt: str | None = None

    @property
    def extra_state_attributes(self) -> dict:
        return {"state": self._state_txt}

    @callback
    def handle_frame(self, d: dict) -> None:
        if d.get("sender_id") != self._sender_id:
            return
        data_hex: str = d.get("data_hex", "")
        if not data_hex or len(data_hex) < 4:  # mindestens F6 + DB0
            return
        b = bytes.fromhex(data_hex)
        if b[0] != 0xF6:
            return
        db0 = b[1]
        if db0 not in WINDOW_CODES:
            return
        state = WINDOW_CODES[db0]
        self._state_txt = state
        self._attr_is_on = state != "closed"  # open/tilt => ON
        self.async_write_ha_state()


class EnOceanTCPPressBS(_BaseBS):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, sender_id: str) -> None:
        super().__init__(
            hass,
            entry,
            sender_id,
            f"Button {sender_id}",
            "ERP1 (F6)",
            BinarySensorDeviceClass.OCCUPANCY,
        )
        self._attr_is_on = False
        self._presses = 0
        self._auto_off_handle = None

    @property
    def extra_state_attributes(self) -> dict:
        return {"presses": self._presses}

    def _schedule_auto_off(self) -> None:
        if self._auto_off_handle:
            self._auto_off_handle()

        def _off(_now) -> None:
            self._attr_is_on = False
            self.async_write_ha_state()

        self._auto_off_handle = async_call_later(self.hass, AUTO_OFF, _off)

    @callback
    def handle_frame(self, d: dict) -> None:
        if d.get("sender_id") != self._sender_id:
            return
        if d.get("rorg") not in (0xF6, 246):
            return
        self._presses += 1
        self._attr_is_on = True
        self.async_write_ha_state()
        self._schedule_auto_off()


class _DynamicPlatform:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.async_add_entities = async_add_entities
        self._entities: dict[str, _BaseBS] = {}
        self._unsub = None

    async def start(self) -> None:
        self._unsub = self.hass.bus.async_listen(EVENT_FRAME, self._handle_event)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_event(self, event) -> None:
        d = event.data
        sender = d.get("sender_id")
        if not sender or d.get("rorg") not in (0xF6, 246):
            return

        data_hex: str = d.get("data_hex", "")
        db0 = bytes.fromhex(data_hex)[1] if data_hex and len(data_hex) >= 4 else None

        ent = self._entities.get(sender)
        if not ent:
            # Fenstergriff oder Button anlegen
            if db0 in WINDOW_CODES:
                ent = EnOceanTCPWindowHandleBS(self.hass, self.entry, sender)
            else:
                ent = EnOceanTCPPressBS(self.hass, self.entry, sender)
            self._entities[sender] = ent
            self.async_add_entities([ent])

        # Event an Entity weiterreichen
        if hasattr(ent, "handle_frame"):
            ent.handle_frame(d)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    platform = _DynamicPlatform(hass, entry, async_add_entities)
    await platform.start()