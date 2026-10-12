"""
NotebookLM command-line interface (uses the same templates as the GUI).

Examples
  python cli.py presets
  python cli.py notebooks
  python cli.py sources -n "Дзвінки"
  python cli.py diff -p Дзвінки
  python cli.py reload -p Дзвінки -p Проект2 --yes          # several templates in parallel
  python cli.py reload --all-presets --yes
  python cli.py reload -n "Notebook title" -f D:\\Docs --yes
  python cli.py sync -p Дзвінки                               # only the difference, no full wipe
  python cli.py upload -n "Notebook" a.pdf b.txt
  python cli.py ask -n "Notebook" "Summarize the key points"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import sync_engine as eng

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Target:
    label: str
    storage: str | None
    notebook: str
    folder: str | None


class Clients:
    """One connection per storage file (= per Google account), shared by parallel jobs."""

    def __init__(self, default_storage: str | None):
        self.default = default_storage
        self._cache: dict[str, tuple] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, storage: str | None):
        key = storage or self.default or ""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._cache:
                self._cache[key] = await eng.connect(key or None)
        return self._cache[key]

    async def close(self):
        for client, _ in self._cache.values():
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass


def _targets(args, store: eng.PresetStore) -> list[Target]:
    names = list(args.preset or [])
    if getattr(args, "all_presets", False):
        names = store.names()
    out = []
    for name in names:
        data = store.get(name)
        if not data:
            sys.exit(f"Шаблон не найден: {name}. Есть: {', '.join(store.names()) or '—'}")
        out.append(Target(name, data.get("storage_path") or args.storage,
                          data.get("notebook_id") or data.get("notebook_title"), data.get("folder") or None))
    if args.notebook:
        out.append(Target(args.notebook, args.storage, args.notebook, getattr(args, "folder", None)))
    if not out:
        sys.exit("Укажите -p ШАБЛОН (можно несколько), --all-presets или -n БЛОКНОТ.")
    return out


async def _notebook(clients: Clients, t: Target):
    client, notebooks = await clients.get(t.storage)
    nb = eng.resolve_notebook(notebooks, t.notebook)
    if not nb:
        raise SystemExit(f"[{t.label}] блокнот не найден: {t.notebook}")
    return client, nb


def _printer(label: str, quiet: bool):
    last = {"t": 0.0}

    def on_event(kind, **d):
        if kind == "error":
            print(f"[{label}] ❌ {d['title']}: {d['error']}", file=sys.stderr)
        elif quiet:
            return
        elif kind == "deleted":
            print(f"[{label}] 🗑 {d['title']}")
        elif kind == "uploaded":
            print(f"[{label}] ☁ {d['name']}")
        elif kind == "progress":
            now = time.monotonic()
            if d["done"] in (0, d["total"]) or now - last["t"] > 2:
                last["t"] = now
                print(f"[{label}] {d['phase']}: {d['done']}/{d['total']}")
    return on_event


# ------------------------------------------------------------------ commands --
async def cmd_presets(args, store, clients):
    if not store.names():
        print("Шаблонов нет. Создайте их в GUI (кнопка ＋ в блоке «Шаблон»).")
    for name in store.names():
        p = store.get(name)
        print(f"{name}\n  блокнот: {p.get('notebook_title') or p.get('notebook_id') or '—'}"
              f"\n  папка:   {p.get('folder') or '—'}\n  storage: {p.get('storage_path') or '(по умолчанию)'}")


async def cmd_notebooks(args, store, clients):
    _, notebooks = await clients.get(args.storage)
    for nb in notebooks:
        print(f"{nb.id}  {nb.title}")


async def cmd_sources(args, store, clients):
    for t in _targets(args, store):
        client, nb = await _notebook(clients, t)
        sources = await client.sources.list(nb.id)
        print(f"== {nb.title} ({len(sources)})")
        for s in sources:
            print(f"  {eng.source_id(s)}  {eng.source_title(s)}")


async def cmd_diff(args, store, clients):
    for t in _targets(args, store):
        client, nb = await _notebook(clients, t)
        files = eng.folder_files(t.folder)
        d = eng.folder_diff(files, await client.sources.list(nb.id))
        print(f"== [{t.label}] {nb.title}: папка {d['files']} · блокнот {d['sources']} · "
              f"новых {len(d['new'])} · лишних {len(d['orphans'])}")
        for f in d["new"]:
            print(f"  + {f.name}")
        for _, title in d["orphans"]:
            print(f"  - {title}")


async def _bulk(args, store, clients, op_name: str):
    targets = _targets(args, store)
    plan = []
    for t in targets:
        files = eng.folder_files(t.folder)
        if not t.folder or not files:
            sys.exit(f"[{t.label}] папка не задана или пуста: {t.folder or '—'}")
        plan.append((t, files))

    if op_name == "reload" and not args.yes:
        print("Reload УДАЛИТ все источники и загрузит заново:")
        for t, files in plan:
            print(f"  [{t.label}] {t.notebook} ← {t.folder} ({len(files)} файлов)")
        if input("Продолжить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            sys.exit("Отменено.")

    op = eng.reload if op_name == "reload" else eng.sync_delta

    async def _run(t: Target, files):
        client, nb = await _notebook(clients, t)
        print(f"[{t.label}] {op_name} → {nb.title}: {len(files)} файлов, параллельно {args.jobs}")
        res = await op(client, nb.id, files, args.jobs, _printer(t.label, args.quiet))
        status = "ПРЕРВАНО (не все источники удалены)" if res["aborted"] else "готово"
        print(f"[{t.label}] {status}: удалено {res['deleted']}, загружено {res['uploaded']}, "
              f"ошибок {res['delete_errors'] + res['upload_errors']}")
        return res

    started = time.monotonic()
    results = await asyncio.gather(*(_run(t, f) for t, f in plan), return_exceptions=True)
    failed = 0
    for (t, _), r in zip(plan, results):
        if isinstance(r, BaseException):
            failed += 1
            print(f"[{t.label}] ❌ {r}", file=sys.stderr)
        elif r["aborted"] or r["delete_errors"] or r["upload_errors"]:
            failed += 1
    print(f"Итого: {len(plan)} задач за {time.monotonic() - started:.0f} с, с ошибками: {failed}")
    return 1 if failed else 0


async def cmd_reload(args, store, clients):
    return await _bulk(args, store, clients, "reload")


async def cmd_sync(args, store, clients):
    return await _bulk(args, store, clients, "sync")


async def cmd_upload(args, store, clients):
    client, nb = await _notebook(clients, Target(args.notebook, args.storage, args.notebook, None))
    files = [Path(f) for f in args.files]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        sys.exit("Нет файлов: " + ", ".join(missing))
    up, err = await eng.upload_many(client, nb.id, files, args.jobs, _printer(nb.title, args.quiet))
    print(f"Загружено {up}, ошибок {err}")
    return 1 if err else 0


async def cmd_ask(args, store, clients):
    t = _targets(args, store)[0]
    client, nb = await _notebook(clients, t)
    result = await client.chat.ask(nb.id, args.question)
    print(result.answer)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="NotebookLM CLI (шаблоны общие с GUI).",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--storage", help="путь к storage_state.json (по умолчанию — стандартный)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def targets(sp, folder=False):
        sp.add_argument("-p", "--preset", action="append", help="имя шаблона (можно повторять)")
        sp.add_argument("-n", "--notebook", help="id или название блокнота")
        if folder:
            sp.add_argument("-f", "--folder", help="папка (вместе с -n)")
            sp.add_argument("--all-presets", action="store_true", help="все шаблоны сразу")

    sub.add_parser("presets", help="список шаблонов")
    sub.add_parser("notebooks", help="список блокнотов")
    targets(sub.add_parser("sources", help="источники блокнота"))
    targets(sub.add_parser("diff", help="разница папка ↔ блокнот"), folder=True)
    for name, hlp in (("reload", "удалить всё и загрузить папку заново"),
                      ("sync", "загрузить новые, удалить лишние")):
        sp = sub.add_parser(name, help=hlp)
        targets(sp, folder=True)
        sp.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    up = sub.add_parser("upload", help="загрузить файлы")
    up.add_argument("-n", "--notebook", required=True)
    up.add_argument("files", nargs="+")
    ask = sub.add_parser("ask", help="задать вопрос блокноту")
    targets(ask)
    ask.add_argument("question")
    for sp in sub.choices.values():
        sp.add_argument("-j", "--jobs", type=int, default=None, help="параллельных операций в задаче")
        sp.add_argument("-q", "--quiet", action="store_true", help="только итоги и ошибки")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = eng.PresetStore()
    args.jobs = args.jobs or store.concurrency
    clients = Clients(args.storage)
    handler = globals()[f"cmd_{args.cmd}"]

    async def _main():
        try:
            return await handler(args, store, clients)
        finally:
            await clients.close()

    try:
        return asyncio.run(_main()) or 0
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
