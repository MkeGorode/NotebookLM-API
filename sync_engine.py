"""
Headless sync engine shared by the GUI (main.py) and the CLI (cli.py).

All operations are async, take an already-connected notebooklm client and
report progress through an optional ``on_event(kind, **data)`` callback:

    progress  phase, done, total
    deleted   sid, title
    uploaded  source, name
    error     title, error
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from core_notebooklm import NotebookLMSync

SUPPORTED_EXTS = {
    ".pdf", ".txt", ".md", ".docx", ".csv",
    ".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".wav",
}
DEFAULT_CONCURRENCY = 4
PRESETS_FILE = Path(__file__).with_name("presets.json")

EventCb = Callable[..., None] | None


def _emit(cb: EventCb, kind: str, **data):
    if cb:
        cb(kind, **data)


# ----------------------------------------------------------------- presets --
class PresetStore:
    """Named templates (storage path + folder + notebook) and global settings, persisted as JSON."""

    def __init__(self, path: Path = PRESETS_FILE):
        self.path = path
        self.presets: dict[str, dict] = {}
        self.settings: dict = {}
        self.last: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.presets = dict(data.get("presets", {}))
            self.settings = dict(data.get("settings", {}))
            self.last = data.get("last")
        except (OSError, ValueError):
            pass

    def _save(self):
        payload = {"presets": self.presets, "settings": self.settings, "last": self.last}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def names(self) -> list[str]:
        return sorted(self.presets, key=str.lower)

    def get(self, name: str) -> dict | None:
        return self.presets.get(name)

    def put(self, name: str, data: dict):
        self.presets[name] = data
        self.last = name
        self._save()

    def update(self, name: str, **fields):
        if name in self.presets:
            self.presets[name].update(fields)
            self._save()

    def remove(self, name: str):
        self.presets.pop(name, None)
        if self.last == name:
            self.last = None
        self._save()

    def touch(self, name: str):
        self.last = name
        self._save()

    @property
    def concurrency(self) -> int:
        try:
            return max(1, int(self.settings.get("concurrency", DEFAULT_CONCURRENCY)))
        except (TypeError, ValueError):
            return DEFAULT_CONCURRENCY

    @concurrency.setter
    def concurrency(self, value: int):
        self.settings["concurrency"] = int(value)
        self._save()


# ----------------------------------------------------------------- helpers --
def folder_files(folder: Path | str | None) -> list[Path]:
    if not folder:
        return []
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return [f for f in sorted(folder.rglob("*"))
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in SUPPORTED_EXTS]


def source_id(src) -> str | None:
    for attr in ("id", "source_id", "uuid"):
        val = getattr(src, attr, None)
        if val:
            return str(val)
    return None


def source_title(src) -> str:
    return str(getattr(src, "title", "—") or "—")


def folder_diff(files: list[Path], sources: list) -> dict:
    """Compare a folder with notebook sources: what is new locally / orphaned remotely."""
    new, _ = NotebookLMSync.compute_delta(files, sources)
    orphans = NotebookLMSync.compute_remote_orphans(files, sources)
    return {"files": len(files), "sources": len(sources), "new": new, "orphans": orphans}


def resolve_notebook(notebooks: list, ref: str):
    """Find a notebook by id, exact title or unique case-insensitive title prefix."""
    for nb in notebooks:
        if nb.id == ref or nb.title == ref:
            return nb
    low = ref.lower()
    hits = [nb for nb in notebooks if nb.title.lower().startswith(low)]
    return hits[0] if len(hits) == 1 else None


async def connect(storage_path: str | None = None):
    sync = NotebookLMSync(storage_path or None)
    notebooks = await sync.connect()
    return sync.client, notebooks


async def delete_source(client, notebook_id: str, sid: str):
    """Delete a source, tolerating the different signatures across notebooklm-py versions."""
    mgr = client.sources
    for method_name, args in (("delete", (notebook_id, sid)), ("delete", (sid,)),
                              ("remove", (notebook_id, sid)), ("remove", (sid,))):
        fn = getattr(mgr, method_name, None)
        if not fn:
            continue
        try:
            result = fn(*args)
        except TypeError:
            continue
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            return await result
        return result
    raise RuntimeError("NotebookLM API does not expose sources.delete/remove in this version.")


# -------------------------------------------------------------- operations --
async def _run_pool(items, worker, concurrency: int, phase: str, on_event: EventCb):
    total = len(items)
    done = ok = 0
    sem = asyncio.Semaphore(max(1, concurrency))
    _emit(on_event, "progress", phase=phase, done=0, total=total)

    async def _one(item):
        nonlocal done, ok
        async with sem:
            success = await worker(item)
        done += 1
        ok += int(success)
        _emit(on_event, "progress", phase=phase, done=done, total=total)

    await asyncio.gather(*(_one(i) for i in items))
    return ok, total - ok


async def delete_many(client, notebook_id: str, targets: list[tuple[str, str]],
                      concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None,
                      phase: str = "delete"):
    """Delete ``(source_id, title)`` pairs in parallel. Returns (deleted, errors)."""
    async def _worker(t):
        sid, title = t
        try:
            await delete_source(client, notebook_id, sid)
            _emit(on_event, "deleted", sid=sid, title=title)
            return True
        except Exception as exc:
            _emit(on_event, "error", title=title, error=str(exc))
            return False

    return await _run_pool(targets, _worker, concurrency, phase, on_event)


async def upload_many(client, notebook_id: str, files: list[Path],
                      concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None,
                      phase: str = "upload"):
    """Upload files in parallel. Returns (uploaded, errors)."""
    async def _worker(path: Path):
        try:
            src = await client.sources.add_file(notebook_id, path)
            _emit(on_event, "uploaded", source=src, name=path.name)
            return True
        except Exception as exc:
            _emit(on_event, "error", title=path.name, error=str(exc))
            return False

    return await _run_pool(files, _worker, concurrency, phase, on_event)


async def wipe_all(client, notebook_id: str, concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None):
    sources = await client.sources.list(notebook_id)
    targets = [(sid, source_title(s)) for s in sources if (sid := source_id(s))]
    deleted, errors = await delete_many(client, notebook_id, targets, concurrency, on_event)
    return deleted, errors + (len(sources) - len(targets))


async def reload(client, notebook_id: str, files: list[Path],
                 concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None) -> dict:
    """Delete every source, then upload *files*. Upload is skipped if any delete failed."""
    deleted, del_err = await wipe_all(client, notebook_id, concurrency, on_event)
    if del_err:
        return {"deleted": deleted, "delete_errors": del_err, "uploaded": 0, "upload_errors": 0, "aborted": True}
    uploaded, up_err = await upload_many(client, notebook_id, files, concurrency, on_event)
    return {"deleted": deleted, "delete_errors": 0, "uploaded": uploaded, "upload_errors": up_err, "aborted": False}


async def sync_delta(client, notebook_id: str, files: list[Path],
                     concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None) -> dict:
    """Upload only files missing in the notebook and delete sources with no local file."""
    sources = await client.sources.list(notebook_id)
    diff = folder_diff(files, sources)
    deleted, del_err = await delete_many(client, notebook_id, diff["orphans"], concurrency, on_event)
    uploaded, up_err = await upload_many(client, notebook_id, diff["new"], concurrency, on_event)
    return {"deleted": deleted, "delete_errors": del_err, "uploaded": uploaded, "upload_errors": up_err,
            "aborted": False}
