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