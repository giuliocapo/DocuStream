"""
transcription_engine.py - Local stereo faster-whisper worker for DocuStream.

The recorder writes a 24 kHz, 16-bit stereo WAV whose left channel contains
only the physical microphone (``Tu``) and whose right channel contains only
WASAPI loopback (``Teams / Interlocutori``). This module splits those channels,
resamples each one to Whisper's 16 kHz array-input rate, transcribes them with
one shared WhisperModel instance, and merges the resulting segments by time.

The generated Markdown uses strict ISO 8601 metadata plus repeated date/project
anchors. AnythingLLM can therefore retain chronological context even after its
document processor splits a long transcript into smaller vector chunks.

Also implements the Storage Retention Manager: raw stereo ``.wav`` files are
bulky (~330 MiB per hour at 24 kHz), so they are garbage-collected according to
the configured policy, while Markdown transcripts are preserved forever.
"""

import json
import re
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


WHISPER_SAMPLE_RATE = 16000
MIC_SPEAKER = "Tu"
LOOPBACK_SPEAKER = "Teams / Interlocutori"
ANCHOR_MAX_LINES = 15
# AnythingLLM commonly creates ~400-500 word chunks. A lower block ceiling
# makes temporal anchors frequent enough to survive that downstream chunking.
ANCHOR_MAX_WORDS = 300


class TranscriptionError(Exception):
    """Raised when the Whisper engine cannot transcribe the given file."""


@dataclass(frozen=True)
class SpeakerSegment:
    """A Whisper segment with an explicit stereo-channel speaker label."""

    start: float
    end: float
    text: str
    speaker: str


class TranscriptionEngine:
    """Lazy-loading faster-whisper wrapper with stereo speaker separation."""

    def __init__(self, model_size: str = "small", device: str = "cpu",
                 compute_type: str = "int8", language: str = "auto",
                 beam_size: int = 5, initial_prompt: str = ""):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type if device == "cpu" else "float16" \
            if compute_type == "int8" else compute_type
        self.language = None if language in (None, "", "auto") else language
        self.beam_size = max(1, int(beam_size))
        self.initial_prompt = (initial_prompt or "").strip() or None
        self._model = None
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------ #

    def _get_model(self, on_status=None):
        """Load (once) and cache the WhisperModel shared by both channels.

        If the configured backend is CUDA but the CUDA runtime libraries
        (cuBLAS/cuDNN) are missing - extremely common on machines without
        the CUDA toolkit - fall back automatically to CPU/int8 instead of
        failing the whole transcription.
        """
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    "faster-whisper is not installed. Run "
                    "bootstrap_portable.bat or 'pip install faster-whisper'."
                ) from exc

            candidates = [(self.device, self.compute_type)]
            if self.device != "cpu":
                candidates.append(("cpu", "int8"))

            last_error = None
            for device, compute in candidates:
                if on_status:
                    on_status(f"Loading Whisper model '{self.model_size}' "
                              f"on {device}/{compute} ...")
                try:
                    model = WhisperModel(
                        self.model_size, device=device,
                        compute_type=compute,
                    )
                    self._probe_backend(model)
                except Exception as exc:
                    last_error = exc
                    if on_status:
                        on_status(f"Backend {device}/{compute} unavailable: "
                                  f"{exc}. Trying next candidate...")
                    continue
                self._model = model
                if device != self.device and on_status:
                    on_status(
                        f"NOTE: CUDA was selected but its libraries are "
                        f"missing - fell back to CPU/int8 automatically. To "
                        f"use the GPU, install the CUDA 12 runtime or pick "
                        f"'cpu' in Settings."
                    )
                    # Remember the working backend for this session.
                    self.device, self.compute_type = device, compute
                return self._model

            raise TranscriptionError(
                f"Failed to load Whisper model '{self.model_size}': "
                f"{last_error}") from last_error

    @staticmethod
    def _probe_backend(model) -> None:
        """Force one real encoder inference on 1s of silence.

        CTranslate2 loads CUDA libraries (cublas64_12.dll, cuDNN) LAZILY,
        on the first transcription - not at model construction. This probe
        makes missing-runtime errors surface here, where the backend
        fallback can still switch to CPU. vad_filter is disabled so the
        encoder always runs (silence would otherwise be skipped).
        """
        import numpy as np
        segments, _info = model.transcribe(
            np.zeros(WHISPER_SAMPLE_RATE, dtype=np.float32),
            vad_filter=False, beam_size=1,
        )
        list(segments)  # execute the lazy generator now

    # ------------------------------------------------------------------ #

    def _transcribe_channel(self, model, audio, speaker: str, channel: str,
                            on_status=None):
        """Transcribe one mono 16 kHz array using the shared Whisper model."""
        if on_status:
            on_status(f"Transcribing {channel} channel as '{speaker}'...")
        try:
            segments_iter, info = model.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=True,
                initial_prompt=self.initial_prompt,
            )
            raw_segments = list(segments_iter)
        except Exception as exc:
            raise TranscriptionError(
                f"Whisper transcription failed for {channel} channel: {exc}"
            ) from exc

        segments = [
            SpeakerSegment(
                start=float(segment.start),
                end=float(segment.end),
                text=segment.text,
                speaker=speaker,
            )
            for segment in raw_segments
        ]
        return segments, info

    def transcribe_to_markdown(self, wav_path, project: str = "General",
                               tags: str = "", participants: str = "",
                               on_status=None) -> Path:
        """Transcribe a stereo WAV and write a speaker-attributed Markdown file.

        Left/channel 0 is always ``Tu``; right/channel 1 is always ``Teams /
        Interlocutori``. Both calls reuse the same loaded model instance.
        Returns the path of the generated ``.md`` file.
        """
        wav_path = Path(wav_path)
        if not wav_path.exists():
            raise TranscriptionError(f"Audio file not found: {wav_path}")

        left_mic, right_loopback, duration_min = _load_stereo_channels(wav_path)
        model = self._get_model(on_status)
        if on_status:
            on_status("Stereo audio ready. Running two local Whisper passes "
                      "with one shared model...")
        started = time.time()

        mic_segments, mic_info = self._transcribe_channel(
            model, left_mic, MIC_SPEAKER, "left microphone", on_status)
        loop_segments, loop_info = self._transcribe_channel(
            model, right_loopback, LOOPBACK_SPEAKER, "right loopback",
            on_status)

        # Stable sorting preserves left-before-right order for exact ties.
        segments = sorted(mic_segments + loop_segments,
                          key=lambda segment: segment.start)
        languages = []
        for info in (mic_info, loop_info):
            language = getattr(info, "language", None)
            if language and language not in languages:
                languages.append(language)
        detected_lang = "/".join(languages) or "unknown"

        md_path = wav_path.with_suffix(".md")
        md_path.write_text(
            _build_markdown(
                segments=segments,
                wav_name=wav_path.name,
                project=project,
                tags=tags,
                participants=participants,
                duration_min=duration_min,
                detected_lang=detected_lang,
                engine_label=f"faster-whisper-{self.model_size}",
                started_at=datetime.fromtimestamp(wav_path.stat().st_mtime),
            ),
            encoding="utf-8",
        )

        if on_status:
            elapsed = time.time() - started
            on_status(
                f"Stereo transcription finished in {elapsed:.0f}s "
                f"({len(mic_segments)} microphone + {len(loop_segments)} "
                f"loopback segments, language: {detected_lang})."
            )
        return md_path


# ---------------------------------------------------------------------- #
# WAV channel separation and Whisper input conversion                     #
# ---------------------------------------------------------------------- #

def _load_stereo_channels(wav_path: Path):
    """Return two mono float32 arrays at 16 kHz and the duration in minutes.

    faster-whisper assumes that numpy array input is sampled at 16 kHz. The
    recorder intentionally writes 24 kHz WAV files, so each channel must be
    resampled before calling ``model.transcribe`` to preserve correct timing.
    """
    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            raw = wav.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise TranscriptionError(
            f"Could not read WAV audio '{wav_path}': {exc}") from exc

    if channels != 2:
        raise TranscriptionError(
            f"Expected a 2-channel stereo recording, but '{wav_path.name}' "
            f"has {channels} channel(s). Record a new call with the upgraded "
            "audio engine (left = microphone, right = loopback)."
        )
    if sample_width != 2:
        raise TranscriptionError(
            f"Expected 16-bit PCM audio, but '{wav_path.name}' uses "
            f"{sample_width * 8}-bit samples."
        )
    if sample_rate <= 0 or frame_count <= 0:
        raise TranscriptionError("The recording contains no usable audio.")

    try:
        interleaved = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
    except ValueError as exc:
        raise TranscriptionError(
            "The stereo WAV data is truncated or malformed.") from exc

    # Copy each non-contiguous channel view before conversion/resampling.
    left_mic = interleaved[:, 0].astype(np.float32) / 32768.0
    right_loopback = interleaved[:, 1].astype(np.float32) / 32768.0
    left_mic = _resample_to_whisper(left_mic, sample_rate)
    right_loopback = _resample_to_whisper(right_loopback, sample_rate)
    duration_min = frame_count / float(sample_rate) / 60.0
    return left_mic, right_loopback, duration_min


def _resample_to_whisper(samples, sample_rate: int):
    """Resample mono float32 audio to 16 kHz without a SciPy dependency."""
    import numpy as np

    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if samples.size == 0 or sample_rate == WHISPER_SAMPLE_RATE:
        return samples
    target_size = int(round(samples.size * WHISPER_SAMPLE_RATE /
                            float(sample_rate)))
    if target_size <= 0:
        return np.zeros(0, dtype=np.float32)
    source_positions = np.arange(samples.size, dtype=np.float64) / sample_rate
    target_positions = (np.arange(target_size, dtype=np.float64) /
                        WHISPER_SAMPLE_RATE)
    return np.interp(target_positions, source_positions, samples).astype(
        np.float32)


# ---------------------------------------------------------------------- #
# Markdown / frontmatter builder                                          #
# ---------------------------------------------------------------------- #

_ENGLISH_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")
_ITALIAN_DAYS = ("Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì",
                 "Sabato", "Domenica")
_ENGLISH_MONTHS = ("January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November",
                   "December")
_ITALIAN_MONTHS = ("Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio",
                   "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre",
                   "Novembre", "Dicembre")


def _date_labels(started_at: datetime):
    """Build locale-independent English/Italian labels and a compact anchor."""
    english = (f"{_ENGLISH_DAYS[started_at.weekday()]} {started_at.day} "
               f"{_ENGLISH_MONTHS[started_at.month - 1]} {started_at.year}")
    italian = (f"{_ITALIAN_DAYS[started_at.weekday()]} {started_at.day} "
               f"{_ITALIAN_MONTHS[started_at.month - 1]} {started_at.year}")
    return english, italian


def _yaml_string(value) -> str:
    """Return a JSON-quoted string, which is also valid safe YAML syntax."""
    return json.dumps(str(value), ensure_ascii=False)


def _fmt_ts(seconds: float) -> str:
    """Format seconds as MM:SS; minutes may exceed 59 on long recordings."""
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _build_markdown(segments, wav_name: str, project: str, tags: str,
                    participants: str, duration_min: float,
                    detected_lang: str, engine_label: str,
                    started_at: datetime) -> str:
    """Render chronological Markdown with metadata in every logical block.

    Besides the strict ISO frontmatter, a compact temporal anchor is inserted
    at the start of the transcript and then at least every 15 utterances (or
    roughly 300 words). This keeps date and project metadata available to the
    downstream AnythingLLM/LanceDB chunks that no longer contain the header.
    """
    iso_date = started_at.strftime("%Y-%m-%dT%H:%M:%S")
    english_date, italian_date = _date_labels(started_at)
    human_date = f"{english_date} / {italian_date}"
    inline_project = " ".join(str(project).split()) or "General"
    anchor = (f"[{started_at.strftime('%Y-%m-%d')} | {italian_date} | "
              f"Project: {inline_project}]")
    title = f"Meeting Transcript - {project}"

    lines = [
        "---",
        f"TITLE: {_yaml_string(title)}",
        f"published: {iso_date}",
        f"DATE_ISO: {iso_date}",
        f"DATE_HUMAN: {_yaml_string(human_date)}",
        f"PROJECT: {_yaml_string(project)}",
        f"DURATION_MIN: {duration_min:.1f}",
        f"ENGINE: {_yaml_string(engine_label)}",
        "SOURCE: DocuStream WASAPI Stereo Dual Capture",
        f"LANGUAGE: {_yaml_string(detected_lang)}",
        f"SOURCE_FILE: {_yaml_string(wav_name)}",
    ]
    if tags.strip():
        lines.append(f"TAGS: {_yaml_string(tags.strip())}")
    if participants.strip():
        lines.append(f"PARTICIPANTS: {_yaml_string(participants.strip())}")
    lines.extend([
        "---",
        "",
        f"# {title}",
        "",
    ])
    if participants.strip():
        lines.append(f"**Participants:** {participants.strip()}  ")
    lines.extend([
        f"**Date (ISO 8601):** {iso_date}  ",
        f"**Date:** {human_date}  ",
        f"**Duration:** {duration_min:.1f} minutes",
        "",
    ])

    clean_segments = [segment for segment in segments
                      if segment.text.strip()]
    if not clean_segments:
        lines.append("*No speech detected in this recording.*")
    else:
        lines.extend([anchor, ""])
        block_lines = 0
        block_words = 0
        for segment in clean_segments:
            text = segment.text.strip()
            word_count = len(text.split())
            if block_lines and (
                    block_lines >= ANCHOR_MAX_LINES or
                    block_words + word_count > ANCHOR_MAX_WORDS):
                lines.extend(["", anchor, ""])
                block_lines = 0
                block_words = 0
            lines.append(
                f"[{_fmt_ts(segment.start)} - {_fmt_ts(segment.end)}] "
                f"{segment.speaker}: {text}"
            )
            block_lines += 1
            block_words += word_count
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Storage Retention Manager                                               #
# ---------------------------------------------------------------------- #

_WAV_DATE_RE = re.compile(r"^(\d{8})_")  # filenames start with YYYYMMDD_


def apply_retention(recordings_dir, mode: str = "keep_days",
                    days: int = 3, on_status=None) -> int:
    """Delete raw ``.wav`` files that fall outside the retention policy.

    Modes
    -----
    keep_today    : delete every .wav not recorded today.
    keep_days     : delete every .wav older than ``days`` calendar days.
    delete_after_transcribe : delete only .wav files that already have a
                              sibling .md transcript (transcription done).
    keep_forever  : delete nothing.

    Markdown transcripts are NEVER touched. Returns the number of files
    deleted.
    """
    recordings_dir = Path(recordings_dir)
    if not recordings_dir.is_dir():
        return 0

    mode = (mode or "keep_days").lower()
    if mode == "keep_forever":
        return 0

    now = datetime.now()
    today_stamp = now.strftime("%Y%m%d")
    cutoff = now - timedelta(days=max(0, int(days)))
    deleted = 0

    for wav_file in recordings_dir.glob("*.wav"):
        try:
            remove = False
            match = _WAV_DATE_RE.match(wav_file.name)
            file_date = datetime.strptime(match.group(1), "%Y%m%d") \
                if match else datetime.fromtimestamp(wav_file.stat().st_mtime)

            if mode == "keep_today":
                remove = file_date.strftime("%Y%m%d") != today_stamp
            elif mode == "keep_days":
                remove = file_date < cutoff
            elif mode == "delete_after_transcribe":
                remove = wav_file.with_suffix(".md").exists()

            if remove:
                wav_file.unlink()
                deleted += 1
                if on_status:
                    on_status(f"Retention: deleted old recording "
                              f"'{wav_file.name}'.")
        except OSError:
            # A locked or disappearing file must never stop the sweep.
            continue
    return deleted


def transcribe_in_background(engine: TranscriptionEngine, wav_path,
                             project: str, tags: str, participants: str,
                             on_status=None, on_done=None, on_error=None):
    """Run ``transcribe_to_markdown`` on a daemon thread.

    ``on_done(md_path)`` or ``on_error(exception)`` is invoked from the
    worker thread; the caller must marshal results back to the GUI thread.
    """
    def _worker():
        try:
            md_path = engine.transcribe_to_markdown(
                wav_path, project=project, tags=tags,
                participants=participants, on_status=on_status,
            )
        except Exception as exc:
            if on_error:
                on_error(exc)
            return
        if on_done:
            on_done(md_path)

    thread = threading.Thread(target=_worker,
                              name="DocuStream-Transcriber", daemon=True)
    thread.start()
    return thread
