"""
transcription_engine.py - Local faster-whisper worker for DocuStream.

Transcribes a captured WAV file entirely offline with faster-whisper and
exports a structured Markdown document carrying an ISO 8601 frontmatter
header - the metadata anchor that makes Chronological RAG possible inside
AnythingLLM / LanceDB.

Also implements the Storage Retention Manager: raw ``.wav`` files are bulky
(~10 MB per hour at 24 kHz mono), so they are garbage-collected according to
the configured policy, while Markdown transcripts are preserved forever.
"""

import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


class TranscriptionError(Exception):
    """Raised when the Whisper engine cannot transcribe the given file."""


class TranscriptionEngine:
    """Lazy-loading faster-whisper wrapper (the model loads on first use)."""

    def __init__(self, model_size: str = "small", device: str = "cpu",
                 compute_type: str = "int8", language: str = "auto",
                 beam_size: int = 5):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type if device == "cpu" else "float16" \
            if compute_type == "int8" else compute_type
        self.language = None if language in (None, "", "auto") else language
        self.beam_size = max(1, int(beam_size))
        self._model = None
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------ #

    def _get_model(self, on_status=None):
        """Load (once) and cache the WhisperModel.

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
            np.zeros(16000, dtype=np.float32),  # 1s @ 16 kHz
            vad_filter=False, beam_size=1,
        )
        list(segments)  # execute the lazy generator now

    # ------------------------------------------------------------------ #

    def transcribe_to_markdown(self, wav_path, project: str = "General",
                               tags: str = "", participants: str = "",
                               on_status=None) -> Path:
        """Transcribe ``wav_path`` and write a Markdown transcript next to it.

        Returns the path of the generated ``.md`` file.
        """
        wav_path = Path(wav_path)
        if not wav_path.exists():
            raise TranscriptionError(f"Audio file not found: {wav_path}")

        model = self._get_model(on_status)
        if on_status:
            on_status("Transcribing audio locally (this can take a few "
                      "minutes per hour of audio)...")
        started = time.time()
        try:
            segments_iter, info = model.transcribe(
                str(wav_path),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=True,   # strip long silences before decoding
            )
            segments = list(segments_iter)  # materialize the generator
        except Exception as exc:
            raise TranscriptionError(f"Whisper transcription failed: {exc}") \
                from exc

        duration_min = getattr(info, "duration", 0.0) / 60.0
        detected_lang = getattr(info, "language", "unknown")

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
                started_at=datetime.fromtimestamp(
                    wav_path.stat().st_mtime),
            ),
            encoding="utf-8",
        )

        if on_status:
            elapsed = time.time() - started
            on_status(
                f"Transcription finished in {elapsed:.0f}s "
                f"({len(segments)} segments, language: {detected_lang})."
            )
        return md_path


# ---------------------------------------------------------------------- #
# Markdown / frontmatter builder                                          #
# ---------------------------------------------------------------------- #

def _fmt_ts(seconds: float) -> str:
    """Format seconds as HH:MM:SS for transcript timestamps."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_markdown(segments, wav_name: str, project: str, tags: str,
                    participants: str, duration_min: float,
                    detected_lang: str, engine_label: str,
                    started_at: datetime) -> str:
    """Render the transcript as Markdown with an ISO 8601 frontmatter header.

    The DATE_ISO frontmatter key is what lets a RAG pipeline order documents
    chronologically, so the LLM can reason about decisions *changing over
    time* instead of hallucinating a single flattened truth.
    """
    iso_date = started_at.strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        "---",
        f'TITLE: Meeting Transcript - {project}',
        f"DATE_ISO: {iso_date}",
        f"PROJECT: {project}",
        f"DURATION_MIN: {duration_min:.1f}",
        f"ENGINE: {engine_label}",
        "SOURCE: DocuStream WASAPI Dual Capture",
        f"LANGUAGE: {detected_lang}",
        f"SOURCE_FILE: {wav_name}",
    ]
    if tags.strip():
        lines.append(f"TAGS: {tags.strip()}")
    if participants.strip():
        lines.append(f"PARTICIPANTS: {participants.strip()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Meeting Transcript - {project}")
    lines.append("")
    if participants.strip():
        lines.append(f"**Participants:** {participants.strip()}  ")
    lines.append(f"**Date:** {iso_date}  ")
    lines.append(f"**Duration:** {duration_min:.1f} minutes")
    lines.append("")
    if not segments:
        lines.append("*No speech detected in this recording.*")
    else:
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            lines.append(f"[{_fmt_ts(seg.start)} - {_fmt_ts(seg.end)}] {text}")
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
