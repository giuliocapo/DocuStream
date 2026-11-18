"""
audio_engine.py - WASAPI dual-channel recorder for DocuStream.

Captures two WASAPI sources simultaneously with zero administrator rights:

* Loopback  - whatever the sound card is playing (Teams / Zoom / Meet output).
* Microphone - the physical WASAPI input device (the user's own voice).

Both streams run on dedicated capture threads that push PCM blocks into
thread-safe ring buffers (``collections.deque``). On stop, the buffered audio
is written as 16-bit 24 kHz stereo PCM WAV: the physical microphone is kept
on the left channel and WASAPI loopback on the right channel. The two voices
are never mixed, so Whisper can transcribe and attribute them independently.

Safety guards:
* Silence watchdog - if the loopback channel stays below the configured RMS
  threshold for ``silence_timeout_min`` consecutive minutes, the recorder
  gracefully stops itself (call ended or was never really a call).
* Hard timeout guard - recording can never exceed ``max_call_duration_min``
  minutes, protecting the disk against a forgotten recording.
"""

import collections
import os
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import pyaudiowpatch as pa
except ImportError:  # pragma: no cover - surfaces as a friendly UI error
    pa = None

# Set DOCUSTREAM_DEBUG=1 to trace recorder lifecycle steps to stdout.
_DEBUG = os.environ.get("DOCUSTREAM_DEBUG", "").strip() in ("1", "true")


def _dbg(message: str) -> None:
    if _DEBUG:
        print(f"[audio_engine] {message}", flush=True)


def _new_pa():
    """Instantiate pyaudiowpatch, tolerating both historical class names."""
    cls = getattr(pa, "PyAudioWPatch", None) or getattr(pa, "PyAudio", None)
    if cls is None:
        raise RecorderError("pyaudiowpatch exposes neither PyAudioWPatch "
                            "nor PyAudio - check the installed version.")
    return cls()


def create_pa_instance():
    """Public factory for a shared PyAudio instance (see AudioRecorder)."""
    return _new_pa()


TARGET_RATE = 24000          # Capture rate; transcription resamples each
                             # channel to Whisper's required 16 kHz input.
FRAMES_PER_BUFFER = 1024
LEVEL_REPORT_HZ = 15         # How often RMS levels are pushed to the UI.


class RecorderError(Exception):
    """Raised when audio capture cannot start or finish cleanly."""


def open_stream_with_timeout(pyaudio_instance, timeout: float = 60.0,
                             **kwargs):
    """Open a PyAudio stream with a generous sanity timeout.

    Healthy-but-slow driver stacks (headsets with virtual mixers, first
    open after boot) can take 10-15s per open - that is fine and must NOT
    be cut short: abandoning an in-flight native open is what wedges the
    Windows audio graph in the first place. A genuinely wedged stack fails
    *fast* with -9996/-9997, so this timeout only exists as a backstop to
    surface an error instead of hanging a background worker forever.
    """
    result = {}

    def _opener():
        try:
            result["stream"] = pyaudio_instance.open(**kwargs)
        except Exception as exc:  # surfaced by the caller
            result["error"] = exc

    thread = threading.Thread(target=_opener, daemon=True,
                              name="DocuStream-StreamOpen")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RecorderError(
            "Timed out while opening the audio device - the Windows audio "
            "stack appears slow or stuck. If this persists, run "
            "'Restart-Service Audiosrv -Force' in an admin PowerShell and "
            "try again."
        )
    if "error" in result:
        raise result["error"]
    return result["stream"]


class AudioRecorder:
    """Dual-source WASAPI recorder with watchdog and hard-timeout guards.

    Parameters
    ----------
    silence_timeout_min : int
        Auto-stop after this many consecutive minutes of silent loopback.
    silence_rms_threshold : float
        Loopback RMS below this value counts as silence.
    max_duration_min : int
        Hard ceiling for a single recording, in minutes.
    on_level : callable(loopback_rms, mic_rms), optional
        Invoked ~15x/second from a capture thread; must be cheap and must not
        touch GUI objects directly (post to a queue instead).
    on_auto_stop : callable(reason: str), optional
        Invoked once if the watchdog or the hard timeout stops the recording.
    loopback_index : int, optional
        PyAudio device index of the loopback (output) device to capture.
        None = system default output.
    mic_index : int, optional
        PyAudio device index of the physical microphone. None = default mic.
    pa_instance : PyAudio, optional
        A pre-initialized shared PyAudio instance owned by the caller.
        When provided, this recorder never terminates it (recommended: keep
        every PortAudio call on one thread - background-thread init/terminate
        cycles can deadlock or freeze the app).
    """

    def __init__(self, silence_timeout_min: int = 8,
                 silence_rms_threshold: float = 120.0,
                 max_duration_min: int = 120,
                 on_level=None, on_auto_stop=None,
                 loopback_index=None, mic_index=None,
                 pa_instance=None):
        if pa is None:
            raise RecorderError(
                "pyaudiowpatch is not installed. Run bootstrap_portable.bat "
                "or 'pip install pyaudiowpatch'."
            )
        self.silence_timeout_sec = max(1, int(silence_timeout_min)) * 60
        self.silence_rms_threshold = float(silence_rms_threshold)
        self.max_duration_sec = max(1, int(max_duration_min)) * 60
        self.on_level = on_level
        self.on_auto_stop = on_auto_stop
        self._loopback_index = loopback_index
        self._mic_index = mic_index
        self._pa_shared = pa_instance

        self._pa = None
        self._loop_stream = None
        self._mic_stream = None

        # Ring buffers: each entry is a float32 numpy array of PCM samples.
        self._loop_buffer: collections.deque = collections.deque()
        self._mic_buffer: collections.deque = collections.deque()
        self._buffer_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._threads: list = []
        self._recording = False
        self._started_at = None

        self._loop_rate = TARGET_RATE
        self._loop_channels = 1
        self._loop_float = False
        self._mic_rate = TARGET_RATE
        self._mic_channels = 1
        self._mic_float = False
        self._mic_active = False

        self._last_loop_rms = 0.0
        self._last_mic_rms = 0.0
        self._stop_reason = "manual"

    # ------------------------------------------------------------------ #
    # Device discovery                                                    #
    # ------------------------------------------------------------------ #

    def _find_loopback_device(self) -> dict:
        """Return the PyAudio device dict for the chosen WASAPI loopback."""
        if self._loopback_index is not None:
            try:
                dev = self._pa.get_device_info_by_index(self._loopback_index)
                if dev.get("isLoopbackDevice"):
                    return dev
            except OSError:
                pass  # stale index from a previous session - use default
        # pyaudiowpatch >= 0.2.12 exposes a convenience helper.
        getter = getattr(self._pa, "get_default_wasapi_loopback", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        # Fallback: scan every host API for a loopback-capable device.
        for api_idx in range(self._pa.get_host_api_count()):
            api = self._pa.get_host_api_info_by_index(api_idx)
            if "WASAPI" not in api.get("name", ""):
                continue
            for dev_idx in range(api.get("deviceCount", 0)):
                dev = self._pa.get_device_info_by_host_api_device_index(
                    api_idx, dev_idx
                )
                if dev.get("isLoopbackDevice", False):
                    return dev
        raise RecorderError(
            "No WASAPI loopback device found. Make sure the Windows Audio "
            "service (Audiosrv) is running and audio output is enabled."
        )

    def _find_mic_device(self):
        """Return the chosen/default input device dict, or None."""
        if self._mic_index is not None:
            try:
                dev = self._pa.get_device_info_by_index(self._mic_index)
                if not dev.get("isLoopbackDevice") \
                        and dev.get("maxInputChannels", 0) > 0:
                    return dev
            except OSError:
                pass  # stale index - fall back to default
        try:
            return self._pa.get_default_input_device_info()
        except OSError:
            return None

    def _open_stream_with_timeout(self, timeout: float = 60.0, **kwargs):
        """Delegate to the module-level helper (kept for readability)."""
        return open_stream_with_timeout(self._pa, timeout, **kwargs)

    def _open_cascade(self, dev: dict, rate: int, native_channels: int,
                      label: str):
        """Try several format/channel combos until the device opens.

        Some WASAPI drivers (and a wedged audio graph) reject the reported
        mix format; trying the native float32 format and mono fallbacks
        covers the common -9997/-9998 driver quirks.

        Returns (stream, is_float_format, channels). Raises RecorderError
        if every combination fails.
        """
        candidates = []
        for fmt in (pa.paInt16, pa.paFloat32):
            for channels in dict.fromkeys([native_channels, 1]):
                candidates.append((fmt, channels))
        last_error = None
        for fmt, channels in candidates:
            fmt_name = "float32" if fmt == pa.paFloat32 else "int16"
            try:
                stream = self._open_stream_with_timeout(
                    format=fmt, channels=channels, rate=rate,
                    input=True, input_device_index=dev["index"],
                    frames_per_buffer=FRAMES_PER_BUFFER,
                )
                _dbg(f"open OK ({label}): {fmt_name} {rate}Hz {channels}ch")
                return stream, fmt == pa.paFloat32, channels
            except RecorderError:
                # A timeout means the audio graph is wedged; every
                # remaining combination will hang the same way - bail out.
                raise
            except Exception as exc:
                last_error = exc
                _dbg(f"open fail ({label}): {fmt_name} {rate}Hz "
                     f"{channels}ch -> {exc!r}")
        raise RecorderError(
            f"Could not open {label} device '{dev.get('name', '?')}': "
            f"{last_error}. If this persists, the Windows audio stack is "
            f"stuck - run 'Restart-Service Audiosrv -Force' in an admin "
            f"PowerShell or reboot."
        )

    # ------------------------------------------------------------------ #
    # Capture threads                                                     #
    # ------------------------------------------------------------------ #

    def _decode_block(self, raw: bytes, is_float: bool = False) -> np.ndarray:
        """Convert raw PCM bytes into normalized float32 samples."""
        if is_float:
            return np.frombuffer(raw, dtype="<f4").astype(np.float32)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    def _capture_loop(self, stream, buffer: collections.deque,
                      channels: int, is_loopback: bool,
                      is_float: bool = False) -> None:
        """Read blocks from a stream into its ring buffer until stopped."""
        last_level_report = 0.0
        while not self._stop_event.is_set():
            try:
                raw = stream.read(FRAMES_PER_BUFFER,
                                  exception_on_overflow=False)
            except OSError:
                # Device glitch (headset unplugged etc.): keep going, the
                # watchdog will shut us down if the stream stays broken.
                time.sleep(0.05)
                continue

            samples = self._decode_block(raw, is_float)
            if channels > 1:
                # Downmix to mono by averaging channels.
                samples = samples.reshape(-1, channels).mean(axis=1)

            rms = float(np.sqrt(np.mean(np.square(samples)))) \
                if samples.size else 0.0
            if is_loopback:
                self._last_loop_rms = rms * 32768.0  # report in int16 units
            else:
                self._last_mic_rms = rms * 32768.0

            with self._buffer_lock:
                buffer.append(samples.astype(np.float32))

            now = time.monotonic()
            if self.on_level and \
                    (now - last_level_report) >= 1.0 / LEVEL_REPORT_HZ:
                last_level_report = now
                try:
                    self.on_level(self._last_loop_rms, self._last_mic_rms)
                except Exception:
                    pass  # UI callbacks must never kill a capture thread

    def _watchdog_loop(self) -> None:
        """Silence watchdog + hard timeout. Runs once per second."""
        silent_since = time.monotonic()
        while not self._stop_event.is_set():
            time.sleep(1.0)
            now = time.monotonic()

            if self._last_loop_rms >= self.silence_rms_threshold:
                silent_since = now

            if now - self._started_at >= self.max_duration_sec:
                self._stop_reason = (
                    f"hard timeout reached "
                    f"({self.max_duration_sec // 60} min)"
                )
                self._initiate_auto_stop()
                return

            if now - silent_since >= self.silence_timeout_sec:
                self._stop_reason = (
                    f"silence watchdog: no system audio for "
                    f"{self.silence_timeout_sec // 60} min"
                )
                self._initiate_auto_stop()
                return

    def _initiate_auto_stop(self) -> None:
        """Ask all threads to stop, then notify the UI (from watchdog thread)."""
        self._stop_event.set()
        if self.on_auto_stop:
            try:
                self.on_auto_stop(self._stop_reason)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    def start(self) -> None:
        """Open both WASAPI streams and start the capture threads."""
        if self._recording:
            raise RecorderError("Recorder is already running.")

        if self._pa_shared is not None:
            self._pa = self._pa_shared
            _dbg("start: using shared PyAudio instance")
        else:
            _dbg("start: creating PyAudio instance")
            self._pa = _new_pa()
            _dbg("start: PyAudio ready")
        self._stop_event.clear()
        self._stop_reason = "manual"
        self._last_loop_rms = self._last_mic_rms = 0.0
        with self._buffer_lock:
            self._loop_buffer.clear()
            self._mic_buffer.clear()

        # --- Loopback stream (system speakers) ---
        loop_dev = self._find_loopback_device()
        _dbg(f"start: loopback device '{loop_dev.get('name', '?')}' "
             f"(index {loop_dev['index']})")
        self._loop_rate = int(loop_dev["defaultSampleRate"])
        native_ch = int(loop_dev["maxInputChannels"]) or 2
        try:
            _dbg("start: opening loopback stream")
            self._loop_stream, self._loop_float, self._loop_channels = \
                self._open_cascade(loop_dev, self._loop_rate, native_ch,
                                   "loopback")
            _dbg(f"start: loopback stream open "
                 f"(float={self._loop_float}, ch={self._loop_channels})")
        except RecorderError:
            self._shutdown_streams()
            raise

        # --- Microphone stream (optional; never blocks the whole recording) ---
        mic_dev = self._find_mic_device()
        _dbg(f"start: mic device "
             f"'{mic_dev.get('name', '?')}' " if mic_dev else
             "start: no microphone available")
        self._mic_stream = None
        self._mic_active = False
        if mic_dev is not None:
            self._mic_rate = int(mic_dev["defaultSampleRate"])
            mic_native_ch = min(int(mic_dev["maxInputChannels"]) or 1, 2)
            try:
                _dbg("start: opening mic stream")
                self._mic_stream, self._mic_float, self._mic_channels = \
                    self._open_cascade(mic_dev, self._mic_rate,
                                       mic_native_ch, "microphone")
                self._mic_active = True
                _dbg(f"start: mic stream open "
                     f"(float={self._mic_float}, ch={self._mic_channels})")
            except RecorderError:
                # A failed mic must not kill system-audio capture.
                self._mic_stream = None
                self._mic_active = False

        self._started_at = time.monotonic()
        self._recording = True

        self._threads = [
            threading.Thread(
                target=self._capture_loop,
                args=(self._loop_stream, self._loop_buffer,
                      self._loop_channels, True, self._loop_float),
                name="DocuStream-Loopback", daemon=True,
            ),
            threading.Thread(
                target=self._watchdog_loop,
                name="DocuStream-Watchdog", daemon=True,
            ),
        ]
        if self._mic_stream is not None:
            self._threads.append(threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_buffer,
                      self._mic_channels, False, self._mic_float),
                name="DocuStream-Mic", daemon=True,
            ))
        for thread in self._threads:
            thread.start()

    def stop(self, output_dir: Path, project: str = "General") -> Path:
        """Stop capture and write microphone + loopback as a stereo WAV.

        Channel 0 (left) is the physical microphone (``Tu``); channel 1
        (right) is WASAPI loopback (``Teams / Interlocutori``).

        Returns the path of the written ``.wav`` file. Safe to call multiple
        times; subsequent calls after the first raise RecorderError.
        """
        if not self._recording:
            raise RecorderError("Recorder is not running.")
        self._recording = False
        self._stop_event.set()
        self._halt_stream_flow()

        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()

        with self._buffer_lock:
            loop_chunks = list(self._loop_buffer)
            mic_chunks = list(self._mic_buffer)
            self._loop_buffer.clear()
            self._mic_buffer.clear()

        self._shutdown_streams()

        loop_pcm = np.concatenate(loop_chunks) if loop_chunks \
            else np.zeros(0, dtype=np.float32)
        mic_pcm = np.concatenate(mic_chunks) if mic_chunks \
            else np.zeros(0, dtype=np.float32)

        loop_pcm = _resample_linear(loop_pcm, self._loop_rate, TARGET_RATE)
        if self._mic_active:
            mic_pcm = _resample_linear(mic_pcm, self._mic_rate, TARGET_RATE)
        else:
            mic_pcm = np.zeros(0, dtype=np.float32)

        # Align lengths: pad the shorter track with zeros.
        length = max(loop_pcm.size, mic_pcm.size)
        if length == 0:
            raise RecorderError("No audio was captured - nothing to save.")
        loop_pcm = _pad_to(loop_pcm, length)
        mic_pcm = _pad_to(mic_pcm, length)

        # Keep the sources separated: left = microphone, right = loopback.
        # Clamp each source independently; unlike a mixdown, no attenuation
        # is needed because the channels are never summed together.
        stereo = np.column_stack((mic_pcm, loop_pcm)).astype(np.float32)
        stereo = np.clip(stereo, -1.0, 1.0)
        pcm16 = (stereo * 32767.0).astype("<i2")  # interleaved little-endian

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_project = _slugify(project)
        wav_path = output_dir / f"{stamp}_{safe_project}.wav"

        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)          # 16-bit PCM
            wav.setframerate(TARGET_RATE)
            wav.writeframes(pcm16.tobytes())

        return wav_path

    def discard(self) -> None:
        """Abort the recording and drop every buffered sample."""
        if self._recording:
            self._recording = False
            self._stop_event.set()
            self._halt_stream_flow()
            for thread in self._threads:
                thread.join(timeout=3.0)
            self._threads.clear()
        with self._buffer_lock:
            self._loop_buffer.clear()
            self._mic_buffer.clear()
        self._shutdown_streams()

    def _halt_stream_flow(self) -> None:
        """Stop stream data flow WITHOUT closing the streams.

        A capture thread blocked inside ``stream.read()`` returns with an
        error once the stream is stopped, letting it exit its loop before
        we close anything. Closing a stream with an in-flight read crashes
        PortAudio at native level (no Python traceback, app just dies).
        """
        for stream in (self._loop_stream, self._mic_stream):
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass

    def _shutdown_streams(self) -> None:
        """Close streams and terminate PortAudio (if owned). Idempotent."""
        for stream in (self._loop_stream, self._mic_stream):
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except OSError:
                    pass
        self._loop_stream = None
        self._mic_stream = None
        if self._pa is not None and self._pa is not self._pa_shared:
            try:
                self._pa.terminate()
            except Exception:
                pass
        self._pa = None


# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #

def _resample_linear(samples: np.ndarray, orig_rate: int,
                     target_rate: int = TARGET_RATE) -> np.ndarray:
    """Resample mono float32 PCM with linear interpolation.

    Linear interpolation is more than adequate for speech and avoids pulling
    in scipy as a dependency on a portable distribution.
    """
    if samples.size == 0 or orig_rate == target_rate:
        return samples
    duration = samples.size / float(orig_rate)
    n_target = int(round(duration * target_rate))
    if n_target <= 0:
        return np.zeros(0, dtype=np.float32)
    src_positions = np.arange(samples.size, dtype=np.float64) / orig_rate
    dst_positions = np.arange(n_target, dtype=np.float64) / target_rate
    return np.interp(dst_positions, src_positions, samples).astype(np.float32)


def _pad_to(samples: np.ndarray, length: int) -> np.ndarray:
    if samples.size >= length:
        return samples
    return np.concatenate([samples, np.zeros(length - samples.size,
                                             dtype=np.float32)])


def _slugify(text: str) -> str:
    """Turn a project name into a filesystem-safe slug."""
    keep = []
    for ch in text.strip():
        keep.append(ch if ch.isalnum() or ch in "-_" else "_")
    slug = "".join(keep).strip("_")
    return slug or "General"


# ---------------------------------------------------------------------- #
# Level monitor (live VU preview without recording)                       #
# ---------------------------------------------------------------------- #

class LevelMonitor:
    """Streams the selected devices for level metering only - nothing is
    buffered or saved. Lets the user visually confirm the chosen output and
    microphone actually carry audio before hitting Record.

    Shares the app's single PyAudio instance when ``pa_instance`` is given.
    """

    def __init__(self, on_level=None, loopback_index=None, mic_index=None,
                 pa_instance=None):
        if pa is None:
            raise RecorderError(
                "pyaudiowpatch is not installed. Run bootstrap_portable.bat.")
        self.on_level = on_level
        self._loopback_index = loopback_index
        self._mic_index = mic_index
        self._pa_instance = pa_instance
        self._p = None
        self._own_pa = False
        self._streams: list = []
        self._threads: list = []
        self._stop_event = threading.Event()
        self._last_loop = 0.0
        self._last_mic = 0.0
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    def _resolve_device(self, p, index, want_loopback):
        if index is not None:
            try:
                return p.get_device_info_by_index(index)
            except OSError:
                pass
        if want_loopback:
            getter = getattr(p, "get_default_wasapi_loopback", None)
            if callable(getter):
                return getter()
        return p.get_default_input_device_info()

    @staticmethod
    def _open(p, dev):
        rate = int(dev["defaultSampleRate"])
        native_ch = int(dev["maxInputChannels"]) or 1
        last = None
        for fmt in (pa.paInt16, pa.paFloat32):
            for channels in dict.fromkeys([native_ch, 1]):
                try:
                    stream = open_stream_with_timeout(
                        p, 60.0,
                        format=fmt, channels=channels, rate=rate, input=True,
                        input_device_index=dev["index"],
                        frames_per_buffer=FRAMES_PER_BUFFER)
                    return stream, fmt == pa.paFloat32, channels
                except RecorderError:
                    raise  # timeout: the whole audio stack is slow/stuck
                except Exception as exc:
                    last = exc
                    _dbg(f"monitor open fail {dev['name'][:30]} fmt={fmt} "
                         f"ch={channels}: {exc!r}")
        raise last

    def _meter_loop(self, stream, channels, is_float, slot: int) -> None:
        last_report = 0.0
        while not self._stop_event.is_set():
            try:
                raw = stream.read(FRAMES_PER_BUFFER,
                                  exception_on_overflow=False)
            except OSError:
                time.sleep(0.05)
                continue
            dtype = "<f4" if is_float else "<i2"
            scale = 1.0 if is_float else 1.0 / 32768.0
            samples = np.frombuffer(raw, dtype=dtype).astype(np.float32) \
                * scale
            if channels > 1 and samples.size:
                samples = samples.reshape(-1, channels).mean(axis=1)
            rms = float(np.sqrt(np.mean(np.square(samples)))) * 32768.0 \
                if samples.size else 0.0
            if slot == 0:
                self._last_loop = rms
            else:
                self._last_mic = rms
            now = time.monotonic()
            if self.on_level and \
                    (now - last_report) >= 1.0 / LEVEL_REPORT_HZ:
                last_report = now
                try:
                    self.on_level(self._last_loop, self._last_mic)
                except Exception:
                    pass

    def start(self) -> list:
        """Open the selected devices and start metering threads.

        Returns a list of warning strings (e.g. the mic failed but the
        output succeeded). Raises RecorderError only when NO device could
        be opened at all.
        """
        self._p = self._pa_instance or _new_pa()
        self._own_pa = self._pa_instance is None
        self._stop_event.clear()
        self._streams = []
        errors = []

        try:
            loop_dev = self._resolve_device(self._p, self._loopback_index,
                                            True)
            _dbg(f"monitor: opening output '{loop_dev['name'][:40]}'")
            stream, is_float, ch = self._open(self._p, loop_dev)
            self._streams.append(stream)
            self._threads.append(threading.Thread(
                target=self._meter_loop, args=(stream, ch, is_float, 0),
                name="DocuStream-MeterLoop", daemon=True))
        except Exception as exc:
            errors.append(f"output: {exc}")

        try:
            mic_dev = self._resolve_device(self._p, self._mic_index, False)
            _dbg(f"monitor: opening mic '{mic_dev['name'][:40]}'")
            stream, is_float, ch = self._open(self._p, mic_dev)
            self._streams.append(stream)
            self._threads.append(threading.Thread(
                target=self._meter_loop, args=(stream, ch, is_float, 1),
                name="DocuStream-MeterMic", daemon=True))
        except Exception as exc:
            errors.append(f"microphone: {exc}")

        if not self._threads:
            self._close_all()
            raise RecorderError(
                f"Could not open any device for level preview. "
                + " | ".join(errors)
                + " If this persists, run 'Restart-Service Audiosrv -Force' "
                  "in an admin PowerShell and retry."
            )
        for thread in self._threads:
            thread.start()
        self._active = True
        return errors

    def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        # IMPORTANT ORDER: stop the streams first so a thread blocked inside
        # stream.read() returns with an error and can exit its loop; only
        # then join the threads and close the streams. Closing a stream
        # while a read is in flight crashes PortAudio at native level.
        for stream in self._streams:
            try:
                stream.stop_stream()
            except Exception:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        self._close_all()
        self._last_loop = self._last_mic = 0.0

    def _close_all(self) -> None:
        for stream in self._streams:
            try:
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        if self._own_pa and self._p is not None:
            try:
                self._p.terminate()
            except Exception:
                pass
        self._p = None


# ---------------------------------------------------------------------- #
# Device enumeration (for the UI pickers)                                 #
# ---------------------------------------------------------------------- #

def list_loopback_devices(pa_instance=None) -> list:
    """List WASAPI loopback (output) devices available for capture.

    Returns a list of dicts: {'index': int, 'name': str}. When
    ``pa_instance`` is provided it is used (and never terminated); otherwise
    a temporary PyAudio instance is created and cleaned up.
    """
    devices = []
    if pa is None:
        return devices
    own = pa_instance is None
    p = _new_pa() if own else pa_instance
    try:
        gen = getattr(p, "get_loopback_device_info_generator", None)
        if callable(gen):
            for dev in gen():
                devices.append({"index": int(dev["index"]),
                                "name": dev["name"]})
            return devices
        for api_idx in range(p.get_host_api_count()):
            api = p.get_host_api_info_by_index(api_idx)
            if "WASAPI" not in api.get("name", ""):
                continue
            for dev_idx in range(api.get("deviceCount", 0)):
                dev = p.get_device_info_by_host_api_device_index(api_idx,
                                                                 dev_idx)
                if dev.get("isLoopbackDevice"):
                    devices.append({"index": int(dev["index"]),
                                    "name": dev["name"]})
    except Exception:
        pass
    finally:
        if own:
            try:
                p.terminate()
            except Exception:
                pass
    return devices


def list_input_devices(pa_instance=None) -> list:
    """List physical WASAPI input (microphone) devices.

    Returns a list of dicts: {'index': int, 'name': str}. Accepts an
    optional shared PyAudio instance (never terminated by this function).
    """
    devices = []
    if pa is None:
        return devices
    own = pa_instance is None
    p = _new_pa() if own else pa_instance
    try:
        for api_idx in range(p.get_host_api_count()):
            api = p.get_host_api_info_by_index(api_idx)
            if "WASAPI" not in api.get("name", ""):
                continue
            for dev_idx in range(api.get("deviceCount", 0)):
                dev = p.get_device_info_by_host_api_device_index(api_idx,
                                                                 dev_idx)
                if not dev.get("isLoopbackDevice") \
                        and dev.get("maxInputChannels", 0) > 0:
                    devices.append({"index": int(dev["index"]),
                                    "name": dev["name"]})
    except Exception:
        pass
    finally:
        if own:
            try:
                p.terminate()
            except Exception:
                pass
    return devices
