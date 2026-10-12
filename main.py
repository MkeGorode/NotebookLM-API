"""
NotebookLM Desktop Client
Full-featured GUI for Google NotebookLM via notebooklm-py.
"""

import asyncio
import concurrent.futures
import queue
import re
import threading
from datetime import datetime
import subprocess
import sys
import tkinter
import tkinter.font
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import sync_engine as eng
from core_notebooklm import NotebookLMSync
from sync_engine import PresetStore

# Fluent Design palette shared with the AI Expert Indexer
COLORS = {
    "primary": "#0078D4",
    "primary_hover": "#106EBE",
    "secondary": "#5B5B5B",
    "success": "#107C10",
    "danger": "#D13438",
    "danger_hover": "#A4262C",
    "danger_bg": "#FDE7E9",
    "danger_border": "#F1BBBC",
    "warning": "#CA5010",
    "background": "#FFFFFF",
    "surface": "#F7F7F7",
    "border": "#E1DFDD",
    "selected": "#EFF6FC",
    "selected_strong": "#C7E0F4",
    "text": "#323130",
    "text_secondary": "#605E5C",
}
FONT = "Segoe UI"
TAB_CHAT, TAB_SYNC, TAB_LOG, TAB_SETTINGS = "💬 Чат", "🔄 Синхронизация", "📜 Журнал", "⚙ Настройки"
NOT_CONNECTED = "Сначала подключитесь"
NO_PRESETS = "— нет шаблонов —"
SEND_TEXT = "Отправить ➤"
PRESET_FIELD_NAMES = {"folder": "папка", "notebook_id": "блокнот", "notebook_title": "блокнот",
                      "ask_after": "вопрос после синхронизации", "post_prompt": "вопрос после синхронизации",
                      "storage_path": "storage"}
PHASE_NAMES = {"delete": "Удаление", "upload": "Загрузка", "processing": "Обработка в NotebookLM",
               "ask": "Вопрос в чат"}


def _kind_icon(kind: str) -> str:
    k = kind.lower()
    for key, icon in (("youtube", "▶"), ("web", "🔗"), ("url", "🔗"), ("pdf", "📕"), ("audio", "🎵"),
                      ("image", "🖼"), ("video", "🎬"), ("drive", "☁"), ("text", "📝")):
        if key in k:
            return icon
    return "📄"


# ---------------------------------------------------------------------------
# Async helper – runs coroutines in a background thread so the GUI stays
# responsive.  Every public API call goes through this.
# ---------------------------------------------------------------------------

_async_loop: asyncio.AbstractEventLoop | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived event loop running in a daemon thread."""
    global _async_loop
    if _async_loop is None or _async_loop.is_closed():
        _async_loop = asyncio.new_event_loop()

        def _run(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, args=(_async_loop,), daemon=True)
        t.start()
    return _async_loop


def run_async(coro, on_success=None, on_error=None):
    """Schedule *coro* on the shared loop; call back on the main thread."""
    loop = _ensure_loop()

    async def _wrapper():
        return await coro

    future = asyncio.run_coroutine_threadsafe(_wrapper(), loop)

    def _poll(root: ctk.CTk):
        if future.done():
            exc = concurrent.futures.CancelledError() if future.cancelled() else future.exception()
            if exc:
                if on_error:
                    on_error(exc)
                else:
                    messagebox.showerror("Error", str(exc))
            else:
                if on_success:
                    on_success(future.result())
        else:
            root.after(100, _poll, root)

    return future, _poll


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

def _guard_tk_finalizers(post_to_main):
    """Tk objects must only be finalized on the main thread.

    Python's GC may run on the asyncio worker thread and call Font/Variable/Image.__del__ there,
    which deadlocks Tk. Such finalizers are forwarded to the main thread instead.
    """
    main_thread = threading.main_thread()
    for cls in (tkinter.font.Font, tkinter.Variable, tkinter.Image):
        original = cls.__dict__.get("__del__")
        if original is None or getattr(original, "_main_thread_guard", False):
            continue

        def guarded(self, _orig=original):
            if threading.current_thread() is main_thread:
                _orig(self)
            else:
                post_to_main(lambda: _orig(self))

        guarded._main_thread_guard = True
        cls.__del__ = guarded


class NotebookLMApp(ctk.CTk):
    APP_TITLE = "NotebookLM Desktop Client"

    def __init__(self):
        super().__init__()
        self.title(self.APP_TITLE)
        self.geometry("1180x780")
        self.minsize(1000, 640)
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=COLORS["background"])

        self.client = None
        self.notebooks: list = []
        self.selected_nb_id: str | None = None
        self._folder_to_upload: Path | None = None
        self._selected_source_id: str | None = None
        self._source_rows: dict = {}
        self._sources_placeholder = None
        self._sources_current: list = []
        self._sources_nb: str | None = None
        self._folder_files: list[Path] = []
        self._diff: dict | None = None
        self.presets = PresetStore()
        self._pending_nb: dict | None = None
        self._applying_preset = False
        self._connected_key: str | None = None
        self._clients: dict[str, tuple] = {}
        self._client_locks: dict = {}
        self.jobs: dict[int, dict] = {}
        self._busy_nbs: set[str] = set()
        self._job_seq = 0
        self._jobs_placeholder = None

        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._fonts: dict = {}
        _guard_tk_finalizers(self._ui)
        self._build_ui()
        self._drain_ui()
        self.storage_entry.bind("<FocusOut>", lambda _e: self._autosave(storage_path=self.storage_entry.get().strip()))
        self._restore_last_preset()

    def _ui(self, fn):
        """Thread-safe: run *fn* on the Tk main thread."""
        self._ui_queue.put(fn)

    def _drain_ui(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:  # keep the pump alive
                print(f"UI callback error: {exc}", file=sys.stderr)
        self.after(40, self._drain_ui)

    # ------------------------------------------------------------------ UI --
    # Widget factories keep the Fluent look consistent
    def _font(self, size: int = 12, weight: str = "normal"):
        key = (size, weight)
        font = self._fonts.get(key)
        if font is None:
            font = self._fonts[key] = ctk.CTkFont(family=FONT, size=size, weight=weight)
        return font

    def _panel(self, parent, **kw):
        return ctk.CTkFrame(parent, corner_radius=8, fg_color=kw.pop("fg_color", COLORS["surface"]),
                            border_width=1, border_color=kw.pop("border_color", COLORS["border"]), **kw)

    def _section(self, parent, text: str):
        return ctk.CTkLabel(parent, text=text.upper(), text_color=COLORS["text_secondary"],
                            font=self._font(11, "bold"), anchor="w")

    def _title(self, parent, text: str, size: int = 15):
        return ctk.CTkLabel(parent, text=text, text_color=COLORS["text"], font=self._font(size, "bold"), anchor="w")

    def _label(self, parent, text: str, **kw):
        kw.setdefault("text_color", COLORS["text"])
        kw.setdefault("anchor", "w")
        return ctk.CTkLabel(parent, text=text, font=self._font(kw.pop("size", 12)), **kw)

    def _hint(self, parent, text: str, wrap: int = 560):
        return ctk.CTkLabel(parent, text=text, text_color=COLORS["text_secondary"], font=self._font(11),
                            anchor="w", justify="left", wraplength=wrap)

    def _entry(self, parent, placeholder: str):
        return ctk.CTkEntry(parent, placeholder_text=placeholder, fg_color=COLORS["background"], height=32,
                            border_color=COLORS["border"], text_color=COLORS["text"], font=self._font(12))

    def _combo(self, parent, values, command=None):
        return ctk.CTkComboBox(parent, values=values, state="readonly", command=command, height=32,
                               fg_color=COLORS["background"], border_color=COLORS["border"],
                               button_color=COLORS["primary"], button_hover_color=COLORS["primary_hover"],
                               dropdown_fg_color=COLORS["background"], dropdown_text_color=COLORS["text"],
                               dropdown_hover_color=COLORS["selected"],
                               text_color=COLORS["text"], font=self._font(12), dropdown_font=self._font(12))

    def _btn(self, parent, text: str, command, kind: str = "primary", width: int = 130, height: int = 32):
        styles = {
            "primary": (COLORS["primary"], COLORS["primary_hover"], "white", None),
            "danger": (COLORS["danger"], COLORS["danger_hover"], "white", None),
            "secondary": (COLORS["background"], COLORS["selected"], COLORS["text"], COLORS["border"]),
            "ghost": ("transparent", COLORS["selected"], COLORS["text_secondary"], None),
        }
        fg, hover, txt, border = styles[kind]
        return ctk.CTkButton(parent, text=text, width=width, height=height, command=command, corner_radius=4,
                             fg_color=fg, hover_color=hover, text_color=txt,
                             border_width=1 if border else 0, border_color=border or COLORS["border"],
                             font=self._font(12, "bold" if kind in ("primary", "danger") else "normal"))

    def _textbox(self, parent):
        return ctk.CTkTextbox(parent, wrap="word", state="disabled", corner_radius=6,
                              fg_color=COLORS["background"], text_color=COLORS["text"],
                              border_width=1, border_color=COLORS["border"], font=self._font(13))

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, padx=20, pady=(12, 8), sticky="nsew")
        body.grid_columnconfigure(0, minsize=330)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_sidebar(body)

        self.tabs = ctk.CTkTabview(
            body, corner_radius=8, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"],
            segmented_button_fg_color=COLORS["border"],
            segmented_button_selected_color=COLORS["selected_strong"],
            segmented_button_selected_hover_color=COLORS["selected_strong"],
            segmented_button_unselected_color=COLORS["surface"],
            segmented_button_unselected_hover_color=COLORS["selected"],
            text_color=COLORS["text"], anchor="w",
        )
        self.tabs.grid(row=0, column=1, padx=(12, 0), sticky="nsew")
        self.tabs._segmented_button.configure(font=self._font(12, "bold"), height=32)
        for name in (TAB_CHAT, TAB_SYNC, TAB_LOG, TAB_SETTINGS):
            self.tabs.add(name)
        self._build_chat_tab(self.tabs.tab(TAB_CHAT))
        self._build_sync_tab(self.tabs.tab(TAB_SYNC))
        self._build_log_tab(self.tabs.tab(TAB_LOG))
        self._build_settings_tab(self.tabs.tab(TAB_SETTINGS))
        self.tabs.set(TAB_CHAT)

        self._build_statusbar()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0, height=64)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(header, fg_color=COLORS["border"], height=1, corner_radius=0).grid(
            row=1, column=0, columnspan=3, sticky="ew")

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=20, pady=10, sticky="w")
        ctk.CTkLabel(brand, text="📓", font=ctk.CTkFont(size=26)).pack(side="left", padx=(0, 10))
        names = ctk.CTkFrame(brand, fg_color="transparent")
        names.pack(side="left")
        ctk.CTkLabel(names, text="NotebookLM Desktop Client", text_color=COLORS["text"],
                     font=self._font(17, "bold"), height=22).pack(anchor="w")
        ctk.CTkLabel(names, text="BY MIKE GORODETSKYY", text_color=COLORS["primary"],
                     font=self._font(9, "bold"), height=14).pack(anchor="w")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, padx=20, pady=10, sticky="e")
        self.lbl_auth_status = ctk.CTkLabel(right, text="● Не подключено", text_color=COLORS["text_secondary"],
                                            font=self._font(12, "bold"))
        self.lbl_auth_status.pack(side="left", padx=(0, 14))
        self.btn_test_conn = self._btn(right, "🔌 Подключиться", self._test_connection, "primary", 140)
        self.btn_test_conn.pack(side="left")

    def _build_sidebar(self, body):
        side = self._panel(body)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(7, weight=1)
        pad = {"padx": 14}

        # Template
        self._section(side, "Шаблон").grid(row=0, column=0, pady=(14, 4), sticky="ew", **pad)
        prow = ctk.CTkFrame(side, fg_color="transparent")
        prow.grid(row=1, column=0, sticky="ew", **pad)
        prow.grid_columnconfigure(0, weight=1)
        self.cb_presets = self._combo(prow, self.presets.names() or [NO_PRESETS], self._apply_preset)
        self.cb_presets.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._btn(prow, "＋", self._new_preset, "secondary", 34).grid(row=0, column=1, padx=(0, 4))
        self._btn(prow, "✖", self._delete_preset, "secondary", 34).grid(row=0, column=2)

        # Notebook
        self._section(side, "Блокнот").grid(row=2, column=0, pady=(16, 4), sticky="ew", **pad)
        nrow = ctk.CTkFrame(side, fg_color="transparent")
        nrow.grid(row=3, column=0, sticky="ew", **pad)
        nrow.grid_columnconfigure(0, weight=1)
        self.cb_notebooks = self._combo(nrow, [NOT_CONNECTED], self._on_notebook_user_selected)
        self.cb_notebooks.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_refresh_nb = self._btn(nrow, "⟳", self._refresh_notebooks, "secondary", 34)
        self.btn_refresh_nb.grid(row=0, column=1)

        # Source folder
        self._section(side, "Папка-источник").grid(row=4, column=0, pady=(16, 4), sticky="ew", **pad)
        frow = ctk.CTkFrame(side, fg_color="transparent")
        frow.grid(row=5, column=0, sticky="ew", **pad)
        frow.grid_columnconfigure(0, weight=1)
        self.btn_side_folder = self._btn(frow, "📁  Выбрать папку…", self._choose_folder_upload, "secondary", 120)
        self.btn_side_folder.configure(anchor="w")
        self.btn_side_folder.grid(row=0, column=0, sticky="ew")
        self.lbl_side_diff = ctk.CTkLabel(frow, text="", font=self._font(11), anchor="w", justify="left",
                                          text_color=COLORS["text_secondary"], cursor="hand2")
        self.lbl_side_diff.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.lbl_side_diff.bind("<Button-1>", lambda _e: self.tabs.set(TAB_SYNC))

        # Sources
        shead = ctk.CTkFrame(side, fg_color="transparent")
        shead.grid(row=6, column=0, pady=(12, 4), sticky="ew", **pad)
        shead.grid_columnconfigure(0, weight=1)
        self.lbl_sources = self._section(shead, "Источники")
        self.lbl_sources.grid(row=0, column=0, sticky="w")
        self._btn(shead, "⟳", self._refresh_sources_combo, "ghost", 28, 24).grid(row=0, column=1)

        self.sources_list = ctk.CTkScrollableFrame(side, fg_color=COLORS["background"], corner_radius=6,
                                                   border_width=1, border_color=COLORS["border"])
        self.sources_list.grid(row=7, column=0, sticky="nsew", **pad)
        self.sources_list.grid_columnconfigure(0, weight=1)

        add = ctk.CTkFrame(side, fg_color="transparent")
        add.grid(row=8, column=0, pady=(10, 14), sticky="ew", **pad)
        add.grid_columnconfigure(0, weight=1)
        self.url_entry = self._entry(add, "Ссылка, путь к файлу или папке…")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.url_entry.bind("<Return>", lambda _: self._add_url_source())
        self._btn(add, "Добавить", self._add_url_source, "secondary", 80).grid(row=0, column=1)
        self._btn(add, "📄 Добавить файлы…", self._add_files, "secondary", 120).grid(
            row=1, column=0, columnspan=2, pady=(6, 0), sticky="ew")

        self._render_sources([], NOT_CONNECTED)

    def _build_chat_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.txt_chat = self._textbox(tab)
        self.txt_chat.grid(row=0, column=0, padx=6, pady=(4, 8), sticky="nsew")

        prompt_row = ctk.CTkFrame(tab, fg_color="transparent")
        prompt_row.grid(row=1, column=0, padx=6, pady=(0, 6), sticky="ew")
        prompt_row.grid_columnconfigure(0, weight=1)
        self.prompt_entry = self._entry(prompt_row, "Спросите что-нибудь об источниках блокнота…")
        self.prompt_entry.configure(height=38)
        self.prompt_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.prompt_entry.bind("<Return>", lambda _: self._send_query())
        self.btn_send = self._btn(prompt_row, SEND_TEXT, self._send_query, "primary", 130, 38)
        self.btn_send.grid(row=0, column=1)

        self._chat_append("Подключитесь, выберите блокнот слева и задайте вопрос.\n")

    def _build_sync_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(6, weight=1)
        self._title(tab, "Синхронизация папки с блокнотом").grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")
        self._hint(tab, "Папка и блокнот запоминаются в активном шаблоне. Выбор папки ничего не меняет в блокноте — "
                        "ниже видно, чем они отличаются.", 700).grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")

        folder = self._panel(tab, fg_color=COLORS["background"])
        folder.grid(row=2, column=0, padx=10, sticky="ew")
        folder.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(folder, text="📁", font=ctk.CTkFont(size=22)).grid(row=0, column=0, rowspan=2, padx=(14, 10), pady=(12, 0))
        self.lbl_folder = self._label(folder, "Папка не выбрана", size=13)
        self.lbl_folder.grid(row=0, column=1, sticky="ew", pady=(12, 0))
        self.lbl_folder_info = self._hint(folder, "—", 560)
        self.lbl_folder_info.grid(row=1, column=1, sticky="ew")
        self._btn(folder, "Выбрать…", self._choose_folder_upload, "secondary", 110).grid(
            row=0, column=2, rowspan=2, padx=14, pady=(12, 0))
        self.lbl_diff = ctk.CTkLabel(folder, text="", font=self._font(12, "bold"), anchor="w", justify="left",
                                     wraplength=640, text_color=COLORS["text_secondary"])
        self.lbl_diff.grid(row=2, column=0, columnspan=2, padx=(14, 0), pady=(6, 12), sticky="ew")
        self._btn(folder, "Подробнее", self._show_plan, "ghost", 100, 26).grid(row=2, column=2, padx=14, pady=(6, 12))

        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=3, column=0, padx=10, pady=(12, 0), sticky="ew")
        actions.grid_columnconfigure(1, weight=1)
        rows = (
            ("⇄  Синхронизировать", self._sync_folder, "primary",
             "Быстро: новые и изменённые файлы, самый свежий индекс; удаляет источники без файла."),
            ("♻  Reload", self._reload_folder, "secondary",
             "Полностью: удаляет ВСЕ источники и загружает папку заново."),
            ("▶  Несколько шаблонов…", self._open_multi_dialog, "secondary",
             "Запустить синхронизацию или Reload сразу для нескольких шаблонов параллельно."),
        )
        for i, (text, cmd, kind, hint) in enumerate(rows):
            btn = self._btn(actions, text, cmd, kind, 200, 36)
            btn.grid(row=i, column=0, pady=(0, 8), sticky="w")
            self._hint(actions, hint, 460).grid(row=i, column=1, padx=14, pady=(0, 8), sticky="w")
        self.btn_reload = actions.grid_slaves(row=1, column=0)[0]

        ask = self._panel(tab, fg_color=COLORS["background"])
        ask.grid(row=4, column=0, padx=10, pady=(4, 0), sticky="ew")
        ask.grid_columnconfigure(0, weight=1)
        self.var_ask = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(ask, text="После синхронизации задать вопрос в чат (ждёт обработки новых источников)",
                        variable=self.var_ask, command=self._on_prompt_changed, font=self._font(12, "bold"),
                        text_color=COLORS["text"], fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"],
                        checkbox_width=18, checkbox_height=18).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")
        self._btn(ask, "По умолчанию", self._reset_prompt, "ghost", 110, 24).grid(row=0, column=1, padx=8, pady=(10, 4))
        self.txt_prompt = ctk.CTkTextbox(ask, height=64, wrap="word", corner_radius=4, fg_color=COLORS["surface"],
                                         border_width=1, border_color=COLORS["border"], text_color=COLORS["text"],
                                         font=self._font(12))
        self.txt_prompt.grid(row=1, column=0, columnspan=2, padx=12, sticky="ew")
        self.txt_prompt.insert("1.0", eng.DEFAULT_POST_PROMPT)
        self.txt_prompt.bind("<FocusOut>", lambda _e: self._on_prompt_changed())
        self._hint(ask, "Подстановки: {new_files} — список загруженных файлов, {notebook} — блокнот, {date} — дата. "
                        "Сохраняется в шаблоне.", 640).grid(row=2, column=0, columnspan=2, padx=12, pady=(2, 10), sticky="w")

        jhead = ctk.CTkFrame(tab, fg_color="transparent")
        jhead.grid(row=5, column=0, padx=10, pady=(8, 4), sticky="ew")
        jhead.grid_columnconfigure(0, weight=1)
        self._section(jhead, "Задачи").grid(row=0, column=0, sticky="w")
        self._btn(jhead, "Удалить все источники…", self._delete_all_sources, "ghost", 170, 24).grid(row=0, column=1)

        self.jobs_list = ctk.CTkScrollableFrame(tab, fg_color=COLORS["surface"], corner_radius=6,
                                                border_width=1, border_color=COLORS["border"])
        self.jobs_list.grid(row=6, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self._show_jobs_placeholder()

    def _build_log_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.txt_log = self._textbox(tab)
        self.txt_log.configure(font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_log.grid(row=0, column=0, padx=6, pady=(4, 8), sticky="nsew")
        self._btn(tab, "Очистить журнал", self._clear_log, "secondary", 140).grid(row=1, column=0, padx=6, pady=(0, 6), sticky="e")

    def _build_settings_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self._title(tab, "Аккаунт Google").grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")
        self._hint(tab, "Первый раз нажмите «Войти через браузер», войдите в Google и подтвердите. "
                        "Сессия сохраняется в файл; поле можно оставить пустым для пути по умолчанию.").grid(
            row=1, column=0, padx=10, pady=(0, 8), sticky="w")

        acc = ctk.CTkFrame(tab, fg_color="transparent")
        acc.grid(row=2, column=0, padx=10, sticky="ew")
        acc.grid_columnconfigure(0, weight=1)
        self.storage_entry = self._entry(acc, "~/.notebooklm/storage_state.json (по умолчанию)")
        self.storage_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.btn_browser_login = self._btn(acc, "🌐 Войти через браузер", self._browser_login, "secondary", 180)
        self.btn_browser_login.grid(row=0, column=1)

        ctk.CTkFrame(tab, fg_color=COLORS["border"], height=1).grid(row=3, column=0, padx=10, pady=20, sticky="ew")

        self._title(tab, "Диагностика").grid(row=4, column=0, padx=10, pady=(0, 8), sticky="w")
        dbg = ctk.CTkFrame(tab, fg_color="transparent")
        dbg.grid(row=5, column=0, padx=10, sticky="w")
        for i, (text, cmd) in enumerate((
            ("Проверить API", self._test_api_status),
            ("Тестовый вопрос", self._run_mock_query),
            ("Список источников в журнал", self._list_sources),
            ("Очистить чат", self._clear_chat),
        )):
            self._btn(dbg, text, cmd, "secondary", 210).grid(row=i // 2, column=i % 2, padx=(0, 8), pady=(0, 8), sticky="w")

        ctk.CTkFrame(tab, fg_color=COLORS["border"], height=1).grid(row=6, column=0, padx=10, pady=20, sticky="ew")
        self._title(tab, "Производительность").grid(row=7, column=0, padx=10, pady=(0, 2), sticky="w")
        self._hint(tab, "Сколько файлов загружать/удалять одновременно внутри одной задачи. Больше — быстрее, "
                        "но выше риск ограничений Google. Разные задачи и так идут параллельно.").grid(
            row=8, column=0, padx=10, pady=(0, 8), sticky="w")
        conc = ctk.CTkFrame(tab, fg_color="transparent")
        conc.grid(row=9, column=0, padx=10, sticky="w")
        self._label(conc, "Параллельных операций:").pack(side="left", padx=(0, 8))
        self.cb_concurrency = self._combo(conc, ["1", "2", "4", "6", "8", "12"], self._on_concurrency)
        self.cb_concurrency.configure(width=80)
        self.cb_concurrency.set(str(self.presets.concurrency))
        self.cb_concurrency.pack(side="left")

        self._title(tab, "Индекс-файлы").grid(row=10, column=0, padx=10, pady=(20, 2), sticky="w")
        self._hint(tab, "Файлы, в имени которых есть этот текст, считаются версиями индекса: при синхронизации в "
                        "блокноте остаётся только самый свежий (по дате в имени, иначе по времени изменения).").grid(
            row=11, column=0, padx=10, pady=(0, 8), sticky="w")
        idx = ctk.CTkFrame(tab, fg_color="transparent")
        idx.grid(row=12, column=0, padx=10, sticky="w")
        self._label(idx, "Имя содержит:").pack(side="left", padx=(0, 8))
        self.entry_index = self._entry(idx, "INDEX")
        self.entry_index.configure(width=160)
        self.entry_index.insert(0, self.presets.index_pattern)
        self.entry_index.pack(side="left")
        self.entry_index.bind("<FocusOut>", lambda _e: self._on_index_pattern())
        self.entry_index.bind("<Return>", lambda _e: self._on_index_pattern())

    def _on_index_pattern(self):
        value = self.entry_index.get().strip()
        if value != self.presets.index_pattern:
            self.presets.index_pattern = value
            self._log(f"[SETTINGS] Индекс-файлы: «{value or '—'}»")
            self._update_diff()

    def _prompt_text(self) -> str:
        return self.txt_prompt.get("1.0", "end").strip()

    def _set_prompt_widgets(self, enabled: bool, text: str):
        self.var_ask.set(enabled)
        self.txt_prompt.delete("1.0", "end")
        self.txt_prompt.insert("1.0", text)

    def _on_prompt_changed(self):
        self._autosave(ask_after=bool(self.var_ask.get()), post_prompt=self._prompt_text())

    def _reset_prompt(self):
        self._set_prompt_widgets(bool(self.var_ask.get()), eng.DEFAULT_POST_PROMPT)
        self._on_prompt_changed()

    def _on_concurrency(self, value: str):
        self.presets.concurrency = int(value)
        self._log(f"[SETTINGS] Параллельных операций: {value}")

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=0, height=30)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(bar, text="Готово", text_color=COLORS["text_secondary"],
                                       font=self._font(11), anchor="w")
        self.lbl_status.grid(row=0, column=0, padx=20, pady=4, sticky="ew")
        self.lbl_op_progress = ctk.CTkLabel(bar, text="", text_color=COLORS["text_secondary"], font=self._font(11))
        self.lbl_op_progress.grid(row=0, column=1, padx=(0, 8))
        self.pb_op = ctk.CTkProgressBar(bar, width=220, height=6, progress_color=COLORS["primary"],
                                        fg_color=COLORS["border"])
        self.pb_op.grid(row=0, column=2, padx=(0, 20))
        self.pb_op.set(0)

    # ------------------------------------------------- Status / sources UI --
    def _set_status(self, text: str, kind: str = "muted"):
        color = {"ok": COLORS["success"], "busy": COLORS["warning"], "error": COLORS["danger"]}.get(
            kind, COLORS["text_secondary"])
        self.lbl_auth_status.configure(text=f"● {text}", text_color=color)

    # -------------------------------------------------------- Chat helpers --
    def _chat_append(self, text: str, tag: str = ""):
        self.txt_chat.configure(state="normal")
        self.txt_chat.insert("end", text + "\n")
        self.txt_chat.configure(state="disabled")
        self.txt_chat.see("end")

    def _log(self, text: str):
        """Technical messages go to the Журнал tab; the latest one is mirrored in the status bar."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"{stamp}  {text}\n")
        self.txt_log.configure(state="disabled")
        self.txt_log.see("end")
        self.lbl_status.configure(text=re.sub(r"^\[[^\]]+\]\s*", "", text.strip()))

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _chat_clear(self):
        self.txt_chat.configure(state="normal")
        self.txt_chat.delete("1.0", "end")
        self.txt_chat.configure(state="disabled")

    # --------------------------------------------------- Async scheduling --
    def _schedule(self, coro, on_success=None, on_error=None):
        """Fire-and-forget async task with GUI callbacks."""
        future, poll = run_async(
            coro,
            on_success=on_success,
            on_error=on_error or (lambda e: messagebox.showerror("Ошибка", str(e))),
        )
        self.after(100, poll, self)

    # ---------------------------------------------------- Auth / connect ---
    async def _connect(self, path: str | None = None):
        # NotebookLMSync.connect применяет патч домена notebook.google.com
        sync = NotebookLMSync(path)
        nbs = await sync.connect()
        return sync.client, nbs

    def _test_connection(self):
        self._set_status("Подключение…", "busy")
        self.btn_test_conn.configure(state="disabled")
        path = self.storage_entry.get().strip() or None

        def _ok(result):
            client, nbs = result
            self.client = client
            self._connected_key = path or ""
            self.notebooks = nbs
            self._populate_notebooks(nbs)
            self._set_status(f"Подключено · {len(nbs)} блокнотов", "ok")
            self.btn_test_conn.configure(state="normal", text="⟳ Переподключить", fg_color=COLORS["background"],
                                         hover_color=COLORS["selected"], text_color=COLORS["text"], border_width=1)
            self._log(f"[SYSTEM] Подключено. Блокнотов: {len(nbs)}")

        def _err(exc):
            self._set_status("Ошибка подключения", "error")
            self.btn_test_conn.configure(state="normal")
            self._log(f"[ERROR] {exc}")
            messagebox.showerror("Connection Error", str(exc))

        self._schedule(self._connect(path), on_success=_ok, on_error=_err)

    def _browser_login(self):
        """Вход через Playwright (Edge) на notebook.google.com; сессия сохраняется в storage notebooklm-py."""
        self._set_status("Вход через браузер…", "busy")
        self._log("[SYSTEM] Откроется браузер — войдите в Google, затем подтвердите в окне программы.")
        threading.Thread(target=self._run_login, daemon=True).start()

    def _run_login(self):
        def _confirm() -> bool:
            done, answer = threading.Event(), []

            def _ask():
                answer.append(messagebox.askokcancel(
                    "Вход в NotebookLM",
                    "Войдите в Google в открывшемся браузере.\nКогда увидите список блокнотов — нажмите OK."))
                done.set()

            self._ui(_ask)
            done.wait()
            return answer[0]

        ok, msg = NotebookLMSync.browser_login(wait_callback=_confirm)
        if ok:
            self._ui(lambda: self._log("[SYSTEM] Вход завершён. Подключаюсь…"))
            self._ui(self._test_connection)
        else:
            self._ui(lambda: messagebox.showerror("Login Error", msg))
            self._ui(lambda: self._set_status("Вход не выполнен", "error"))

    # ---------------------------------------------------- Presets ----------
    def _refresh_presets_combo(self, select: str | None = None):
        names = self.presets.names()
        self.cb_presets.configure(values=names or [NO_PRESETS])
        self.cb_presets.set(select if select in names else (names[0] if names else NO_PRESETS))

    def _active_preset(self) -> str | None:
        name = self.cb_presets.get()
        return name if name in self.presets.presets else None

    def _current_preset_data(self) -> dict:
        nb = next((n for n in self.notebooks if n.id == self.selected_nb_id), None)
        return {
            "storage_path": self.storage_entry.get().strip(),
            "folder": str(self._folder_to_upload) if self._folder_to_upload else "",
            "notebook_id": self.selected_nb_id or "",
            "notebook_title": getattr(nb, "title", "") if nb else "",
            "ask_after": bool(self.var_ask.get()),
            "post_prompt": self._prompt_text(),
        }

    def _autosave(self, **fields):
        """Write user changes (folder / notebook / storage) into the active template."""
        name = self._active_preset()
        if not name or self._applying_preset:
            return
        data = self.presets.get(name) or {}
        changed = {k: v for k, v in fields.items() if data.get(k) != v}
        if not changed:
            return
        self.presets.update(name, **changed)
        what = ", ".join(dict.fromkeys(PRESET_FIELD_NAMES[k] for k in changed if k in PRESET_FIELD_NAMES))
        self._log(f"[PRESET] «{name}» сохранён: {what}")

    def _new_preset(self):
        dialog = ctk.CTkInputDialog(
            text="Название нового шаблона.\nВ него запишутся текущие папка, блокнот и путь storage.",
            title="Новый шаблон")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        if name in self.presets.presets and not messagebox.askyesno(
                "Шаблон", f"Шаблон «{name}» уже есть. Перезаписать текущими настройками?"):
            return
        self.presets.put(name, self._current_preset_data())
        self._refresh_presets_combo(name)
        self._log(f"[PRESET] Создан: {name}")

    def _apply_preset(self, name: str | None = None):
        name = name or self.cb_presets.get()
        data = self.presets.get(name)
        if not data:
            return
        self._applying_preset = True
        try:
            if data.get("storage_path"):
                self.storage_entry.delete(0, "end")
                self.storage_entry.insert(0, data["storage_path"])
            elif self.storage_entry.get():
                self.storage_entry.delete(0, "end")
                self.storage_entry._activate_placeholder()

            if data.get("folder"):
                self._set_folder(Path(data["folder"]))
            else:
                self._clear_folder()

            self._set_prompt_widgets(*self.presets.post_prompt(name))
            self._pending_nb = {"id": data.get("notebook_id"), "title": data.get("notebook_title")}
            if self.notebooks:
                self._apply_pending_notebook()
        finally:
            self._applying_preset = False
        self.presets.touch(name)
        self.cb_presets.set(name)
        self._log(f"[PRESET] Применён: {name}")
        if self.client and (data.get("storage_path") or "") != self._connected_key:
            self._log("[PRESET] Другой аккаунт (storage) — переподключаюсь…")
            self._test_connection()

    def _apply_pending_notebook(self):
        pend, self._pending_nb = self._pending_nb, None
        if not pend:
            return
        for nb in self.notebooks:
            if nb.id == pend.get("id") or (pend.get("title") and nb.title == pend["title"]):
                self.cb_notebooks.set(nb.title)
                self._on_notebook_selected(nb.title)
                return
        self._log(f"[PRESET] Блокнот «{pend.get('title') or pend.get('id')}» не найден.")

    def _delete_preset(self):
        name = self._active_preset()
        if not name:
            return
        if messagebox.askyesno("Шаблон", f"Удалить шаблон «{name}»?\nПапка и блокнот не затрагиваются."):
            self.presets.remove(name)
            self._refresh_presets_combo()

    def _restore_last_preset(self):
        self._refresh_presets_combo(self.presets.last)
        if self.presets.last in self.presets.presets:
            self._apply_preset(self.presets.last)

    # ---------------------------------------------------- Notebooks --------
    def _populate_notebooks(self, nbs):
        if not nbs:
            self.cb_notebooks.configure(values=["Нет блокнотов"])
            self.cb_notebooks.set("Нет блокнотов")
            self.selected_nb_id = None
            self._render_sources([], "Нет блокнотов")
            return
        titles = [nb.title for nb in nbs]
        self.cb_notebooks.configure(values=titles)
        if self._pending_nb:
            self._apply_pending_notebook()
            if self.selected_nb_id in {nb.id for nb in nbs}:
                return
        current = next((nb for nb in nbs if nb.id == self.selected_nb_id), nbs[0])
        self.cb_notebooks.set(current.title)
        self._on_notebook_selected(current.title)

    def _on_notebook_selected(self, choice: str):
        idx = self.cb_notebooks.cget("values").index(choice) if choice in self.cb_notebooks.cget("values") else -1
        if 0 <= idx < len(self.notebooks):
            self.selected_nb_id = self.notebooks[idx].id
            self._selected_source_id = None
            self._log(f"[SYSTEM] Выбран блокнот: {self.notebooks[idx].title}")
            self._refresh_sources_combo()

    def _on_notebook_user_selected(self, choice: str):
        self._on_notebook_selected(choice)
        nb = next((n for n in self.notebooks if n.id == self.selected_nb_id), None)
        if nb:
            self._autosave(notebook_id=nb.id, notebook_title=nb.title)

    def _source_id_of(self, src) -> str | None:
        return eng.source_id(src)

    def _source_title_of(self, src) -> str:
        return eng.source_title(src)

    def _source_kind_of(self, src) -> str:
        return str(getattr(src, "kind", "?"))

    def _populate_sources(self, sources: list):
        self._sources_current = list(sources)
        self._sources_nb = self.selected_nb_id
        self._render_sources(self._sources_current)
        self._update_diff()

    def _refresh_sources_combo(self):
        if not self.client or not self.selected_nb_id:
            self._selected_source_id = None
            self._sources_nb = None
            self._render_sources([], NOT_CONNECTED)
            self._update_diff()
            return

        nb_id = self.selected_nb_id
        client = self.client
        self._render_sources([], "Загрузка…")

        async def _fetch():
            return await client.sources.list(nb_id)

        def _ok(sources):
            if nb_id != self.selected_nb_id:
                return
            self._populate_sources(sources)
            self._log(f"[SOURCES] Обновлено: {len(sources)}")

        self._schedule(_fetch(), on_success=_ok)

    def _refresh_notebooks(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала нажмите «Подключиться».")
            return

        async def _fetch():
            return await self.client.notebooks.list()

        def _ok(nbs):
            self.notebooks = nbs
            self._populate_notebooks(nbs)
            self._log(f"[SYSTEM] Список обновлён: {len(nbs)} блокнотов")

        self._schedule(_fetch(), on_success=_ok)

    # ------------------------------------------------------ Sources list ---
    def _render_sources(self, sources: list, placeholder: str | None = None):
        for w in self.sources_list.winfo_children():
            w.destroy()
        self._source_rows = {}
        self._sources_placeholder = None
        if placeholder or not sources:
            self.lbl_sources.configure(text="ИСТОЧНИКИ")
            self._sources_placeholder = self._hint(self.sources_list, placeholder or "В блокноте нет источников", 260)
            self._sources_placeholder.pack(anchor="w", padx=8, pady=10)
            return
        for src in sources:
            self._add_source_row(src)
        if self._selected_source_id in self._source_rows:
            self._select_source(self._selected_source_id)

    def _add_source_row(self, src):
        sid = eng.source_id(src)
        if not sid or sid in self._source_rows:
            return
        if self._sources_placeholder is not None:
            self._sources_placeholder.destroy()
            self._sources_placeholder = None
        title = eng.source_title(src)
        row = ctk.CTkFrame(self.sources_list, fg_color="transparent", corner_radius=4, height=30)
        row.pack(fill="x", pady=1)
        row.grid_columnconfigure(0, weight=1)
        short = title if len(title) <= 34 else title[:33] + "…"
        lbl = ctk.CTkLabel(row, text=f"{_kind_icon(self._source_kind_of(src))}  {short}", anchor="w",
                           text_color=COLORS["text"], font=self._font(12), cursor="hand2")
        lbl.grid(row=0, column=0, padx=(6, 0), sticky="ew")
        delete = ctk.CTkButton(row, text="✕", width=24, height=24, corner_radius=4, fg_color="transparent",
                               hover_color=COLORS["danger_bg"], text_color=COLORS["text_secondary"],
                               font=self._font(12), command=lambda s=sid, t=title: self._delete_source(s, t))
        delete.grid(row=0, column=1, padx=(2, 4))
        for w in (row, lbl):
            w.bind("<Button-1>", lambda _e, s=sid: self._select_source(s))
        self._source_rows[sid] = row
        self._update_sources_count()

    def _remove_source_row(self, sid: str):
        row = self._source_rows.pop(sid, None)
        if row is not None:
            row.destroy()
        self._sources_current = [s for s in self._sources_current if eng.source_id(s) != sid]
        self._update_sources_count()

    def _update_sources_count(self):
        self.lbl_sources.configure(text=f"ИСТОЧНИКИ · {len(self._source_rows)}")

    def _select_source(self, sid: str):
        self._selected_source_id = sid
        for key, row in self._source_rows.items():
            row.configure(fg_color=COLORS["selected"] if key == sid else "transparent")

    # ---------------------------------------------------- Folder & diff ----
    def _set_folder(self, folder: Path):
        self._folder_to_upload = folder
        self._folder_files = eng.folder_files(folder)
        short = folder.name if len(folder.name) <= 26 else folder.name[:25] + "…"
        if not folder.exists():
            self.lbl_folder.configure(text=folder.name, text_color=COLORS["danger"])
            self.lbl_folder_info.configure(text=f"Папка не найдена: {folder}")
            self.btn_side_folder.configure(text=f"⚠  {short} — не найдена", text_color=COLORS["danger"])
        else:
            self.lbl_folder.configure(text=folder.name, text_color=COLORS["text"])
            self.lbl_folder_info.configure(text=f"{folder}  ·  {len(self._folder_files)} поддерживаемых файлов")
            self.btn_side_folder.configure(text=f"📁  {short} · {len(self._folder_files)} файлов",
                                           text_color=COLORS["text"])
        self._update_diff()

    def _clear_folder(self):
        self._folder_to_upload = None
        self._folder_files = []
        self.lbl_folder.configure(text="Папка не выбрана", text_color=COLORS["text"])
        self.lbl_folder_info.configure(text="—")
        self.btn_side_folder.configure(text="📁  Выбрать папку…", text_color=COLORS["text"])
        self._update_diff()

    def _choose_folder_upload(self):
        start = self._folder_to_upload if self._folder_to_upload and self._folder_to_upload.exists() else None
        options = {"initialdir": str(start)} if start else {}
        folder = filedialog.askdirectory(
            parent=self, mustexist=True, **options,
            title="Папка-источник: откройте нужную папку и нажмите «Выбор папки» (файлы здесь не показываются)")
        if not folder:
            return
        self._set_folder(Path(folder))
        self._autosave(folder=str(self._folder_to_upload))
        self._log(f"[FOLDER] {self._folder_to_upload} · {len(self._folder_files)} файлов")
        self.tabs.set(TAB_SYNC)

    def _show_plan(self):
        """Write the exact sync plan (what will be uploaded / deleted) to the Журнал tab."""
        d = self._diff
        if d is None:
            messagebox.showinfo("Синхронизация", "Подключитесь, выберите блокнот и папку — тогда план будет готов.")
            return
        self._log(f"[PLAN] {self._selected_nb_title()} ← {self._folder_to_upload}: "
                  f"загрузить {len(d['upload'])}, удалить {len(d['delete'])}, без изменений остальные")
        for f in d["new"]:
            self._log(f"  + новый: {f.name}")
        for f in d["changed"]:
            self._log(f"  ~ изменён после загрузки (заменится): {f.name}")
        for f in d["index_upload"]:
            self._log(f"  ★ свежий индекс: {f.name}")
        changed = {f.name.lower() for f in d["changed"]}
        index_del = {sid for sid, _ in d["index_delete"]}
        for sid, title in d["delete"]:
            why = ("старая версия индекса" if sid in index_del else
                   "заменится новой версией" if title.strip().lower() in changed else
                   "файла нет в папке / ошибка обработки")
            self._log(f"  − удалить ({why}): {title}")
        for f in d["stale_index"]:
            self._log(f"  · пропущен (старый индекс в папке): {f.name}")
        self.tabs.set(TAB_LOG)

    def _update_diff(self):
        """Show how the chosen folder differs from the notebook (nothing is changed until Reload/Sync)."""
        self._diff = None
        self.lbl_side_diff.configure(text="", text_color=COLORS["text_secondary"])
        if not self._folder_to_upload:
            self.lbl_diff.configure(text="", text_color=COLORS["text_secondary"])
            return
        if not self._folder_files:
            self.lbl_diff.configure(text="В папке нет поддерживаемых файлов.", text_color=COLORS["warning"])
            return
        if not self.selected_nb_id or self._sources_nb != self.selected_nb_id:
            self.lbl_diff.configure(text="Подключитесь и выберите блокнот, чтобы сравнить с папкой.",
                                    text_color=COLORS["text_secondary"])
            return
        d = self._diff = eng.folder_diff(self._folder_files, self._sources_current, self.presets.index_pattern)
        stale = f"\nСтарые версии индекса в папке пропускаются: {len(d['stale_index'])}." if d["stale_index"] else ""
        if not d["upload"] and not d["delete"]:
            self.lbl_diff.configure(text="✓ Блокнот совпадает с папкой, индекс самый свежий." + stale,
                                    text_color=COLORS["success"])
            self.lbl_side_diff.configure(text="✓ совпадает с блокнотом", text_color=COLORS["success"])
            return
        self.lbl_side_diff.configure(text=f"к синхронизации: +{len(d['upload'])} / −{len(d['delete'])}  →",
                                     text_color=COLORS["warning"])
        parts = []
        if d["new"]:
            parts.append(f"+{len(d['new'])} новых")
        if d["changed"]:
            parts.append(f"~{len(d['changed'])} изменённых")
        if d["index_upload"]:
            parts.append("★ индекс → " + ", ".join(f.name for f in d["index_upload"]))
        if d["delete"]:
            parts.append(f"−{len(d['delete'])} к удалению")
        self.lbl_diff.configure(text="Отличия: " + " · ".join(parts) + ". Применятся по «Синхронизировать»." + stale,
                                text_color=COLORS["warning"])

    # ------------------------------------------------------------ Jobs -----
    def _require_notebook(self) -> bool:
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала нажмите «Подключиться».")
            return False
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return False
        return True

    def _selected_nb_title(self) -> str:
        nb = next((n for n in self.notebooks if n.id == self.selected_nb_id), None)
        return nb.title if nb else (self.selected_nb_id or "—")

    async def _client_for(self, storage: str):
        """Connection for a template's account; reuses the current one when the storage path matches."""
        key = storage or ""
        if self.client and key == self._connected_key:
            return self.client, self.notebooks
        lock = self._client_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._clients:
                self._clients[key] = await eng.connect(key or None)
        return self._clients[key]

    def _show_jobs_placeholder(self):
        self._jobs_placeholder = self._hint(self.jobs_list, "Нет задач. Reload и синхронизация разных "
                                                            "шаблонов могут идти одновременно.", 520)
        self._jobs_placeholder.pack(anchor="w", padx=8, pady=8)

    def _start_job(self, kind: str, label: str, nb_id: str | None, nb_title: str, make_coro):
        """Run make_coro(on_event) as an independent background job with its own progress row."""
        if nb_id and nb_id in self._busy_nbs:
            messagebox.showwarning("Занято", f"Для блокнота «{nb_title}» уже выполняется задача.")
            return None
        self._job_seq += 1
        jid = self._job_seq
        if self._jobs_placeholder is not None:
            self._jobs_placeholder.destroy()
            self._jobs_placeholder = None

        row = self._panel(self.jobs_list, fg_color=COLORS["background"])
        row.pack(fill="x", padx=2, pady=3)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=f"{kind} · {label} → {nb_title}", text_color=COLORS["text"],
                     font=self._font(12, "bold"), anchor="w").grid(row=0, column=0, padx=10, pady=(6, 0), sticky="ew")
        status = self._hint(row, "Подготовка…", 480)
        status.grid(row=1, column=0, padx=10, sticky="ew")
        bar = ctk.CTkProgressBar(row, height=6, progress_color=COLORS["primary"], fg_color=COLORS["border"])
        bar.grid(row=2, column=0, columnspan=2, padx=10, pady=(2, 8), sticky="ew")
        bar.set(0)
        btn = self._btn(row, "Отмена", lambda: self._cancel_job(jid), "secondary", 80, 26)
        btn.grid(row=0, column=1, rowspan=2, padx=8, pady=(6, 0))

        job = {"id": jid, "kind": kind, "label": label, "nb_id": nb_id, "nb_title": nb_title,
               "state": "running", "done": 0, "total": 0, "errors": 0,
               "row": row, "status": status, "bar": bar, "btn": btn, "future": None}
        self.jobs[jid] = job
        if nb_id:
            self._busy_nbs.add(nb_id)

        def on_event(ev, **d):
            self._ui(lambda: self._job_event(jid, ev, d))

        future, poll = run_async(make_coro(on_event),
                                 on_success=lambda r: self._ui(lambda: self._job_done(jid, r)),
                                 on_error=lambda e: self._ui(lambda: self._job_failed(jid, e)))
        job["future"] = future
        self.after(100, poll, self)
        self._log(f"[JOB] Старт: {kind} · {label} → {nb_title}")
        self._update_global_progress()
        return jid

    def _job_event(self, jid: int, ev: str, d: dict):
        job = self.jobs.get(jid)
        if not job or job["state"] != "running":
            return
        if ev == "progress":
            job.update(done=d["done"], total=d["total"])
            job["bar"].set(d["done"] / d["total"] if d["total"] else 1)
            job["status"].configure(text=f"{PHASE_NAMES.get(d['phase'], d['phase'])}: {d['done']} из {d['total']}"
                                         + (f" · ошибок {job['errors']}" if job["errors"] else ""))
            self._update_global_progress()
        elif ev == "deleted":
            if job["nb_id"] == self.selected_nb_id:
                self._remove_source_row(d["sid"])
            self._log(f"  🗑 [{job['label']}] {d['title']}")
        elif ev == "uploaded":
            src = d.get("source")
            if src is not None and job["nb_id"] == self.selected_nb_id and self._sources_nb == job["nb_id"]:
                self._sources_current.append(src)
                self._add_source_row(src)
            self._log(f"  ☁ [{job['label']}] {d['name']}")
        elif ev == "error":
            job["errors"] += 1
            self._log(f"  ❌ [{job['label']}] {d['title']}: {d['error']}")

    def _job_done(self, jid: int, result):
        job = self.jobs.get(jid)
        if not job or job["state"] != "running":
            return
        if isinstance(result, dict):
            errors = result["delete_errors"] + result["upload_errors"]
            if result["aborted"]:
                text, color = "⚠ Прервано: не все источники удалены, загрузка не выполнялась.", COLORS["danger"]
            else:
                text = f"✓ Готово: удалено {result['deleted']}, загружено {result['uploaded']}, ошибок {errors}"
                color = COLORS["warning"] if errors else COLORS["success"]
                if result.get("index"):
                    text += " · индекс: " + ", ".join(result["index"])
            qa = result.get("qa")
            if qa:
                self._chat_append(f"\n🔄 После синхронизации · {job['label']} → {job['nb_title']}\n"
                                  f"🧑 {qa['question']}\n🤖 NotebookLM:\n{qa['answer']}\n")
                text += " · ответ в чате 💬"
                if qa.get("pending"):
                    text += f" (ещё обрабатывалось: {qa['pending']})"
            elif result.get("qa_error"):
                text += " · вопрос в чат не удался"
                self._log(f"[CHAT] {job['label']}: {result['qa_error']}")
        else:
            ok, errors = result
            text = f"✓ Готово: успешно {ok}, ошибок {errors}"
            color = COLORS["warning"] if errors else COLORS["success"]
        self._finish_job(job, text, color)

    def _job_failed(self, jid: int, exc: BaseException):
        job = self.jobs.get(jid)
        if not job or job["state"] != "running":
            return
        if isinstance(exc, (asyncio.CancelledError, concurrent.futures.CancelledError)):
            self._finish_job(job, "Отменено пользователем.", COLORS["text_secondary"])
        else:
            self._finish_job(job, f"❌ {exc}", COLORS["danger"])

    def _finish_job(self, job: dict, text: str, color: str):
        job["state"] = "done"
        job["status"].configure(text=text, text_color=color)
        job["bar"].configure(progress_color=color)
        job["bar"].set(1)
        job["btn"].configure(text="Скрыть", command=lambda: self._hide_job(job["id"]))
        if job["nb_id"]:
            self._busy_nbs.discard(job["nb_id"])
        self._log(f"[JOB] {job['kind']} · {job['label']}: {text}")
        self._update_global_progress()
        if job["nb_id"] == self.selected_nb_id:
            self._refresh_sources_combo()

    def _cancel_job(self, jid: int):
        job = self.jobs.get(jid)
        if job and job["state"] == "running" and job["future"]:
            job["status"].configure(text="Отмена…")
            job["future"].cancel()

    def _hide_job(self, jid: int):
        job = self.jobs.pop(jid, None)
        if job:
            job["row"].destroy()
        if not self.jobs:
            self._show_jobs_placeholder()

    def _update_global_progress(self):
        running = [j for j in self.jobs.values() if j["state"] == "running"]
        if not running:
            self.pb_op.set(0)
            self.lbl_op_progress.configure(text="")
            return
        done = sum(j["done"] for j in running)
        total = sum(j["total"] for j in running)
        self.pb_op.set(done / total if total else 0)
        self.lbl_op_progress.configure(text=f"Задач: {len(running)} · {done}/{total}")

    # ----------------------------------------------------- Job launchers ---
    def _add_files(self):
        if not self._require_notebook():
            return
        paths = filedialog.askopenfilenames(
            title="Выберите файлы для загрузки",
            filetypes=[
                ("Все поддерживаемые", "*.pdf *.txt *.md *.docx *.csv *.png *.jpg *.jpeg *.mp3 *.mp4 *.wav"),
                ("PDF", "*.pdf"), ("Text", "*.txt *.md"), ("Word", "*.docx"),
                ("CSV", "*.csv"), ("Images", "*.png *.jpg *.jpeg"),
                ("Audio/Video", "*.mp3 *.mp4 *.wav"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        files = [Path(p) for p in paths]
        client, nb_id, conc = self.client, self.selected_nb_id, self.presets.concurrency
        self._start_job("Загрузка файлов", f"{len(files)} шт.", nb_id, self._selected_nb_title(),
                        lambda ev: eng.upload_many(client, nb_id, files, conc, ev))

    def _add_url_source(self):
        """Add a source from the entry: URL → web source, file path → upload, folder path → source folder.

        With an empty entry the file picker opens instead.
        """
        raw = self.url_entry.get().strip().strip('"')
        if not raw:
            self._add_files()
            return
        local = Path(raw).expanduser()
        if not raw.lower().startswith(("http://", "https://", "www.")) and local.exists():
            self.url_entry.delete(0, "end")
            if local.is_dir():
                self._set_folder(local)
                self._autosave(folder=str(local))
                self._log(f"[FOLDER] {local} · {len(self._folder_files)} файлов")
                self.tabs.set(TAB_SYNC)
                return
            if not self._require_notebook():
                return
            client, nb_id, conc = self.client, self.selected_nb_id, self.presets.concurrency
            self._start_job("Загрузка файлов", local.name, nb_id, self._selected_nb_title(),
                            lambda ev: eng.upload_many(client, nb_id, [local], conc, ev))
            return
        if not self._require_notebook():
            return
        url = raw if "://" in raw else "https://" + raw
        nb_id, client = self.selected_nb_id, self.client

        async def _add():
            return await client.sources.add_url(nb_id, url)

        def _ok(src):
            title = getattr(src, "title", url)
            self._log(f"[SOURCE] URL добавлен: {title}")
            self.url_entry.delete(0, "end")
            if nb_id == self.selected_nb_id:
                self._refresh_sources_combo()

        self._log(f"[URL] Добавление {url}…")
        self._schedule(_add(), on_success=_ok)

    def _list_sources(self):
        if not self._require_notebook():
            return
        nb_id, client = self.selected_nb_id, self.client

        async def _fetch():
            return await client.sources.list(nb_id)

        def _ok(sources):
            self._log(f"[SOURCES] Источники ({len(sources)}):")
            for i, src in enumerate(sources, 1):
                self._log(f"  {i}. [{self._source_kind_of(src)}] {eng.source_title(src)}")

        self._schedule(_fetch(), on_success=_ok)

    def _delete_source(self, src_id: str, title: str):
        if not self._require_notebook():
            return
        if not messagebox.askyesno("Удалить источник", f"Удалить «{title}» из блокнота?"):
            return
        nb_id, client = self.selected_nb_id, self.client

        def _ok(_):
            self._log(f"[DELETE] Удалён: {title}")
            if self._selected_source_id == src_id:
                self._selected_source_id = None
            if nb_id == self.selected_nb_id:
                self._remove_source_row(src_id)
                self._update_diff()

        self._schedule(eng.delete_source(client, nb_id, src_id), on_success=_ok)

    def _delete_all_sources(self):
        if not self._require_notebook():
            return
        title = self._selected_nb_title()
        if not messagebox.askyesno("ОПАСНО", f"Удалить ВСЕ источники в блокноте «{title}»?\nЭто необратимо."):
            return
        client, nb_id, conc = self.client, self.selected_nb_id, self.presets.concurrency
        self._start_job("Удаление всех", self._active_preset() or "—", nb_id, title,
                        lambda ev: eng.wipe_all(client, nb_id, conc, ev))

    def _folder_job_ready(self) -> bool:
        if not self._require_notebook():
            return False
        if not self._folder_to_upload or not self._folder_to_upload.exists():
            messagebox.showwarning("Папка", "Сначала выберите папку.")
            return False
        if not self._folder_files:
            messagebox.showwarning("Папка", "В папке нет поддерживаемых файлов.")
            return False
        return True

    def _sync_job(self, mode: str, files: list[Path], ask: tuple[bool, str], resolve):
        """Job factory for sync/reload; *resolve* is an async callable returning (client, nb_id, nb_title)."""
        conc, pattern = self.presets.concurrency, self.presets.index_pattern
        op = eng.reload if mode == "reload" else eng.sync_delta
        enabled, prompt = ask

        async def _run(on_event):
            client, nb_id, nb_title = await resolve()
            res = await op(client, nb_id, files, conc, on_event, pattern)
            if enabled:
                try:
                    res["qa"] = await eng.ask_after_sync(client, nb_id, nb_title, prompt, res, on_event)
                except Exception as exc:
                    res["qa_error"] = str(exc)
            return res
        return _run

    def _current_resolver(self):
        client, nb_id, title = self.client, self.selected_nb_id, self._selected_nb_title()

        async def _resolve():
            return client, nb_id, title
        return _resolve

    def _reload_folder(self):
        """Delete every source in the notebook, then upload the whole folder."""
        if not self._folder_job_ready():
            return
        files, title = list(self._folder_files), self._selected_nb_title()
        regular, latest, _ = eng.split_index_files(files, self.presets.index_pattern)
        if not messagebox.askyesno(
            "Reload",
            f"Блокнот «{title}»: сначала будут УДАЛЕНЫ все источники ({len(self._sources_current)}),\n"
            f"затем загружено {len(regular) + len(latest)} файлов из папки (только свежий индекс).\n"
            f"Это необратимо. Продолжить?",
        ):
            return
        self._start_job("Reload", self._active_preset() or "—", self.selected_nb_id, title,
                        self._sync_job("reload", files, (bool(self.var_ask.get()), self._prompt_text()),
                                       self._current_resolver()))

    def _sync_folder(self):
        """Upload only new files and delete sources whose file is gone."""
        if not self._folder_job_ready():
            return
        files, title = list(self._folder_files), self._selected_nb_title()
        if self._diff is not None:
            up, dl = len(self._diff["upload"]), len(self._diff["delete"])
            if not up and not dl:
                messagebox.showinfo("Синхронизация", "Блокнот уже совпадает с папкой, индекс самый свежий.")
                return
            if dl and not messagebox.askyesno(
                    "Синхронизация", f"Будет загружено {up} файлов и удалено {dl} источников "
                                     f"(устаревшие, изменённые и старые версии индекса). Продолжить?"):
                return
        self._start_job("Синхронизация", self._active_preset() or "—", self.selected_nb_id, title,
                        self._sync_job("sync", files, (bool(self.var_ask.get()), self._prompt_text()),
                                       self._current_resolver()))

    def _open_multi_dialog(self):
        candidates = [n for n in self.presets.names()
                      if (p := self.presets.get(n)).get("folder") and (p.get("notebook_id") or p.get("notebook_title"))]
        if not candidates:
            messagebox.showinfo("Несколько шаблонов", "Нет шаблонов, в которых заданы и папка, и блокнот.")
            return

        win = ctk.CTkToplevel(self)
        win.title("Запуск по шаблонам")
        win.geometry(f"620x{min(220 + 34 * len(candidates), 620)}")
        win.configure(fg_color=COLORS["background"])
        win.transient(self)
        win.after(150, win.grab_set)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        self._title(win, "Выберите шаблоны — задачи пойдут параллельно").grid(
            row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        box = ctk.CTkScrollableFrame(win, fg_color=COLORS["surface"], corner_radius=6)
        box.grid(row=1, column=0, padx=16, sticky="nsew")
        checks = {}
        for name in candidates:
            p = self.presets.get(name)
            var = ctk.BooleanVar(value=True)
            folder = Path(p["folder"])
            ctk.CTkCheckBox(box, text=f"{name}  —  {p.get('notebook_title') or p.get('notebook_id')}  ←  "
                                      f"{folder.name} ({len(eng.folder_files(folder))} файлов)",
                            variable=var, font=self._font(12), text_color=COLORS["text"],
                            fg_color=COLORS["primary"], hover_color=COLORS["primary_hover"]).pack(
                anchor="w", padx=8, pady=4)
            checks[name] = var

        mode = ctk.StringVar(value="sync")
        modes = ctk.CTkFrame(win, fg_color="transparent")
        modes.grid(row=2, column=0, padx=16, pady=10, sticky="w")
        for value, text in (("sync", "Синхронизировать изменения"), ("reload", "Reload (удалить всё и загрузить)")):
            ctk.CTkRadioButton(modes, text=text, value=value, variable=mode, font=self._font(12),
                               text_color=COLORS["text"], fg_color=COLORS["primary"]).pack(side="left", padx=(0, 16))

        def _start():
            chosen = [n for n, v in checks.items() if v.get()]
            if not chosen:
                return
            if mode.get() == "reload" and not messagebox.askyesno(
                    "Reload", f"В {len(chosen)} блокнотах будут удалены ВСЕ источники и загружены заново. Продолжить?",
                    parent=win):
                return
            win.destroy()
            for n in chosen:
                self._start_preset_job(n, mode.get())
            self.tabs.set(TAB_SYNC)

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=3, column=0, padx=16, pady=(0, 14), sticky="e")
        self._btn(btns, "Отмена", win.destroy, "secondary", 100).pack(side="left", padx=(0, 8))
        self._btn(btns, "Запустить", _start, "primary", 120).pack(side="left")

    def _start_preset_job(self, name: str, mode: str):
        data = self.presets.get(name) or {}
        files = eng.folder_files(data.get("folder"))
        if not files:
            self._log(f"[JOB] «{name}»: папка пуста или не найдена — пропущено.")
            return
        storage = data.get("storage_path") or ""
        ref_id, ref_title = data.get("notebook_id") or "", data.get("notebook_title") or ""

        async def _resolve():
            client, nbs = await self._client_for(storage)
            nb = (eng.resolve_notebook(nbs, ref_id) if ref_id else None) or \
                (eng.resolve_notebook(nbs, ref_title) if ref_title else None)
            if not nb:
                raise RuntimeError(f"Блокнот не найден: {ref_title or ref_id}")
            return client, nb.id, nb.title

        self._start_job("Reload" if mode == "reload" else "Синхронизация", name, ref_id or None,
                        ref_title or ref_id, self._sync_job(mode, files, self.presets.post_prompt(name), _resolve))

    # ---------------------------------------------------- Chat -------------
    def _send_query(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return
        question = self.prompt_entry.get().strip()
        if not question:
            return

        self.prompt_entry.delete(0, "end")
        self._chat_append(f"\n🧑 Вы: {question}")
        self.btn_send.configure(state="disabled", text="⏳ …")

        nb_id = self.selected_nb_id

        async def _ask():
            return await self.client.chat.ask(nb_id, question)

        def _ok(result):
            self._chat_append(f"🤖 NotebookLM:\n{result.answer}\n")
            if result.references:
                refs = ", ".join(f"[{r.citation_number}]" for r in result.references if r.citation_number)
                if refs:
                    self._log(f"   📚 Ссылки: {refs}")
            self.btn_send.configure(state="normal", text="📤 Отправить")

        def _err(exc):
            self._chat_append(f"❌ Ошибка: {exc}")
            self.btn_send.configure(state="normal", text="📤 Отправить")
            messagebox.showerror("Chat Error", str(exc))

        self._schedule(_ask(), on_success=_ok, on_error=_err)

    # ---------------------------------------------------- Debug buttons ----
    def _test_api_status(self):
        """Quick connectivity test — try to list notebooks."""
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return

        self._log("[DEBUG] Проверка API…")

        async def _ping():
            nbs = await self.client.notebooks.list()
            return len(nbs)

        def _ok(count):
            self._log(f"[DEBUG] ✅ API доступен. Блокнотов: {count}")
            messagebox.showinfo("API Status", f"API работает.\nБлокнотов: {count}")

        self._schedule(_ping(), on_success=_ok)

    def _run_mock_query(self):
        """Send a pre-built test question to the selected notebook."""
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return

        mock_q = "Give me a brief summary of the main topics covered in the sources."
        self._chat_append(f"\n🧪 [MOCK QUERY]: {mock_q}")
        self.btn_send.configure(state="disabled", text="⏳ …")

        nb_id = self.selected_nb_id

        async def _ask():
            return await self.client.chat.ask(nb_id, mock_q)

        def _ok(result):
            self._chat_append(f"🤖 NotebookLM:\n{result.answer}\n")
            self.btn_send.configure(state="normal", text="📤 Отправить")

        def _err(exc):
            self._chat_append(f"❌ Mock query error: {exc}")
            self.btn_send.configure(state="normal", text="📤 Отправить")
            messagebox.showerror("Mock Query Error", str(exc))

        self._schedule(_ask(), on_success=_ok, on_error=_err)

    def _clear_chat(self):
        self._chat_clear()
        self._log("[SYSTEM] Лог очищен.\n")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

def main():
    app = NotebookLMApp()
    app.mainloop()
    # Clean up the async loop on exit
    if _async_loop and _async_loop.is_running():
        _async_loop.call_soon_threadsafe(_async_loop.stop)


if __name__ == "__main__":
    main()
