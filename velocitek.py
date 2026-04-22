"""Constants, device enumeration, and connection protocol for Velocitek devices."""

import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pyftdi.ftdi import Ftdi
from pyftdi.usbtools import UsbTools

FTDI_VID = 0x0403

# (product_id, model_name) for every Velocitek device the original Mac app knew
# about. Product IDs come from VTDevice.m:54-59 in the Objective-C source.
PRODUCTS = {
    0xB709: "SpeedPuck",
    0xB70A: "ProStart",
    0x6001: "S10",
    0xB708: "SC1",
}

# Wire-level settings used by the original Mac app (VTConnection.m:693-700).
BAUDRATE = 115200

# Post-RTS settle delays from VTConnection.m:745,755 — the device is slow to
# notice RTS transitions and needs this breathing room or it'll miss bytes.
RTS_SET_DELAY_S = 0.050
RTS_CLEAR_DELAY_S = 0.500


class ProtocolError(Exception):
    """Raised when the device sends something unexpected back."""


# 7-byte PIC date format used on the wire (VTDateTime.m): year-2000, month,
# day, hour, minute, second, hundredths-of-second. Always UTC.
PIC_DATE_SIZE = 7


def parse_pic_date(data: bytes) -> datetime:
    y, mo, d, h, mi, s, hs = data[:PIC_DATE_SIZE]
    return datetime(
        2000 + y, mo, d, h, mi, s, hs * 10_000, tzinfo=timezone.utc
    )


def encode_pic_date(dt: datetime) -> bytes:
    """Encode a datetime to the 7-byte PIC format (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    hundredths = dt.microsecond // 10_000
    return bytes(
        [dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second, hundredths]
    )


def parse_pic_float(data: bytes) -> float:
    """Decode a 4-byte PIC float to Python float.

    The PIC stores the IEEE-754 single-precision components rearranged so
    that the 8-bit exponent occupies a full byte (the IEEE layout splits it
    across two). See VTFloat.m:138 for the reference implementation.
    """
    exponent = data[0]
    sign_bit = data[1] & 0x80
    ieee = bytes(
        [
            data[3],
            data[2],
            ((exponent & 0x01) << 7) | (data[1] & 0x7F),
            sign_bit | ((exponent & 0xFE) >> 1),
        ]
    )
    return struct.unpack("<f", ieee)[0]


@dataclass
class Trackpoint:
    """One GPS fix in a trackpoint log.

    Matches VTTrackpointRecord in VTRecord.m:22. Speed and heading units are
    whatever the device produces; the Mac app passes them through untouched.
    """
    timestamp: datetime
    latitude: float
    longitude: float
    speed: float
    heading: float

    WIRE_SIZE = PIC_DATE_SIZE + 4 * 4 + 9  # 32

    @classmethod
    def from_bytes(cls, body: bytes) -> "Trackpoint":
        assert len(body) == cls.WIRE_SIZE, f"expected {cls.WIRE_SIZE} bytes, got {len(body)}"
        return cls(
            timestamp=parse_pic_date(body[0:7]),
            latitude=parse_pic_float(body[7:11]),
            longitude=parse_pic_float(body[11:15]),
            speed=parse_pic_float(body[15:19]),
            heading=parse_pic_float(body[19:23]),
            # body[23:32] are 9 padding bytes
        )


@dataclass
class TrackLog:
    """One entry from the 'O' (list trackpoint logs) command.

    Matches VTTrackpointLogRecord in VTRecord.m:313.
    """
    log_index: int
    num_trackpoints: int
    start: datetime
    end: datetime

    WIRE_SIZE = 1 + 4 + PIC_DATE_SIZE + PIC_DATE_SIZE  # 19

    @classmethod
    def from_bytes(cls, body: bytes) -> "TrackLog":
        assert len(body) == cls.WIRE_SIZE, f"expected {cls.WIRE_SIZE} bytes, got {len(body)}"
        # The Mac app reads numTrackpoints by casting 4 bytes to native unsigned
        # int (VTConnection.m:476). Mac is little-endian, so the wire is too.
        log_index = body[0]
        num_trackpoints = struct.unpack_from("<I", body, 1)[0]
        start = parse_pic_date(body[5:12])
        end = parse_pic_date(body[12:19])
        return cls(log_index, num_trackpoints, start, end)


def register_custom_pids():
    """Teach pyftdi about Velocitek's non-standard FTDI product IDs.

    pyftdi ships with the stock FTDI PIDs baked in (0x6001 among them); the
    SpeedPuck, ProStart, and SC1 use custom PIDs that FTDI programmed into the
    chip's EEPROM, so we have to register those before UsbTools/Ftdi will
    touch them. add_custom_product raises on re-registration, so we skip
    anything pyftdi already knows.
    """
    known = set(Ftdi.PRODUCT_IDS.get(FTDI_VID, {}).values())
    for pid in PRODUCTS:
        if pid not in known:
            Ftdi.add_custom_product(FTDI_VID, pid)


def find_devices():
    """Return a list of (UsbDeviceDescriptor, interface_count) tuples for every
    attached Velocitek device."""
    register_custom_pids()
    vps = [(FTDI_VID, pid) for pid in PRODUCTS]
    return UsbTools.find_all(vps)


class Connection:
    """Open connection to a Velocitek device, implementing the handshake
    protocol from VTConnection.m.

    The protocol for every command is:
      1. host asserts RTS, waits 50ms
      2. host writes a 1-byte signal character
      3. device echoes the signal back
      4. host writes 'X' to confirm
      5. host writes any parameter bytes
      6. device echoes 'X' back
      7. device sends a 1-byte record header, then the record body
      8. host deasserts RTS, waits 500ms
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.model = PRODUCTS.get(descriptor.pid, "Unknown")
        self._ftdi = Ftdi()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        register_custom_pids()
        d = self.descriptor
        self._ftdi.open(vendor=d.vid, product=d.pid, serial=d.sn)
        self._ftdi.reset()
        self._ftdi.purge_buffers()
        self._ftdi.set_baudrate(BAUDRATE)
        self._ftdi.set_line_property(8, 1, "N")
        self._ftdi.set_flowctrl("")
        # Default latency is 16ms; 1ms makes the many small reads in the
        # echo-handshake feel snappy without otherwise changing behavior.
        self._ftdi.set_latency_timer(1)

    def close(self):
        if self._ftdi.is_connected:
            self._ftdi.close()

    def _write_all(self, data: bytes):
        n = self._ftdi.write_data(data)
        if n != len(data):
            raise IOError(f"short write: {n}/{len(data)} bytes")

    def _read_exact(self, n: int, timeout_s: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while len(buf) < n:
            chunk = self._ftdi.read_data_bytes(n - len(buf), attempt=1)
            if chunk:
                buf.extend(chunk)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"timed out reading {n} bytes (got {len(buf)}: "
                    f"{bytes(buf).hex()})"
                )
            time.sleep(0.002)
        return bytes(buf)

    def _begin_command(self, signal: int, parameter: bytes = b""):
        self._write_all(bytes([signal]))
        echo = self._read_exact(1)
        if echo[0] != signal:
            raise ProtocolError(
                f"signal echo: expected {signal:#04x}, got {echo[0]:#04x}"
            )
        self._write_all(b"X" + parameter)
        echo = self._read_exact(1)
        if echo[0] != ord("X"):
            raise ProtocolError(
                f"confirm echo: expected 0x58 ('X'), got {echo[0]:#04x}"
            )

    def run_command(
        self,
        signal: int,
        parameter: bytes = b"",
        expected_header: int | None = None,
        reply_body_size: int = 0,
    ) -> bytes:
        """Run a simple fixed-size command. Returns just the record body
        (everything after the 1-byte record header)."""
        self._ftdi.set_rts(True)
        time.sleep(RTS_SET_DELAY_S)
        try:
            self._begin_command(signal, parameter)

            if expected_header is not None:
                header = self._read_exact(1)
                if header[0] != expected_header:
                    raise ProtocolError(
                        f"record header: expected {expected_header:#04x}, "
                        f"got {header[0]:#04x}"
                    )

            return self._read_exact(reply_body_size) if reply_body_size else b""
        finally:
            self._ftdi.set_rts(False)
            time.sleep(RTS_CLEAR_DELAY_S)

    def run_list_command(
        self,
        signal: int,
        record_body_size: int,
        parameter: bytes = b"",
        expected_header: int | None = None,
        record_timeout_s: float = 0.5,
        on_record=None,
    ) -> list[bytes]:
        """Run a command that returns a stream of fixed-size records. Ends
        when no new byte arrives within record_timeout_s (matches
        VTConnection.m:399 waitForResponseLength:1 timeout:500).

        If expected_header is set, each record is preceded by that byte on
        the wire and it's validated + stripped. If None (as with 'T' track
        downloads), the first byte is part of the record body.

        on_record(record_bytes, count_so_far) is called after each record is
        received — useful for progress reporting on long downloads.
        """
        self._ftdi.set_rts(True)
        time.sleep(RTS_SET_DELAY_S)
        try:
            self._begin_command(signal, parameter)

            records: list[bytes] = []
            while True:
                try:
                    first = self._read_exact(1, timeout_s=record_timeout_s)
                except TimeoutError:
                    break
                if expected_header is not None:
                    if first[0] != expected_header:
                        raise ProtocolError(
                            f"record header: expected {expected_header:#04x}, "
                            f"got {first[0]:#04x}"
                        )
                    body = self._read_exact(record_body_size)
                else:
                    body = first + self._read_exact(record_body_size - 1)
                records.append(body)
                if on_record is not None:
                    on_record(body, len(records))
            return records
        finally:
            self._ftdi.set_rts(False)
            time.sleep(RTS_CLEAR_DELAY_S)

    def firmware_version(self) -> str:
        """Read the firmware version. Matches the 'V' command in the Mac app
        (VTRecord.m:288 VTFirmwareVersionRecord): 1 byte major, 1 byte minor,
        2 bytes ignored."""
        body = self.run_command(
            signal=ord("V"),
            expected_header=ord("v"),
            reply_body_size=4,
        )
        return f"{body[0]}.{body[1]}"

    def list_trackpoint_logs(self) -> list[TrackLog]:
        """List the trackpoint logs stored on the device ('O' command)."""
        bodies = self.run_list_command(
            signal=ord("O"),
            expected_header=ord("l"),
            record_body_size=TrackLog.WIRE_SIZE,
        )
        return [TrackLog.from_bytes(b) for b in bodies]

    def download_trackpoints(
        self,
        start: datetime,
        end: datetime,
        on_progress=None,
    ) -> list[Trackpoint]:
        """Download trackpoints between start and end (inclusive) via the
        'T' command. on_progress(count) is called periodically."""
        parameter = encode_pic_date(start) + encode_pic_date(end)
        cb = None
        if on_progress is not None:
            def cb(_body, count):
                on_progress(count)
        bodies = self.run_list_command(
            signal=ord("T"),
            record_body_size=Trackpoint.WIRE_SIZE,
            parameter=parameter,
            on_record=cb,
        )
        return [Trackpoint.from_bytes(b) for b in bodies]
