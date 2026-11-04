"""
app.py - DocuStream main GUI application (CustomTkinter).

A 100% private, zero-footprint Windows companion that records any meeting or
lecture (WASAPI loopback + microphone), transcribes it locally with
faster-whisper, and optionally pushes the ISO-annotated Markdown transcript
into an AnythingLLM Workspace for vector embedding.

All cross-thread communication flows through a single ``queue.Queue`` that
the Tk main loop polls every 120 ms. Worker threads never touch GUI objects
directly, which keeps the interface responsive during long transcriptions.

The UI uses a sidebar navigation shell (Recorder / Settings / Guide) with a
hero recorder card, pulsing REC indicator, dual audio-activity meters and a
fully styled in-app guide.
"""

import queue
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from config_manager import ConfigManager, BASE_DIR
from audio_engine import (
    AudioRecorder, LevelMonitor, RecorderError, create_pa_instance,
    list_loopback_devices, list_input_devices,
)
from transcription_engine import (
    TranscriptionEngine, TranscriptionError,
    apply_retention, transcribe_in_background,
)
from anythingllm_client import AnythingLLMClient, AnythingLLMError

APP_TITLE = "DocuStream"
APP_SUBTITLE = "Private Meeting Recorder & RAG Bridge"
POLL_MS = 120
PULSE_MS = 550

# ---------------------------------------------------------------------- #
# Design tokens (light, dark) tuples keep CustomTkinter theme-aware       #
# ---------------------------------------------------------------------- #

ACCENT = "#4f8cff"
ACCENT_HOVER = "#3a76e0"
SIDEBAR_BG = ("#e9ecf3", "#10131a")
CARD_BG = ("#ffffff", "#1a1e27")
CARD_BG_SOFT = ("#f3f5fa", "#141821")
CODE_BG = ("#eceff5", "#0d1017")
GREEN = "#2fa572"
GREEN_HOVER = "#22805a"
RED = "#d0342c"
RED_HOVER = "#a8281f"
AMBER = "#e0a800"
PURPLE = "#8e44ad"
GRAY_BTN = "#6b6b6b"
TEXT_SUB = "gray65"

STATE_COLORS = {
    "IDLE": ACCENT,
    "RECORDING": RED,
    "TRANSCRIBING": AMBER,
    "INGESTING": PURPLE,
    "DONE": GREEN,
    "ERROR": "#7a1f1f",
}

FONT = "Segoe UI"


def _font(size=13, weight="normal", family=FONT):
    return ctk.CTkFont(family=family, size=size, weight=weight)


# ---------------------------------------------------------------------- #
# Styled in-app guide content (rendered as cards, not raw markdown)       #
# ---------------------------------------------------------------------- #

GUIDE_BLOCKS = [
    {
        "type": "hero",
        "title": "📖  User Guide & RAG Architecture",
        "subtitle": "How DocuStream records, transcribes and feeds AnythingLLM "
                    "- and why ISO timestamps matter.",
    },
    {
        "type": "card",
        "icon": "🎙",
        "title": "Standalone Usage (no server needed)",
        "body": [
            ("text", "DocuStream works entirely on its own - no account, no "
                     "cloud, no admin rights. Everything happens inside this "
                     "single portable folder."),
            ("step", "Open the Recorder page."),
            ("step", "Type a Project name (e.g. \"Physics 101\" or \"Sprint "
                     "Planning\"). Optional: add tags and participants - they "
                     "land in the transcript metadata and make retrieval far "
                     "more accurate later."),
            ("step", "Click ● Start Recording. DocuStream captures BOTH the "
                     "system output (what Teams/Zoom/Meet play through your "
                     "speakers) AND your physical microphone, mixed into one "
                     "clean WAV."),
            ("step", "When the call ends, click ■ Finish & Process. "
                     "faster-whisper transcribes the audio completely offline "
                     "and writes a .md file into the recordings/ folder, next "
                     "to the .wav."),
            ("bullet", "Discard Call deletes the recording immediately and "
                       "skips everything."),
            ("bullet", "Save Audio Only keeps the raw .wav without running "
                       "Whisper."),
        ],
    },
    {
        "type": "card",
        "icon": "🛡",
        "title": "Automatic Safety Nets",
        "body": [
            ("bullet", "Silence Watchdog - if system audio stays silent for "
                       "the configured minutes (default 8), recording stops "
                       "gracefully by itself. Your call probably ended."),
            ("bullet", "Hard Timeout - a recording can never exceed the "
                       "configured ceiling (default 120 min), so a forgotten "
                       "recording can't fill your disk."),
            ("bullet", "Retention Manager - old raw .wav files are deleted "
                       "automatically per your storage policy; .md "
                       "transcripts are kept FOREVER."),
        ],
    },
    {
        "type": "card",
        "icon": "🔗",
        "title": "Pairing with AnythingLLM Desktop + LanceDB",
        "body": [
            ("text", "AnythingLLM is a free, local RAG server that stores "
                     "embeddings in LanceDB on your own disk. DocuStream is "
                     "its automated audio ingestion bridge."),
            ("step", "Install AnythingLLM Desktop and enable its Developer "
                     "API (Settings > Tools > Developer API). Generate an "
                     "API key."),
            ("step", "Create one Workspace per project (e.g. "
                     "\"Project-Alpha\")."),
            ("step", "In DocuStream Settings: set the Base URL "
                     "(http://localhost:3001/api/v1), paste the API key, "
                     "enable Auto-upload, and click Test Connection."),
            ("step", "Back on the Recorder page, click Refresh Workspaces "
                     "and pick your Workspace from the Project selector."),
            ("text", "From then on, every Finish & Process records, "
                     "transcribes AND embeds the transcript in one click. If "
                     "AnythingLLM is offline, nothing is lost: the .md stays "
                     "in recordings/ and you get a clear warning - never a "
                     "crash, never silent data loss."),
        ],
    },
    {
        "type": "card",
        "icon": "🕰",
        "title": "Chronological RAG - why ISO timestamps matter",
        "body": [
            ("text", "Every transcript carries an ISO 8601 frontmatter "
                     "header, and every utterance keeps its own "
                     "[HH:MM:SS - HH:MM:SS] window:"),
            ("code", "---\n"
                     "TITLE: Meeting Transcript - Project Alpha\n"
                     "DATE_ISO: 2026-11-04T14:32:10\n"
                     "PROJECT: Project Alpha\n"
                     "DURATION_MIN: 47.5\n"
                     "ENGINE: faster-whisper-small\n"
                     "SOURCE: DocuStream WASAPI Dual Capture\n"
                     "---"),
            ("text", "When LanceDB embeds these documents, the LLM can ORDER "
                     "the meetings in time. That matters enormously:"),
            ("bullet", "Week 1: \"The deadline is March 1st.\""),
            ("bullet", "Week 3: \"Decision changed - the deadline moved to "
                       "April 15th.\""),
            ("text", "Without chronological grounding the LLM retrieves both "
                     "statements and may hallucinate a blended, wrong "
                     "answer. With explicit DATE_ISO anchors, the retriever "
                     "prefers the NEWEST decision: \"April 15th - changed "
                     "during the March 18th meeting.\" Time-aware retrieval "
                     "that prevents stale-decision hallucinations."),
        ],
    },
    {
        "type": "card",
        "icon": "👁",
        "title": "Multimodal & Vision models - what do you actually need?",
        "body": [
            ("text", "Common misconception: you do NOT need a big or a "
                     "Vision model to chat with your transcripts."),
            ("bullet", "Text (and audio turned into text) is just tokens - "
                       "ANY local LLM, even a tiny 1.5B or 3B model, can "
                       "answer questions, summarize and extract action items "
                       "from DocuStream transcripts."),
            ("bullet", "VISION models (e.g. llama3.2-vision) are only "
                       "required when you feed IMAGES into the chat: "
                       "whiteboard photos, architecture diagrams, slide "
                       "screenshots. DocuStream never produces images."),
            ("highlight", "Rule of thumb:  text notes → any LLM works  ·  "
                          "diagrams & screenshots → pick a Vision model "
                          "(per Workspace, in AnythingLLM)."),
        ],
    },
    {
        "type": "card",
        "icon": "🔒",
        "title": "Privacy in one sentence",
        "body": [
            ("text", "No telemetry, no cloud calls, no registry writes, no "
                     "leftover files outside this folder: if your firewall "
                     "blocks DocuStream completely, nothing changes - except "
                     "the optional one-hop POST to your own localhost "
                     "AnythingLLM."),
        ],
    },
]


class DocuStreamApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        ctk.set_appearance_mode(self.config.get("ui.appearance_mode", "dark"))
        ctk.set_default_color_theme(self.config.get("ui.color_theme", "blue"))

        self.title(APP_TITLE)
        self.geometry("1060x760")
        self.minsize(920, 680)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.app_state = "IDLE"
        self.recorder = None
        self.current_wav: Path | None = None
        self.current_md: Path | None = None
        self.polling = True
        self._pulse_job = None
        self._pulse_dim = False
        self._wrap_labels: list = []
        self._loop_devices: list = []
        self._mic_devices: list = []
        # Single shared PortAudio instance, created and used ONLY on the
        # Tk main thread. pyaudiowpatch/PortAudio init and terminate from
        # background threads can wedge the Windows audio stack and freeze
        # the whole app (observed: silent GIL deadlock in native calls).
        self._pa_shared = None
        self._monitor = None
        self._preview_starting = False
        self._meter_target_psys = 0.0
        self._meter_target_pmic = 0.0
        self._toast_count = 0
        # Smoothed meter animation: capture threads set targets, the UI
        # eases the bars toward them every poll tick (feels alive).
        self._meter_target_sys = 0.0
        self._meter_target_mic = 0.0

        # Cross-thread events land here; _poll_queue drains them on the UI
        # thread. Items are tuples: ("kind", payload...).
        self.events: queue.Queue = queue.Queue()

        self._build_ui()
        self._apply_retention_on_startup()
        self._refresh_workspaces(silent=True)
        self._reload_audio_devices()
        self._set_state("IDLE", "Ready. Pick a project and start recording.")

        self.after(POLL_MS, self._poll_queue)

    # ------------------------------------------------------------------ #
    # UI shell: sidebar + pages                                           #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # ---------------- Sidebar --------------------------------------
        sidebar = ctk.CTkFrame(self, width=244, corner_radius=0,
                               fg_color=SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(sidebar, text="◉ DocuStream",
                     font=_font(21, "bold")).grid(
            row=0, column=0, padx=20, pady=(22, 0), sticky="w")
        ctk.CTkLabel(sidebar, text="Private · Local · Portable",
                     font=_font(11), text_color=TEXT_SUB).grid(
            row=1, column=0, padx=20, pady=(0, 18), sticky="w")

        ctk.CTkFrame(sidebar, height=1, fg_color=("gray75", "gray25")).grid(
            row=2, column=0, padx=16, pady=(0, 14), sticky="ew")

        self._nav_buttons = {}
        nav_items = [
            ("recorder", "🎙   Recorder"),
            ("settings", "⚙   Settings"),
            ("guide", "📖   Guide"),
        ]
        for i, (key, label) in enumerate(nav_items, start=3):
            btn = ctk.CTkButton(
                sidebar, text=label, anchor="w", height=42,
                font=_font(14), corner_radius=10, fg_color="transparent",
                text_color=("gray20", "gray85"), hover_color=("gray80",
                                                              "gray22"),
                command=lambda k=key: self._show_page(k))
            btn.grid(row=i, column=0, padx=12, pady=3, sticky="ew")
            self._nav_buttons[key] = btn

        # Status pill pinned to the bottom of the sidebar.
        ctk.CTkLabel(sidebar, text="PIPELINE STATUS", font=_font(9, "bold"),
                     text_color=TEXT_SUB).grid(
            row=7, column=0, padx=20, pady=(0, 4), sticky="w")
        self.status_badge = ctk.CTkLabel(
            sidebar, text=" IDLE ", corner_radius=10,
            font=_font(12, "bold"), fg_color=ACCENT, text_color="white",
            height=30)
        self.status_badge.grid(row=8, column=0, padx=18, pady=(0, 20),
                               sticky="ew")

        # ---------------- Pages ----------------------------------------
        self._page_host = ctk.CTkFrame(self, fg_color="transparent")
        self._page_host.grid(row=0, column=1, sticky="nsew")
        self._page_host.grid_rowconfigure(0, weight=1)
        self._page_host.grid_columnconfigure(0, weight=1)

        self._pages = {}
        for key, builder in (("recorder", self._build_recorder_page),
                             ("settings", self._build_settings_page),
                             ("guide", self._build_guide_page)):
            page = ctk.CTkFrame(self._page_host, fg_color="transparent")
            self._pages[key] = page
            builder(page)

        # One page is gridded at a time (grid/grid_remove is deterministic;
        # stacked frames + tkraise let CTkScrollableFrame canvases leak
        # through and visually crop/overlap sibling pages and the sidebar).
        initial = sys.argv[1] if len(sys.argv) > 1 else "recorder"
        self._show_page(initial if initial in self._pages else "recorder")

    def _show_page(self, key: str):
        for name, page in self._pages.items():
            selected = name == key
            if selected:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()
            btn = self._nav_buttons[name]
            if selected:
                btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER,
                              text_color="white")
            else:
                btn.configure(fg_color="transparent",
                              hover_color=("gray80", "gray22"),
                              text_color=("gray20", "gray85"))
        self._reflow_wrap_labels()

    def _page_title(self, page, title: str, subtitle: str):
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, padx=24, pady=(22, 10), sticky="ew")
        ctk.CTkLabel(head, text=title, font=_font(24, "bold")
                     ).pack(anchor="w")
        ctk.CTkLabel(head, text=subtitle, font=_font(12),
                     text_color=TEXT_SUB).pack(anchor="w", pady=(2, 0))
        return head

    def _card(self, parent, accent=None):
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG,
                            border_width=1,
                            border_color=("gray85", "gray22"))
        if accent:
            strip = ctk.CTkFrame(card, width=4, corner_radius=2,
                                 fg_color=accent)
            strip.grid(row=0, column=0, sticky="ns", padx=(0, 0),
                       pady=(10, 10))
        return card

    # ------------------------- Recorder page -------------------------- #

    def _build_recorder_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(5, weight=1)

        self._page_title(page, "Recorder",
                         "Capture the call, transcribe locally, feed your "
                         "knowledge base.")

        # --- Hero card: REC dot + timer + dual meters -------------------
        hero = self._card(page)
        hero.grid(row=1, column=0, padx=24, pady=6, sticky="ew")
        hero.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(hero, fg_color="transparent")
        left.grid(row=0, column=0, padx=(22, 10), pady=18, sticky="w")

        rec_row = ctk.CTkFrame(left, fg_color="transparent")
        rec_row.pack(anchor="w")
        self.rec_dot = ctk.CTkLabel(rec_row, text="●", font=_font(20, "bold"),
                                    text_color="gray45")
        self.rec_dot.pack(side="left", padx=(0, 6))
        self.rec_status_label = ctk.CTkLabel(rec_row, text="Standby",
                                             font=_font(15, "bold"),
                                             text_color=TEXT_SUB)
        self.rec_status_label.pack(side="left")

        self.timer_label = ctk.CTkLabel(
            left, text="00:00:00",
            font=_font(46, "bold", family="Consolas"))
        self.timer_label.pack(anchor="w", pady=(4, 0))

        meters = ctk.CTkFrame(hero, fg_color="transparent")
        meters.grid(row=0, column=1, padx=(10, 22), pady=18, sticky="ew")
        meters.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(meters, text="🔊  System audio (loopback)",
                     font=_font(11, "bold"), text_color=TEXT_SUB
                     ).grid(row=0, column=0, sticky="w")
        self.activity_sys_var = ctk.DoubleVar(value=0.0)
        ctk.CTkProgressBar(meters, height=10, corner_radius=5,
                           progress_color=ACCENT,
                           variable=self.activity_sys_var
                           ).grid(row=1, column=0, sticky="ew", pady=(2, 10))

        ctk.CTkLabel(meters, text="🎤  Your microphone",
                     font=_font(11, "bold"), text_color=TEXT_SUB
                     ).grid(row=2, column=0, sticky="w")
        self.activity_mic_var = ctk.DoubleVar(value=0.0)
        ctk.CTkProgressBar(meters, height=10, corner_radius=5,
                           progress_color=GREEN,
                           variable=self.activity_mic_var
                           ).grid(row=3, column=0, sticky="ew", pady=(2, 2))

        # --- Call details ------------------------------------------------
        meta = self._card(page)
        meta.grid(row=2, column=0, padx=24, pady=6, sticky="ew")
        meta.grid_columnconfigure(1, weight=1)
        meta.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(meta, text="Call details", font=_font(14, "bold")
                     ).grid(row=0, column=0, columnspan=4, padx=20,
                            pady=(14, 2), sticky="w")

        ctk.CTkLabel(meta, text="Project / Workspace", font=_font(12),
                     text_color=TEXT_SUB).grid(
            row=1, column=0, padx=(20, 8), pady=8, sticky="w")
        self.project_var = ctk.StringVar(
            value=self.config.get("last_project", "General"))
        self.project_combo = ctk.CTkComboBox(
            meta, values=["General"], variable=self.project_var, height=34,
            font=_font(12))
        self.project_combo.grid(row=1, column=1, padx=(0, 8), pady=8,
                                sticky="ew")
        ctk.CTkButton(meta, text="⟳  Refresh Workspaces", height=34,
                      width=170, font=_font(12), fg_color=GRAY_BTN,
                      hover_color="#565656",
                      command=self._on_refresh_workspaces
                      ).grid(row=1, column=2, columnspan=2, padx=(0, 20),
                             pady=8, sticky="w")

        ctk.CTkLabel(meta, text="Tags", font=_font(12), text_color=TEXT_SUB
                     ).grid(row=2, column=0, padx=(20, 8), pady=(0, 8),
                            sticky="w")
        self.tags_var = ctk.StringVar(value=self.config.get("last_tags", ""))
        ctk.CTkEntry(meta, textvariable=self.tags_var, height=34,
                     placeholder_text="budget, q4, hiring"
                     ).grid(row=2, column=1, padx=(0, 8), pady=(0, 8),
                            sticky="ew")

        ctk.CTkLabel(meta, text="Participants", font=_font(12),
                     text_color=TEXT_SUB).grid(
            row=2, column=2, padx=(8, 8), pady=(0, 8), sticky="w")
        self.participants_var = ctk.StringVar(
            value=self.config.get("last_participants", ""))
        ctk.CTkEntry(meta, textvariable=self.participants_var, height=34,
                     placeholder_text="Alice, Bob, Carol"
                     ).grid(row=2, column=3, padx=(0, 20), pady=(0, 8),
                            sticky="ew")

        # --- Audio devices ----------------------------------------------
        devs = self._card(page)
        devs.grid(row=3, column=0, padx=24, pady=6, sticky="ew")
        devs.grid_columnconfigure(1, weight=1)
        devs.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(devs, text="Audio devices", font=_font(14, "bold")
                     ).grid(row=0, column=0, columnspan=5, padx=20,
                            pady=(14, 2), sticky="w")

        ctk.CTkLabel(devs, text="Output to record", font=_font(12),
                     text_color=TEXT_SUB).grid(
            row=1, column=0, padx=(20, 8), pady=(4, 12), sticky="w")
        self.loop_var = ctk.StringVar(value="Auto (default)")
        self.loop_combo = ctk.CTkComboBox(
            devs, values=["Auto (default)"], variable=self.loop_var,
            height=34, font=_font(11), state="readonly", width=280,
            command=lambda _v: self.config.set(
                "audio.loopback", self.loop_combo.get()))
        self.loop_combo.grid(row=1, column=1, padx=(0, 8), pady=(4, 12),
                             sticky="ew")

        ctk.CTkLabel(devs, text="Microphone", font=_font(12),
                     text_color=TEXT_SUB).grid(
            row=1, column=2, padx=(8, 8), pady=(4, 12), sticky="w")
        self.mic_var = ctk.StringVar(value="Auto (default)")
        self.mic_combo = ctk.CTkComboBox(
            devs, values=["Auto (default)"], variable=self.mic_var,
            height=34, font=_font(11), state="readonly", width=240,
            command=lambda _v: self.config.set(
                "audio.microphone", self.mic_combo.get()))
        self.mic_combo.grid(row=1, column=3, padx=(0, 8), pady=(4, 12),
                            sticky="ew")
        ctk.CTkButton(devs, text="⟳", width=38, height=34,
                      font=_font(15), fg_color=GRAY_BTN,
                      hover_color="#565656",
                      command=lambda: self._reload_audio_devices(
                          announce=True)
                      ).grid(row=1, column=4, padx=(0, 20), pady=(4, 12))

        # Live level preview: animated VU meters fed by LevelMonitor so the
        # user can SEE that the chosen devices actually carry audio.
        ctk.CTkLabel(devs, text="Output level", font=_font(11),
                     text_color=TEXT_SUB).grid(
            row=2, column=0, padx=(20, 8), pady=(0, 14), sticky="w")
        self.preview_sys_var = ctk.DoubleVar(value=0.0)
        ctk.CTkProgressBar(devs, height=8, corner_radius=4,
                           progress_color=ACCENT,
                           variable=self.preview_sys_var
                           ).grid(row=2, column=1, padx=(0, 8), pady=(0, 14),
                                  sticky="ew")

        ctk.CTkLabel(devs, text="Mic level", font=_font(11),
                     text_color=TEXT_SUB).grid(
            row=2, column=2, padx=(8, 8), pady=(0, 14), sticky="w")
        self.preview_mic_var = ctk.DoubleVar(value=0.0)
        ctk.CTkProgressBar(devs, height=8, corner_radius=4,
                           progress_color=GREEN,
                           variable=self.preview_mic_var
                           ).grid(row=2, column=3, padx=(0, 8), pady=(0, 14),
                                  sticky="ew")

        self.preview_btn = ctk.CTkButton(
            devs, text="🎚  Preview levels", width=160, height=30,
            font=_font(12), fg_color=GRAY_BTN, hover_color="#565656",
            command=self._on_preview_toggle)
        self.preview_btn.grid(row=2, column=4, padx=(0, 20), pady=(0, 14))

        # --- Controls ----------------------------------------------------
        controls = ctk.CTkFrame(page, fg_color="transparent")
        controls.grid(row=4, column=0, padx=24, pady=6, sticky="ew")
        for col in range(3):
            controls.grid_columnconfigure(col, weight=1)

        self.start_btn = ctk.CTkButton(
            controls, text="●   Start Recording", height=52, corner_radius=12,
            font=_font(16, "bold"), fg_color=GREEN, hover_color=GREEN_HOVER,
            command=self._on_start_stop)
        self.start_btn.grid(row=0, column=0, padx=(0, 8), pady=6, sticky="ew")

        self.save_only_btn = ctk.CTkButton(
            controls, text="💾   Save Audio Only", height=52,
            corner_radius=12, state="disabled", font=_font(14),
            command=self._on_save_audio_only)
        self.save_only_btn.grid(row=0, column=1, padx=8, pady=6, sticky="ew")

        self.discard_btn = ctk.CTkButton(
            controls, text="🗑   Discard Call", height=52, corner_radius=12,
            state="disabled", font=_font(14), fg_color=RED,
            hover_color=RED_HOVER, command=self._on_discard)
        self.discard_btn.grid(row=0, column=2, padx=(8, 0), pady=6,
                              sticky="ew")

        # --- Activity log -------------------------------------------------
        log_card = self._card(page)
        log_card.grid(row=5, column=0, padx=24, pady=(6, 20), sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_card, text="📋  Activity log",
                     font=_font(13, "bold")).grid(
            row=0, column=0, padx=20, pady=(12, 2), sticky="w")
        self.log_box = ctk.CTkTextbox(
            log_card, wrap="word", font=_font(12, family="Consolas"),
            corner_radius=10, fg_color=CARD_BG_SOFT, border_width=1,
            border_color=("gray85", "gray22"))
        self.log_box.grid(row=1, column=0, padx=14, pady=(4, 14),
                          sticky="nsew")
        self.log_box.configure(state="disabled")

    # ------------------------- Settings page -------------------------- #

    def _settings_card(self, parent, row, icon, title, subtitle=""):
        card = self._card(parent)
        card.grid(row=row, column=0, padx=24, pady=6, sticky="ew")
        card.grid_columnconfigure(1, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=4, padx=18, pady=(14, 0),
                  sticky="w")
        ctk.CTkLabel(head, text=f"{icon}  {title}", font=_font(15, "bold")
                     ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(head, text=subtitle, font=_font(11),
                         text_color=TEXT_SUB).pack(anchor="w", pady=(2, 0))
        return card

    def _build_settings_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        self._page_title(page, "Settings",
                         "Engine, safety guards, storage and AnythingLLM "
                         "integration.")

        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        def field(card, r, label, hint=""):
            ctk.CTkLabel(card, text=label, font=_font(12, "bold")).grid(
                row=r, column=0, padx=(22, 12), pady=8, sticky="w")
            if hint:
                ctk.CTkLabel(card, text=hint, font=_font(11),
                             text_color=TEXT_SUB).grid(
                    row=r, column=2, padx=12, pady=8, sticky="w")

        # --- Engine card ---
        engine = self._settings_card(scroll, 0, "🧠", "Transcription Engine",
                                     "faster-whisper, running 100% offline "
                                     "on this machine.")
        field(engine, 1, "Model size")
        self.model_var = ctk.StringVar(
            value=self.config.get("whisper_model", "small"))
        ctk.CTkComboBox(
            engine, values=["tiny", "base", "small", "medium"],
            variable=self.model_var, state="readonly", width=200, height=32,
            command=lambda _v: self._update_model_cache_label()
        ).grid(row=1, column=1, pady=8, sticky="w")

        self.model_cache_label = ctk.CTkLabel(
            engine, text="", font=_font(11), text_color=TEXT_SUB,
            justify="left", anchor="w")
        self.model_cache_label.grid(row=1, column=2, padx=12, pady=8,
                                    sticky="w")
        self._update_model_cache_label()

        field(engine, 2, "Device")
        self.device_var = ctk.StringVar(
            value=self.config.get("whisper_device", "cpu"))
        ctk.CTkComboBox(engine, values=["cpu", "cuda"],
                        variable=self.device_var, state="readonly", width=200,
                        height=32).grid(row=2, column=1, pady=8, sticky="w")

        field(engine, 3, "Compute type",
              "int8 on CPU = ultra-low RAM, ~3-5 min per hour of audio.")
        self.compute_var = ctk.StringVar(
            value=self.config.get("whisper_compute_type", "int8"))
        ctk.CTkComboBox(engine, values=["int8", "int8_float16", "float16",
                                        "float32"], variable=self.compute_var,
                        state="readonly", width=200, height=32
                        ).grid(row=3, column=1, pady=(8, 16), sticky="w")

        # --- Safety guards card ---
        guards = self._settings_card(scroll, 1, "🛡", "Safety Guards",
                                     "Automatic stops that protect your "
                                     "disk and your recordings.")
        field(guards, 1, "Max call duration")
        self.maxdur_var = ctk.DoubleVar(
            value=self.config.get("max_call_duration_min", 120))
        self.maxdur_label = ctk.CTkLabel(
            guards, text=f"{self.maxdur_var.get():.0f} min", width=70,
            font=_font(12, "bold"), text_color=ACCENT)
        ctk.CTkSlider(guards, from_=30, to=240, number_of_steps=14,
                      variable=self.maxdur_var, width=280,
                      progress_color=ACCENT, button_color=ACCENT,
                      command=lambda v: self.maxdur_label.configure(
                          text=f"{v:.0f} min"),
                      ).grid(row=1, column=1, pady=10, sticky="w")
        self.maxdur_label.grid(row=1, column=2, padx=12, sticky="w")

        field(guards, 2, "Silence timeout")
        self.silence_var = ctk.DoubleVar(
            value=self.config.get("silence_timeout_min", 8))
        self.silence_label = ctk.CTkLabel(
            guards, text=f"{self.silence_var.get():.0f} min", width=70,
            font=_font(12, "bold"), text_color=ACCENT)
        ctk.CTkSlider(guards, from_=2, to=20, number_of_steps=18,
                      variable=self.silence_var, width=280,
                      progress_color=ACCENT, button_color=ACCENT,
                      command=lambda v: self.silence_label.configure(
                          text=f"{v:.0f} min"),
                      ).grid(row=2, column=1, pady=10, sticky="w")
        self.silence_label.grid(row=2, column=2, padx=12, sticky="w")

        field(guards, 3, "Silence RMS threshold",
              "Level below which the system audio counts as silent.")
        self.rms_var = ctk.DoubleVar(
            value=self.config.get("silence_rms_threshold", 120.0))
        ctk.CTkSlider(guards, from_=20, to=600, number_of_steps=58,
                      variable=self.rms_var, width=280,
                      progress_color=ACCENT, button_color=ACCENT
                      ).grid(row=3, column=1, pady=(10, 16), sticky="w")

        # --- Storage card ---
        storage = self._settings_card(scroll, 2, "💾", "Storage Retention",
                                      "Raw .wav cleanup. Markdown "
                                      "transcripts are always kept.")
        field(storage, 1, "Retention policy")
        self.retention_var = ctk.StringVar(
            value=self.config.get("retention_mode", "keep_days"))
        ctk.CTkComboBox(
            storage,
            values=["keep_today", "keep_days", "delete_after_transcribe",
                    "keep_forever"],
            variable=self.retention_var, state="readonly", width=240,
            height=32).grid(row=1, column=1, pady=8, sticky="w")

        field(storage, 2, "Retention days")
        self.days_var = ctk.StringVar(
            value=str(self.config.get("retention_days", 3)))
        ctk.CTkEntry(storage, textvariable=self.days_var, width=100,
                     height=32).grid(row=2, column=1, pady=(8, 16),
                                     sticky="w")

        # --- AnythingLLM card ---
        llm = self._settings_card(scroll, 3, "🔗", "AnythingLLM Integration",
                                  "Optional bridge to your local RAG "
                                  "server and LanceDB.")
        field(llm, 1, "Base URL")
        self.llm_url_var = ctk.StringVar(value=self.config.get(
            "anythingllm.base_url", "http://localhost:3001/api/v1"))
        ctk.CTkEntry(llm, textvariable=self.llm_url_var, width=320,
                     height=32).grid(row=1, column=1, pady=8, sticky="w")

        field(llm, 2, "API Key")
        self.llm_key_var = ctk.StringVar(
            value=self.config.get("anythingllm.api_key", ""))
        ctk.CTkEntry(llm, textvariable=self.llm_key_var, width=320,
                     height=32, show="*"
                     ).grid(row=2, column=1, pady=8, sticky="w")

        self.autoupload_var = ctk.BooleanVar(
            value=bool(self.config.get("anythingllm.auto_upload", False)))
        ctk.CTkCheckBox(llm, text="Auto-upload transcripts to the "
                                  "Workspace after transcription",
                        variable=self.autoupload_var, font=_font(12),
                        ).grid(row=3, column=0, columnspan=3, padx=22,
                               pady=10, sticky="w")

        ping_row = ctk.CTkFrame(llm, fg_color="transparent")
        ping_row.grid(row=4, column=0, columnspan=3, padx=22, pady=(0, 16),
                      sticky="w")
        ctk.CTkButton(ping_row, text="⚡  Test Connection", width=170,
                      height=34, fg_color=GRAY_BTN, hover_color="#565656",
                      command=self._on_test_connection
                      ).pack(side="left", padx=(0, 12))
        self.ping_label = ctk.CTkLabel(ping_row, text="●  Not tested",
                                       font=_font(12, "bold"),
                                       text_color=TEXT_SUB)
        self.ping_label.pack(side="left")

        # --- Save button ---
        ctk.CTkButton(scroll, text="✔   Save Settings", height=48,
                      corner_radius=12, font=_font(15, "bold"),
                      fg_color=GREEN, hover_color=GREEN_HOVER,
                      command=self._on_save_settings
                      ).grid(row=4, column=0, padx=24, pady=(10, 16),
                             sticky="ew")

    # --------------------------- Guide page --------------------------- #

    def _build_guide_page(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        scroll.grid_columnconfigure(0, weight=1)

        row = 0
        for block in GUIDE_BLOCKS:
            if block["type"] == "hero":
                hero = self._card(scroll, accent=ACCENT)
                hero.grid(row=row, column=0, padx=24, pady=(22, 6),
                          sticky="ew")
                inner = ctk.CTkFrame(hero, fg_color="transparent")
                inner.grid(row=0, column=1, padx=20, pady=(18, 18),
                           sticky="ew")
                ctk.CTkLabel(inner, text=block["title"],
                             font=_font(22, "bold")).pack(anchor="w")
                self._wrap(inner, block["subtitle"], _font(12), TEXT_SUB,
                           pady=(4, 0))
                row += 1
                continue

            card = self._card(scroll)
            card.grid(row=row, column=0, padx=24, pady=6, sticky="ew")
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.grid(row=0, column=1, padx=20, pady=(14, 16), sticky="ew")
            card.grid_columnconfigure(1, weight=1)
            inner.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(inner,
                         text=f"{block['icon']}   {block['title']}",
                         font=_font(16, "bold")).grid(
                row=0, column=0, sticky="w", pady=(0, 6))

            r = 1
            step_no = 0
            for kind, text in block["body"]:
                if kind == "step":
                    step_no += 1
                    holder = ctk.CTkFrame(inner, fg_color="transparent")
                    holder.grid(row=r, column=0, sticky="ew", pady=3)
                    badge = ctk.CTkLabel(holder, text=f" {step_no} ",
                                         corner_radius=8, width=30,
                                         height=24, font=_font(11, "bold"),
                                         fg_color=ACCENT, text_color="white")
                    badge.pack(side="left", anchor="n", padx=(0, 10))
                    self._wrap(holder, text, _font(12),
                               side="left", pad_x=38)
                elif kind == "bullet":
                    holder = ctk.CTkFrame(inner, fg_color="transparent")
                    holder.grid(row=r, column=0, sticky="ew", pady=2)
                    ctk.CTkLabel(holder, text="•", font=_font(14, "bold"),
                                 text_color=ACCENT
                                 ).pack(side="left", anchor="n",
                                        padx=(2, 8))
                    self._wrap(holder, text, _font(12),
                               side="left", pad_x=22)
                elif kind == "code":
                    code_frame = ctk.CTkFrame(inner, corner_radius=10,
                                              fg_color=CODE_BG,
                                              border_width=1,
                                              border_color=("gray80",
                                                            "gray25"))
                    code_frame.grid(row=r, column=0, sticky="ew",
                                    pady=(8, 8))
                    ctk.CTkLabel(code_frame, text=text, justify="left",
                                 font=_font(12, family="Consolas"),
                                 text_color=("gray15", "#9ecbff")
                                 ).pack(anchor="w", padx=14, pady=10)
                elif kind == "highlight":
                    hl = ctk.CTkFrame(inner, corner_radius=10,
                                      fg_color=("gray90", "#12305c"))
                    hl.grid(row=r, column=0, sticky="ew", pady=(8, 4))
                    self._wrap(hl, text, _font(12, "bold"),
                               pad_in_y=10, pad_in_x=14)
                else:  # plain text
                    self._wrap(inner, text, _font(12), row=r, pady=2)
                r += 1
            row += 1

        scroll.bind("<Configure>", self._reflow_wrap_labels)

    def _wrap(self, parent, text, font, color=None, side=None, row=None,
              pady=(0, 0), pad_x=0, pad_in_x=0, pad_in_y=0):
        """Add a wrap-aware body label and register it for reflow."""
        lbl = ctk.CTkLabel(parent, text=text, font=font, justify="left",
                           anchor="w",
                           text_color=color or ("gray15", "gray85"),
                           wraplength=700)
        if side == "left":
            lbl.pack(side="left", anchor="n", fill="x", expand=True,
                     padx=(0, 0), pady=0)
            self._wrap_labels.append((lbl, pad_x + 60))
        elif row is not None:
            lbl.grid(row=row, column=0, sticky="w", pady=pady)
            self._wrap_labels.append((lbl, pad_x + 60))
        else:  # packed in hero/highlight
            lbl.pack(anchor="w", padx=pad_in_x, pady=pad_in_y, fill="x")
            self._wrap_labels.append((lbl, pad_x + 60))
        return lbl

    def _reflow_wrap_labels(self, event=None):
        """Keep guide text wrapping when the window is resized."""
        try:
            width = self._page_host.winfo_width()
        except Exception:
            return
        if width < 100:
            return
        for lbl, pad in self._wrap_labels:
            try:
                lbl.configure(wraplength=max(300, width - 260 - pad))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    _MODEL_INFO = {
        "tiny":   ("~75 MB",  "fastest, least accurate"),
        "base":   ("~145 MB", "fast, light"),
        "small":  ("~460 MB", "recommended balance"),
        "medium": ("~1.5 GB", "best offline quality"),
    }

    def _update_model_cache_label(self):
        """Show download size + whether the model is already cached locally.

        faster-whisper caches models under the HuggingFace hub directory
        (~/.cache/huggingface/hub/models--Systran--faster-whisper-<size>).
        Nothing is bundled inside DocuStream itself.
        """
        size = self.model_var.get()
        dl, desc = self._MODEL_INFO.get(size, ("?", ""))
        cache_dir = (Path.home() / ".cache" / "huggingface" / "hub" /
                     f"models--Systran--faster-whisper-{size}")
        cached = cache_dir.is_dir() and any(cache_dir.iterdir())
        state = "✔ downloaded, ready offline" if cached \
            else "⭳ will download on first transcription"
        self.model_cache_label.configure(
            text=f"{dl} — {desc}\n{state}")

    def _log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{timestamp}] {message}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _append)

    def _client(self) -> AnythingLLMClient:
        return AnythingLLMClient(
            base_url=self.llm_url_var.get(),
            api_key=self.llm_key_var.get())

    def _set_state(self, state: str, log_line: str = ""):
        self.app_state = state
        # Immediate, unmistakable feedback for the user on every transition.
        if state == "RECORDING":
            self._toast("Recording in progress…", "info")
        elif state == "DONE":
            self._toast("All done ✔", "success")
        elif state == "ERROR":
            short = (log_line[:110] + "…") if len(log_line) > 110 \
                else log_line
            self._toast(short or "Something went wrong", "error")
        def _apply():
            color = STATE_COLORS.get(state, ACCENT)
            self.status_badge.configure(text=f" {state} ", fg_color=color)
            recording = state == "RECORDING"
            # Only the long-running stages disable the button. During
            # RECORDING it MUST stay clickable: it IS the "Finish &
            # Process" button the user presses to end the call.
            processing = state in ("TRANSCRIBING", "INGESTING")
            busy_text = {"TRANSCRIBING": "⏳   Transcribing…",
                         "INGESTING": "⏳   Uploading…"}
            self.start_btn.configure(
                text="■   Finish & Process" if recording
                else busy_text.get(state, "Working...") if processing
                else "●   Start Recording",
                fg_color=RED if recording
                else GRAY_BTN if processing else GREEN,
                hover_color=RED_HOVER if recording else GREEN_HOVER,
                state="disabled" if processing else "normal",
            )
            self.save_only_btn.configure(
                state="normal" if recording else "disabled")
            self.discard_btn.configure(
                state="normal" if recording else "disabled")

            # Hero indicator + pulse handling
            if recording:
                self.rec_status_label.configure(
                    text="Recording in progress", text_color=RED)
                self._start_pulse()
            else:
                self._stop_pulse()
                labels = {"IDLE": "Standby", "TRANSCRIBING": "Transcribing…",
                          "INGESTING": "Uploading to AnythingLLM…",
                          "DONE": "Finished", "ERROR": "Something went wrong"}
                self.rec_status_label.configure(
                    text=labels.get(state, state),
                    text_color=AMBER if state == "ERROR" else TEXT_SUB)
            if not recording and state in ("IDLE", "DONE", "ERROR"):
                self.timer_label.configure(text="00:00:00")
                self.activity_sys_var.set(0.0)
                self.activity_mic_var.set(0.0)
                self._meter_target_sys = 0.0
                self._meter_target_mic = 0.0
        self.after(0, _apply)
        if log_line:
            self._log(log_line)

    def _start_pulse(self):
        self._pulse_dim = False
        if self._pulse_job is None:
            self._pulse_job = self.after(PULSE_MS, self._pulse_rec)

    def _stop_pulse(self):
        if self._pulse_job is not None:
            try:
                self.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None
        self.rec_dot.configure(text_color="gray45")

    def _pulse_rec(self):
        """Blink the REC dot while recording."""
        if self.app_state != "RECORDING":
            self._pulse_job = None
            return
        self._pulse_dim = not self._pulse_dim
        self.rec_dot.configure(
            text_color=RED if self._pulse_dim else "#7a2a26")
        self._pulse_job = self.after(PULSE_MS, self._pulse_rec)

    def _apply_retention_on_startup(self):
        try:
            deleted = apply_retention(
                self.config.recordings_dir,
                mode=self.config.get("retention_mode", "keep_days"),
                days=self.config.get("retention_days", 3))
            if deleted:
                self._log(f"Retention Manager removed {deleted} old "
                          f"recording(s).")
        except Exception as exc:
            self._log(f"Retention Manager warning: {exc}")

    # ------------------------------------------------------------------ #
    # Event queue pump (single entry point for worker -> UI messages)     #
    # ------------------------------------------------------------------ #

    def _poll_queue(self):
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "status":
                    self._log(item[1])
                elif kind == "record_started":
                    self.current_wav = None
                    self.current_md = None
                    self._set_state(
                        "RECORDING",
                        f"Recording started for project '{item[1]}'. "
                        "Loopback + microphone capture active.")
                elif kind == "record_error":
                    self.recorder = None
                    self._set_state("ERROR",
                                    f"Could not start recording: {item[1]}")
                elif kind == "level":
                    loop_rms, mic_rms = item[1], item[2]
                    self._meter_target_sys = min(1.0, loop_rms / 6000.0)
                    self._meter_target_mic = min(1.0, mic_rms / 5000.0)
                    if self.recorder and self.recorder.is_recording:
                        elapsed = int(self.recorder.elapsed_seconds)
                        h, rem = divmod(elapsed, 3600)
                        m, s = divmod(rem, 60)
                        self.timer_label.configure(
                            text=f"{h:02d}:{m:02d}:{s:02d}")
                elif kind == "monitor_level":
                    self._meter_target_psys = min(1.0, item[1] / 6000.0)
                    self._meter_target_pmic = min(1.0, item[2] / 5000.0)
                elif kind == "preview_started":
                    self._preview_starting = False
                    self._monitor, warnings = item[1], item[2]
                    self.preview_btn.configure(
                        text="■   Stop preview", state="normal",
                        fg_color=RED, hover_color=RED_HOVER)
                    for warning in warnings:
                        self._log(f"Level preview warning: {warning}")
                    self._log("Level preview started - the bars now follow "
                              "the live audio on the selected devices.")
                    self._toast("Preview ON - play sound to test"
                                if not warnings
                                else "Preview ON (one device failed)",
                                "success" if not warnings else "warn")
                elif kind == "preview_error":
                    self._preview_starting = False
                    self.preview_btn.configure(
                        text="🎚  Preview levels", state="normal",
                        fg_color=GRAY_BTN, hover_color="#565656")
                    self._log(f"Level preview failed: {item[1]}")
                    self._toast("Preview failed - check Windows Audio",
                                "error")
                elif kind == "auto_stop":
                    self._log(f"Automatic stop: {item[1]}")
                    self._finish_and_process()
                elif kind == "transcribed":
                    self._on_transcribed(item[1])
                elif kind == "transcribe_error":
                    self._set_state("ERROR",
                                    f"Transcription failed: {item[1]}")
                elif kind == "ingested":
                    self._set_state(
                        "DONE",
                        f"Transcript embedded into Workspace "
                        f"'{item[1]}' (location: {item[2]}).")
                elif kind == "ingest_error":
                    self._set_state(
                        "DONE",
                        f"AnythingLLM unavailable: {item[1]} "
                        f"Transcript kept locally: {item[2]}")
                elif kind == "workspaces":
                    self._apply_workspaces(item[1])
                elif kind == "workspaces_error":
                    if not item[2]:
                        self._log(f"Workspace refresh failed: {item[1]}")
                elif kind == "ping":
                    self.ping_label.configure(
                        text="●  ONLINE" if item[1]
                        else "●  OFFLINE / BAD KEY",
                        text_color=GREEN if item[1] else RED)
                    self._toast("AnythingLLM is online", "success") \
                        if item[1] else self._toast(
                            "AnythingLLM offline or bad API key", "error")
        except queue.Empty:
            pass
        # Smooth the activity meters toward their latest targets so the
        # bars animate between the ~15 Hz capture updates.
        for var, attr in ((self.activity_sys_var, "_meter_target_sys"),
                          (self.activity_mic_var, "_meter_target_mic"),
                          (self.preview_sys_var, "_meter_target_psys"),
                          (self.preview_mic_var, "_meter_target_pmic")):
            target = getattr(self, attr)
            current = var.get()
            if abs(target - current) > 0.003:
                var.set(current + (target - current) * 0.35)
        if self.polling:
            self.after(POLL_MS, self._poll_queue)

    # ------------------------------------------------------------------ #
    # Recorder button handlers                                            #
    # ------------------------------------------------------------------ #

    def _on_start_stop(self):
        if self.app_state == "STARTING":
            return  # a start worker is already opening the devices
        if self.app_state == "IDLE" or self.app_state in ("DONE", "ERROR"):
            # Immediate visual feedback: WASAPI device opening can take a
            # moment, and a button that does nothing *looks* broken. The
            # STARTING state also blocks accidental double-clicks, which
            # would otherwise spawn two recording workers fighting over
            # the same audio devices.
            self.app_state = "STARTING"
            self.start_btn.configure(text="⏳   Starting…",
                                     state="disabled")
            self.rec_status_label.configure(text="Opening audio devices…",
                                            text_color=ACCENT)
            self.update_idletasks()
            self.after(30, self._start_recording)
        elif self.app_state == "RECORDING":
            self.start_btn.configure(text="⏳   Finishing…",
                                     state="disabled")
            self.update_idletasks()
            self.after(30, self._finish_and_process)

    def _start_recording(self):
        self._stop_preview()  # the same devices are about to be recorded
        project = self.project_var.get().strip() or "General"
        self.config.set("last_project", project)
        self.config.set("last_tags", self.tags_var.get())
        self.config.set("last_participants", self.participants_var.get())
        self.config.set("audio.loopback", self.loop_var.get())
        self.config.set("audio.microphone", self.mic_var.get())

        loop_idx = self._device_index("loop")
        mic_idx = self._device_index("mic")
        silence_min = int(self.silence_var.get())
        rms_thr = float(self.rms_var.get())
        max_min = int(self.maxdur_var.get())
        pa_shared = self._pa_shared

        def _worker():
            # Device opening runs off the UI thread so a wedged audio
            # driver can never freeze the window; the engine caps each
            # attempt with an internal timeout anyway.
            try:
                recorder = AudioRecorder(
                    silence_timeout_min=silence_min,
                    silence_rms_threshold=rms_thr,
                    max_duration_min=max_min,
                    on_level=lambda l, m: self.events.put(("level", l, m)),
                    on_auto_stop=lambda reason: self.events.put(
                        ("auto_stop", reason)),
                    loopback_index=loop_idx,
                    mic_index=mic_idx,
                    pa_instance=pa_shared,
                )
                recorder.start()
            except RecorderError as exc:
                self.events.put(("record_error", str(exc)))
                return
            except Exception as exc:
                self.events.put(("record_error",
                                 f"Audio backend error: {exc!r}"))
                return
            self.recorder = recorder
            self.events.put(("record_started", project))

        threading.Thread(target=_worker, name="DocuStream-Start",
                         daemon=True).start()

    def _finish_and_process(self):
        """Stop capture, persist the WAV, then transcribe (and ingest)."""
        if not self.recorder:
            return
        recorder, self.recorder = self.recorder, None
        project = self.project_var.get().strip() or "General"
        try:
            self.current_wav = recorder.stop(self.config.recordings_dir,
                                             project)
        except RecorderError as exc:
            self._set_state("ERROR", f"Failed to save recording: {exc}")
            return
        self._set_state("TRANSCRIBING",
                        f"Audio saved: {self.current_wav.name}. "
                        "Starting local Whisper transcription...")
        self._start_transcription_thread()

    def _on_save_audio_only(self):
        if not self.recorder:
            return
        recorder, self.recorder = self.recorder, None
        project = self.project_var.get().strip() or "General"
        try:
            wav_path = recorder.stop(self.config.recordings_dir, project)
            self.current_wav = wav_path
            self._set_state("DONE",
                            f"Audio saved without transcription: "
                            f"{wav_path.name}")
        except RecorderError as exc:
            self._set_state("ERROR", f"Failed to save recording: {exc}")

    def _on_discard(self):
        """Immediately abort the recording and delete everything."""
        if self.recorder:
            self.recorder.discard()
            self.recorder = None
        # Also delete a just-written WAV if one exists and nothing was
        # transcribed from it yet.
        if self.current_wav and self.current_wav.exists() \
                and not self.current_md:
            try:
                self.current_wav.unlink()
                self._log(f"Discarded audio file "
                          f"'{self.current_wav.name}'.")
            except OSError as exc:
                self._log(f"Could not delete audio file: {exc}")
        self.current_wav = None
        self.current_md = None
        self._set_state("IDLE",
                        "Recording discarded. Whisper and AnythingLLM were "
                        "skipped.")
        self._toast("Recording discarded", "warn")

    # ------------------------------------------------------------------ #
    # Transcription pipeline                                              #
    # ------------------------------------------------------------------ #

    def _start_transcription_thread(self):
        engine = TranscriptionEngine(
            model_size=self.model_var.get(),
            device=self.device_var.get(),
            compute_type=self.compute_var.get(),
            language=self.config.get("whisper_language", "auto"),
            beam_size=self.config.get("beam_size", 5),
        )
        transcribe_in_background(
            engine, self.current_wav,
            project=self.project_var.get().strip() or "General",
            tags=self.tags_var.get(),
            participants=self.participants_var.get(),
            on_status=lambda msg: self.events.put(("status", msg)),
            on_done=lambda md: self.events.put(("transcribed", str(md))),
            on_error=lambda exc: self.events.put(
                ("transcribe_error", str(exc))),
        )

    def _on_transcribed(self, md_path_str: str):
        self.current_md = Path(md_path_str)
        self._log(f"Transcript written: {self.current_md.name}")

        # Post-call retention sweep keeps the disk tidy.
        try:
            apply_retention(self.config.recordings_dir,
                            mode=self.retention_var.get(),
                            days=int(self.days_var.get() or 3))
        except Exception as exc:
            self._log(f"Retention Manager warning: {exc}")

        if self.autoupload_var.get():
            self._set_state("INGESTING",
                            "Uploading transcript to AnythingLLM "
                            "workspace...")
            threading.Thread(target=self._ingest_worker,
                             name="DocuStream-Ingest", daemon=True).start()
        else:
            self._set_state("DONE",
                            "Done. Auto-upload is disabled; transcript is "
                            "in the recordings/ folder.")

    def _ingest_worker(self):
        client = self._client()
        target = self.project_var.get().strip()
        try:
            slug = self._resolve_workspace_slug(client, target)
            location = client.ingest_transcript(self.current_md, slug)
        except AnythingLLMError as exc:
            self.events.put(("ingest_error", exc, self.current_md))
            return
        self.events.put(("ingested", target, location))

    # ------------------------------------------------------------------ #
    # AnythingLLM workspace helpers                                       #
    # ------------------------------------------------------------------ #

    def _resolve_workspace_slug(self, client: AnythingLLMClient,
                                name: str) -> str:
        """Map a workspace display name to its slug; fall back to config."""
        for ws in client.get_workspaces():
            if ws["name"].lower() == name.lower():
                return ws["slug"]
        # The name might already be a slug.
        for ws in client.get_workspaces():
            if ws["slug"] == name:
                return ws["slug"]
        fallback = self.config.get("anythingllm.default_workspace", "")
        if fallback:
            return fallback
        raise AnythingLLMError(
            f"Workspace '{name}' not found in AnythingLLM.")

    def _refresh_workspaces(self, silent=False):
        def _worker():
            try:
                workspaces = self._client().get_workspaces()
            except Exception as exc:
                self.events.put(("workspaces_error", str(exc), silent))
                return
            self.events.put(("workspaces", workspaces))
        threading.Thread(target=_worker, name="DocuStream-WS-Fetch",
                         daemon=True).start()

    def _on_refresh_workspaces(self):
        self._log("Fetching workspaces from AnythingLLM...")
        self._refresh_workspaces(silent=False)

    def _apply_workspaces(self, workspaces):
        names = [ws["name"] for ws in workspaces] or ["General"]
        current = self.project_var.get()
        self.project_combo.configure(values=names)
        if current not in names:
            self.project_var.set(names[0])
        self._log(f"Loaded {len(names)} workspace(s) from AnythingLLM.")

    # ------------------------------------------------------------------ #
    # Toast notifications (clear "it worked / it failed" feedback)        #
    # ------------------------------------------------------------------ #

    def _toast(self, message: str, kind: str = "info"):
        """Small auto-hiding popup in the top-right corner confirming the
        result of an action: success / error / warning / info."""
        colors = {"success": GREEN, "error": RED, "warn": AMBER,
                  "info": ACCENT}
        icons = {"success": "✔", "error": "✖", "warn": "⚠", "info": "ℹ"}
        color = colors.get(kind, ACCENT)
        icon = icons.get(kind, "ℹ")

        toast = ctk.CTkFrame(self, corner_radius=10, fg_color=CARD_BG,
                             border_width=2, border_color=color)
        ctk.CTkLabel(toast, text=icon, font=_font(13, "bold"),
                     text_color=color).pack(side="left", padx=(12, 2),
                                            pady=9)
        ctk.CTkLabel(toast, text=message, font=_font(12),
                     wraplength=320, justify="left"
                     ).pack(side="left", padx=(2, 14), pady=9)
        slot = self._toast_count
        self._toast_count = min(self._toast_count + 1, 4)
        toast.place(relx=1.0, x=-16, y=16 + slot * 46, anchor="ne")
        toast.lift()

        def _expire():
            self._toast_count = max(0, self._toast_count - 1)
            try:
                toast.destroy()
            except Exception:
                pass
        self.after(3200, _expire)

    # ------------------------------------------------------------------ #
    # Level preview (animated VU meters in the device card)              #
    # ------------------------------------------------------------------ #

    def _on_preview_toggle(self):
        if self._monitor is not None:
            self._stop_preview()
            self._toast("Audio preview stopped", "info")
            return
        if self._preview_starting:
            return
        self._preview_starting = True
        # Device opening can take seconds (or hang on a wedged driver):
        # do it off the UI thread - the engine caps each attempt at ~8s.
        self.preview_btn.configure(text="⏳   Opening…", state="disabled")
        loop_idx = self._device_index("loop")
        mic_idx = self._device_index("mic")
        pa_shared = self._pa_shared

        def _worker():
            try:
                monitor = LevelMonitor(
                    on_level=lambda l, m: self.events.put(
                        ("monitor_level", l, m)),
                    loopback_index=loop_idx,
                    mic_index=mic_idx,
                    pa_instance=pa_shared,
                )
                warnings = monitor.start()
            except Exception as exc:
                self.events.put(("preview_error", str(exc)))
                return
            self.events.put(("preview_started", monitor, warnings))

        threading.Thread(target=_worker, name="DocuStream-Preview",
                         daemon=True).start()

    def _stop_preview(self):
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                pass
            self._monitor = None
        self._meter_target_psys = 0.0
        self._meter_target_pmic = 0.0
        self.preview_btn.configure(text="🎚  Preview levels",
                                   state="normal",
                                   fg_color=GRAY_BTN, hover_color="#565656")

    # ------------------------------------------------------------------ #
    # Audio device selection                                              #
    # ------------------------------------------------------------------ #

    def _reload_audio_devices(self, announce: bool = False):
        """Enumerate loopback + mic devices on the main thread (fast).

        All PortAudio work happens here on the UI thread via the shared
        instance; enumeration takes a fraction of a second, so no worker
        thread is needed (or safe).
        """
        try:
            if self._pa_shared is None:
                self._pa_shared = create_pa_instance()
            loops = list_loopback_devices(self._pa_shared)
            mics = list_input_devices(self._pa_shared)
        except Exception as exc:
            self._log(f"Audio device enumeration failed: {exc!r}")
            loops, mics = [], []
        self._apply_audio_devices(loops, mics)
        if announce:
            self._log(f"Audio devices refreshed: {len(loops)} output(s), "
                      f"{len(mics)} microphone(s).")
            self._toast(f"{len(loops)} outputs, {len(mics)} mics found",
                        "success" if loops else "warn")

    def _apply_audio_devices(self, loops: list, mics: list):
        self._loop_devices = loops
        self._mic_devices = mics
        loop_names = ["Auto (default)"] + [d["name"] for d in loops]
        mic_names = ["Auto (default)"] + [d["name"] for d in mics]
        saved_loop = self.config.get("audio.loopback", "")
        saved_mic = self.config.get("audio.microphone", "")
        self.loop_combo.configure(values=loop_names)
        self.mic_combo.configure(values=mic_names)
        self.loop_var.set(saved_loop if saved_loop in loop_names
                          else "Auto (default)")
        self.mic_var.set(saved_mic if saved_mic in mic_names
                         else "Auto (default)")

    def _device_index(self, which: str):
        """Resolve the combo selection ('loop' or 'mic') to a device index."""
        name = self.loop_var.get() if which == "loop" else self.mic_var.get()
        if not name or name.startswith("Auto"):
            return None
        devs = self._loop_devices if which == "loop" else self._mic_devices
        for dev in devs:
            if dev["name"] == name:
                return dev["index"]
        return None

    def _on_test_connection(self):
        self.ping_label.configure(text="●  Testing...",
                                  text_color=TEXT_SUB)
        def _worker():
            try:
                ok = self._client().ping()
            except Exception:
                ok = False
            self.events.put(("ping", ok))
        threading.Thread(target=_worker, name="DocuStream-Ping",
                         daemon=True).start()

    # ------------------------------------------------------------------ #
    # Settings + lifecycle                                               #
    # ------------------------------------------------------------------ #

    def _on_save_settings(self):
        try:
            days = int(self.days_var.get())
            if days < 0:
                raise ValueError
        except ValueError:
            self._log("Settings NOT saved: retention days must be a "
                      "non-negative integer.")
            self._toast("Invalid retention days - not saved", "error")
            return
        self.config.set("whisper_model", self.model_var.get())
        self.config.set("whisper_device", self.device_var.get())
        self.config.set("whisper_compute_type", self.compute_var.get())
        self.config.set("max_call_duration_min", int(self.maxdur_var.get()))
        self.config.set("silence_timeout_min", int(self.silence_var.get()))
        self.config.set("silence_rms_threshold", float(self.rms_var.get()))
        self.config.set("retention_mode", self.retention_var.get())
        self.config.set("retention_days", days)
        self.config.set("anythingllm.base_url", self.llm_url_var.get())
        self.config.set("anythingllm.api_key", self.llm_key_var.get())
        self.config.set("anythingllm.auto_upload",
                        bool(self.autoupload_var.get()))
        self._log("Settings saved to config.json.")
        self._toast("Settings saved", "success")

    def _on_close(self):
        """Graceful shutdown: stop recording, persist metadata, exit."""
        self._stop_preview()
        if self.recorder and self.recorder.is_recording:
            try:
                self.recorder.discard()
            except Exception:
                pass
        try:
            self.config.set("last_project", self.project_var.get())
            self.config.set("last_tags", self.tags_var.get())
            self.config.set("last_participants",
                            self.participants_var.get())
            self.config.set("audio.loopback", self.loop_var.get())
            self.config.set("audio.microphone", self.mic_var.get())
        except Exception:
            pass
        self.polling = False
        self.destroy()


def main():
    try:
        app = DocuStreamApp()
        app.mainloop()
    except Exception:
        # Last-resort crash log next to the executable - portable folder
        # stays self-contained.
        crash = BASE_DIR / "docustream_crash.log"
        with open(crash, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== Crash at {datetime.now().isoformat()} ===\n")
            traceback.print_exc(file=fh)
        raise


if __name__ == "__main__":
    sys.exit(main() or 0)
