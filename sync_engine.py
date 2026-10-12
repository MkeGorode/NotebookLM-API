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
import re
from datetime import date
from pathlib import Path
from typing import Callable

from core_notebooklm import NotebookLMSync

SUPPORTED_EXTS = {
    ".pdf", ".txt", ".md", ".docx", ".csv",
    ".png", ".jpg", ".jpeg", ".mp3", ".mp4", ".wav",
}
DEFAULT_CONCURRENCY = 4
DEFAULT_INDEX_PATTERN = "INDEX"
MTIME_SLACK_SEC = 5
PROCESSING_STATUSES = (1, 5)  # PROCESSING, PREPARING
DEFAULT_POST_PROMPT = (
    "Источники блокнота «{notebook}» обновлены {date}. Новые и изменённые файлы:\n"
    "{new_files}\n\n"
    "Опираясь на самый свежий индекс и эти файлы, кратко перечисли главное: ключевые факты, "
    "решения, договорённости и следующие шаги."
)
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

    @property
    def index_pattern(self) -> str:
        return str(self.settings.get("index_pattern", DEFAULT_INDEX_PATTERN))

    @index_pattern.setter
    def index_pattern(self, value: str):
        self.settings["index_pattern"] = value.strip()
        self._save()

    def post_prompt(self, name: str | None) -> tuple[bool, str]:
        """(enabled, prompt template) used after a sync of template *name*."""
        p = self.presets.get(name or "", {})
        return bool(p.get("ask_after", True)), p.get("post_prompt") or DEFAULT_POST_PROMPT


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


def folder_diff(files: list[Path], sources: list, index_pattern: str | None = None) -> dict:
    """Plan a sync between a folder and notebook sources.

    - new files are uploaded, sources without a local file are deleted;
    - files modified locally after their source was uploaded are replaced;
    - index files (name contains *index_pattern*) are grouped by name without the date and only the
      newest one is kept: older index versions in the notebook are deleted, older local ones ignored;
    - failed (red) sources are deleted so the file is re-uploaded.
    """
    pattern = DEFAULT_INDEX_PATTERN if index_pattern is None else index_pattern
    regular, latest_idx, stale_idx = split_index_files(files, pattern)
    usable = [s for s in sources if source_id(s)]
    failed = [s for s in usable if getattr(s, "status", 2) == 3]
    healthy = [s for s in usable if getattr(s, "status", 2) != 3]
    remote_idx = [s for s in healthy if is_index(source_title(s), pattern)]
    remote_reg = [s for s in healthy if not is_index(source_title(s), pattern)]

    new, _ = NotebookLMSync.compute_delta(regular, remote_reg)
    new_set = set(new)
    changed, changed_del = [], []
    for f in regular:
        if f in new_set:
            continue
        olds = _exact_matches(f, remote_reg)
        if olds and any(_modified_after_upload(f, s) for s in olds):
            changed.append(f)
            changed_del += [(source_id(s), source_title(s)) for s in olds]
    orphans = list(NotebookLMSync.compute_remote_orphans(regular, remote_reg))

    index_upload, index_delete = [], []
    latest_families = {}
    for f in latest_idx:
        latest_families[_family(f.name)] = f
    for fam, f in latest_families.items():
        remote = [s for s in remote_idx if _family(source_title(s)) == fam]
        up_to_date = (len(remote) == 1 and source_title(remote[0]).strip() == f.name
                      and not _modified_after_upload(f, remote[0]))
        if not up_to_date:
            index_upload.append(f)
            index_delete += [(source_id(s), source_title(s)) for s in remote]
    index_delete += [(source_id(s), source_title(s)) for s in remote_idx
                     if _family(source_title(s)) not in latest_families]

    delete, seen = [], set()
    for sid, title in changed_del + orphans + index_delete + [(source_id(s), source_title(s)) for s in failed]:
        if sid not in seen:
            seen.add(sid)
            delete.append((sid, title))
    return {"files": len(files), "sources": len(sources), "new": new, "changed": changed,
            "orphans": orphans, "failed": len(failed), "index_upload": index_upload,
            "index_delete": index_delete, "stale_index": stale_idx,
            "upload": new + changed + index_upload, "delete": delete}


# ------------------------------------------------------------ index files --
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")


def is_index(name: str, pattern: str | None) -> bool:
    return bool(pattern) and pattern.lower() in name.lower()


def _family(name: str) -> str:
    """Name without dates/extension: '00_MASTER_INDEX_All_Calls_2026-10-12.txt' -> '00_master_index_all_calls'."""
    return re.sub(r"[\s_\-.]+$", "", _DATE_RE.sub("", Path(name.strip()).stem)).lower()


def _version(path: Path):
    m = _DATE_RE.search(path.stem)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return ("".join(m.groups()) if m else "", mtime)


def split_index_files(files: list[Path], pattern: str | None):
    """Return (regular files, newest file of each index family, older index versions)."""
    regular, groups = [], {}
    for f in files:
        if is_index(f.name, pattern):
            groups.setdefault(_family(f.name), []).append(f)
        else:
            regular.append(f)
    latest, stale = [], []
    for group in groups.values():
        group.sort(key=_version)
        latest.append(group[-1])
        stale += group[:-1]
    return regular, latest, stale


def _exact_matches(path: Path, sources: list) -> list:
    name, stem = path.name.strip().lower(), path.stem.strip().lower()
    out = []
    for s in sources:
        t = source_title(s).strip().lower()
        if t == name or Path(t).stem == stem:
            out.append(s)
    return out


def _modified_after_upload(path: Path, src) -> bool:
    created = getattr(src, "created_at", None)
    if not created:
        return False
    try:
        return path.stat().st_mtime > created.timestamp() + MTIME_SLACK_SEC
    except (OSError, ValueError, OverflowError):
        return False


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


def _tracking(on_event: EventCb, names: list[str]):
    def _cb(kind, **d):
        if kind == "uploaded":
            names.append(d["name"])
        _emit(on_event, kind, **d)
    return _cb


async def reload(client, notebook_id: str, files: list[Path],
                 concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None,
                 index_pattern: str | None = None) -> dict:
    """Delete every source, then upload *files* (only the newest version of each index file).

    Upload is skipped if any delete failed.
    """
    pattern = DEFAULT_INDEX_PATTERN if index_pattern is None else index_pattern
    regular, latest_idx, _ = split_index_files(files, pattern)
    deleted, del_err = await wipe_all(client, notebook_id, concurrency, on_event)
    if del_err:
        return {"deleted": deleted, "delete_errors": del_err, "uploaded": 0, "upload_errors": 0,
                "aborted": True, "uploaded_names": [], "index": []}
    names: list[str] = []
    uploaded, up_err = await upload_many(client, notebook_id, regular + latest_idx, concurrency,
                                         _tracking(on_event, names))
    return {"deleted": deleted, "delete_errors": 0, "uploaded": uploaded, "upload_errors": up_err,
            "aborted": False, "uploaded_names": names, "index": [f.name for f in latest_idx]}


async def sync_delta(client, notebook_id: str, files: list[Path],
                     concurrency: int = DEFAULT_CONCURRENCY, on_event: EventCb = None,
                     index_pattern: str | None = None) -> dict:
    """Bring the notebook in line with the folder using the plan from :func:`folder_diff`."""
    plan = folder_diff(files, await client.sources.list(notebook_id), index_pattern)
    deleted, del_err = await delete_many(client, notebook_id, plan["delete"], concurrency, on_event)
    names: list[str] = []
    uploaded, up_err = await upload_many(client, notebook_id, plan["upload"], concurrency,
                                         _tracking(on_event, names))
    return {"deleted": deleted, "delete_errors": del_err, "uploaded": uploaded, "upload_errors": up_err,
            "aborted": False, "uploaded_names": names, "index": [f.name for f in plan["index_upload"]]}


async def wait_processed(client, notebook_id: str, timeout: float = 300, on_event: EventCb = None) -> int:
    """Wait until NotebookLM finishes processing new sources. Returns how many are still pending."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        sources = await client.sources.list(notebook_id)
        pending = sum(1 for s in sources if getattr(s, "status", 2) in PROCESSING_STATUSES)
        _emit(on_event, "progress", phase="processing", done=len(sources) - pending, total=len(sources))
        if not pending or loop.time() >= deadline:
            return pending
        await asyncio.sleep(min(5, max(0.5, deadline - loop.time())))


def build_prompt(template: str, result: dict, notebook_title: str) -> str:
    listing = "\n".join(f"- {n}" for n in sorted(result.get("uploaded_names") or [])) or "- (нет)"
    return (template.replace("{new_files}", listing).replace("{notebook}", notebook_title)
            .replace("{date}", date.today().isoformat()))


async def ask_after_sync(client, notebook_id: str, notebook_title: str, template: str, result: dict,
                         on_event: EventCb = None, wait_timeout: float = 300) -> dict | None:
    """After a successful sync with uploads: wait for processing, then ask *template* in the chat."""
    if not template.strip() or result.get("aborted") or not result.get("uploaded"):
        return None
    pending = await wait_processed(client, notebook_id, wait_timeout, on_event)
    question = build_prompt(template, result, notebook_title)
    _emit(on_event, "progress", phase="ask", done=0, total=1)
    answer = await client.chat.ask(notebook_id, question)
    _emit(on_event, "progress", phase="ask", done=1, total=1)
    return {"question": question, "answer": getattr(answer, "answer", str(answer)), "pending": pending}
