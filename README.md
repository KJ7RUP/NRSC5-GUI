# NRSC5-GUI
NRSC5 GUI for Windows 11

Built by Claude Sonnet 4.6

A Windows desktop GUI for receiving **HD Radio (NRSC-5) and analog FM broadcasts** using a low-cost RTL-SDR dongle. The interface runs as a local web app that opens automatically in your browser when the program starts.

Built on top of [theori-io/nrsc5](https://github.com/theori-io/nrsc5).

---

## Features

- **Analog FM** — wideband FM demodulation with RDS decoding (station name, RadioText)
- **HD Radio (NRSC-5)** — digital HD1–HD4 sub-channel selection; inactive channels are greyed out automatically
- **Now playing** — station callsign, track title, artist, album; album art when transmitted by the station
- **Signal telemetry** — MER (Modulation Error Ratio), Bit Error Rate, digital gain for HD; dBFS signal level for analog
- **Presets** — save any station/mode combination, double-click to instantly retune
- **Seamless mode switching** — switching between Analog and HD channels reuses the same RTL-SDR session without closing and reopening the device
- **Single executable** — distributed as one `.exe` with no runtime dependencies for the end user

---

## Requirements

### Hardware
- Any **RTL-SDR dongle** based on the RTL2832U chipset (e.g. RTL-SDR Blog V3, NooElec NESDR)

### One-time setup (not bundled)
| Item | Where to get it |
|---|---|
| WinUSB driver | [Zadig](https://zadig.akeo.ie/) — see Step 1 |
| `libnrsc5.dll` | Built from the nrsc5 repo — see Step 2 |
| `librtlsdr.dll` | Produced alongside `libnrsc5.dll` during the MSYS2 build |
| `nrsc5.py` | `support/nrsc5.py` from the [nrsc5 repo](https://github.com/theori-io/nrsc5) |
| Python 3.10+ | [python.org](https://www.python.org/downloads/) — only needed to build the exe |

---

## Setup

### Step 1 — Install the RTL-SDR driver

This is a one-time step per machine.

1. Plug in your RTL-SDR dongle.
2. Download and run [Zadig](https://zadig.akeo.ie/).
3. Select your RTL-SDR device from the dropdown. If it doesn't appear, enable **Options → List All Devices**.
4. Set the driver to **WinUSB**.
5. Click **Replace Driver** and wait for it to finish.

### Step 2 — Build libnrsc5.dll

Follow the [**Building on Windows with MSYS2**](https://github.com/theori-io/nrsc5#building-on-windows-with-msys2) instructions in the nrsc5 README. Once the build completes, locate the following files and copy them into the root of this project (next to `build.bat`):

```
C:\msys64\mingw64\bin\libnrsc5.dll   →  ./libnrsc5.dll
C:\msys64\mingw64\bin\librtlsdr.dll  →  ./librtlsdr.dll
```

Also copy the Python API wrapper from the nrsc5 source tree:

```
nrsc5/support/nrsc5.py  →  ./nrsc5.py
```

### Step 3 — Build the executable

Double-click **`build.bat`**. The script will:

1. Verify that `libnrsc5.dll`, `librtlsdr.dll`, and `nrsc5.py` are present
2. Install all Python dependencies (`flask`, `flask-socketio`, `pyaudio`, `numpy`, `scipy`, `pyrtlsdr`, etc.)
3. Run PyInstaller and produce a self-contained `dist\nrsc5-gui.exe`

The resulting `.exe` bundles everything and requires no Python installation to run.

### Step 4 — Run

Double-click `dist\nrsc5-gui.exe`. Your default browser will open automatically at `http://localhost:7373`.

---

## Using the app

**Tuning**
- Type a frequency in the box (e.g. `107.1`) and press **Tune** or Enter.
- Select **Analog** for standard FM, or **HD1–HD4** for digital sub-channels before or after tuning.
- Switching between Analog and HD does not interrupt or restart the RTL-SDR — it swaps the active decoder in place.

**Display**
- The large area shows station callsign (or frequency if not yet received), track title, artist, and album.
- When a station transmits album art it replaces the callsign display.
- In Analog mode, an **RDS** badge appears when RDS data is being decoded. The station name and RadioText fill the same fields as HD metadata.

**Telemetry** (lower section)
- *HD mode:* MER (signal quality in dB), Bit Error Rate, and digital audio gain.
- *Analog mode:* signal level in dBFS. BER is not applicable and is hidden.

**Presets**
- Click **Save** in the header to save the current station, mode, and channel as a preset.
- Click **Presets** to open the slide-out panel. Each preset shows a colour-coded dot (green = Analog, blue = HD) and the frequency and channel.
- **Double-click** any preset to instantly tune to it.
- Click **✕** on a preset to remove it. Presets are saved to `presets.json` next to the executable and persist across sessions.

---

## Development / running from source

```bash
# Install dependencies
pip install flask flask-socketio pyaudio eventlet numpy scipy pyrtlsdr

# Place next to app.py (from nrsc5 repo build / support/):
#   libnrsc5.dll
#   librtlsdr.dll
#   nrsc5.py

python app.py
```

Then open `http://localhost:7373` in your browser.

---

## Project structure

```
nrsc5-gui/
├── app.py              # Flask + SocketIO backend; WFM demod; RDS decoder; nrsc5 integration
├── ui/
│   └── index.html      # Self-contained web UI (HTML + CSS + JS, no build step)
├── requirements.txt    # Python dependencies
├── nrsc5_gui.spec      # PyInstaller build configuration
├── build.bat           # One-click Windows build script
├── .gitignore
└── README.md
```

Files you supply (not committed to this repo):

```
libnrsc5.dll            # From nrsc5 MSYS2 build
librtlsdr.dll           # From nrsc5 MSYS2 build
nrsc5.py                # From nrsc5/support/nrsc5.py
presets.json            # Auto-created at runtime
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Cannot open RTL-SDR device" | Re-run Zadig and confirm WinUSB is selected; try unplugging and replugging the dongle |
| "libnrsc5.dll not found" | Ensure `libnrsc5.dll` and `librtlsdr.dll` are in the same folder as the exe |
| No audio / silence | Check Windows default audio output device; try increasing dongle gain in `app.py` |
| HD channels all greyed out | The station may not broadcast HD, or the signal is too weak to lock |
| RDS not appearing | RDS decoding requires a strong signal; weak or noisy stations may not decode reliably |
| Browser doesn't open | Navigate manually to `http://localhost:7373` |
| Build fails | Confirm Python 3.10+ is installed and on PATH; run `pip install pyinstaller` manually |

---

## Attribution & licence

This project is built on **[nrsc5](https://github.com/theori-io/nrsc5)** by [theori-io](https://github.com/theori-io), licensed under the [GNU General Public License v2.0](https://github.com/theori-io/nrsc5/blob/master/LICENSE).

`nrsc5.py` (included at build time from the nrsc5 repository) and `libnrsc5.dll` / `librtlsdr.dll` (linked at runtime) are components of that project and are used here under the terms of the GPL-2.0.

In accordance with the GPL-2.0, this project is also released under the **GNU General Public License v2.0**. See [`LICENSE`](LICENSE) for the full text.
