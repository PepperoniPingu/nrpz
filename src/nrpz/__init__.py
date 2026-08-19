"""Native Linux driver for Rohde & Schwarz NRP-Z USB power sensors.

No VISA, no vendor DLLs -- talks the sensor's USB protocol directly via libusb.
The protocol was reverse-engineered from R&S nrp.dll and verified against an
NRP-Z11 (readings checked against a reference source).

The sensor is a vendor-specific USB device (0aad:000c) with two 16-byte bulk
endpoints and speaks SCPI. Old sensors like the NRP-Z11 are "legacy-native":
just claim the interface and talk -- no vendor control transfer (newer sensors
booting in binary mode would need VRT_SET_LEGACY_OPEN, req 0x09, which the
NRP-Z11 STALLs).

Wire format
-----------
Write : SCPI text + '\\n' to EP 0x01 (bulk OUT).
Read  : stream of 16-byte records from EP 0x82 (bulk IN). byte[0] = record type:
          'T' 0x54  text     : byte[4:6]=uint16 LE offset, byte[6:16]=10 chars
          'L' 0x4c  parameter: float32 LE at byte[4:8]      (e.g. FREQ readback)
          'E' 0x45  result   : float32 LE at byte[4:8]; byte[1]=result status
          'Z' 0x5a  state    : byte[2]=trigger state (0 idle,2 wait,3 measuring)
          'R' 0x52  end/ack  : byte[1]=status/error code (0=OK, e.g. 0x89=unknown)
        The sensor answers EVERY command with one message ending in an 'R'; a
        query puts its data records before that 'R'. Read one full message per
        command written or the TX FIFO desyncs. Measurement results arrive as a
        separate pushed message ('Z' MEASURING -> 'E' result -> 'Z' IDLE) after
        the INIT command's own 'R' ack.

Scope: legacy text/scalar mode -- *IDN?, config, zeroing, average power. Trace/
CCDF waveform block reads (the native binary typed-queue protocol) are not
decoded.
"""
import sys
import time
import struct
import math

import usb.core
import usb.util

__version__ = "0.1.0"
__all__ = ["NrpZ", "NrpError", "decode_message", "dbm", "main",
           "VID", "PID", "DEV_ERR"]

VID, PID = 0x0AAD, 0x000C
EP_OUT, EP_IN = 0x01, 0x82
R_TEXT, R_PARAM, R_RESULT, R_STATE, R_END = 0x54, 0x4C, 0x45, 0x5A, 0x52

# Device status codes carried in a record's status byte (see R&S nrpdef.h).
DEV_ERR = {0x02: "over-range: A/D limit reached, result may be wrong",
           0x08: "OVERLOAD: reduce RF input power immediately",
           0x40: "result questionable (disrupted USB transfer); re-measure"}


class NrpError(Exception):
    pass


def decode_message(records):
    """Parse a list of 16-byte response records into (status, text, floats).

    Pure function (no I/O) so the framing logic is unit-testable without
    hardware. 'T' records are reassembled by offset into text; 'L'/'E' records
    yield float32 values; the terminating 'R' record supplies the status byte.
    """
    parts, floats, status = {}, [], 0
    for rec in records:
        if len(rec) < 6:
            continue
        t = rec[0]
        if t == R_END:
            status = rec[1]
            break
        elif t == R_TEXT:
            parts[rec[4] | (rec[5] << 8)] = rec[6:16]
        elif t in (R_PARAM, R_RESULT):
            floats.append(struct.unpack("<f", rec[4:8])[0])
        # 'Z' state / 'z' keepalive / 'M' misc: ignored
    out = bytearray()
    for off in sorted(parts):
        if len(out) < off:
            out.extend(b"\x00" * (off - len(out)))
        out[off:off + 10] = parts[off]
    return status, out.split(b"\x00", 1)[0].decode("latin1").strip(), floats


def dbm(w):
    """Convert power in watts to dBm. Returns -inf for non-positive power."""
    return 10 * math.log10(w / 1e-3) if w and w > 0 else float("-inf")


class NrpZ:
    def __init__(self, serial=None):
        match = (lambda d: usb.util.get_string(d, d.iSerialNumber) == serial) if serial else None
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, custom_match=match)
        if self.dev is None:
            raise NrpError("NRP-Z sensor not found (0aad:000c). Plugged in? In 'dialout' group?")

    # ---- lifecycle -----------------------------------------------------
    def open(self):
        d = self.dev
        try:
            if d.is_kernel_driver_active(0):
                d.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        # NB: do NOT set_configuration() -- SET_CONFIGURATION resets the sensor's
        # SCPI session and the first responses come back empty.
        usb.util.claim_interface(d, 0)
        self._drain()
        return self

    def close(self):
        usb.util.dispose_resources(self.dev)

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    # ---- raw record I/O ------------------------------------------------
    def _drain(self):
        while True:
            try:
                self.dev.read(EP_IN, 16, timeout=60)
            except usb.core.USBError:
                return

    def _rec(self, timeout_ms):
        return bytes(self.dev.read(EP_IN, 16, timeout=timeout_ms))

    def _read_records(self, idle_ms, max_ms):
        """Read raw 16-byte records up to and including the 'R' terminator.
        Inactivity timeout: keeps waiting while records arrive (including 'z'
        keepalives during slow ops), stops on 'R' or `idle_ms` of silence."""
        start = time.monotonic()
        recs = []
        while (time.monotonic() - start) * 1000 < max_ms:
            try:
                rec = self._rec(idle_ms)
            except usb.core.USBError as e:
                if "timed out" in str(e).lower():
                    break
                raise
            recs.append(rec)
            if len(rec) >= 1 and rec[0] == R_END:
                break
        return recs

    def _read_message(self, idle_ms=1500, max_ms=15000):
        """Read one response message and decode it -> (status, text, floats)."""
        return decode_message(self._read_records(idle_ms, max_ms))

    # ---- command layer -------------------------------------------------
    def write(self, cmd):
        """Send a command and consume its ack. Raises on device error status."""
        self.dev.write(EP_OUT, (cmd.rstrip("\n") + "\n").encode(), timeout=2000)
        status, _, _ = self._read_message(idle_ms=2000)
        if status:
            raise NrpError(f"{cmd!r} -> device status 0x{status:02x}")

    def ask(self, cmd, idle_ms=1500, max_ms=15000):
        """Query returning text (str) or, for numeric queries, a float."""
        self.dev.write(EP_OUT, (cmd.rstrip("\n") + "\n").encode(), timeout=2000)
        _, text, floats = self._read_message(idle_ms=idle_ms, max_ms=max_ms)
        if floats and not text:
            return floats[0]
        return text

    # ---- high level ----------------------------------------------------
    def idn(self):
        return self.ask("*IDN?")

    def info(self):
        """Parse SYSTEM:INFO? into a dict (cal dates, power/freq range, etc.)."""
        d = {}
        for line in self.ask("SYSTEM:INFO?").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.strip()] = v.strip()
        return d

    _flim = None

    def freq_limits(self):
        """(min_hz, max_hz) of the sensor's calibrated frequency range."""
        if self._flim is None:
            i = self.info()
            self._flim = (float(i["MinFreq"]), float(i["MaxFreq"]))
        return self._flim

    def selftest(self):
        return self.ask("TEST:SENS?")

    def zero(self):
        """Zero the sensor. The RF input must be quiet (below the auto-zero
        threshold, ~ -30 dBm). Blocks until the ~4s calibration completes and
        raises if the sensor rejects it (status 0x04 = CALZERO, i.e. too much
        power at the input to zero)."""
        self.dev.write(EP_OUT, b"CALibration:ZERO:AUTO ONCE\n", timeout=2000)
        status, _, _ = self._read_message(idle_ms=2500, max_ms=20000)
        self._drain()
        if status:
            raise NrpError(f"zero rejected (status 0x{status:02x}); disconnect or "
                           "terminate the RF input (needs < ~-30 dBm) and retry")

    def measure_power(self, freq_hz=1e9, count=4, timeout=8000, check_freq=True):
        """One-shot average-power measurement. Returns calibrated power in watts
        (signed float32 -- near the noise floor with no signal it can read
        slightly negative, which is normal). `freq_hz` sets the calibration
        frequency: the sensor applies its stored cal-factor at this frequency,
        so pass the actual carrier frequency for a calibrated reading. `count`
        is the averaging factor; integration time = 2 * aperture * count."""
        if check_freq:
            lo, hi = self.freq_limits()
            if not (lo <= freq_hz <= hi):
                raise NrpError(f"freq {freq_hz:g} Hz outside calibrated range "
                               f"{lo:g}-{hi:g} Hz; reading would be uncalibrated")
        for c in ("*RST",
                  'SENSe:FUNCtion "POWer:AVG"',
                  f"SENSe:FREQuency {freq_hz:g}",
                  "SENSe:AVERage:COUNt:AUTO OFF",
                  f"SENSe:AVERage:COUNt {count}",
                  "SENSe:AVERage:TCONtrol REPeat",   # single-shot: clear+refill the
                  "SENSe:AVERage:STATe ON",          # filter, so integration = count windows
                  "INITiate:CONTinuous OFF",
                  "TRIGger:SOURce IMMediate"):
            self.write(c)
        # INIT: ack is 'Z'+'R'; the result is pushed afterwards as an 'E' record.
        self.dev.write(EP_OUT, b"INITiate:IMMediate\n", timeout=2000)
        deadline = time.monotonic() + timeout / 1000.0
        while time.monotonic() < deadline:
            try:
                rec = self._rec(1000)
            except usb.core.USBError:
                continue
            if len(rec) < 8:
                continue
            if rec[0] == R_RESULT:
                st = rec[1]                       # result status: over-range/overload/etc.
                w = struct.unpack("<f", rec[4:8])[0]
                self._drain()
                if st:
                    raise NrpError(DEV_ERR.get(st, f"result status 0x{st:02x}"))
                return w
            if rec[0] == R_END and rec[1]:
                raise NrpError(f"measurement error 0x{rec[1]:02x}")
        raise NrpError("measurement timed out (no result pushed)")


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        with NrpZ() as s:
            if not args or args[0] in ("id", "idn"):
                print(s.idn())
            elif args[0] in ("test", "selftest"):
                print(s.selftest())
            elif args[0] in ("power", "meas", "measure"):
                nums = [a for a in args[1:] if not a.startswith("--")]
                freq = float(nums[0]) if nums else 1e9
                if "--zero" in args:
                    try:
                        s.zero()
                    except NrpError as e:
                        print(f"warning: {e}", file=sys.stderr)
                w = s.measure_power(freq)
                print(f"{w:.6e} W   ({dbm(w):.2f} dBm)  @ {freq:g} Hz")
            else:                                    # raw SCPI passthrough
                for c in args:
                    print(f"{c} -> {s.ask(c)!r}" if "?" in c else f"{c} (sent)")
                    if "?" not in c:
                        s.write(c)
    except NrpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
