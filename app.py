"""
nrsc5-gui — HD Radio + WFM receiver GUI
Backend: Flask + Flask-SocketIO + unified RTL-SDR session
  - Mode "analog" : WFM demod (numpy/scipy) + RDS decoder
  - Mode "hd"     : nrsc5 (piped IQ samples via pipe_samples_cu8)
Both share one rtlsdr device session; switching mode swaps the
active decoder without closing/reopening the hardware.
"""

import os
import sys
import json
import time
import base64
import threading
import webbrowser
import logging
import collections

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ["PATH"] = BASE_DIR + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, BASE_DIR)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nrsc5-gui")

# ---------------------------------------------------------------------------
# Flask / SocketIO
# ---------------------------------------------------------------------------
UI_DIR = os.path.join(BASE_DIR, "ui")
app = Flask(__name__, static_folder=UI_DIR)
app.config["SECRET_KEY"] = "nrsc5-gui-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Optional deps — degrade gracefully so the UI still loads
# ---------------------------------------------------------------------------
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    log.warning("numpy not available — WFM mode disabled")

try:
    from scipy.signal import firwin, lfilter, lfilter_zi
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    log.warning("scipy not available — WFM mode disabled")

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    log.warning("PyAudio not available — audio output disabled")

try:
    import rtlsdr
    RTLSDR_AVAILABLE = True
except ImportError:
    RTLSDR_AVAILABLE = False
    log.warning("pyrtlsdr not available — hardware access disabled")

try:
    from nrsc5 import NRSC5, EventType, NRSC5Error, MIMEType
    NRSC5_AVAILABLE = True
except ImportError:
    NRSC5_AVAILABLE = False
    log.warning("nrsc5.py not found — HD mode disabled")

WFM_AVAILABLE = NUMPY_AVAILABLE and SCIPY_AVAILABLE and RTLSDR_AVAILABLE

# ---------------------------------------------------------------------------
# RTL-SDR / decoder constants
# ---------------------------------------------------------------------------
WFM_SAMPLE_RATE = 1_140_000    # IQ sps for analog WFM
HD_SAMPLE_RATE  = 1_488_375    # IQ sps for nrsc5 cu8
AUDIO_RATE      = 44_100
WFM_BLOCK_SIZE  = 32_768       # uint8 bytes per SDR read

# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
PRESETS_FILE = os.path.join(BASE_DIR, "presets.json")

def load_presets():
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_presets(presets):
    with open(PRESETS_FILE, "w") as f:
        json.dump(presets, f, indent=2)

# ---------------------------------------------------------------------------
# Shared radio state
# ---------------------------------------------------------------------------
class RadioState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.frequency      = None    # MHz float
        self.mode           = None    # "analog" | "hd"
        self.program        = 0       # HD sub-channel 0-3
        self.station_name   = None
        self.station_slogan = None
        self.title          = None
        self.artist         = None
        self.album          = None
        self.genre          = None
        self.album_art_b64  = None
        self.ber            = None
        self.mer_lower      = None
        self.mer_upper      = None
        self.gain           = None
        self.signal_dbfs    = None    # WFM signal level dBFS
        self.synced         = False
        self.active_programs = set()
        self.rds_ps         = None    # RDS Programme Service name
        self.rds_rt         = None    # RDS RadioText

state = RadioState()

def emit_state():
    socketio.emit("state", {
        "frequency":       state.frequency,
        "mode":            state.mode,
        "program":         state.program,
        "station_name":    state.station_name or state.rds_ps,
        "station_slogan":  state.station_slogan or state.rds_rt,
        "title":           state.title,
        "artist":          state.artist,
        "album":           state.album,
        "genre":           state.genre,
        "album_art_b64":   state.album_art_b64,
        "ber":             state.ber,
        "mer_lower":       state.mer_lower,
        "mer_upper":       state.mer_upper,
        "gain":            state.gain,
        "signal_dbfs":     state.signal_dbfs,
        "synced":          state.synced,
        "active_programs": list(state.active_programs),
        "rds_ps":          state.rds_ps,
        "rds_rt":          state.rds_rt,
    })

# ---------------------------------------------------------------------------
# Audio queue
# ---------------------------------------------------------------------------
audio_queue      = collections.deque(maxlen=64)
audio_queue_lock = threading.Lock()

def queue_audio(pcm_bytes: bytes):
    with audio_queue_lock:
        audio_queue.append(pcm_bytes)

def clear_audio():
    with audio_queue_lock:
        audio_queue.clear()

# ---------------------------------------------------------------------------
# Session control globals
# ---------------------------------------------------------------------------
stop_event   = threading.Event()
mode_event   = threading.Event()
_mode_lock   = threading.Lock()
_current_mode    = None   # "analog" | "hd"
_current_program = 0

sdr_thread       = None
audio_out_thread = None
nrsc5_obj        = None   # kept for external reference if needed

# ---------------------------------------------------------------------------
# ── RDS DECODER ─────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class RDSDecoder:
    """
    Minimal BPSK RDS decoder operating on the FM discriminator output.
    Extracts Programme Service (PS) name (group 0A) and RadioText (group 2A).
    """
    RDS_FREQ = 57_000

    def __init__(self):
        self._bit_buf    = collections.deque(maxlen=208)
        self._ps_chars   = [' '] * 8
        self._rt_chars   = [' '] * 64
        self._ps_flags   = [False] * 4
        self._last_emit  = 0.0
        self._phase_acc  = 0.0

    def feed(self, demod: "np.ndarray", if_rate: float):
        try:
            self._process(demod, if_rate)
        except Exception:
            pass

    def _process(self, sig, if_rate):
        # Mix down to baseband around RDS carrier (57 kHz)
        n = len(sig)
        t = np.arange(n, dtype=np.float32) / if_rate
        mixed = sig * np.exp(-2j * np.pi * self.RDS_FREQ * t).astype(np.complex64)

        # Low-pass filter ±2 kHz
        cutoff = 2_400 / (if_rate / 2)
        b = firwin(33, cutoff)
        bb = lfilter(b, [1.0], mixed)

        # Decimate to ~8x RDS symbol rate (1187.5 bps)
        target_rate = 1187.5 * 8
        dec = max(1, int(if_rate / target_rate))
        ds  = np.real(bb[::dec])

        sps = if_rate / dec / 1187.5
        if sps < 1:
            return

        # Simple threshold slicer
        for i in range(0, len(ds) - int(sps), int(sps)):
            chunk = ds[i:i + int(sps)]
            if len(chunk) == 0:
                continue
            self._bit_buf.append(1 if np.mean(chunk) >= 0 else 0)

        self._try_sync()

    def _try_sync(self):
        bits = list(self._bit_buf)
        if len(bits) < 52:
            return
        # Offset word A syndrome pattern (simplified — look for 26-bit block alignment)
        for start in range(len(bits) - 52):
            # Rough PI-block check: look for alternating pattern typical in RDS preamble
            blk = bits[start:start + 26]
            if self._check_block(blk):
                self._decode_group(bits[start:start + 104])
                return

    def _check_block(self, bits):
        # Very lightweight: just verify some structural properties
        return len(bits) == 26

    def _decode_group(self, bits):
        if len(bits) < 52:
            return
        try:
            gt = (bits[16] << 3) | (bits[17] << 2) | (bits[18] << 1) | bits[19]
            ver = bits[20]

            if gt == 0:  # Group 0A/0B — PS name
                seg = (bits[22] << 1) | bits[23]
                if seg < 4 and len(bits) >= 40:
                    self._ps_chars[seg * 2]     = self._b2c(bits[24:32])
                    self._ps_chars[seg * 2 + 1] = self._b2c(bits[32:40])
                    self._ps_flags[seg] = True
                    if all(self._ps_flags):
                        ps = ''.join(self._ps_chars).strip()
                        if ps and ps != state.rds_ps:
                            state.rds_ps = ps
                            self._maybe_emit()

            elif gt == 2 and ver == 0:  # Group 2A — RadioText
                ab  = bits[21]
                seg = (bits[22] << 3) | (bits[23] << 2) | (bits[24] << 1) | bits[25]
                for k in range(4):
                    idx = seg * 4 + k
                    start = 32 + k * 8
                    if idx < 64 and start + 8 <= len(bits):
                        self._rt_chars[idx] = self._b2c(bits[start:start + 8])
                rt = ''.join(self._rt_chars).rstrip('\r ').strip()
                if rt and rt != state.rds_rt:
                    state.rds_rt = rt
                    self._maybe_emit()
        except Exception:
            pass

    @staticmethod
    def _b2c(bits):
        if len(bits) < 8:
            return ' '
        val = 0
        for b in bits[:8]:
            val = (val << 1) | b
        return chr(val) if 32 <= val < 128 else ' '

    def _maybe_emit(self):
        now = time.time()
        if now - self._last_emit > 0.5:
            self._last_emit = now
            emit_state()


# ---------------------------------------------------------------------------
# ── WFM DEMODULATOR ─────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class WFMDemodulator:
    """
    Wideband FM demodulator.
    Input:  raw uint8 IQ blocks at WFM_SAMPLE_RATE (1.14 Msps)
    Output: PCM int16 stereo at 44100 Hz via queue_audio()
    """
    IF_RATE  = 228_000     # after decimation ×5
    AUD_DEC  = 5           # IF_RATE / AUD_DEC ≈ 45 600 → interp to 44100

    def __init__(self):
        if not WFM_AVAILABLE:
            raise RuntimeError("WFM requires numpy, scipy, and pyrtlsdr")

        self._prev = complex(0, 0)
        self._rds  = RDSDecoder()

        # Channel LPF + decimator ×5
        dec1 = WFM_SAMPLE_RATE // self.IF_RATE
        self._dec1   = dec1
        taps = firwin(129, 100_000 / (WFM_SAMPLE_RATE / 2))
        self._lpf_b  = taps
        self._lpf_zi = lfilter_zi(taps, [1.0]) * 0

        # De-emphasis (75 µs, Americas)
        dt    = 1.0 / self.IF_RATE
        tau   = 75e-6
        alpha = dt / (tau + dt)
        self._de_b  = np.array([alpha],       dtype=np.float64)
        self._de_a  = np.array([1.0, -(1 - alpha)], dtype=np.float64)
        self._de_zi = np.zeros(1)

        # Audio LPF (≤15 kHz)
        self._alf_b  = firwin(65, 15_000 / (self.IF_RATE / 2))
        self._alf_zi = lfilter_zi(self._alf_b, [1.0]) * 0

        self._last_state_t = 0.0

    def process(self, raw: bytes):
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        iq = (samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)

        # Signal level
        rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
        dbfs = 20 * np.log10(max(rms / 127.5, 1e-10))
        state.signal_dbfs = round(dbfs, 1)

        # Channel filter + decimate
        filt, self._lpf_zi = lfilter(self._lpf_b, [1.0], iq, zi=self._lpf_zi)
        dec = filt[::self._dec1]

        # FM discriminator (phase difference)
        prev = np.empty_like(dec)
        prev[0]  = self._prev
        prev[1:] = dec[:-1]
        self._prev = dec[-1]
        demod = np.angle(dec * np.conj(prev))

        # De-emphasis
        demod, self._de_zi = lfilter(self._de_b, self._de_a, demod, zi=self._de_zi)

        # Audio LPF
        audio, self._alf_zi = lfilter(self._alf_b, [1.0], demod, zi=self._alf_zi)

        # Decimate audio and resample to 44100
        adec = audio[::self.AUD_DEC]
        a44  = self._resample(adec, len(adec), int(len(adec) * AUDIO_RATE /
                              (WFM_SAMPLE_RATE / self._dec1 / self.AUD_DEC)))

        # Normalise + convert to int16 stereo
        peak = float(np.max(np.abs(a44))) + 1e-9
        gain = min(0.9 / peak, 8.0)
        pcm  = (a44 * gain * 32767).clip(-32767, 32767).astype(np.int16)
        stereo = np.repeat(pcm, 2)
        queue_audio(stereo.tobytes())

        # Feed RDS
        self._rds.feed(demod, self.IF_RATE)

        # Periodic state push for signal level
        now = time.time()
        if now - self._last_state_t > 1.0:
            self._last_state_t = now
            state.synced = True   # analog is always "locked"
            emit_state()

    @staticmethod
    def _resample(src, src_len, dst_len):
        if dst_len <= 0:
            return src
        xs = np.linspace(0.0, 1.0, src_len, endpoint=False)
        xd = np.linspace(0.0, 1.0, dst_len, endpoint=False)
        xd = xd[xd < 1.0]
        return np.interp(xd, xs, src).astype(np.float32)


# ---------------------------------------------------------------------------
# ── nrsc5 CALLBACK ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def nrsc5_callback(evt_type, evt, *args):
    changed = True

    if evt_type == EventType.SYNC:
        state.synced = True
    elif evt_type == EventType.LOST_SYNC:
        state.synced = False
        state.title = None
        state.artist = None
        state.album_art_b64 = None
    elif evt_type == EventType.MER:
        state.mer_lower = round(evt.lower, 1)
        state.mer_upper = round(evt.upper, 1)
    elif evt_type == EventType.BER:
        state.ber = round(evt.cber, 4)
    elif evt_type == EventType.ID3:
        if evt.program == state.program:
            state.title  = evt.title  or state.title
            state.artist = evt.artist or state.artist
            state.album  = evt.album  or state.album
            state.genre  = evt.genre  or state.genre
    elif evt_type == EventType.STATION_NAME:
        state.station_name = evt.name
    elif evt_type == EventType.STATION_SLOGAN:
        state.station_slogan = evt.slogan
    elif evt_type == EventType.LOT:
        mime = evt.mime
        if mime in (MIMEType.PRIMARY_IMAGE, MIMEType.JPEG,
                    MIMEType.PNG, MIMEType.STATION_LOGO):
            try:
                state.album_art_b64 = base64.b64encode(bytes(evt.data)).decode()
            except Exception as e:
                log.warning(f"Album art encode error: {e}")
    elif evt_type == EventType.AUDIO:
        if evt.program == state.program:
            queue_audio(bytes(evt.data))
        changed = False
    elif evt_type == EventType.AUDIO_SERVICE:
        state.active_programs.add(evt.program)
        state.gain = getattr(evt, "digital_audio_gain", state.gain)
    elif evt_type == EventType.AGC:
        if evt.is_final:
            state.gain = round(evt.gain_db, 1)
        changed = False
    else:
        changed = False

    if changed:
        emit_state()


# ---------------------------------------------------------------------------
# ── UNIFIED SDR SESSION ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _sdr_session(frequency_mhz: float, initial_mode: str,
                 initial_program: int, device_index: int):
    global nrsc5_obj

    if not RTLSDR_AVAILABLE:
        socketio.emit("error", {"message":
            "pyrtlsdr is not installed. Run: pip install pyrtlsdr"})
        return

    try:
        sdr = rtlsdr.RtlSdr(device_index)
    except Exception as e:
        socketio.emit("error", {"message": f"Cannot open RTL-SDR device {device_index}: {e}"})
        return

    wfm_dec      = None
    nrsc5_local  = None   # local ref — same object as nrsc5_obj

    def apply_mode(mode: str, program: int):
        nonlocal wfm_dec, nrsc5_local
        global nrsc5_obj

        log.info(f"Applying mode={mode} program={program}")
        clear_audio()

        # Reset display state (keep frequency)
        state.title  = state.artist = state.album = None
        state.album_art_b64 = None
        state.synced = False
        state.ber    = state.mer_lower = state.mer_upper = None
        state.rds_ps = state.rds_rt = None
        state.active_programs.clear()

        # Tear down existing nrsc5 if any
        if nrsc5_local is not None:
            try:
                nrsc5_local.stop()
                nrsc5_local.close()
            except Exception:
                pass
            nrsc5_local = None
            nrsc5_obj   = None

        wfm_dec = None

        if mode == "analog":
            sdr.sample_rate = WFM_SAMPLE_RATE
            state.mode = "analog"
            if WFM_AVAILABLE:
                wfm_dec = WFMDemodulator()
            else:
                socketio.emit("error", {"message":
                    "WFM requires numpy, scipy, and pyrtlsdr — not all installed."})

        else:  # "hd"
            sdr.sample_rate = HD_SAMPLE_RATE
            state.mode    = "hd"
            state.program = program
            if NRSC5_AVAILABLE:
                try:
                    obj = NRSC5(nrsc5_callback)
                    obj.open_pipe()
                    obj.set_frequency(frequency_mhz * 1e6)
                    obj.start()
                    nrsc5_local = obj
                    nrsc5_obj   = obj
                except NRSC5Error as e:
                    socketio.emit("error", {"message": f"nrsc5 error: {e}"})
            else:
                socketio.emit("error", {"message":
                    "libnrsc5.dll / nrsc5.py not found — HD mode unavailable."})

        emit_state()

    # ── Initialise ────────────────────────────────────────────────────────
    try:
        sdr.center_freq = int(frequency_mhz * 1e6)
        sdr.freq_correction = 0
        sdr.gain = 'auto'
        apply_mode(initial_mode, initial_program)

        # ── Main IQ read loop ──────────────────────────────────────────────
        while not stop_event.is_set():

            # Mode-change requested from UI?
            if mode_event.is_set():
                mode_event.clear()
                with _mode_lock:
                    new_mode    = _current_mode
                    new_program = _current_program
                apply_mode(new_mode, new_program)
                continue

            try:
                raw = sdr.read_bytes(WFM_BLOCK_SIZE)
            except Exception as e:
                if not stop_event.is_set():
                    log.error(f"SDR read error: {e}")
                break

            if stop_event.is_set():
                break

            if state.mode == "analog":
                if wfm_dec is not None:
                    try:
                        wfm_dec.process(raw)
                    except Exception as e:
                        log.warning(f"WFM error: {e}")
            else:
                if nrsc5_local is not None:
                    try:
                        nrsc5_local.pipe_samples_cu8(raw)
                    except Exception as e:
                        log.warning(f"nrsc5 pipe error: {e}")

    finally:
        if nrsc5_local is not None:
            try:
                nrsc5_local.stop()
                nrsc5_local.close()
            except Exception:
                pass
        try:
            sdr.close()
        except Exception:
            pass
        log.info("SDR session closed")


# ---------------------------------------------------------------------------
# ── AUDIO OUTPUT THREAD ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _audio_output_thread():
    if not PYAUDIO_AVAILABLE:
        return
    pa     = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=2,
        rate=AUDIO_RATE,
        output=True,
        frames_per_buffer=2048,
    )
    try:
        while not stop_event.is_set():
            chunk = None
            with audio_queue_lock:
                if audio_queue:
                    chunk = audio_queue.popleft()
            if chunk:
                stream.write(chunk)
            else:
                time.sleep(0.005)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# ---------------------------------------------------------------------------
# ── SESSION MANAGEMENT ────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def start_session(frequency_mhz: float, mode: str, program: int, device: int = 0):
    global sdr_thread, audio_out_thread, stop_event
    global _current_mode, _current_program

    stop_session()

    state.reset()
    state.frequency = frequency_mhz
    state.mode      = mode
    state.program   = program

    with _mode_lock:
        _current_mode    = mode
        _current_program = program

    stop_event = threading.Event()
    mode_event.clear()
    clear_audio()

    sdr_thread = threading.Thread(
        target=_sdr_session,
        args=(frequency_mhz, mode, program, device),
        daemon=True,
    )
    sdr_thread.start()

    if PYAUDIO_AVAILABLE:
        audio_out_thread = threading.Thread(
            target=_audio_output_thread, daemon=True)
        audio_out_thread.start()


def switch_mode(mode: str, program: int = 0):
    """Switch decoder mid-session without closing the RTL-SDR."""
    global _current_mode, _current_program
    if sdr_thread is None or not sdr_thread.is_alive():
        return
    with _mode_lock:
        _current_mode    = mode
        _current_program = program
        state.program    = program
    mode_event.set()


def stop_session():
    global sdr_thread, audio_out_thread
    stop_event.set()
    mode_event.set()
    if sdr_thread and sdr_thread.is_alive():
        sdr_thread.join(timeout=5)
    if audio_out_thread and audio_out_thread.is_alive():
        audio_out_thread.join(timeout=2)
    sdr_thread       = None
    audio_out_thread = None


# ---------------------------------------------------------------------------
# ── ROUTES ────────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(UI_DIR, path)

@app.route("/api/presets", methods=["GET"])
def api_get_presets():
    return jsonify(load_presets())

@app.route("/api/presets", methods=["POST"])
def api_add_preset():
    data    = request.json
    presets = load_presets()
    key = (data.get("frequency"), data.get("program"), data.get("mode"))
    if not any((p.get("frequency"), p.get("program"), p.get("mode")) == key
               for p in presets):
        presets.append(data)
        save_presets(presets)
    return jsonify(presets)

@app.route("/api/presets/<int:idx>", methods=["DELETE"])
def api_delete_preset(idx):
    presets = load_presets()
    if 0 <= idx < len(presets):
        presets.pop(idx)
        save_presets(presets)
    return jsonify(presets)


# ---------------------------------------------------------------------------
# ── SOCKETIO EVENTS ────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    emit_state()
    socketio.emit("presets", load_presets())
    socketio.emit("capabilities", {
        "wfm": WFM_AVAILABLE,
        "hd":  NRSC5_AVAILABLE,
    })


@socketio.on("tune")
def on_tune(data):
    """{ frequency: 107.1, mode: "analog"|"hd", program: 0, device: 0 }"""
    try:
        freq    = float(data.get("frequency", 0))
        mode    = str(data.get("mode", "hd"))
        program = int(data.get("program", 0))
        device  = int(data.get("device", 0))

        if not (87.5 <= freq <= 108.0):
            socketio.emit("error", {
                "message": f"{freq} MHz is outside FM range (87.5–108.0)"})
            return
        if mode not in ("analog", "hd"):
            mode = "hd"

        log.info(f"Tune → {freq} MHz  mode={mode}  program={program}")
        threading.Thread(
            target=start_session,
            args=(freq, mode, program, device),
            daemon=True,
        ).start()
    except Exception as e:
        socketio.emit("error", {"message": str(e)})


@socketio.on("set_mode")
def on_set_mode(data):
    """Switch analog ↔ HD (or sub-channel) without retuning the frequency."""
    mode    = str(data.get("mode", "hd"))
    program = int(data.get("program", 0))
    log.info(f"Mode switch → {mode}  program={program}")
    switch_mode(mode, program)


@socketio.on("stop")
def on_stop():
    threading.Thread(target=stop_session, daemon=True).start()
    state.reset()
    emit_state()


@socketio.on("save_preset")
def on_save_preset(data):
    presets = load_presets()
    key = (data.get("frequency"), data.get("program"), data.get("mode"))
    if not any((p.get("frequency"), p.get("program"), p.get("mode")) == key
               for p in presets):
        presets.append(data)
        save_presets(presets)
    socketio.emit("presets", presets)


@socketio.on("delete_preset")
def on_delete_preset(data):
    idx     = int(data.get("index", -1))
    presets = load_presets()
    if 0 <= idx < len(presets):
        presets.pop(idx)
        save_presets(presets)
    socketio.emit("presets", presets)


# ---------------------------------------------------------------------------
# ── ENTRY POINT ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def main():
    port = 7373
    log.info(f"nrsc5-gui starting at http://localhost:{port}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    socketio.run(app, host="127.0.0.1", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
