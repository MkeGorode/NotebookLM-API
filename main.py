"""
NotebookLM Desktop Client
Full-featured GUI for Google NotebookLM via notebooklm-py.
"""

import asyncio
import json
import threading
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
    "warning": "#CA5010",
    "background": "#FFFFFF",
    "surface": "#F3F3F3",
    "border": "#E0E0E0",
    "text": "#323130",
    "text_secondary": "#605E5C",
}
FONT = "Segoe UI"
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
        self.geometry("1060x820")
        self.minsize(900, 700)
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=COLORS["background"])

        self.client = None
        self.notebooks: list = []
        self.selected_nb_id: str | None = None
        self._file_to_upload: Path | None = None
        self._folder_to_upload: Path | None = None
        self._sources_cache: list = []
        self._source_display_to_id: dict[str, str] = {}
        self._selected_source_id: str | None = None
        self.presets = PresetStore()
        self._pending_nb: dict | None = None

        self._build_ui()
        self._restore_last_preset()

    # ------------------------------------------------------------------ UI --
    # Widget factories keep the Fluent look consistent
    def _card(self, row: int, **grid):
        frame = ctk.CTkFrame(self, corner_radius=6, fg_color=COLORS["surface"],
                             border_width=1, border_color=COLORS["border"])
        frame.grid(row=row, column=0, padx=24, pady=grid.pop("pady", 6), sticky=grid.pop("sticky", "ew"))
        return frame

    def _title(self, parent, text: str):
        return ctk.CTkLabel(parent, text=text, text_color=COLORS["primary"],
                            font=ctk.CTkFont(family=FONT, size=13, weight="bold"))

    def _label(self, parent, text: str, **kw):
        kw.setdefault("text_color", COLORS["text"])
        return ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(family=FONT, size=12), **kw)

    def _entry(self, parent, placeholder: str):
        return ctk.CTkEntry(parent, placeholder_text=placeholder, fg_color=COLORS["background"],
                            border_color=COLORS["border"], text_color=COLORS["text"],
                            font=ctk.CTkFont(family=FONT, size=12))

    def _combo(self, parent, values, command=None):
        return ctk.CTkComboBox(parent, values=values, state="readonly", command=command,
                               fg_color=COLORS["background"], border_color=COLORS["border"],
                               button_color=COLORS["primary"], button_hover_color=COLORS["primary_hover"],
                               text_color=COLORS["text"], font=ctk.CTkFont(family=FONT, size=12))

    def _btn(self, parent, text: str, command, kind: str = "primary", width: int = 130):
        styles = {
            "primary": (COLORS["primary"], COLORS["primary_hover"], "white", None),
            "danger": (COLORS["danger"], COLORS["danger_hover"], "white", None),
            "secondary": (COLORS["background"], COLORS["border"], COLORS["text"], COLORS["border"]),
        }
        fg, hover, txt, border = styles[kind]
        return ctk.CTkButton(parent, text=text, width=width, command=command, corner_radius=4,
                             fg_color=fg, hover_color=hover, text_color=txt,
                             border_width=1 if border else 0, border_color=border or fg,
                             font=ctk.CTkFont(family=FONT, size=12))

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # chat area stretches

        # === 0. HEADER ===
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=24, pady=(16, 6), sticky="ew")
        ctk.CTkLabel(header, text="📓", font=ctk.CTkFont(size=32), text_color=COLORS["primary"]).pack(side="left", padx=(0, 14))
        title_block = ctk.CTkFrame(header, fg_color="transparent")
        title_block.pack(side="left")
        ctk.CTkLabel(title_block, text="NotebookLM Desktop Client", text_color=COLORS["text"],
                     font=ctk.CTkFont(family=FONT, size=18, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_block, text="BY MIKE GORODETSKYY", text_color=COLORS["primary"],
                     font=ctk.CTkFont(family=FONT, size=10, weight="bold")).pack(anchor="w")

        # === 1. AUTH FRAME ===
        auth_frame = self._card(1)
        auth_frame.grid_columnconfigure(1, weight=1)

        self._title(auth_frame, "🔑 Авторизация").grid(row=0, column=0, columnspan=4, padx=14, pady=(10, 4), sticky="w")

        self._label(auth_frame, "Storage path:").grid(row=1, column=0, padx=(14, 4), pady=4, sticky="w")
        self.storage_entry = self._entry(auth_frame, "~/.notebooklm/storage_state.json (по умолчанию)")
        self.storage_entry.grid(row=1, column=1, padx=4, pady=4, sticky="ew")

        self.btn_browser_login = self._btn(auth_frame, "🌐 Browser Login", self._browser_login, "secondary", 140)
        self.btn_browser_login.grid(row=1, column=2, padx=4, pady=4)

        self.btn_test_conn = self._btn(auth_frame, "✅ Test Connection", self._test_connection, "primary", 140)
        self.btn_test_conn.grid(row=1, column=3, padx=(4, 14), pady=4)

        self.lbl_auth_status = self._label(auth_frame, "Статус: не подключено", text_color=COLORS["text_secondary"])
        self.lbl_auth_status.grid(row=2, column=0, columnspan=4, padx=14, pady=(0, 6), sticky="w")

        # -- Presets row --
        preset_row = ctk.CTkFrame(auth_frame, fg_color="transparent")
        preset_row.grid(row=3, column=0, columnspan=4, padx=14, pady=(0, 10), sticky="ew")
        preset_row.grid_columnconfigure(1, weight=1)
        self._title(preset_row, "📌 Шаблон:").grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.cb_presets = self._combo(preset_row, self.presets.names() or ["-- нет шаблонов --"])
        self.cb_presets.grid(row=0, column=1, padx=4, sticky="ew")
        self._btn(preset_row, "▶ Применить", self._apply_preset, "primary", 110).grid(row=0, column=2, padx=4)
        self._btn(preset_row, "💾 Сохранить", self._save_preset, "secondary", 110).grid(row=0, column=3, padx=4)
        self._btn(preset_row, "✖ Удалить", self._delete_preset, "secondary", 100).grid(row=0, column=4, padx=(4, 0))

        # === 2. NOTEBOOKS + SOURCES ===
        mid_frame = self._card(2)
        mid_frame.grid_columnconfigure(1, weight=1)

        # -- Notebooks row --
        self._title(mid_frame, "📓 Блокнот:").grid(row=0, column=0, padx=(14, 4), pady=(10, 4), sticky="w")
        self.cb_notebooks = self._combo(mid_frame, ["-- сначала подключитесь --"], self._on_notebook_selected)
        self.cb_notebooks.grid(row=0, column=1, padx=4, pady=(10, 4), sticky="ew")

        self.btn_refresh_nb = self._btn(mid_frame, "🔄 Refresh Notebooks", self._refresh_notebooks, "secondary", 160)
        self.btn_refresh_nb.grid(row=0, column=2, padx=(4, 14), pady=(10, 4))

        # -- Sources row --
        self._title(mid_frame, "📎 Источник:").grid(row=1, column=0, padx=(14, 4), pady=4, sticky="w")
        self.lbl_file = self._label(mid_frame, "файл не выбран", text_color=COLORS["text_secondary"])
        self.lbl_file.grid(row=1, column=1, padx=4, pady=4, sticky="w")

        src_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        src_btns.grid(row=1, column=2, padx=(4, 14), pady=4, sticky="e")
        self._btn(src_btns, "📂 Выбрать файл", self._choose_file, "secondary", 120).pack(side="left", padx=(0, 6))
        self._btn(src_btns, "⬆ Загрузить", self._upload_source, "primary", 120).pack(side="left")

        # -- URL source row --
        self._label(mid_frame, "🔗 URL:").grid(row=2, column=0, padx=(14, 4), pady=(4, 8), sticky="w")
        self.url_entry = self._entry(mid_frame, "https://example.com (Enter для добавления)")
        self.url_entry.grid(row=2, column=1, padx=4, pady=(4, 8), sticky="ew")
        self.url_entry.bind("<Return>", lambda _: self._add_url_source())
        self._btn(mid_frame, "➕ Добавить URL", self._add_url_source, "primary", 160).grid(
            row=2, column=2, padx=(4, 14), pady=(4, 8)
        )

        # -- Manage existing sources row --
        self._title(mid_frame, "🗑 Source:").grid(row=3, column=0, padx=(14, 4), pady=(4, 8), sticky="w")
        self.cb_sources = self._combo(mid_frame, ["-- выберите блокнот --"], self._on_source_selected)
        self.cb_sources.grid(row=3, column=1, padx=4, pady=(4, 8), sticky="ew")

        src_manage_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        src_manage_btns.grid(row=3, column=2, padx=(4, 14), pady=(4, 8), sticky="e")
        self._btn(src_manage_btns, "🔄", self._refresh_sources_combo, "secondary", 44).pack(side="left", padx=(0, 6))
        self._btn(src_manage_btns, "🗑 Удалить", self._delete_selected_source, "secondary", 110).pack(side="left", padx=(0, 6))
        self._btn(src_manage_btns, "☢ Удалить ВСЕ", self._delete_all_sources, "danger", 130).pack(side="left")

        # -- Bulk reupload / reload row --
        self._title(mid_frame, "📁 Reload:").grid(row=4, column=0, padx=(14, 4), pady=(0, 10), sticky="w")
        self.lbl_folder = self._label(mid_frame, "папка не выбрана", text_color=COLORS["text_secondary"])
        self.lbl_folder.grid(row=4, column=1, padx=4, pady=(0, 10), sticky="w")

        folder_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        folder_btns.grid(row=4, column=2, padx=(4, 14), pady=(0, 10), sticky="e")
        self._btn(folder_btns, "📂 Выбрать папку", self._choose_folder_upload, "secondary", 120).pack(side="left", padx=(0, 6))
        self._btn(folder_btns, "⬆ Загрузить папку", self._reupload_folder, "secondary", 140).pack(side="left", padx=(0, 6))
        self.btn_reload = self._btn(folder_btns, "♻ Reload", self._reload_folder, "primary", 110)
        self.btn_reload.pack(side="left")

        # -- Operation progress row --
        self.lbl_op_progress = self._label(mid_frame, "Progress: 0%", text_color=COLORS["text_secondary"])
        self.lbl_op_progress.grid(row=5, column=0, columnspan=2, padx=(14, 4), pady=(0, 2), sticky="w")
        self.pb_op = ctk.CTkProgressBar(mid_frame, height=8, progress_color=COLORS["primary"],
                                        fg_color=COLORS["border"])
        self.pb_op.grid(row=6, column=0, columnspan=3, padx=14, pady=(0, 12), sticky="ew")
        self.pb_op.set(0)

        # === 3. CHAT AREA ===
        chat_frame = self._card(3, pady=6, sticky="nsew")
        chat_frame.grid_columnconfigure(0, weight=1)
        chat_frame.grid_rowconfigure(1, weight=1)

        self._title(chat_frame, "💬 Чат").grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")

        self.txt_chat = ctk.CTkTextbox(chat_frame, wrap="word", state="disabled",
                                       fg_color=COLORS["background"], text_color=COLORS["text"],
                                       border_width=1, border_color=COLORS["border"],
                                       font=ctk.CTkFont(family=FONT, size=13))
        self.txt_chat.grid(row=1, column=0, padx=14, pady=4, sticky="nsew")

        prompt_row = ctk.CTkFrame(chat_frame, fg_color="transparent")
        prompt_row.grid(row=2, column=0, padx=14, pady=(4, 12), sticky="ew")
        prompt_row.grid_columnconfigure(0, weight=1)

        self.prompt_entry = self._entry(prompt_row, "Введите вопрос…")
        self.prompt_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.prompt_entry.bind("<Return>", lambda _: self._send_query())

        self.btn_send = self._btn(prompt_row, "📤 Отправить", self._send_query, "primary", 130)
        self.btn_send.grid(row=0, column=1)

        # === 4. DEBUG PANEL ===
        dbg_frame = self._card(4, pady=(6, 16))

        self._title(dbg_frame, "🛠 Debug / Testing").pack(anchor="w", padx=14, pady=(10, 4))
        btn_row = ctk.CTkFrame(dbg_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 10))

        self._btn(btn_row, "🏓 Test API Status", self._test_api_status, "secondary", 160).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "🧪 Run Mock Query", self._run_mock_query, "secondary", 160).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "🗑 Clear Logs/Chat", self._clear_chat, "secondary", 160).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "📋 List Sources", self._list_sources, "secondary", 160).pack(side="left")

    # -------------------------------------------------------- Chat helpers --
    def _chat_append(self, text: str, tag: str = ""):
        self.txt_chat.configure(state="normal")
        self.txt_chat.insert("end", text + "\n")
        self.txt_chat.configure(state="disabled")
        self.txt_chat.see("end")

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
        self.lbl_auth_status.configure(text="Статус: подключение…", text_color="orange")
        path = self.storage_entry.get().strip() or None

        def _ok(result):
            client, nbs = result
            self.client = client
            self.notebooks = nbs
            self._populate_notebooks(nbs)
            self.lbl_auth_status.configure(text=f"✅ Подключено — найдено {len(nbs)} блокнотов", text_color="green")
            self._chat_append(f"[SYSTEM] Подключено. Блокнотов: {len(nbs)}")

        def _err(exc):
            self.lbl_auth_status.configure(text=f"❌ Ошибка: {exc}", text_color="red")
            messagebox.showerror("Connection Error", str(exc))

        self._schedule(self._connect(path), on_success=_ok, on_error=_err)

    def _browser_login(self):
        """Вход через Playwright (Edge) на notebook.google.com; сессия сохраняется в storage notebooklm-py."""
        self.lbl_auth_status.configure(text="Статус: открывается браузер для входа…", text_color="orange")
        self._chat_append("[SYSTEM] Откроется браузер — войдите в Google, затем подтвердите в окне программы.")
        threading.Thread(target=self._run_login, daemon=True).start()

    def _run_login(self):
        def _confirm() -> bool:
            done, answer = threading.Event(), []

            def _ask():
                answer.append(messagebox.askokcancel(
                    "Вход в NotebookLM",
                    "Войдите в Google в открывшемся браузере.\nКогда увидите список блокнотов — нажмите OK."))
                done.set()

            self.after(0, _ask)
            done.wait()
            return answer[0]

        ok, msg = NotebookLMSync.browser_login(wait_callback=_confirm)
        if ok:
            self.after(0, lambda: self.lbl_auth_status.configure(
                text="✅ Вход выполнен — нажмите Test Connection", text_color="green"))
            self.after(0, lambda: self._chat_append("[SYSTEM] Вход завершён. Нажмите 'Test Connection'."))
        else:
            self.after(0, lambda: messagebox.showerror("Login Error", msg))
            self.after(0, lambda: self.lbl_auth_status.configure(text="❌ Login failed", text_color="red"))

    # ---------------------------------------------------- Presets ----------
    def _refresh_presets_combo(self, select: str | None = None):
        names = self.presets.names()
        self.cb_presets.configure(values=names or ["-- нет шаблонов --"])
        self.cb_presets.set(select if select in names else (names[0] if names else "-- нет шаблонов --"))

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
        self._chat_append(f"[PRESET] Сохранён: {name}")

    def _apply_preset(self, name: str | None = None):
        name = name or self.cb_presets.get()
        data = self.presets.presets.get(name)
        if not data:
            messagebox.showwarning("Шаблон", "Выберите сохранённый шаблон.")
            return
        self.storage_entry.delete(0, "end")
        if data.get("storage_path"):
            self.storage_entry.insert(0, data["storage_path"])

        folder = data.get("folder")
        if folder:
            self._folder_to_upload = Path(folder)
            exists = self._folder_to_upload.exists()
            self.lbl_folder.configure(text=self._folder_to_upload.name + ("" if exists else " (не найдена)"),
                                      text_color=COLORS["text"] if exists else COLORS["danger"])

        self._pending_nb = {"id": data.get("notebook_id"), "title": data.get("notebook_title")}
        if self.notebooks:
            self._apply_pending_notebook()
        self.presets.touch(name)
        self.cb_presets.set(name)
        self._chat_append(f"[PRESET] Применён: {name}")

    def _apply_pending_notebook(self):
        pend, self._pending_nb = self._pending_nb, None
        if not pend:
            return
        for nb in self.notebooks:
            if nb.id == pend.get("id") or (pend.get("title") and nb.title == pend["title"]):
                self.cb_notebooks.set(nb.title)
                self._on_notebook_selected(nb.title)
                return
        self._chat_append(f"[PRESET] Блокнот «{pend.get('title') or pend.get('id')}» не найден.")

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
            self.cb_notebooks.configure(values=["-- нет блокнотов --"])
            self.cb_notebooks.set("-- нет блокнотов --")
            return
        titles = [nb.title for nb in nbs]
        self.cb_notebooks.configure(values=titles)
        self.cb_notebooks.set(titles[0])
        self.selected_nb_id = nbs[0].id
        if self._pending_nb:
            self._apply_pending_notebook()
            return
        # Fetch source count for the first selected notebook
        self._fetch_source_count(nbs[0].id, nbs[0].title)
        self._refresh_sources_combo()

    def _on_notebook_selected(self, choice: str):
        idx = self.cb_notebooks.cget("values").index(choice) if choice in self.cb_notebooks.cget("values") else -1
        if 0 <= idx < len(self.notebooks):
            self.selected_nb_id = self.notebooks[idx].id
            self._chat_append(f"[SYSTEM] Выбран блокнот: {self.notebooks[idx].title}")
            self._fetch_source_count(self.notebooks[idx].id, self.notebooks[idx].title)
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
        self._sources_cache = list(sources)
        self._source_display_to_id = {}
        values = []
        for i, src in enumerate(sources, 1):
            sid = self._source_id_of(src)
            if not sid:
                continue
            display = f"{i}. [{self._source_kind_of(src)}] {self._source_title_of(src)}"
            self._source_display_to_id[display] = sid
            values.append(display)

        if not values:
            self._selected_source_id = None
            self.cb_sources.configure(values=["-- источников нет --"])
            self.cb_sources.set("-- источников нет --")
            return

        self.cb_sources.configure(values=values)
        self.cb_sources.set(values[0])
        self._selected_source_id = self._source_display_to_id.get(values[0])

    def _on_source_selected(self, choice: str):
        self._selected_source_id = self._source_display_to_id.get(choice)

    def _refresh_sources_combo(self):
        if not self.client or not self.selected_nb_id:
            self.cb_sources.configure(values=["-- сначала подключитесь --"])
            self.cb_sources.set("-- сначала подключитесь --")
            self._selected_source_id = None
            return

        nb_id = self.selected_nb_id

        async def _fetch():
            return await self.client.sources.list(nb_id)

        def _ok(sources):
            self._populate_sources(sources)
            self._chat_append(f"[SOURCES] Обновлено: {len(sources)}")

        self._schedule(_fetch(), on_success=_ok)

    def _fetch_source_count(self, nb_id: str, title: str):
        """Fetch actual source count for a notebook."""
        if not self.client:
            return

        async def _count():
            sources = await self.client.sources.list(nb_id)
            return len(sources)

        def _ok(count):
            self._chat_append(f"   📎 Источников: {count}")

        self._schedule(_count(), on_success=_ok)

    def _refresh_notebooks(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь (Test Connection).")
            return

        async def _fetch():
            return await self.client.notebooks.list()

        def _ok(nbs):
            self.notebooks = nbs
            self._populate_notebooks(nbs)
            self._chat_append(f"[SYSTEM] Список обновлён: {len(nbs)} блокнотов")

        self._schedule(_fetch(), on_success=_ok)

    # ---------------------------------------------------- Sources ----------
    def _set_progress(self, current: int, total: int, prefix: str = "Progress"):
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
        self.lbl_op_progress.configure(text="Progress: 0%")

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл для загрузки",
            filetypes=[
                ("Все поддерживаемые", "*.pdf *.txt *.md *.docx *.csv *.png *.jpg *.jpeg *.mp3 *.mp4 *.wav"),
                ("PDF", "*.pdf"), ("Text", "*.txt *.md"), ("Word", "*.docx"),
                ("CSV", "*.csv"), ("Images", "*.png *.jpg *.jpeg"),
                ("Audio/Video", "*.mp3 *.mp4 *.wav"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._file_to_upload = Path(path)
            self.lbl_file.configure(text=self._file_to_upload.name, text_color=COLORS["text"])

    def _upload_source(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return
        if not self._file_to_upload:
            messagebox.showwarning("Файл", "Сначала выберите файл.")
            return

        nb_id = self.selected_nb_id
        fpath = self._file_to_upload

        async def _upload():
            return await self.client.sources.add_file(nb_id, fpath)

        def _ok(src):
            title = getattr(src, "title", fpath.name)
            self._chat_append(f"[SOURCE] Загружен: {title}")
            messagebox.showinfo("Готово", f"Файл «{title}» загружен в блокнот.")

        self._chat_append(f"[UPLOAD] Загрузка {fpath.name}…")
        self._schedule(_upload(), on_success=_ok)

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
            self._chat_append(f"[SOURCE] URL добавлен: {title}")
            self.url_entry.delete(0, "end")

        self._chat_append(f"[URL] Добавление {url}…")
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
                self._chat_append("[SOURCES] Источников нет.")
                return
            self._chat_append(f"[SOURCES] Источники ({len(sources)}):")
            for i, src in enumerate(sources, 1):
                kind = getattr(src, "kind", "?")
                title = getattr(src, "title", "—")
                self._chat_append(f"  {i}. [{kind}] {title}")

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

    def _delete_selected_source(self):
        if not self.client or not self.selected_nb_id:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь и выберите блокнот.")
            return
        if not self._selected_source_id:
            messagebox.showwarning("Источник", "Выберите источник для удаления.")
            return

        if not messagebox.askyesno("Подтверждение", "Удалить выбранный источник из блокнота?"):
            return

        nb_id = self.selected_nb_id
        src_id = self._selected_source_id

        async def _delete_one():
            return await self._delete_source_api(nb_id, src_id)

        def _ok(_):
            self._chat_append("[DELETE] Источник удален.")
            self._refresh_sources_combo()

        self._schedule(_delete_one(), on_success=_ok)

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
            self._chat_append(f"[DELETE ALL] Готово: удалено {deleted}, ошибок {errors}")
            self._set_progress(deleted + errors, max(deleted + errors, 1), "Delete all")
            self._refresh_sources_combo()

        self._schedule(self._wipe_all_coro(nb_id), on_success=_ok)

    async def _wipe_all_coro(self, nb_id: str, prefix: str = "Delete all"):
        sources = await self.client.sources.list(nb_id)
        total = len(sources)
        deleted = 0
        errors = 0
        self.after(0, lambda: self._set_progress(0, total, prefix))
        for src in sources:
            sid = self._source_id_of(src)
            title = self._source_title_of(src)
            if not sid:
                errors += 1
            else:
                try:
                    await self._delete_source_api(nb_id, sid)
                    deleted += 1
                    self.after(0, lambda t=title: self._chat_append(f"  🗑 {t}"))
                except Exception as e:
                    errors += 1
                    self.after(0, lambda t=title, err=str(e): self._chat_append(f"  ❌ {t}: {err}"))
            done = deleted + errors
            self.after(0, lambda d=done, t=total: self._set_progress(d, t, prefix))
        return deleted, errors

    async def _upload_all_coro(self, nb_id: str, files: list[Path], prefix: str = "Reupload"):
        total = len(files)
        uploaded = 0
        errors = 0
        self.after(0, lambda: self._set_progress(0, total, prefix))
        for i, fpath in enumerate(files, 1):
            try:
                await self.client.sources.add_file(nb_id, fpath)
                uploaded += 1
                self.after(0, lambda n=fpath.name, a=i, t=total: self._chat_append(f"  ☁️ [{a}/{t}] {n}"))
            except Exception as e:
                errors += 1
                self.after(0, lambda n=fpath.name, err=str(e): self._chat_append(f"  ❌ {n}: {err}"))
            done = uploaded + errors
            self.after(0, lambda d=done, t=total: self._set_progress(d, t, prefix))
        return uploaded, errors

    def _choose_folder_upload(self):
        folder = filedialog.askdirectory(title="Выберите папку для полной перезагрузки")
        if folder:
            self._folder_to_upload = Path(folder)
            self.lbl_folder.configure(text=self._folder_to_upload.name, text_color=COLORS["text"])

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
        self._chat_append(f"[RELOAD] Старт: удаление всех источников → загрузка {len(files)} файлов")

        async def _reload():
            deleted, del_err = await self._wipe_all_coro(nb_id, "Reload: delete")
            self.after(0, lambda: self._chat_append(f"[RELOAD] Удалено {deleted}, ошибок {del_err}. Загрузка…"))
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
                self._chat_append("[RELOAD] ⚠ Прервано: часть источников не удалена, загрузка не выполнялась.")
            else:
                self._chat_append(f"[RELOAD] Готово: удалено {deleted}, загружено {uploaded}, ошибок {up_err}")
            _done()

        def _err(exc):
            self._chat_append(f"[RELOAD] ❌ {exc}")
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
        self._chat_append(f"[REUPLOAD] Старт: {len(files)} файлов из {self._folder_to_upload}")

        def _ok(result):
            uploaded, errors = result
            self._chat_append(f"[REUPLOAD] Готово: загружено {uploaded}, ошибок {errors}")
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
                    self._chat_append(f"   📚 Ссылки: {refs}")
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

        self._chat_append("[DEBUG] Проверка API…")

        async def _ping():
            nbs = await self.client.notebooks.list()
            return len(nbs)

        def _ok(count):
            self._chat_append(f"[DEBUG] ✅ API доступен. Блокнотов: {count}")
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
        self._chat_append("[SYSTEM] Лог очищен.\n")


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
