"""
NotebookLM Desktop Client
Full-featured GUI for Google NotebookLM via notebooklm-py.
"""

import asyncio
import json
import queue
import re
import threading
from datetime import datetime
import subprocess
import sys
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core_notebooklm import NotebookLMSync

_SUPPORTED_UPLOAD_EXTS = {
    ".pdf", ".txt", ".md", ".docx", ".csv",
    ".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".wav",
}

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


def _kind_icon(kind: str) -> str:
    k = kind.lower()
    for key, icon in (("youtube", "▶"), ("web", "🔗"), ("url", "🔗"), ("pdf", "📕"), ("audio", "🎵"),
                      ("image", "🖼"), ("video", "🎬"), ("drive", "☁"), ("text", "📝")):
        if key in k:
            return icon
    return "📄"
PRESETS_FILE = Path(__file__).with_name("presets.json")


class PresetStore:
    """Named templates: folder + notebook + settings, persisted as JSON."""

    def __init__(self, path: Path = PRESETS_FILE):
        self.path = path
        self.presets: dict[str, dict] = {}
        self.last: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.presets = dict(data.get("presets", {}))
            self.last = data.get("last")
        except (OSError, ValueError):
            pass

    def _save(self):
        payload = {"presets": self.presets, "last": self.last}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def names(self) -> list[str]:
        return sorted(self.presets, key=str.lower)

    def put(self, name: str, data: dict):
        self.presets[name] = data
        self.last = name
        self._save()

    def remove(self, name: str):
        self.presets.pop(name, None)
        if self.last == name:
            self.last = None
        self._save()

    def touch(self, name: str):
        self.last = name
        self._save()


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
            exc = future.exception()
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
        self.presets = PresetStore()
        self._pending_nb: dict | None = None

        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._build_ui()
        self._drain_ui()
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
        return ctk.CTkFont(family=FONT, size=size, weight=weight)

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
        side.grid_rowconfigure(6, weight=1)
        pad = {"padx": 14}

        # Template
        self._section(side, "Шаблон").grid(row=0, column=0, pady=(14, 4), sticky="ew", **pad)
        prow = ctk.CTkFrame(side, fg_color="transparent")
        prow.grid(row=1, column=0, sticky="ew", **pad)
        prow.grid_columnconfigure(0, weight=1)
        self.cb_presets = self._combo(prow, self.presets.names() or [NO_PRESETS], self._apply_preset)
        self.cb_presets.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._btn(prow, "💾", self._save_preset, "secondary", 34).grid(row=0, column=1, padx=(0, 4))
        self._btn(prow, "✖", self._delete_preset, "secondary", 34).grid(row=0, column=2)

        # Notebook
        self._section(side, "Блокнот").grid(row=2, column=0, pady=(16, 4), sticky="ew", **pad)
        nrow = ctk.CTkFrame(side, fg_color="transparent")
        nrow.grid(row=3, column=0, sticky="ew", **pad)
        nrow.grid_columnconfigure(0, weight=1)
        self.cb_notebooks = self._combo(nrow, [NOT_CONNECTED], self._on_notebook_selected)
        self.cb_notebooks.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_refresh_nb = self._btn(nrow, "⟳", self._refresh_notebooks, "secondary", 34)
        self.btn_refresh_nb.grid(row=0, column=1)

        # Sources
        shead = ctk.CTkFrame(side, fg_color="transparent")
        shead.grid(row=4, column=0, pady=(16, 4), sticky="ew", **pad)
        shead.grid_columnconfigure(0, weight=1)
        self.lbl_sources = self._section(shead, "Источники")
        self.lbl_sources.grid(row=0, column=0, sticky="w")
        self._btn(shead, "⟳", self._refresh_sources_combo, "ghost", 28, 24).grid(row=0, column=1)

        self.sources_list = ctk.CTkScrollableFrame(side, fg_color=COLORS["background"], corner_radius=6,
                                                   border_width=1, border_color=COLORS["border"])
        self.sources_list.grid(row=6, column=0, sticky="nsew", **pad)
        self.sources_list.grid_columnconfigure(0, weight=1)

        add = ctk.CTkFrame(side, fg_color="transparent")
        add.grid(row=7, column=0, pady=(10, 14), sticky="ew", **pad)
        add.grid_columnconfigure(0, weight=1)
        self.url_entry = self._entry(add, "Вставьте ссылку и нажмите Enter")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.url_entry.bind("<Return>", lambda _: self._add_url_source())
        self._btn(add, "＋", self._add_url_source, "secondary", 34).grid(row=0, column=1)
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
        self._title(tab, "Синхронизация папки с блокнотом").grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")
        self._hint(tab, "Выберите локальную папку — её файлы станут источниками выбранного блокнота. "
                        "Папка сохраняется в шаблоне.").grid(row=1, column=0, padx=10, pady=(0, 12), sticky="w")

        folder = self._panel(tab, fg_color=COLORS["background"])
        folder.grid(row=2, column=0, padx=10, sticky="ew")
        folder.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(folder, text="📁", font=ctk.CTkFont(size=22)).grid(row=0, column=0, rowspan=2, padx=(14, 10), pady=12)
        self.lbl_folder = self._label(folder, "Папка не выбрана", size=13)
        self.lbl_folder.grid(row=0, column=1, sticky="ew", pady=(12, 0))
        self.lbl_folder_info = self._hint(folder, "—")
        self.lbl_folder_info.grid(row=1, column=1, sticky="ew", pady=(0, 12))
        self._btn(folder, "Выбрать…", self._choose_folder_upload, "secondary", 110).grid(
            row=0, column=2, rowspan=2, padx=14)

        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=3, column=0, padx=10, pady=(16, 0), sticky="ew")
        actions.grid_columnconfigure(1, weight=1)

        self.btn_reload = self._btn(actions, "♻  Reload", self._reload_folder, "primary", 190, 40)
        self.btn_reload.grid(row=0, column=0, pady=(0, 10), sticky="w")
        self._hint(actions, "Рекомендуется. Удаляет ВСЕ источники блокнота и загружает файлы папки заново — "
                            "блокнот точно совпадёт с папкой.", 420).grid(row=0, column=1, padx=14, pady=(0, 10), sticky="w")

        self.btn_reupload = self._btn(actions, "⬆  Только добавить", self._reupload_folder, "secondary", 190, 40)
        self.btn_reupload.grid(row=1, column=0, sticky="w")
        self._hint(actions, "Добавляет файлы папки к существующим источникам без удаления. "
                            "Возможны дубликаты.", 420).grid(row=1, column=1, padx=14, sticky="w")

        danger = self._panel(tab, fg_color=COLORS["danger_bg"], border_color=COLORS["danger_border"])
        danger.grid(row=4, column=0, padx=10, pady=(24, 10), sticky="ew")
        danger.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(danger, text="Опасная зона", text_color=COLORS["danger"], font=self._font(12, "bold"),
                     anchor="w").grid(row=0, column=0, padx=14, pady=(10, 0), sticky="w")
        self._hint(danger, "Удалить все источники блокнота без загрузки новых. Действие необратимо.", 420).grid(
            row=1, column=0, padx=14, pady=(0, 10), sticky="w")
        self._btn(danger, "Удалить все источники", self._delete_all_sources, "danger", 190).grid(
            row=0, column=1, rowspan=2, padx=14)

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

    def _render_sources(self, sources: list, placeholder: str | None = None):
        for w in self.sources_list.winfo_children():
            w.destroy()
        self._source_rows = {}
        if placeholder or not sources:
            self.lbl_sources.configure(text="ИСТОЧНИКИ")
            self._hint(self.sources_list, placeholder or "В блокноте нет источников", 260).grid(
                row=0, column=0, padx=8, pady=10, sticky="w")
            return
        self.lbl_sources.configure(text=f"ИСТОЧНИКИ · {len(sources)}")
        for i, src in enumerate(sources):
            sid = self._source_id_of(src)
            if not sid:
                continue
            title = self._source_title_of(src)
            row = ctk.CTkFrame(self.sources_list, fg_color="transparent", corner_radius=4, height=30)
            row.grid(row=i, column=0, sticky="ew", pady=1)
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
        if self._selected_source_id in self._source_rows:
            self._select_source(self._selected_source_id)

    def _select_source(self, sid: str):
        self._selected_source_id = sid
        for key, row in self._source_rows.items():
            row.configure(fg_color=COLORS["selected"] if key == sid else "transparent")

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

    def _current_preset_data(self) -> dict:
        nb = next((n for n in self.notebooks if n.id == self.selected_nb_id), None)
        return {
            "storage_path": self.storage_entry.get().strip(),
            "folder": str(self._folder_to_upload) if self._folder_to_upload else "",
            "notebook_id": self.selected_nb_id or "",
            "notebook_title": getattr(nb, "title", "") if nb else "",
        }

    def _save_preset(self):
        current = self.cb_presets.get()
        default = current if current in self.presets.presets else ""
        dialog = ctk.CTkInputDialog(text="Название шаблона (существующее будет перезаписано):",
                                    title="Сохранить шаблон")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        if name in self.presets.presets and name != default and not messagebox.askyesno(
                "Шаблон", f"Шаблон «{name}» уже есть. Перезаписать?"):
            return
        self.presets.put(name, self._current_preset_data())
        self._refresh_presets_combo(name)
        self._log(f"[PRESET] Сохранён: {name}")

    def _apply_preset(self, name: str | None = None):
        name = name or self.cb_presets.get()
        data = self.presets.presets.get(name)
        if not data:
            messagebox.showwarning("Шаблон", "Выберите сохранённый шаблон.")
            return
        if data.get("storage_path"):
            self.storage_entry.delete(0, "end")
            self.storage_entry.insert(0, data["storage_path"])
        elif self.storage_entry.get():
            self.storage_entry.delete(0, "end")
            self.storage_entry._activate_placeholder()

        folder = data.get("folder")
        if folder:
            self._set_folder(Path(folder))

        self._pending_nb = {"id": data.get("notebook_id"), "title": data.get("notebook_title")}
        if self.notebooks:
            self._apply_pending_notebook()
        self.presets.touch(name)
        self.cb_presets.set(name)
        self._log(f"[PRESET] Применён: {name}")

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
        name = self.cb_presets.get()
        if name not in self.presets.presets:
            return
        if messagebox.askyesno("Шаблон", f"Удалить шаблон «{name}»?"):
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

    def _source_id_of(self, src) -> str | None:
        for attr in ("id", "source_id", "uuid"):
            val = getattr(src, attr, None)
            if val:
                return str(val)
        return None

    def _source_title_of(self, src) -> str:
        return str(getattr(src, "title", "—"))

    def _source_kind_of(self, src) -> str:
        return str(getattr(src, "kind", "?"))

    def _populate_sources(self, sources: list):
        self._render_sources(list(sources))

    def _refresh_sources_combo(self):
        if not self.client or not self.selected_nb_id:
            self._selected_source_id = None
            self._render_sources([], NOT_CONNECTED)
            return

        nb_id = self.selected_nb_id
        self._render_sources([], "Загрузка…")

        async def _fetch():
            return await self.client.sources.list(nb_id)

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

    # ---------------------------------------------------- Sources ----------
    def _set_progress(self, current: int, total: int, prefix: str = "Прогресс"):
        if total <= 0:
            pct = 0
            value = 0.0
        else:
            value = max(0.0, min(current / total, 1.0))
            pct = round(value * 100)
        self.pb_op.set(value)
        self.lbl_op_progress.configure(text=f"{prefix}: {pct}% ({current}/{total})")

    def _reset_progress(self):
        self.pb_op.set(0)
        self.lbl_op_progress.configure(text="")

    def _require_notebook(self) -> bool:
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала нажмите «Подключиться».")
            return False
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return False
        return True

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
        nb_id = self.selected_nb_id
        self._log(f"[UPLOAD] Загрузка {len(files)} файл(ов)…")

        def _ok(result):
            uploaded, errors = result
            self._log(f"[UPLOAD] Готово: загружено {uploaded}, ошибок {errors}")
            self._refresh_sources_combo()

        self._schedule(self._upload_all_coro(nb_id, files, "Загрузка"), on_success=_ok)

    def _add_url_source(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return
        url = self.url_entry.get().strip()
        if not url:
            return
        nb_id = self.selected_nb_id

        async def _add():
            return await self.client.sources.add_url(nb_id, url)

        def _ok(src):
            title = getattr(src, "title", url)
            self._log(f"[SOURCE] URL добавлен: {title}")
            self.url_entry.delete(0, "end")
            self._refresh_sources_combo()

        self._log(f"[URL] Добавление {url}…")
        self._schedule(_add(), on_success=_ok)

    def _list_sources(self):
        if not self.client or not self.selected_nb_id:
            messagebox.showwarning("Нет данных", "Подключитесь и выберите блокнот.")
            return

        nb_id = self.selected_nb_id

        async def _fetch():
            return await self.client.sources.list(nb_id)

        def _ok(sources):
            self._populate_sources(sources)
            if not sources:
                self._log("[SOURCES] Источников нет.")
                return
            self._log(f"[SOURCES] Источники ({len(sources)}):")
            for i, src in enumerate(sources, 1):
                kind = getattr(src, "kind", "?")
                title = getattr(src, "title", "—")
                self._log(f"  {i}. [{kind}] {title}")

        self._schedule(_fetch(), on_success=_ok)

    async def _delete_source_api(self, notebook_id: str, source_id: str):
        """Try supported delete/remove signatures from notebooklm-py versions."""
        mgr = self.client.sources
        variants = [
            ("delete", (notebook_id, source_id)),
            ("delete", (source_id,)),
            ("remove", (notebook_id, source_id)),
            ("remove", (source_id,)),
        ]

        for method_name, args in variants:
            fn = getattr(mgr, method_name, None)
            if not fn:
                continue
            try:
                result = fn(*args)
                if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                    return await result
                return result
            except TypeError:
                continue

        raise RuntimeError(
            "NotebookLM API does not expose sources.delete/remove in this version."
        )

    def _delete_source(self, src_id: str, title: str):
        if not self._require_notebook():
            return
        if not messagebox.askyesno("Удалить источник", f"Удалить «{title}» из блокнота?"):
            return
        nb_id = self.selected_nb_id

        def _ok(_):
            self._log(f"[DELETE] Удалён: {title}")
            if self._selected_source_id == src_id:
                self._selected_source_id = None
            self._refresh_sources_combo()

        self._schedule(self._delete_source_api(nb_id, src_id), on_success=_ok)

    def _delete_all_sources(self):
        if not self.client or not self.selected_nb_id:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь и выберите блокнот.")
            return

        if not messagebox.askyesno(
            "ОПАСНО",
            "Удалить ВСЕ источники в выбранном блокноте?\nЭто действие необратимо.",
        ):
            return

        nb_id = self.selected_nb_id

        def _ok(result):
            deleted, errors = result
            self._log(f"[DELETE ALL] Готово: удалено {deleted}, ошибок {errors}")
            self._set_progress(deleted + errors, max(deleted + errors, 1), "Delete all")
            self._refresh_sources_combo()

        self._schedule(self._wipe_all_coro(nb_id), on_success=_ok)

    async def _wipe_all_coro(self, nb_id: str, prefix: str = "Delete all"):
        sources = await self.client.sources.list(nb_id)
        total = len(sources)
        deleted = 0
        errors = 0
        self._ui(lambda: self._set_progress(0, total, prefix))
        for src in sources:
            sid = self._source_id_of(src)
            title = self._source_title_of(src)
            if not sid:
                errors += 1
            else:
                try:
                    await self._delete_source_api(nb_id, sid)
                    deleted += 1
                    self._ui(lambda t=title: self._log(f"  🗑 {t}"))
                except Exception as e:
                    errors += 1
                    self._ui(lambda t=title, err=str(e): self._log(f"  ❌ {t}: {err}"))
            done = deleted + errors
            self._ui(lambda d=done, t=total: self._set_progress(d, t, prefix))
        return deleted, errors

    async def _upload_all_coro(self, nb_id: str, files: list[Path], prefix: str = "Reupload"):
        total = len(files)
        uploaded = 0
        errors = 0
        self._ui(lambda: self._set_progress(0, total, prefix))
        for i, fpath in enumerate(files, 1):
            try:
                await self.client.sources.add_file(nb_id, fpath)
                uploaded += 1
                self._ui(lambda n=fpath.name, a=i, t=total: self._log(f"  ☁️ [{a}/{t}] {n}"))
            except Exception as e:
                errors += 1
                self._ui(lambda n=fpath.name, err=str(e): self._log(f"  ❌ {n}: {err}"))
            done = uploaded + errors
            self._ui(lambda d=done, t=total: self._set_progress(d, t, prefix))
        return uploaded, errors

    def _set_folder(self, folder: Path):
        self._folder_to_upload = folder
        if not folder.exists():
            self.lbl_folder.configure(text=folder.name, text_color=COLORS["danger"])
            self.lbl_folder_info.configure(text=f"Папка не найдена: {folder}")
            return
        count = sum(1 for f in folder.rglob("*") if f.is_file() and not f.name.startswith(".")
                    and f.suffix.lower() in _SUPPORTED_UPLOAD_EXTS)
        self.lbl_folder.configure(text=folder.name, text_color=COLORS["text"])
        self.lbl_folder_info.configure(text=f"{folder}  ·  {count} поддерживаемых файлов")

    def _choose_folder_upload(self):
        folder = filedialog.askdirectory(title="Выберите папку для полной перезагрузки")
        if folder:
            self._set_folder(Path(folder))

    def _collect_folder_files(self) -> list[Path] | None:
        """Validate connection/notebook/folder and return files to upload, or None."""
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return None
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return None
        if not self._folder_to_upload or not self._folder_to_upload.exists():
            messagebox.showwarning("Папка", "Сначала выберите папку для загрузки.")
            return None

        files = [
            f for f in sorted(self._folder_to_upload.rglob('*'))
            if f.is_file() and not f.name.startswith('.') and f.suffix.lower() in _SUPPORTED_UPLOAD_EXTS
        ]
        if not files:
            messagebox.showwarning("Папка", "Подходящие файлы не найдены.")
            return None
        return files

    def _reload_folder(self):
        """Delete every source in the notebook, then upload the whole folder."""
        files = self._collect_folder_files()
        if files is None:
            return

        if not messagebox.askyesno(
            "Reload",
            f"Сначала будут УДАЛЕНЫ все источники выбранного блокнота,\n"
            f"затем загружено {len(files)} файлов из папки.\nЭто необратимо. Продолжить?",
        ):
            return

        nb_id = self.selected_nb_id
        self.btn_reload.configure(state="disabled")
        self._log(f"[RELOAD] Старт: удаление всех источников → загрузка {len(files)} файлов")

        async def _reload():
            deleted, del_err = await self._wipe_all_coro(nb_id, "Reload: delete")
            self._ui(lambda: self._log(f"[RELOAD] Удалено {deleted}, ошибок {del_err}. Загрузка…"))
            if del_err:
                return deleted, 0, 0, True
            uploaded, up_err = await self._upload_all_coro(nb_id, files, "Reload: upload")
            return deleted, uploaded, up_err, False

        def _done():
            self.btn_reload.configure(state="normal")
            self._refresh_sources_combo()

        def _ok(result):
            deleted, uploaded, up_err, aborted = result
            if aborted:
                self._log("[RELOAD] ⚠ Прервано: часть источников не удалена, загрузка не выполнялась.")
            else:
                self._log(f"[RELOAD] Готово: удалено {deleted}, загружено {uploaded}, ошибок {up_err}")
            _done()

        def _err(exc):
            self._log(f"[RELOAD] ❌ {exc}")
            _done()
            messagebox.showerror("Reload", str(exc))

        self._schedule(_reload(), on_success=_ok, on_error=_err)

    def _reupload_folder(self):
        files = self._collect_folder_files()
        if files is None:
            return

        if not messagebox.askyesno(
            "Reupload Folder",
            f"Загрузить {len(files)} файлов в выбранный блокнот?",
        ):
            return

        nb_id = self.selected_nb_id
        self._log(f"[REUPLOAD] Старт: {len(files)} файлов из {self._folder_to_upload}")

        def _ok(result):
            uploaded, errors = result
            self._log(f"[REUPLOAD] Готово: загружено {uploaded}, ошибок {errors}")
            self._set_progress(uploaded + errors, max(uploaded + errors, 1), "Reupload")
            self._refresh_sources_combo()

        self._schedule(self._upload_all_coro(nb_id, files), on_success=_ok)

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
