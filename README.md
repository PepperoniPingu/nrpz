# nrpz — native Linux driver for R&S NRP-Z power sensors

Talk to a Rohde & Schwarz **NRP-Z** USB power sensor from Linux with no VISA,
no vendor DLLs, and no VM — just `libusb`. 

- **Tested:** NRP-Z11 (`0aad:000c`).
- **Should work (untested):** other legacy NRP-Z diode sensors — same protocol.
- **Platform:** Linux (uses `libusb`; the sensor exposes no kernel driver).

## Contents

- [Install](#install)
- [Permissions (one-time)](#permissions-one-time)
- [Quick start](#quick-start)
- [Command-line reference](#command-line-reference)
- [Python API reference](#python-api-reference)
- [Recipes](#recipes)
- [Understanding the measurement](#understanding-the-measurement)
- [Error handling](#error-handling)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Tests](#tests)
- [License](#license)

## Install

```bash
pip install nrpz
```

Requires Python ≥ 3.8, [`pyusb`](https://github.com/pyusb/pyusb) (installed
automatically), and a system `libusb-1.0` (e.g. `sudo dnf install libusb1` /
`sudo apt install libusb-1.0-0`).

From source (for development): clone the repo and run `pip install -e .`.

## Permissions (one-time)

The sensor's USB node is root-only by default. Install the udev rule so your
user can open it:

```bash
sudo tee /etc/udev/rules.d/59-nrpz.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="0aad", MODE="0660", GROUP="dialout", TAG+="uaccess"
EOF
sudo udevadm control --reload
sudo udevadm trigger --attr-match=idVendor=0aad
```

Your user must be in the `dialout` group:

```bash
sudo usermod -aG dialout $USER   # then log out and back in
```

Verify the sensor is present and readable:

```bash
lsusb | grep 0aad                # -> Rohde & Schwarz ... NRP-Z11
nrpz                             # -> ROHDE&SCHWARZ,NRP-Z11,<serial>,04.16
```

## Quick start

```bash
nrpz                       # identity
nrpz power 1e9             # average power at 1 GHz
nrpz power 2.4e9 --zero    # zero first, then measure at 2.4 GHz
```

```python
from nrpz import NrpZ, dbm

with NrpZ() as s:
    s.zero()                                   # 50 Ω terminator on the input
    w = s.measure_power(freq_hz=2.4e9, count=16)
    print(f"{w:.3e} W = {dbm(w):.2f} dBm")
```

## Command-line reference

Invoke as `nrpz <command>` (console script), or `python -m nrpz <command>`.

| Command | Description |
|---|---|
| `nrpz` / `nrpz idn` | Print `*IDN?` — manufacturer, model, serial, firmware. |
| `nrpz power <freq_hz> [--zero]` | Average-power measurement at the given frequency (Hz). `--zero` runs a zero calibration first (input must be quiet). |
| `nrpz test` | Run the sensor self-test (memory, voltages, temperature, cal checksum). |
| `nrpz '<SCPI>'` | Raw SCPI passthrough. Queries (containing `?`) print the reply; plain commands are just sent. Quote to protect from the shell. |

Frequency is in **Hz** (`1e9` = 1 GHz, `100e6` = 100 MHz) and must be inside the
sensor's calibrated range or it is rejected.

Examples:

```console
$ nrpz idn
ROHDE&SCHWARZ,NRP-Z11,101699,04.16

$ nrpz power 1e9 --zero
1.834959e-06 W   (-27.36 dBm)  @ 1e+09 Hz

$ nrpz 'SYSTEM:INFO?'
Manufacturer:Rohde & Schwarz
Type:NRP-Z11
...
```

On error, the CLI prints `error: <message>` to stderr and exits non-zero.

## Python API reference

```python
from nrpz import NrpZ, NrpError, dbm
```

### `NrpZ(serial=None)`

Open a sensor. With no argument, the first NRP-Z found is used; pass a
`serial` string (e.g. `"101699"`) to select a specific one. Use as a context
manager (recommended) so the interface is claimed and released cleanly:

```python
with NrpZ() as s:
    ...
```

Or manually: `s = NrpZ().open()` … `s.close()`. Raises `NrpError` if no
matching sensor is found.

### Measurement

| Method | Returns / raises |
|---|---|
| `measure_power(freq_hz=1e9, count=4, timeout=8000, check_freq=True)` | Calibrated average power in **watts** (signed `float`). `freq_hz` = carrier frequency for the cal-factor. `count` = averaging factor (higher = lower noise, slower). `check_freq=False` skips the in-range guard. Raises `NrpError` on over-range/overload/questionable result or timeout. |
| `zero()` | Runs a zero calibration (~4 s, blocking). The RF input **must be quiet** (below ~-30 dBm). Raises `NrpError` if the sensor rejects it. |
| `selftest()` | Self-test report (`str`). |

### Info

| Method | Returns |
|---|---|
| `idn()` | `*IDN?` string. |
| `info()` | `SYSTEM:INFO?` parsed into a `dict` (cal dates, `MinPower`/`MaxPower`, `MinFreq`/`MaxFreq`, technology, …). |
| `freq_limits()` | `(min_hz, max_hz)` tuple of the calibrated frequency range (cached). |

### Raw SCPI

| Method | Description |
|---|---|
| `write(cmd)` | Send a command and consume its ack. Raises `NrpError` on a non-zero device status. |
| `ask(cmd, idle_ms=1500, max_ms=15000)` | Send a query and return the reply — a `str` for text queries, a `float` for numeric ones. `idle_ms` is the inactivity timeout (raise `max_ms` for very slow operations). |

### Module-level helpers

| Name | Description |
|---|---|
| `dbm(w)` | Watts → dBm (`10·log10(w/1e-3)`); returns `-inf` for non-positive `w`. |
| `decode_message(records)` | Pure parser: list of 16-byte records → `(status, text, floats)`. Useful for testing / protocol work. |
| `NrpError` | Raised for device errors and not-found conditions. |

## Recipes

**Single calibrated reading**

```python
with NrpZ() as s:
    w = s.measure_power(1e9)          # count=4 default (~0.16 s)
    print(dbm(w), "dBm")
```

**Low-noise reading (more averaging)**

```python
w = s.measure_power(1e9, count=64)    # integration = 2 × 20 ms × 64 ≈ 2.6 s
```

**Zero, then measure low levels**

```python
with NrpZ() as s:
    try:
        s.zero()                      # needs a 50 Ω terminator / quiet input
    except NrpError as e:
        print("zero skipped:", e)     # e.g. too much power at the input
    print(s.measure_power(1e9, count=16))
```

**Frequency sweep**

```python
with NrpZ() as s:
    for f in (100e6, 1e9, 2.4e9, 5e9):
        print(f/1e9, "GHz:", dbm(s.measure_power(f, count=16)), "dBm")
```

**Read calibration / limits**

```python
with NrpZ() as s:
    i = s.info()
    print(i["Type"], i["Cal. Abs."], i["MinPower"], "…", i["MaxPower"], "W")
    print("freq range:", s.freq_limits())
```

**Pick a specific sensor (multiple connected)**

```python
with NrpZ(serial="101699") as s:
    print(s.idn())
```

**Anything else via SCPI**

```python
with NrpZ() as s:
    print(s.ask("SENS:FREQ?"))            # -> float
    print(s.ask('SYSTEM:INFO? "SW BUILD"'))
    s.write("SENS:FREQ 2.4e9")            # plain command
```

## Understanding the measurement

**It measures total broadband power.** The NRP-Z11 is a diode *terminating*
sensor — it reports the sum of all RF power at its input across its whole
**10 MHz–8 GHz** range. It is **not** frequency-selective: `freq_hz` only
selects which stored calibration factor to apply, not a filter. For a clean
single-carrier reading, make sure only that signal is at the input.

**Units.** Results are in watts; use `dbm()` for dBm. Near the noise floor with
no signal the calibrated power can read slightly **negative** — that is normal
(the sensor is reading noise around zero), and `dbm()` returns `-inf` there.

**Averaging, integration time, and the two "counts".** There are three knobs:

| Knob | SCPI | Effect |
|---|---|---|
| Aperture | `SENS:POW:AVG:APER` | One sampling window (default 20 ms). |
| **Averaging count** (`count=`) | `SENS:AVER:COUN` | Windows averaged **into each result**. Noise ∝ 1/√count. Integration time = `2 × aperture × count`. |
| Trigger count | `TRIG:COUN` | Number of results produced **per `INIT`** (buffered acquisition). |

`measure_power(count=…)` sets the **averaging** count (noise vs speed). Trigger
count is left at 1 (one result per call); use `write("TRIG:COUN n")` +
raw SCPI if you need buffered block capture.

**Zeroing.** Removes the detector's zero offset; needed for accurate low-level
readings. Do it with the input terminated/quiet. A sensor reset (`*RST`) does
**not** clear a zero offset, but a power-cycle requires re-zeroing.

## Error handling

`measure_power()` inspects the result status and raises `NrpError` for:

| Code | Meaning |
|---|---|
| `0x02` | over-range — A/D limit reached, result may be wrong |
| `0x08` | **overload** — reduce RF input power immediately |
| `0x40` | result questionable (disrupted USB transfer) — re-measure |

`zero()` raises on `0x04` (CALZERO — too much power at the input to zero).
Wrap calls in `try/except NrpError` for robust scripts.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NRP-Z sensor not found` | `lsusb \| grep 0aad` — is it enumerated? Re-seat the cable. |
| `Access denied` / permission error | Install the udev rule (above); be in `dialout`; log out/in once. |
| `zero rejected (status 0x04)` | Input isn't quiet enough — terminate/disconnect the RF input and retry. |
| `freq … outside calibrated range` | Use a frequency within `freq_limits()` (10 MHz–8 GHz on the Z11). |
| `measurement timed out` | Usually auto-averaging with no signal; this driver disables it, so check the connection/power. |

## How it works

The sensor is a vendor-specific USB device with two 16-byte bulk endpoints and
speaks SCPI. Commands go out as text on EP `0x01`; responses come back as
framed 16-byte records on EP `0x82`:

| Record | Byte 0 | Meaning |
|---|---|---|
| `T` | `0x54` | text (offset in bytes 4–5, 10 chars in 6–15) |
| `L` | `0x4c` | parameter value (float32 LE at bytes 4–7) |
| `E` | `0x45` | measurement result (float32 LE; byte 1 = status) |
| `Z` | `0x5a` | trigger state (byte 2: 0 idle, 2 wait, 3 measuring) |
| `R` | `0x52` | end-of-message / ack (byte 1 = status code) |

Every command is answered by one message ending in an `R`; queries put their
data records before it. Measurement results are pushed as a separate message
after the `INIT` ack. See the module docstring in `src/nrpz/__init__.py` for
the complete wire format.

## Limitations

- Linux only (libusb-based).
- Only the **legacy text/scalar** path: `*IDN?`, config, zeroing, and average
  power. Trace / CCDF / statistics waveform block reads (the native binary
  typed-queue protocol) are **not** implemented.
- S-parameter and level-offset corrections are not wired into the high-level
  API (reachable via raw SCPI if needed).

## Tests

```bash
python tests/test_nrpz.py     # or: pytest
```

Hardware-free — they exercise the framing parser and unit conversion against
real captured device bytes.

## License

MIT — see `LICENSE`. (Placeholder default; change it before publishing if you
prefer something else.)
