#!/usr/bin/env python3
"""Interactive CLI for talking to a Velocitek device.

On Linux, non-root USB access requires the udev rules in 99-velocitek.rules:
    sudo cp 99-velocitek.rules /etc/udev/rules.d/
    sudo udevadm control --reload && sudo udevadm trigger
Then unplug/replug the device.
"""

import os
import sys
import time

import gpx
from velocitek import Connection, PRODUCTS, ProtocolError, find_devices


def prompt(msg: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            answer = input(f"{msg}{suffix}: ").strip()
        except EOFError:
            print()
            sys.exit(0)
        if answer:
            return answer
        if default is not None:
            return default


def choose_device():
    devices = find_devices()
    if not devices:
        print("No Velocitek devices found.")
        print("(Looked for FTDI VID 0x0403 with PIDs: "
              + ", ".join(f"0x{pid:04x} ({name})" for pid, name in PRODUCTS.items())
              + ")")
        return None

    print(f"Found {len(devices)} Velocitek device(s):")
    for i, (desc, _) in enumerate(devices, start=1):
        model = PRODUCTS.get(desc.pid, "Unknown")
        print(f"  {i}. {model:10s}  serial={desc.sn}  bus:addr={desc.bus}:{desc.address}")

    while True:
        answer = prompt("Choose device", default="1" if len(devices) == 1 else None)
        if answer.lower() in ("q", "quit", "exit"):
            return None
        try:
            idx = int(answer)
        except ValueError:
            print("  (enter a number, or 'q' to quit)")
            continue
        if 1 <= idx <= len(devices):
            return devices[idx - 1][0]
        print(f"  (choose 1..{len(devices)})")


def cmd_firmware_version(conn: Connection):
    print(f"  Firmware version: {conn.firmware_version()}")


def cmd_list_logs(conn):
    logs = conn.list_trackpoint_logs()
    if not logs:
        print("  (no logs on device)")
        return
    print(f"  {len(logs)} log(s):")
    print(f"    {'idx':>3}  {'points':>8}  {'start (UTC)':<19}  {'end (UTC)':<19}")
    for log in sorted(logs, key=lambda l: l.start, reverse=True):
        print(
            f"    {log.log_index:>3}  {log.num_trackpoints:>8}  "
            f"{log.start:%Y-%m-%d %H:%M:%S}  {log.end:%Y-%m-%d %H:%M:%S}"
        )


def _gpx_paths(log):
    """Default output filename and GPX track name for a log."""
    return (
        f"track-{log.start:%Y%m%d-%H%M%S}.gpx",
        f"Velocitek {log.start:%Y-%m-%d %H:%M} UTC",
    )


def export_log(conn: Connection, log, path, on_progress=None):
    """Download `log` and write it to `path` as GPX. Returns (name, points, written)."""
    _, name = _gpx_paths(log)
    points = conn.download_trackpoints(log.start, log.end, on_progress=on_progress)
    written = gpx.write_gpx(path, points, name=name)
    return name, points, written


def _make_progress(expected):
    """Progress callback that prints `count/expected (pct%)` over \\r at most every 0.5s."""
    last_print = [0.0]

    def on_progress(count: int):
        now = time.monotonic()
        if now - last_print[0] >= 0.5 or count == expected:
            pct = (100.0 * count / expected) if expected else 0.0
            print(f"\r    {count}/{expected} ({pct:5.1f}%)", end="", flush=True)
            last_print[0] = now

    return on_progress


def cmd_export_gpx(conn: Connection):
    logs = conn.list_trackpoint_logs()
    if not logs:
        print("  (no logs on device)")
        return

    logs_sorted = sorted(logs, key=lambda l: l.start, reverse=True)
    print(f"  {'#':>3}  {'points':>8}  {'start (UTC)':<19}  {'end (UTC)':<19}")
    for i, log in enumerate(logs_sorted, start=1):
        print(
            f"  {i:>3}  {log.num_trackpoints:>8}  "
            f"{log.start:%Y-%m-%d %H:%M:%S}  {log.end:%Y-%m-%d %H:%M:%S}"
        )

    answer = prompt("Choose log to export")
    if answer.lower() in ("q", "quit", "exit", ""):
        return
    try:
        idx = int(answer)
    except ValueError:
        print("  (not a number)")
        return
    if not (1 <= idx <= len(logs_sorted)):
        print(f"  (choose 1..{len(logs_sorted)})")
        return
    log = logs_sorted[idx - 1]

    default_name, _ = _gpx_paths(log)
    path = os.path.expanduser(prompt("Output path", default=default_name))

    expected = log.num_trackpoints
    print(f"  Downloading {expected} points from {log.start:%Y-%m-%d %H:%M:%S} UTC...")

    start_t = time.monotonic()
    _, points, written = export_log(conn, log, path, on_progress=_make_progress(expected))
    print()  # newline after progress
    elapsed = time.monotonic() - start_t
    print(f"  Downloaded {len(points)} points in {elapsed:.1f}s.")

    if len(points) != expected:
        print(f"  Warning: got {len(points)} points, expected {expected}.")

    print(f"  Wrote {written} points to {path}")


COMMANDS = [
    ("Read firmware version", cmd_firmware_version),
    ("List trackpoint logs", cmd_list_logs),
    ("Export track to GPX", cmd_export_gpx),
]


def command_loop(connection: Connection):
    while True:
        print()
        print("Commands:")
        for i, (label, _) in enumerate(COMMANDS, start=1):
            print(f"  {i}. {label}")
        print("  q. Quit")
        answer = prompt("Choose", default="q")
        if answer.lower() in ("q", "quit", "exit"):
            return
        try:
            idx = int(answer)
        except ValueError:
            print("  (enter a number, or 'q' to quit)")
            continue
        if not (1 <= idx <= len(COMMANDS)):
            print(f"  (choose 1..{len(COMMANDS)} or q)")
            continue

        _, fn = COMMANDS[idx - 1]
        try:
            fn(connection)
        except ProtocolError as exc:
            print(f"  Protocol error: {exc}")
        except TimeoutError as exc:
            print(f"  Timeout: {exc}")
        except Exception as exc:
            print(f"  Error: {exc!r}")


def export_newest(conn: Connection) -> int:
    logs = conn.list_trackpoint_logs()
    if not logs:
        print("Error: no logs on device.", file=sys.stderr)
        return 1

    log = max(logs, key=lambda l: l.start)
    path, name = _gpx_paths(log)
    print(f"Track: {name}")

    try:
        _, _, written = export_log(conn, log, path, on_progress=_make_progress(log.num_trackpoints))
        print()  # newline after progress
    except Exception as exc:
        print()
        print(f"Error: {exc!r}", file=sys.stderr)
        return 1

    print(f"Success: wrote {written} points to {path}")
    return 0


def main() -> int:
    newest = "--newest" in sys.argv[1:]

    if newest:
        devices = find_devices()
        if not devices:
            print("Error: no Velocitek devices found.", file=sys.stderr)
            return 1
        descriptor = devices[0][0]
    else:
        descriptor = choose_device()
        if descriptor is None:
            return 0

    model = PRODUCTS.get(descriptor.pid, "Unknown")
    print(f"\nConnecting to {model} (serial {descriptor.sn})...")
    try:
        with Connection(descriptor) as conn:
            print("Connected.")
            if newest:
                return export_newest(conn)
            command_loop(conn)
    except Exception as exc:
        print(f"Connection failed: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
