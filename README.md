# velocitek-export

Cross-platform (Linux/macOS) Python CLI for downloading tracks from Velocitek
sailing instruments (SpeedPuck, ProStart, SC1, S10) and exporting them as GPX.

Talks to the device's FTDI USB-serial chip directly via libusb/pyftdi, so no
proprietary drivers are required.

## Install

### Linux

    git clone <repo-url> velocitek-export
    cd velocitek-export

    # Install udev rule so a regular user can access the device over USB
    sudo cp 99-velocitek.rules /etc/udev/rules.d/
    sudo udevadm control --reload
    sudo udevadm trigger

    # Set up a Python virtualenv
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Plug the device in (unplug/replug it once after installing the udev rule so
the new permissions take effect), then run:

    ./velocitek_cli.py

### macOS

    git clone <repo-url> velocitek-export
    cd velocitek-export

    # libusb is required by pyftdi on macOS
    brew install libusb

    # Set up a Python virtualenv
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Then:

    ./velocitek_cli.py

Note: macOS has a built-in FTDI kernel driver (`AppleUSBFTDI`). The
SpeedPuck/ProStart/SC1 use custom FTDI product IDs that the kernel driver
ignores, so they work without any extra steps. The S10 uses the stock FTDI
product ID (`0x6001`); if macOS claims it first, you may need to unload the
kernel driver (`sudo kextunload -b com.apple.driver.AppleUSBFTDI`).

## Usage

    ./velocitek_cli.py

The CLI is interactive. It will:

1. List attached Velocitek devices and ask which to use.
2. Show a menu with: read firmware version, list tracks, export track to GPX.
3. For the export option, list the tracks on the device (newest first), ask
   which to export, and ask for an output filename (defaults to
   `track-YYYYMMDD-HHMMSS.gpx`).

### Quick export

    ./velocitek_cli.py --newest

Non-interactive shortcut: uses the first detected device, exports its
newest track to `track-YYYYMMDD-HHMMSS.gpx` in the current directory, prints
the track name and a success or error message, and exits.

## Supported hardware

Tested on: SpeedPuck.

Should work on (same protocol, same FTDI chip, but not yet verified):
ProStart, SC1, S10.
