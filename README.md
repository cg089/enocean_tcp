# enocean_tcp
EnOcean TCP Gateway for use in Home Assistant

I built an EnOcean to ESP8266 (or ESP32) bridge using:
https://github.com/Techserv-krY/EnOcean_ESP8266_Gateway/wiki

EnOcean Pi Module:
https://www.domadoo.fr/fr/dongle-enocean/2466-enocean-module-radio-enocean-pi-868mhz.html

Wiring:

  ESP - ENOCEAN
   TX - RX  10
   RX - TX   8
  Vcc - Vcc  1
  GND - GND  6

The Gateway relays all packages on port 9999.

Just copy enocean_typ folder to Home Assistant / config / custom_components, restart, Add Integration -> enocean_tcp


In Home Assistant, listen to enocean_tcp_frame and use a EnOcean sender:

event_type: enocean_tcp_frame
data:
packet_type: 1
data_hex: F6E08100EA2720
opt_hex: 00FFFFFFFF4F00
rorg: 246
sender_id: 8100EA27
status: 32
raw: PT=01 DATA=F6E08100EA2720 OPT=00FFFFFFFF4F00
origin: LOCAL
time_fired: "2025-09-22T23:36:09.663588+00:00"
context:
id: 01K5SWHS5ZHAP2M55AW6DG8K5G
parent_id: null
user_id: null


# Home Assistant Custom Integration: `enocean_tcp`

**Ziel:** EnOcean‑Stick über TCP (z. B. Port 9999) an Home Assistant anbinden. Der Stick liefert/erwartet *Raw ESP3*-Frames.\
**Funktionsprinzip:** Asynchroner TCP‑Client verbindet sich dauerhaft zum Stick, parst ESP3‑Frames und feuert für jedes gültige Paket ein HA‑Event `enocean_tcp_frame`. Über den Service `enocean_tcp.send_raw` können Hex‑Telegramme (ESP3) an den Stick gesendet werden.\
**Scope (v1):** Events + Raw‑Service. Keine Gerätemodelle/Plattformen – dafür stabiler Kern, den man via Automationen/Template‑Sensoren nutzen kann. (Plattformen lassen sich später ergänzen.)

---

## Verzeichnisstruktur

```
custom_components/enocean_tcp/
├─ __init__.py
├─ config_flow.py
├─ const.py
├─ hub.py
├─ manifest.json
├─ services.yaml
└─ translations/
   ├─ en.json
   └─ de.json
```

---

## `manifest.json`

```json
{
  "domain": "enocean_tcp",
  "name": "EnOcean TCP Bridge",
  "version": "0.1.0",
  "documentation": "https://example.local/enocean_tcp",
  "issue_tracker": "https://example.local/enocean_tcp/issues",
  "codeowners": ["@you"],
  "iot_class": "local_push",
  "integration_type": "hub",
  "requirements": [],
  "config_flow": true
}
```

---

## `const.py`

```python
DOMAIN = "enocean_tcp"
DEFAULT_PORT = 9999
CONF_HOST = "host"
CONF_PORT = "port"
CONF_RECONNECT = "reconnect_interval"
DEFAULT_RECONNECT = 5  # Sekunden
EVENT_FRAME = "enocean_tcp_frame"
SERVICE_SEND_RAW = "send_raw"
```

---

## `hub.py`

```python
from __future__ import annotations
import asyncio
import binascii
import logging
from typing import Optional, Tuple

from homeassistant.core import HomeAssistant, callback
from .const import EVENT_FRAME

_LOGGER = logging.getLogger(__name__)

# --- ESP3 Hilfsfunktionen ---

def _crc8(data: bytes) -> int:
    # Polynom 0x07, Initial 0x00 – Standard ESP3 CRC8
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc

class ESP3Packet:
    """Minimaler ESP3 Parser für ERP1 (Typ 0x01) und generische Pakete."""

    def __init__(self, packet_type: int, data: bytes, opt: bytes):
        self.packet_type = packet_type
        self.data = data
        self.opt = opt

    @property
    def rorg(self) -> Optional[int]:
        # Bei ERP1 ist erstes Datenbyte RORG (z.B. 0xF6, 0xD2, 0xA5, ...)
        if self.packet_type == 0x01 and len(self.data) > 0:
            return self.data[0]
        return None

    @property
    def sender_id(self) -> Optional[str]:
        # Bei ERP1 ist die Sender-ID typischerweise die letzten 4 Bytes vor Status
        if self.packet_type == 0x01 and len(self.data) >= 6:
            # data: RORG | payload.. | sender(4) | status(1)
            sid = self.data[-5:-1]
            return sid.hex().upper()
        return None

    @property
    def status(self) -> Optional[int]:
        if self.packet_type == 0x01 and len(self.data) >= 1:
            return self.data[-1]
        return None

    def as_dict(self) -> dict:
        d = {
            "packet_type": self.packet_type,
            "data_hex": self.data.hex().upper(),
            "opt_hex": self.opt.hex().upper(),
        }
        if self.rorg is not None:
            d["rorg"] = self.rorg
        if self.sender_id is not None:
            d["sender_id"] = self.sender_id
        if self.status is not None:
            d["status"] = self.status
        return d

class ESP3StreamParser:
    """Zustandsmaschine zum Parsen von ESP3-Frames aus einem Bytestrom."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        self._buf.extend(chunk)

    def packets(self):
        # Liefert 0..n komplette Packets; behält Rest im Buffer
        out = []
        while True:
            pkt = self._try_extract_packet()
            if not pkt:
                break
            out.append(pkt)
        return out

    def _try_extract_packet(self) -> Optional[ESP3Packet]:
        buf = self._buf
        # Suche nach Sync 0x55
        while True:
            idx = buf.find(b"\x55")
            if idx == -1:
                # Kein Sync im Buffer
                self._buf = bytearray()
                return None
            if idx > 0:
                del buf[:idx]
            if len(buf) < 6:
                return None
            # Header nach Sync: DL(2), OL(1), PT(1), CRC8H(1)
            dl = (buf[1] << 8) | buf[2]
            ol = buf[3]
            pt = buf[4]
            crch = buf[5]
            header = bytes([buf[1], buf[2], buf[3], buf[4]])
            if _crc8(header) != crch:
                # Header-CRC falsch – Sync verwerfen und neu suchen
                del buf[0]
                continue
            frame_len = 6 + dl + ol + 1  # Sync+Header(6) + Data + Opt + CRC8D
            if len(buf) < frame_len:
                return None
            data = bytes(buf[6:6+dl])
            opt = bytes(buf[6+dl:6+dl+ol])
            crcd = buf[6+dl+ol]
            if _crc8(data + opt) != crcd:
                # Daten-CRC falsch – Sync verwerfen und weiter
                del buf[0]
                continue
            # Gültig – entferne Frame aus Buffer
            del buf[:frame_len]
            return ESP3Packet(pt, data, opt)

class EnOceanTCPHub:
    def __init__(self, hass: HomeAssistant, host: str, port: int, reconnect_interval: int = 5):
        self.hass = hass
        self.host = host
        self.port = port
        self.reconnect_interval = reconnect_interval
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._parser = ESP3StreamParser()

    async def start(self):
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close()

    async def _connect(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        _LOGGER.info("enocean_tcp: Verbinde zu %s:%s", self.host, self.port)
        return await asyncio.open_connection(self.host, self.port)

    async def _close(self):
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa
                pass
        self._reader = None
        self._writer = None

    async def _run(self):
        while not self._stopped.is_set():
            try:
                self._reader, self._writer = await self._connect()
                _LOGGER.info("enocean_tcp: verbunden")
                await self._read_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa
                _LOGGER.warning("enocean_tcp: Verbindung verloren/Fehler: %s", e)
            await self._close()
            if self._stopped.is_set():
                break
            await asyncio.sleep(self.reconnect_interval)

    async def _read_loop(self):
        while not self._stopped.is_set():
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("EOF vom TCP‑Stick")
            self._parser.feed(chunk)
            for pkt in self._parser.packets():
                self._emit_event(pkt)

    @callback
    def _emit_event(self, pkt: ESP3Packet):
        data = pkt.as_dict()
        # Rohframe (ohne Sync/CRC) wieder zusammenbauen für Referenz
        raw_hex = f"PT={pkt.packet_type:02X} DATA={pkt.data.hex().upper()} OPT={pkt.opt.hex().upper()}"
        data["raw"] = raw_hex
        self.hass.bus.async_fire(EVENT_FRAME, data)

    async def send_raw_hex(self, hex_string: str):
        """Sendet einen kompletten ESP3‑Frame in Hex (mit oder ohne Sync/CRCs)."""
        payload = _normalize_hex(hex_string)
        if not payload:
            raise ValueError("Leere/ungültige Hex‑Payload")

        # Akzeptiere zwei Modi:
        # 1) Vollständiger Frame ab 0x55 … inkl. Header/Daten‑CRCs
        # 2) Nur (PT, DATA, OPT) – wir bauen Header+CRCs
        if payload[0] == 0x55:
            frame = bytes(payload)
        else:
            # Erwartet: PT | DATA… | 0x7C | OPT…  (Separator 0x7C '|' für Aufteilung)
            # Alternativ akzeptieren wir ein Tripel als Text über Service (siehe __init__.py)
            raise ValueError(
                "Sende bitte einen vollständigen ESP3‑Frame beginnend mit 55 … oder nutze den Service mit Feldern pt/data/opt."
            )

        if not self._writer:
            raise ConnectionError("Nicht verbunden – kann nicht senden")
        self._writer.write(frame)
        await self._writer.drain()

    async def send_triplet(self, pt: int, data_hex: str, opt_hex: str = ""):
        data = bytes.fromhex(data_hex.replace(" ", "")) if data_hex else b""
        opt = bytes.fromhex(opt_hex.replace(" ", "")) if opt_hex else b""
        header = bytes([(len(data) >> 8) & 0xFF, len(data) & 0xFF, len(opt) & 0xFF, pt & 0xFF])
        crch = _crc8(header)
        crcd = _crc8(data + opt)
        frame = b"\x55" + header + bytes([crch]) + data + opt + bytes([crcd])
        if not self._writer:
            raise ConnectionError("Nicht verbunden – kann nicht senden")
        self._writer.write(frame)
        await self._writer.drain()


def _normalize_hex(s: str) -> bytearray:
    s = s.strip().replace(" ", "").replace("-", "").replace(":", "")
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if len(s) % 2 != 0:
        raise ValueError("Ungerade Hex‑Länge")
    try:
        return bytearray(binascii.unhexlify(s))
    except binascii.Error as e:
        raise ValueError(f"Ungültige Hexdaten: {e}")
```

---

## `__init__.py`

```python
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
```

---

## `config_flow.py`

```python
from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_RECONNECT, DEFAULT_RECONNECT

class EnOceanTCPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"{host}:{port}", data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=9999): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_import(self, user_input=None) -> FlowResult:
        # Für YAML‑Import optional
        return await self.async_step_user(user_input)

class EnOceanTCPOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_RECONNECT, default=self.config_entry.options.get(CONF_RECONNECT, DEFAULT_RECONNECT)): int,
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    @staticmethod
    async def async_get_options_flow(config_entry):
        return EnOceanTCPOptionsFlowHandler(config_entry)
```

---

## `services.yaml`

```yaml
send_raw:
  name: Send raw ESP3
  description: "Sende ein EnOcean‑ESP3 Telegramm. Entweder komplettes RAW‑Hex (mit 55...) ODER Triplet (pt/data/opt)."
  fields:
    raw:
      name: RAW Frame (Hex)
      description: "Vollständiger ESP3‑Frame als Hex, beginnend mit 55. Beispiel: 55 00 07 07 01 A5 02 01 12 34 56 80 01 00"
      example: "55 00 07 07 01 A5 02 01 12 34 56 80 01 00"
      required: false
      selector:
        text:
    pt:
      name: Packet Type
      description: "ESP3 Packet Type als Zahl/Hex (z. B. 1 für ERP1)."
      required: false
      selector:
        number:
          min: 0
          max: 255
          mode: box
    data:
      name: DATA (Hex)
      description: "Reiner DATA‑Block als Hex (ohne Leerzeichen optional)."
      required: false
      selector:
        text:
    opt:
      name: OPT (Hex)
      description: "Optionaler OPT‑Block als Hex."
      required: false
      selector:
        text:
```

---

## `translations/de.json`

```json
{
  "title": "EnOcean TCP Bridge",
  "config": {
    "step": {
      "user": {
        "title": "EnOcean TCP konfigurieren",
        "description": "Verbinde dich mit einem EnOcean‑Stick, der ESP3 über TCP bereitstellt.",
        "data": {
          "host": "Host/IP",
          "port": "Port"
        }
      }
    },
    "options": {
      "step": {
        "init": {
          "data": {
            "reconnect_interval": "Reconnect‑Intervall (Sekunden)"
          }
        }
      }
    }
  }
}
```

---

## `translations/en.json`

```json
{
  "title": "EnOcean TCP Bridge",
  "config": {
    "step": {
      "user": {
        "title": "Configure EnOcean TCP",
        "description": "Connect to an EnOcean stick exposing ESP3 over TCP.",
        "data": {
          "host": "Host/IP",
          "port": "Port"
        }
      }
    },
    "options": {
      "step": {
        "init": {
          "data": {
            "reconnect_interval": "Reconnect interval (seconds)"
          }
        }
      }
    }
  }
}
```

---

## Installation

1. Ordner `custom_components/enocean_tcp` wie oben in dein Home‑Assistant‑Config‑Verzeichnis kopieren.
2. Home Assistant neu starten.
3. **Einstellungen → Integrationen → Integration hinzufügen → "EnOcean TCP Bridge"** auswählen.
4. Host/IP und Port (Standard **9999**) eintragen. Optional im Options‑Dialog das Reconnect‑Intervall anpassen.

> Der TCP‑Stick sollte als Server/Listener laufen (HA verbindet sich als Client). Falls dein Stick umgekehrt einen Client benötigt, gib mir Bescheid – ich ergänze einen TCP‑Server‑Modus.

---

## Nutzung (Events & Services)

### Event empfangen

Für jedes gültige ESP3‑Frame feuert die Integration:

- **Event:** `enocean_tcp_frame`
- **Eventdaten (Beispiele):**
  ```json
  {
    "packet_type": 1,
    "data_hex": "F6...AABBCCDD80",  
    "opt_hex": "",
    "rorg": 246,
    "sender_id": "AABBCCDD",
    "status": 128,
    "raw": "PT=01 DATA=F6... OPT="
  }
  ```

Damit kannst du Automationen/Blueprints bauen, z. B. per `event_data.sender_id == "AABBCCDD"`.

### Telegramm senden

1. Kompletter ESP3‑Frame (Hex, inklusive `55`… und CRCs):

```yaml
service: enocean_tcp.send_raw
data:
  raw: "55 00 07 07 01 A5 02 01 12 34 56 80 01 00"
```

2. Triplet (Integration baut Header & CRCs automatisch):

```yaml
service: enocean_tcp.send_raw
data:
  pt: 1             # ERP1
  data: "A5 02 01 12 34 56 80"   # DATA
  opt: "01 00"                   # OPT (optional)
```

---

## Beispiele: Template‑Sensoren

**Binary Sensor aus Taster (RORG F6, 0xF6):**

```yaml
template:
  - trigger:
      - platform: event
        event_type: enocean_tcp_frame
        event_data:
          rorg: 246   # 0xF6
          sender_id: "AABBCCDD"
    binary_sensor:
      - name: "EnOcean Taster AABBCCDD gedrückt"
        state: "{{ true }}"
        auto_off: 1
```

**Temperatur aus 4BS (A5‑10‑xx, RORG 0xA5) – stark vereinfacht:**

```yaml
template:
  - trigger:
      - platform: event
        event_type: enocean_tcp_frame
        event_data:
          rorg: 165   # 0xA5
          sender_id: "11223344"
    sensor:
      - name: "EnOcean Temp"
        unit_of_measurement: "°C"
        state: >-
          {% set d = trigger.event.data.data_hex %}
          {% set b = bytes.fromhex(d) %}
          {# Beispielprofil A5-02-05: Temp = (b[2])*40/255 #}
          {{ (b[2] * 40 / 255) | round(1) }}
```

> Hinweis: Für echte EEP‑Profile (A5‑xx‑xx etc.) braucht es spezifische Dekodierlogik. Diese Basis‑Integration liefert die Rohdaten; Dekoder/Plattformen können in einer späteren Version folgen.

---

## Troubleshooting

- **Keine Events?** Prüfe, ob der Stick erreichbar ist (`nc -v HOST 9999`). In den HA‑Logs sollte „verbunden“ erscheinen.
- **CRC‑Fehler im Log:** Meist Leitungs‑/Proxy‑Probleme oder der Stick sendet kein ESP3. Prüfe, ob wirklich ESP3 geroutet wird.
- **Senden schlägt fehl:** Der Stick muss bidirektional sein und das TCP‑Fenster für eingehende Daten akzeptieren.

---

## Roadmap (optional)

- Decoder für gängige EEPs (A5‑02‑xx, A5‑10‑xx, F6‑02‑xx …) und automatische Gerätemodelle.
- Option, die Events in einen eigenen Sensor‑/Binary‑Sensor‑Namespace zu gießen.
- TCP‑Server‑Modus (HA lauscht, Stick verbindet sich).
- Statistik/Diagnose‑Seite.

```
```
