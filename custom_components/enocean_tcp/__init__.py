from __future__ import annotations
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_RECONNECT, DEFAULT_RECONNECT, SERVICE_SEND_RAW
from .hub import EnOceanTCPHub

PLATFORMS: list = []  # v1: Nur Events+Services

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    reconnect = entry.options.get(CONF_RECONNECT, DEFAULT_RECONNECT)

    hub = EnOceanTCPHub(hass, host, port, reconnect)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    try:
        await hub.start()
    except Exception as e:  # noqa
        raise ConfigEntryNotReady(str(e))

    # Service: RAW (vollständiger ESP3‑Frame) ODER Triplet (pt/data/opt)
    async def _send_raw(call: ServiceCall):
        raw_hex = call.data.get("raw")
        pt = call.data.get("pt")
        data_hex = call.data.get("data")
        opt_hex = call.data.get("opt", "")
        if raw_hex:
            await hub.send_raw_hex(raw_hex)
        elif pt is not None and data_hex is not None:
            await hub.send_triplet(int(pt), str(data_hex), str(opt_hex))
        else:
            raise vol.Invalid("Entweder 'raw' ODER ('pt' und 'data') angeben")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW,
        _send_raw,
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub: EnOceanTCPHub = hass.data[DOMAIN][entry.entry_id]
    await hub.stop()
    hass.services.async_remove(DOMAIN, SERVICE_SEND_RAW)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)