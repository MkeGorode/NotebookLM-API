"""
core_notebooklm.py — Headless NotebookLM API wrapper for VirtualExpert Liga.
Provides async operations: connect, login, list notebooks/sources, upload files.
No GUI dependencies — can be used standalone or embedded in main_ui.
"""

import asyncio
import hashlib
import logging
import threading
from pathlib import Path

_logger = logging.getLogger('VirtualExpert.notebooklm')


def _sha256_file(filepath: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65_536), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Async event-loop helper (shared daemon thread)
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


def run_async(coro):
    """Schedule *coro* on the shared loop, return a concurrent.futures.Future."""
    loop = _ensure_loop()

    async def _wrapper():
        return await coro

    return asyncio.run_coroutine_threadsafe(_wrapper(), loop)


# ---------------------------------------------------------------------------
# Domain migration patch
# ---------------------------------------------------------------------------
_NLM_PATCH_APPLIED = False

def _patch_notebooklm_domain():
    """Patch all hardcoded notebooklm.google.com → notebook.google.com.

    Google renamed the service and the old domain now 302-redirects to
    accounts.google.com (login).  The ``notebooklm-py`` library is still
    hardcoded to the old domain, so we monkey-patch every reference we can
    find at import time.
    """
    global _NLM_PATCH_APPLIED
    if _NLM_PATCH_APPLIED:
        return
    _NLM_PATCH_APPLIED = True

    OLD = "notebooklm.google.com"
    NEW = "notebook.google.com"

    # 1. auth.py — fetch_tokens URL + ALLOWED_COOKIE_DOMAINS
    try:
        import notebooklm.auth as _auth
        if NEW not in _auth.ALLOWED_COOKIE_DOMAINS:
            _auth.ALLOWED_COOKIE_DOMAINS = _auth.ALLOWED_COOKIE_DOMAINS | {NEW}

        _orig_fetch_tokens = _auth.fetch_tokens
        async def _patched_fetch_tokens(cookies):
            """fetch_tokens with notebook.google.com instead of the old domain."""
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            import httpx
            from notebooklm._url_utils import is_google_auth_redirect
            from notebooklm.auth import extract_csrf_from_html, extract_session_id_from_html
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://{NEW}/",
                    headers={"Cookie": cookie_header},
                    follow_redirects=True,
                    timeout=30.0,
                )
                response.raise_for_status()
                final_url = str(response.url)
                if is_google_auth_redirect(final_url):
                    raise ValueError(
                        "Authentication expired or invalid. "
                        "Redirected to: " + final_url + "\n"
                        "Run 'notebooklm login' to re-authenticate."
                    )
                csrf = extract_csrf_from_html(response.text, final_url)
                session_id = extract_session_id_from_html(response.text, final_url)
                return csrf, session_id
        _auth.fetch_tokens = _patched_fetch_tokens
    except Exception as exc:
        _logger.warning("Failed to patch notebooklm.auth: %s", exc)

    # 2. rpc/types.py — BATCHEXECUTE_URL, QUERY_URL, UPLOAD_URL
    try:
        import notebooklm.rpc.types as _rpc_types
        import notebooklm.rpc as _rpc_pkg
        import notebooklm._core as _core_mod
        for attr in ("BATCHEXECUTE_URL", "QUERY_URL", "UPLOAD_URL"):
            val = getattr(_rpc_types, attr, "")
            if OLD in val:
                new_val = val.replace(OLD, NEW)
                setattr(_rpc_types, attr, new_val)
                # Patch re-exported copies in rpc/__init__ and _core
                if hasattr(_rpc_pkg, attr):
                    setattr(_rpc_pkg, attr, new_val)
                if hasattr(_core_mod, attr):
                    setattr(_core_mod, attr, new_val)
        # Also patch the local copy in _sources (imported at module load time)
        try:
            import notebooklm._sources as _src
            if hasattr(_src, 'UPLOAD_URL') and OLD in getattr(_src, 'UPLOAD_URL', ''):
                _src.UPLOAD_URL = _src.UPLOAD_URL.replace(OLD, NEW)
                _logger.info("Patched _sources.UPLOAD_URL → %s", _src.UPLOAD_URL)
        except Exception:
            pass
    except Exception as exc:
        _logger.warning("Failed to patch notebooklm.rpc.types: %s", exc)

    # 3. client.py — refresh_auth fetches from old domain
    try:
        import notebooklm.client as _client_mod
        _orig_refresh = _client_mod.NotebookLMClient.refresh_auth
        import re as _re
        from notebooklm._url_utils import is_google_auth_redirect as _is_redir
        async def _patched_refresh(self):
            http_client = self._core.get_http_client()
            response = await http_client.get(f"https://{NEW}/")
            response.raise_for_status()
            final_url = str(response.url)
            if _is_redir(final_url):
                raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
            csrf_match = _re.search(r'"SNlM0e":"([^"]+)"', response.text)
            if not csrf_match:
                raise ValueError("Failed to extract CSRF token (SNlM0e).")
            self._core.auth.csrf_token = csrf_match.group(1)
            sid_match = _re.search(r'"FdrFJe":"([^"]+)"', response.text)
            if not sid_match:
                raise ValueError("Failed to extract session ID (FdrFJe).")
            self._core.auth.session_id = sid_match.group(1)
            self._core.update_auth_headers()
            return self._core.auth
        _client_mod.NotebookLMClient.refresh_auth = _patched_refresh
    except Exception as exc:
        _logger.warning("Failed to patch notebooklm.client: %s", exc)

    # 4. _sources.py — Origin / Referer headers in upload methods
    #    Both methods create their OWN httpx.AsyncClient (not self._core),
    #    so we must temporarily replace httpx.AsyncClient globally.
    try:
        import notebooklm._sources as _sources_mod
        import httpx as _httpx_mod

        _SourcesAPI = getattr(_sources_mod, 'SourcesAPI', None)
        if _SourcesAPI is None:
            # Fallback: scan for the class
            for _name in dir(_sources_mod):
                _obj = getattr(_sources_mod, _name)
                if isinstance(_obj, type) and hasattr(_obj, '_start_resumable_upload'):
                    _SourcesAPI = _obj
                    break

        if _SourcesAPI is not None:
            _orig_start = _SourcesAPI._start_resumable_upload
            _orig_stream = _SourcesAPI._upload_file_streaming

            def _make_patched_client(real_cls):
                """Create a subclass of httpx.AsyncClient that fixes Origin/Referer."""
                class _Patched(real_cls):
                    async def post(self, url, *a, headers=None, **kw):
                        if headers:
                            h = dict(headers)
                            for k in ('Origin', 'Referer', 'origin', 'referer'):
                                if k in h and OLD in h[k]:
                                    h[k] = h[k].replace(OLD, NEW)
                            headers = h
                        if isinstance(url, str) and OLD in url:
                            url = url.replace(OLD, NEW)
                        return await super().post(url, *a, headers=headers, **kw)
                return _Patched

            async def _patched_start(self, notebook_id, filename, file_size, source_id):
                """Wrapper: temporarily swaps httpx.AsyncClient to fix headers."""
                real_cls = _httpx_mod.AsyncClient
                _httpx_mod.AsyncClient = _make_patched_client(real_cls)
                try:
                    return await _orig_start(self, notebook_id, filename, file_size, source_id)
                finally:
                    _httpx_mod.AsyncClient = real_cls

            async def _patched_stream(self, upload_url, file_path):
                """Wrapper: temporarily swaps httpx.AsyncClient to fix headers."""
                real_cls = _httpx_mod.AsyncClient
                _httpx_mod.AsyncClient = _make_patched_client(real_cls)
                try:
                    return await _orig_stream(self, upload_url, file_path)
                finally:
                    _httpx_mod.AsyncClient = real_cls

            _SourcesAPI._start_resumable_upload = _patched_start
            _SourcesAPI._upload_file_streaming = _patched_stream
            _logger.info("Patched _sources.py upload methods (Origin/Referer → %s)", NEW)
        else:
            _logger.warning("Could not find SourcesAPI class in _sources.py")
    except Exception as exc:
        _logger.warning("Failed to patch notebooklm._sources: %s", exc)

    # 5. cli/session.py — NOTEBOOKLM_URL / NOTEBOOKLM_HOST
    try:
        import notebooklm.cli.session as _cli_session
        if hasattr(_cli_session, "NOTEBOOKLM_URL"):
            _cli_session.NOTEBOOKLM_URL = f"https://{NEW}/"
        if hasattr(_cli_session, "NOTEBOOKLM_HOST"):
            _cli_session.NOTEBOOKLM_HOST = NEW
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NotebookLMSync — headless API layer
# ---------------------------------------------------------------------------

class NotebookLMSync:
    """Headless wrapper around notebooklm-py for use from any UI or CLI."""

    def __init__(self, storage_path: str | None = None):
        self.storage_path = storage_path
        self.client = None
        self.notebooks: list = []

    # --- Connection ---

    async def connect(self) -> list:
        """Connect to NotebookLM, return list of notebooks."""
        import notebooklm
        from notebooklm import NotebookLMClient
        import notebooklm.exceptions

        # MONKEY PATCH: Google renamed NotebookLM to notebook.google.com.
        # The `notebooklm-py` package is hardcoded to notebooklm.google.com
        # in many modules. We patch all of them here before connecting.
        _patch_notebooklm_domain()

        self.client = await NotebookLMClient.from_storage(
            self.storage_path if self.storage_path else None
        )
        await self.client.__aenter__()
        self.notebooks = await self.client.notebooks.list()
        return self.notebooks

    @property
    def is_connected(self) -> bool:
        return self.client is not None

    @staticmethod
    def browser_login(wait_callback=None) -> tuple[bool, str]:
        """Open a Playwright browser for Google / NotebookLM login.

        *wait_callback* — an optional callable that **blocks** until the user
        confirms login is complete (e.g. ``messagebox.askokcancel``).
        Must return ``True`` to save the session or ``False`` to cancel.
        If *None*, the method waits for the browser to be closed manually.

        If launch_persistent_context fails (corrupt profile), automatically
        cleans the browser_profile directory and retries once.

        Returns (success, message).
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, (
                "Playwright not installed.\n"
                "Install: pip install 'notebooklm-py[browser]'\n"
                "Then:    playwright install chromium"
            )

        from notebooklm.paths import get_storage_path, get_browser_profile_dir

        storage_path = get_storage_path()
        profile_dir = get_browser_profile_dir()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Apply domain patch so ALLOWED_COOKIE_DOMAINS includes the new host
        _patch_notebooklm_domain()

        NOTEBOOKLM_URL = "https://notebook.google.com/"
        GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"
        NOTEBOOKLM_HOST = "notebook.google.com"

        def _safe_goto(page, url: str, timeout_ms: int = 20_000) -> bool:
            """Navigate with a bounded timeout so login flow never stalls forever."""
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return True
            except Exception as exc:
                _logger.warning("Navigation timeout/error for %s: %s", url, exc)
                return False

        for attempt in range(2):
            try:
                with sync_playwright() as p:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        headless=False,
                        channel="msedge",
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--password-store=basic",
                        ],
                        ignore_default_args=["--enable-automation"],
                    )

                    page = context.pages[0] if context.pages else context.new_page()
                    _safe_goto(page, NOTEBOOKLM_URL)

                    # Wait for user to confirm login
                    if wait_callback is not None:
                        confirmed = wait_callback()
                    else:
                        # Fallback: wait until the browser window is closed
                        try:
                            page.wait_for_event("close", timeout=300_000)
                        except Exception:
                            pass
                        confirmed = True

                    if not confirmed:
                        context.close()
                        return False, "Login cancelled by user."

                    # Navigate to collect all necessary cookies
                    _safe_goto(page, GOOGLE_ACCOUNTS_URL)
                    _safe_goto(page, NOTEBOOKLM_URL)

                    current_url = page.url
                    if NOTEBOOKLM_HOST not in current_url:
                        _logger.warning("After login, URL is %s (expected notebook.google.com)", current_url)

                    context.storage_state(path=str(storage_path))
                    context.close()

                return True, "Browser login completed."

            except Exception as exc:
                err = str(exc)
                if ("TargetClosedError" in err or "launch_persistent_context" in err):
                    if attempt == 0 and profile_dir.exists():
                        _logger.warning(
                            "Browser profile appears corrupted, cleaning %s and retrying…",
                            profile_dir,
                        )
                        import shutil
                        shutil.rmtree(profile_dir, ignore_errors=True)
                        continue
                    return False, (
                        "Browser profile is corrupted and cleanup did not help.\n"
                        f"Try manually deleting:\n  {profile_dir}\n\n{err[:300]}"
                    )
                return False, err

        return False, "Login failed after retry."

    # --- Notebooks ---

    async def list_notebooks(self) -> list:
        self.notebooks = await self.client.notebooks.list()
        return self.notebooks

    def get_notebook_id_by_title(self, title: str) -> str | None:
        for nb in self.notebooks:
            if nb.title == title:
                return nb.id
        return None

    async def rename_notebook(self, notebook_id: str, new_title: str):
        """Rename a notebook on the server. Returns the updated Notebook."""
        return await self.client.notebooks.rename(notebook_id, new_title)

    # --- Sources ---

    async def list_sources(self, notebook_id: str) -> list:
        return await self.client.sources.list(notebook_id)

    async def upload_file(self, notebook_id: str, file_path: Path):
        """Upload a single file. Returns the source object."""
        return await self.client.sources.add_file(notebook_id, file_path)

    async def delete_failed_sources(self, notebook_id: str, sources: list | None = None) -> list:
        """Delete sources that failed to import (status == 3/ERROR).

        Returns the list of deleted source titles. Failed ("red") sources
        otherwise block re-uploading the corresponding local file.
        """
        if sources is None:
            sources = await self.list_sources(notebook_id)
        deleted = []
        for src in sources:
            if getattr(src, 'status', 2) == 3:
                sid = getattr(src, 'id', None)
                if not sid:
                    continue
                try:
                    await self.client.sources.delete(notebook_id, sid)
                    deleted.append(getattr(src, 'title', sid))
                except Exception:
                    pass
        return deleted

    async def delete_source(self, notebook_id: str, source_id: str):
        """Delete a single source from a notebook by its source id.

        Used to remove orphaned cloud sources whose local file was deleted,
        so the NotebookLM notebook stays in sync with the local export.
        """
        return await self.client.sources.delete(notebook_id, source_id)

    # --- Delta computation ---

    @staticmethod
    def compute_delta(
        local_files: list[Path],
        remote_sources: list,
        db_uploads: dict[str, str] | None = None,
    ) -> tuple[list[Path], set[str]]:
        """Compare local exported files against what's already in NotebookLM.

        Comparison strategy (layered):
        1. If *db_uploads* is provided (from nlm_uploads table):
           - file present + hash matches  → skip (unchanged)
           - file present + hash differs  → UPLOAD (content updated)
           - file absent                  → check remote titles
        2. Fallback (no DB): compare by filename / title against remote_sources.

        Returns ``(to_upload, reuploads)``:
        - *to_upload* — list of local files that need to be (re-)uploaded.
        - *reuploads* — set of filenames where the content changed (hash
          differs from DB). These files already exist in NLM and their old
          version must be **deleted before** re-uploading to avoid duplicates.
        """
        # Build remote titles set (used as fallback when DB has no record).
        # IMPORTANT: skip sources that FAILED to import (status == 3/ERROR) so a
        # broken/red source does not block re-uploading the file.
        remote_titles = set()
        remote_titles_lower = []   # for fuzzy substring matching
        for src in remote_sources:
            # SourceStatus: 1=PROCESSING, 2=READY, 3=ERROR, 5=PREPARING.
            if getattr(src, 'status', 2) == 3:
                continue  # failed import → treat as absent so it gets retried
            title = getattr(src, 'title', '')
            if title:
                t = title.strip()
                remote_titles.add(t)
                remote_titles.add(Path(t).stem.strip())
                remote_titles_lower.append(t.lower())

        def _exists_in_remote(fpath: Path) -> bool:
            """Check if file already exists in remote by exact or fuzzy match."""
            name = fpath.name.strip()
            stem = fpath.stem.strip()
            # Exact match
            if name in remote_titles or stem in remote_titles:
                return True
            # Fuzzy: local stem appears inside any remote title
            stem_lower = stem.lower()
            for rt in remote_titles_lower:
                if stem_lower in rt:
                    return True
            return False

        reuploads: set[str] = set()  # filenames whose content changed

        if db_uploads is not None:
            to_upload = []
            for fpath in local_files:
                saved_hash = db_uploads.get(fpath.name)
                if saved_hash is not None:
                    # File tracked in DB — compare hashes
                    local_hash = _sha256_file(fpath)
                    if saved_hash != local_hash:
                        to_upload.append(fpath)
                        reuploads.add(fpath.name)
                else:
                    # Not in DB — fall back to remote title check
                    if not _exists_in_remote(fpath):
                        to_upload.append(fpath)
            return to_upload, reuploads

        # Fallback: title-based matching (no DB available)
        to_upload = []
        for fpath in local_files:
            if not _exists_in_remote(fpath):
                to_upload.append(fpath)
        return to_upload, reuploads

    @staticmethod
    def find_matching_sources(filename: str, remote_sources: list) -> list[tuple[str, str]]:
        """Find all remote sources whose title matches *filename*.

        Returns a list of ``(source_id, title)`` tuples. Used to delete old
        versions before re-uploading updated content (e.g. guide files that
        change on every export).
        """
        name = filename.strip()
        stem = Path(name).stem.strip()
        name_lower = name.lower()
        stem_lower = stem.lower()
        matches = []
        for src in remote_sources:
            if getattr(src, 'status', 2) == 3:
                continue
            sid = getattr(src, 'id', None)
            title = getattr(src, 'title', '') or ''
            if not sid or not title:
                continue
            t = title.strip()
            t_lower = t.lower()
            t_stem = Path(t).stem.strip().lower()
            if (t_lower == name_lower or t_stem == stem_lower
                    or stem_lower in t_lower):
                matches.append((sid, t))
        return matches

    @staticmethod
    def compute_remote_orphans(local_files: list[Path], remote_sources: list) -> list:
        """Find remote sources that have no corresponding local file.

        These are "orphans" left in the NotebookLM notebook after their source
        file was deleted locally. Matching mirrors :meth:`compute_delta`
        (exact name/stem or fuzzy local-stem-in-remote-title). Failed sources
        (status == 3) are excluded — those are handled by
        :meth:`delete_failed_sources`.

        Returns a list of ``(source_id, title)`` tuples to delete.
        """
        local_names = set()
        local_stems_lower = []
        for f in local_files:
            local_names.add(f.name.strip())
            local_names.add(f.stem.strip())
            s = f.stem.strip().lower()
            if s:
                local_stems_lower.append(s)

        def _matches_local(title: str) -> bool:
            t = title.strip()
            stem = Path(t).stem.strip()
            if t in local_names or stem in local_names:
                return True
            t_lower = t.lower()
            return any(ls in t_lower for ls in local_stems_lower)

        orphans = []
        for src in remote_sources:
            if getattr(src, 'status', 2) == 3:
                continue  # failed import — handled by delete_failed_sources
            sid = getattr(src, 'id', None)
            title = getattr(src, 'title', '') or ''
            if not sid or not title:
                continue
            if not _matches_local(title):
                orphans.append((sid, title.strip()))
        return orphans

    # --- Batch upload with progress ---

    async def upload_delta(
        self,
        notebook_id: str,
        files: list[Path],
        on_progress=None,
        on_file_done=None,
    ) -> dict:
        """Upload a list of files sequentially (API limitation).
        
        on_progress(percent: float) — called after each file
        on_file_done(path: Path, success: bool, error: str|None)
        
        Returns {"uploaded": int, "errors": int, "total": int}
        """
        total = len(files)
        uploaded = 0
        errors = 0

        for i, fpath in enumerate(files):
            try:
                await self.client.sources.add_file(notebook_id, fpath)
                uploaded += 1
                if on_file_done:
                    on_file_done(fpath, True, None)
            except Exception as e:
                errors += 1
                _logger.error("Upload failed for %s: %s", fpath.name, e)
                if on_file_done:
                    on_file_done(fpath, False, str(e))

            if on_progress and total > 0:
                on_progress((i + 1) / total * 100)

        return {"uploaded": uploaded, "errors": errors, "total": total}

    # --- Chat (pass-through) ---

    async def ask(self, notebook_id: str, question: str):
        return await self.client.chat.ask(notebook_id, question)

    # --- Cleanup ---

    def close(self):
        if self.client and _async_loop and _async_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.client.__aexit__(None, None, None), _async_loop
            )
        self.client = None


def stop_loop():
    """Call on app exit to stop the background event loop."""
    global _async_loop
    if _async_loop and _async_loop.is_running():
        _async_loop.call_soon_threadsafe(_async_loop.stop)
    _async_loop = None
