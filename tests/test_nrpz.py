"""Hardware-free tests for the framing parser and helpers.

The record vectors below are real bytes captured from an NRP-Z11 answering
*IDN? and SENS:FREQ?, so this is a genuine regression test of the wire decode.
Run with `pytest`, or directly: `python tests/test_nrpz.py`.
"""
from nrpz import decode_message, dbm

# Real *IDN? response: four 'T' text records (offset in bytes[4:6]) + 'R' end.
IDN_RECORDS = [
    bytes.fromhex("54000b030000524f4844452653434857"),  # off 0  "ROHDE&SCHW"
    bytes.fromhex("54000b030a0041525a2c4e52502d5a31"),  # off 10 "ARZ,NRP-Z1"
    bytes.fromhex("54000b031400312c3130313639392c30"),  # off 20 "1,101699,0"
    bytes.fromhex("54000b031e00342e3136000000000000"),  # off 30 "4.16"
    bytes.fromhex("52000b03000000000000000000000000"),  # 'R' end, status 0
]

# Real SENS:FREQ? response: one 'L' float record (1e9) + 'R' end.
FREQ_RECORDS = [
    bytes.fromhex("4c000309286b6e4e0000000000000000"),  # float32 LE 0x4e6e6b28 = 1e9
    bytes.fromhex("52000309000000000000000000000000"),
]


def test_text_reassembly():
    status, text, floats = decode_message(IDN_RECORDS)
    assert status == 0
    assert text == "ROHDE&SCHWARZ,NRP-Z11,101699,04.16"
    assert floats == []


def test_numeric_query():
    status, text, floats = decode_message(FREQ_RECORDS)
    assert status == 0
    assert text == ""
    assert len(floats) == 1
    assert abs(floats[0] - 1e9) < 1.0        # exact float32 for 1e9


def test_error_status_byte():
    # 'R' with a non-zero status (0x89 = unknown command) must surface.
    recs = [bytes.fromhex("52890000000000000000000000000000")]
    status, _, _ = decode_message(recs)
    assert status == 0x89


def test_dbm():
    assert abs(dbm(1e-3) - 0.0) < 1e-9       # 1 mW == 0 dBm
    assert abs(dbm(1.0) - 30.0) < 1e-9       # 1 W  == +30 dBm
    assert dbm(0.0) == float("-inf")
    assert dbm(-1e-9) == float("-inf")       # negative (below zero) -> -inf


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
