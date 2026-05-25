"""
NotebookLM Desktop Client
Full-featured GUI for Google NotebookLM via notebooklm-py.
"""

import asyncio
import threading
import subprocess
import sys
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

_SUPPORTED_UPLOAD_EXTS = {
    ".pdf", ".txt", ".md", ".docx", ".csv",
    ".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".wav",
}

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
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.client = None
        self.notebooks: list = []
        self.selected_nb_id: str | None = None
        self._file_to_upload: Path | None = None
        self._folder_to_upload: Path | None = None
        self._sources_cache: list = []
        self._source_display_to_id: dict[str, str] = {}
        self._selected_source_id: str | None = None

        self._build_ui()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)  # chat area stretches

        # === 1. AUTH FRAME ===
        auth_frame = ctk.CTkFrame(self, corner_radius=8)
        auth_frame.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        auth_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(auth_frame, text="🔑 Авторизация", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=10, pady=(8, 4), sticky="w"
        )

        ctk.CTkLabel(auth_frame, text="Storage path:").grid(row=1, column=0, padx=(10, 4), pady=4, sticky="w")
        self.storage_entry = ctk.CTkEntry(auth_frame, placeholder_text="~/.notebooklm/storage_state.json (по умолчанию)")
        self.storage_entry.grid(row=1, column=1, padx=4, pady=4, sticky="ew")

        self.btn_browser_login = ctk.CTkButton(auth_frame, text="🌐 Browser Login", width=140, command=self._browser_login)
        self.btn_browser_login.grid(row=1, column=2, padx=4, pady=4)

        self.btn_test_conn = ctk.CTkButton(auth_frame, text="✅ Test Connection", width=140, command=self._test_connection)
        self.btn_test_conn.grid(row=1, column=3, padx=(4, 10), pady=4)

        self.lbl_auth_status = ctk.CTkLabel(auth_frame, text="Статус: не подключено", text_color="gray")
        self.lbl_auth_status.grid(row=2, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="w")

        # === 2. NOTEBOOKS + SOURCES ===
        mid_frame = ctk.CTkFrame(self, corner_radius=8)
        mid_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        mid_frame.grid_columnconfigure(1, weight=1)

        # -- Notebooks row --
        ctk.CTkLabel(mid_frame, text="📓 Блокнот:", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, padx=(10, 4), pady=(8, 4), sticky="w"
        )
        self.cb_notebooks = ctk.CTkComboBox(mid_frame, values=["-- сначала подключитесь --"], state="readonly",
                                            command=self._on_notebook_selected)
        self.cb_notebooks.grid(row=0, column=1, padx=4, pady=(8, 4), sticky="ew")

        self.btn_refresh_nb = ctk.CTkButton(mid_frame, text="🔄 Refresh Notebooks", width=160, command=self._refresh_notebooks)
        self.btn_refresh_nb.grid(row=0, column=2, padx=(4, 10), pady=(8, 4))

        # -- Sources row --
        ctk.CTkLabel(mid_frame, text="📎 Источник:", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=1, column=0, padx=(10, 4), pady=4, sticky="w"
        )
        self.lbl_file = ctk.CTkLabel(mid_frame, text="файл не выбран", text_color="gray")
        self.lbl_file.grid(row=1, column=1, padx=4, pady=4, sticky="w")

        src_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        src_btns.grid(row=1, column=2, padx=(4, 10), pady=4, sticky="e")
        ctk.CTkButton(src_btns, text="📂 Выбрать файл", width=120, command=self._choose_file).pack(side="left", padx=(0, 6))
        ctk.CTkButton(src_btns, text="⬆ Загрузить", width=120, command=self._upload_source).pack(side="left")

        # -- URL source row --
        ctk.CTkLabel(mid_frame, text="🔗 URL:").grid(row=2, column=0, padx=(10, 4), pady=(4, 8), sticky="w")
        self.url_entry = ctk.CTkEntry(mid_frame, placeholder_text="https://example.com (Enter для добавления)")
        self.url_entry.grid(row=2, column=1, padx=4, pady=(4, 8), sticky="ew")
        self.url_entry.bind("<Return>", lambda _: self._add_url_source())
        ctk.CTkButton(mid_frame, text="➕ Добавить URL", width=160, command=self._add_url_source).grid(
            row=2, column=2, padx=(4, 10), pady=(4, 8)
        )

        # -- Manage existing sources row --
        ctk.CTkLabel(mid_frame, text="🗑 Source:").grid(row=3, column=0, padx=(10, 4), pady=(4, 8), sticky="w")
        self.cb_sources = ctk.CTkComboBox(
            mid_frame,
            values=["-- выберите блокнот --"],
            state="readonly",
            command=self._on_source_selected,
        )
        self.cb_sources.grid(row=3, column=1, padx=4, pady=(4, 8), sticky="ew")

        src_manage_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        src_manage_btns.grid(row=3, column=2, padx=(4, 10), pady=(4, 8), sticky="e")
        ctk.CTkButton(src_manage_btns, text="🔄", width=44, command=self._refresh_sources_combo).pack(side="left", padx=(0, 6))
        ctk.CTkButton(src_manage_btns, text="🗑 Удалить", width=110, command=self._delete_selected_source).pack(side="left", padx=(0, 6))
        ctk.CTkButton(src_manage_btns, text="☢ Удалить ВСЕ", width=130, command=self._delete_all_sources).pack(side="left")

        # -- Bulk reupload row --
        ctk.CTkLabel(mid_frame, text="📁 Reupload:").grid(row=4, column=0, padx=(10, 4), pady=(0, 10), sticky="w")
        self.lbl_folder = ctk.CTkLabel(mid_frame, text="папка не выбрана", text_color="gray")
        self.lbl_folder.grid(row=4, column=1, padx=4, pady=(0, 10), sticky="w")

        folder_btns = ctk.CTkFrame(mid_frame, fg_color="transparent")
        folder_btns.grid(row=4, column=2, padx=(4, 10), pady=(0, 10), sticky="e")
        ctk.CTkButton(folder_btns, text="📂 Выбрать папку", width=120, command=self._choose_folder_upload).pack(side="left", padx=(0, 6))
        ctk.CTkButton(folder_btns, text="⬆ Reupload Folder", width=150, command=self._reupload_folder).pack(side="left")

        # -- Operation progress row --
        self.lbl_op_progress = ctk.CTkLabel(mid_frame, text="Progress: 0%", text_color="gray")
        self.lbl_op_progress.grid(row=5, column=0, padx=(10, 4), pady=(0, 10), sticky="w")
        self.pb_op = ctk.CTkProgressBar(mid_frame, height=12)
        self.pb_op.grid(row=5, column=1, padx=4, pady=(0, 10), sticky="ew")
        self.pb_op.set(0)

        # === 3. CHAT AREA ===
        chat_frame = ctk.CTkFrame(self, corner_radius=8)
        chat_frame.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")
        chat_frame.grid_columnconfigure(0, weight=1)
        chat_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(chat_frame, text="💬 Чат", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 4), sticky="w"
        )

        self.txt_chat = ctk.CTkTextbox(chat_frame, wrap="word", state="disabled", font=ctk.CTkFont(size=13))
        self.txt_chat.grid(row=1, column=0, padx=10, pady=4, sticky="nsew")

        prompt_row = ctk.CTkFrame(chat_frame, fg_color="transparent")
        prompt_row.grid(row=2, column=0, padx=10, pady=(4, 8), sticky="ew")
        prompt_row.grid_columnconfigure(0, weight=1)

        self.prompt_entry = ctk.CTkEntry(prompt_row, placeholder_text="Введите вопрос…")
        self.prompt_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.prompt_entry.bind("<Return>", lambda _: self._send_query())

        self.btn_send = ctk.CTkButton(prompt_row, text="📤 Отправить", width=130, command=self._send_query)
        self.btn_send.grid(row=0, column=1)

        # === 4. DEBUG PANEL ===
        dbg_frame = ctk.CTkFrame(self, corner_radius=8)
        dbg_frame.grid(row=3, column=0, padx=10, pady=(5, 10), sticky="ew")

        ctk.CTkLabel(dbg_frame, text="🛠 Debug / Testing", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 4)
        )
        btn_row = ctk.CTkFrame(dbg_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkButton(btn_row, text="🏓 Test API Status", width=160, command=self._test_api_status).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="🧪 Run Mock Query", width=160, command=self._run_mock_query).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="🗑 Clear Logs/Chat", width=160, command=self._clear_chat).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="📋 List Sources", width=160, command=self._list_sources).pack(side="left")

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
        from notebooklm import NotebookLMClient
        storage = path if path else None
        client = await NotebookLMClient.from_storage(storage)
        await client.__aenter__()
        # Quick smoke-test: list notebooks
        nbs = await client.notebooks.list()
        return client, nbs

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
        """Run `notebooklm login` in a subprocess (opens browser)."""
        try:
            self.lbl_auth_status.configure(text="Статус: открывается браузер для входа…", text_color="orange")
            self._chat_append("[SYSTEM] Запуск notebooklm login — откроется браузер…")
            threading.Thread(target=self._run_login_subprocess, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Login Error", str(e))

    def _run_login_subprocess(self):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "notebooklm", "login"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                self.after(0, lambda: self.lbl_auth_status.configure(
                    text="✅ Вход выполнен — нажмите Test Connection", text_color="green"))
                self.after(0, lambda: self._chat_append("[SYSTEM] Browser login завершён. Нажмите 'Test Connection'."))
            else:
                err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                self.after(0, lambda: messagebox.showerror("Login Error", err))
                self.after(0, lambda: self.lbl_auth_status.configure(
                    text=f"❌ Login failed", text_color="red"))
        except subprocess.TimeoutExpired:
            self.after(0, lambda: messagebox.showwarning("Timeout", "Login timed out (5 min)."))
        except FileNotFoundError:
            self.after(0, lambda: messagebox.showerror(
                "Not Found",
                "notebooklm CLI not found.\n\nУстановите: pip install 'notebooklm-py[browser]'\nЗатем: playwright install chromium"))

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
            self.lbl_file.configure(text=self._file_to_upload.name, text_color="white")

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

        async def _wipe_all():
            sources = await self.client.sources.list(nb_id)
            total = len(sources)
            deleted = 0
            errors = 0
            self.after(0, lambda: self._set_progress(0, total, "Delete all"))
            for src in sources:
                sid = self._source_id_of(src)
                if not sid:
                    errors += 1
                    done = deleted + errors
                    self.after(0, lambda d=done, t=total: self._set_progress(d, t, "Delete all"))
                    continue
                try:
                    await self._delete_source_api(nb_id, sid)
                    deleted += 1
                    title = self._source_title_of(src)
                    self.after(0, lambda t=title: self._chat_append(f"  🗑 {t}"))
                except Exception as e:
                    errors += 1
                    title = self._source_title_of(src)
                    self.after(0, lambda t=title, err=str(e): self._chat_append(f"  ❌ {t}: {err}"))
                done = deleted + errors
                self.after(0, lambda d=done, t=total: self._set_progress(d, t, "Delete all"))
            return deleted, errors

        def _ok(result):
            deleted, errors = result
            self._chat_append(f"[DELETE ALL] Готово: удалено {deleted}, ошибок {errors}")
            self._set_progress(deleted + errors, max(deleted + errors, 1), "Delete all")
            self._refresh_sources_combo()

        self._schedule(_wipe_all(), on_success=_ok)

    def _choose_folder_upload(self):
        folder = filedialog.askdirectory(title="Выберите папку для полной перезагрузки")
        if folder:
            self._folder_to_upload = Path(folder)
            self.lbl_folder.configure(text=self._folder_to_upload.name, text_color="white")

    def _reupload_folder(self):
        if not self.client:
            messagebox.showwarning("Нет подключения", "Сначала подключитесь.")
            return
        if not self.selected_nb_id:
            messagebox.showwarning("Блокнот", "Выберите блокнот.")
            return
        if not self._folder_to_upload or not self._folder_to_upload.exists():
            messagebox.showwarning("Папка", "Сначала выберите папку для загрузки.")
            return

        files = [
            f for f in sorted(self._folder_to_upload.rglob('*'))
            if f.is_file() and not f.name.startswith('.') and f.suffix.lower() in _SUPPORTED_UPLOAD_EXTS
        ]
        if not files:
            messagebox.showwarning("Папка", "Подходящие файлы не найдены.")
            return

        if not messagebox.askyesno(
            "Reupload Folder",
            f"Загрузить {len(files)} файлов в выбранный блокнот?",
        ):
            return

        nb_id = self.selected_nb_id
        self._chat_append(f"[REUPLOAD] Старт: {len(files)} файлов из {self._folder_to_upload}")

        async def _upload_all():
            total = len(files)
            uploaded = 0
            errors = 0
            self.after(0, lambda: self._set_progress(0, total, "Reupload"))
            for i, fpath in enumerate(files, 1):
                try:
                    await self.client.sources.add_file(nb_id, fpath)
                    uploaded += 1
                    self.after(0, lambda n=fpath.name, a=i, t=total: self._chat_append(f"  ☁️ [{a}/{t}] {n}"))
                except Exception as e:
                    errors += 1
                    self.after(0, lambda n=fpath.name, err=str(e): self._chat_append(f"  ❌ {n}: {err}"))
                done = uploaded + errors
                self.after(0, lambda d=done, t=total: self._set_progress(d, t, "Reupload"))
            return uploaded, errors

        def _ok(result):
            uploaded, errors = result
            self._chat_append(f"[REUPLOAD] Готово: загружено {uploaded}, ошибок {errors}")
            self._set_progress(uploaded + errors, max(uploaded + errors, 1), "Reupload")
            self._refresh_sources_combo()

        self._schedule(_upload_all(), on_success=_ok)

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
