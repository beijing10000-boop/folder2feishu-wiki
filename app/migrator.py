from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .db import Database, now_iso
from .feishu import FeishuError, FeishuWikiClient, parse_wiki_token


IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
WINDOWS_OFFLINE_ATTRIBUTE = 0x1000


def portable_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return "/".join(rel.parts)


def parent_rel(rel_path: str) -> str:
    if "/" not in rel_path:
        return ""
    return rel_path.rsplit("/", 1)[0]


def safe_remote_name(name: str, max_length: int = 250) -> str:
    cleaned = "".join(ch for ch in name if ord(ch) >= 32).strip()
    if not cleaned:
        cleaned = "未命名"
    if len(cleaned) <= max_length:
        return cleaned
    suffix = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
    stem, extension = os.path.splitext(cleaned)
    if len(extension) > 24:
        extension = extension[:24]
    room = max_length - len(extension) - len(suffix) - 2
    return f"{stem[:max(1, room)]}~{suffix}{extension}"


def file_sha256(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class SourceScanner:
    def __init__(self, db: Database):
        self.db = db

    def scan(
        self,
        root_value: str,
        *,
        reset: bool = False,
        progress: Callable[[int, str], None] | None = None,
    ) -> dict:
        root = Path(root_value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"本地目录不存在：{root}")
        old_root = str(self.db.get_meta("source_root", "") or "")
        if old_root and Path(old_root) != root and not reset:
            raise ValueError(
                f"当前台账属于 {old_root}。如需切换目录，请选择“重置台账后扫描”。"
            )
        if reset:
            self.db.reset_inventory()

        scan_id = uuid.uuid4().hex
        self.db.set_meta("source_root", str(root))
        self.db.set_meta("source_root_name", root.name)
        self.db.set_meta("scan_started_at", now_iso())
        counts = {"folders": 0, "files": 0, "bytes": 0, "offline": 0, "ignored": 0}

        def onerror(exc: OSError) -> None:
            self.db.add_event("error", f"扫描目录失败：{exc}", str(exc.filename or ""))

        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
            directory = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not (directory / name).is_symlink()
            )
            if directory != root:
                rel = portable_rel(directory, root)
                self.db.upsert_folder(
                    {
                        "rel_path": rel,
                        "parent_rel_path": parent_rel(rel),
                        "name": directory.name,
                        "remote_title": safe_remote_name(directory.name),
                        "depth": len(Path(rel).parts),
                    },
                    scan_id,
                )
                counts["folders"] += 1

            for name in sorted(filenames):
                source = directory / name
                if (
                    name.casefold() in IGNORED_NAMES
                    or name.startswith("~$")
                    or source.is_symlink()
                ):
                    counts["ignored"] += 1
                    continue
                rel = portable_rel(source, root)
                try:
                    stat = source.stat()
                except OSError as exc:
                    self.db.add_event("error", f"读取文件信息失败：{exc}", rel)
                    continue
                attrs = int(getattr(stat, "st_file_attributes", 0))
                offline = bool(attrs & WINDOWS_OFFLINE_ATTRIBUTE)
                self.db.upsert_file(
                    {
                        "rel_path": rel,
                        "parent_rel_path": parent_rel(rel),
                        "name": name,
                        "remote_name": safe_remote_name(name),
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "offline": offline,
                    },
                    scan_id,
                )
                counts["files"] += 1
                counts["bytes"] += stat.st_size
                counts["offline"] += int(offline)
                if progress and counts["files"] % 250 == 0:
                    progress(counts["files"], rel)

        self.db.finish_scan(scan_id)
        self.db.set_meta("scan_finished_at", now_iso())
        self.db.add_event(
            "success",
            f"扫描完成：{counts['folders']} 个目录，{counts['files']} 个文件",
            str(root),
        )
        return {**counts, "root": str(root), "scan_id": scan_id}


class MigrationManager:
    def __init__(
        self,
        db: Database,
        client_factory: Callable[[], FeishuWikiClient],
    ):
        self.db = db
        self.client_factory = client_factory
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._pause = threading.Event()
        self._stop = threading.Event()
        job = self.db.get_meta("job", {})
        if job.get("state") in {"running", "paused", "stopping"}:
            job["state"] = "interrupted"
            job["message"] = "上次任务被中断，可以直接继续"
            job["updated_at"] = now_iso()
            self.db.set_meta("job", job)

    def status(self) -> dict:
        state = self.db.get_meta(
            "job",
            {
                "state": "idle",
                "message": "等待开始",
                "done": 0,
                "total": 0,
                "current": "",
            },
        )
        state["alive"] = bool(self._thread and self._thread.is_alive())
        return state

    def configure_target(
        self,
        wiki_url_or_token: str,
        *,
        create_wrapper: bool = True,
    ) -> dict:
        client = self.client_factory()
        token = parse_wiki_token(wiki_url_or_token)
        node = client.get_node(token)
        if not node.get("space_id"):
            raise FeishuError("读取到了节点，但响应中缺少 space_id")
        previous = self.db.get_meta("target", {})
        summary = self.db.summary()
        if (
            previous.get("node_token")
            and previous.get("node_token") != token
            and summary["files"].get("success", {}).get("count", 0)
        ):
            raise ValueError("已有文件迁移成功，不能直接切换目标知识库节点")
        target = {
            "url": wiki_url_or_token.strip(),
            "space_id": str(node["space_id"]),
            "node_token": str(node.get("node_token") or token),
            "title": str(node.get("title") or ""),
            "create_wrapper": bool(create_wrapper),
            "wrapper_node_token": (
                previous.get("wrapper_node_token")
                if previous.get("node_token") == token
                else ""
            ),
            "verified_at": now_iso(),
        }
        self.db.set_meta("target", target)
        self.db.add_event(
            "success",
            f"目标知识库验证成功：{target['title'] or target['node_token']}",
        )
        return target

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("迁移任务已经在运行")
            if not self.db.get_meta("source_root", ""):
                raise RuntimeError("请先扫描本地目录")
            target = self.db.get_meta("target", {})
            if not target.get("space_id") or not target.get("node_token"):
                raise RuntimeError("请先验证目标知识库节点")
            self._pause.clear()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="folder2wiki-migration",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def _thread_main(self) -> None:
        try:
            self._run()
        finally:
            self.db.close()
            with self._lock:
                self._thread = None

    def pause(self) -> dict:
        self._pause.set()
        self._set_job(state="paused", message="将在当前文件完成后暂停")
        return self.status()

    def resume(self) -> dict:
        if self._thread and self._thread.is_alive():
            self._pause.clear()
            self._set_job(state="running", message="迁移已继续")
            return self.status()
        return self.start()

    def stop(self) -> dict:
        self._stop.set()
        self._pause.clear()
        self._set_job(state="stopping", message="将在当前文件完成后停止")
        return self.status()

    def retry_failed(self) -> dict:
        result = self.db.retry_failed()
        self.db.add_event(
            "info",
            f"已将失败项重新排队：目录 {result['folders']}，文件 {result['files']}",
        )
        return result

    def _set_job(self, **fields) -> None:
        job = self.db.get_meta("job", {})
        job.update(fields)
        job["updated_at"] = now_iso()
        self.db.set_meta("job", job)

    def _wait_if_paused(self) -> bool:
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.25)
        return self._stop.is_set()

    def _run(self) -> None:
        client = self.client_factory()
        folders = self.db.rows("folders", ("pending",))
        files = self.db.rows("files", ("pending", "uploaded"))
        total = len(folders) + len(files) + 1
        done = 0
        self._set_job(
            state="running",
            message="正在准备知识库根目录",
            total=total,
            done=done,
            current="",
            started_at=now_iso(),
        )
        try:
            self._ensure_wrapper(client)
            done += 1
            self._set_job(done=done)

            for folder in folders:
                if self._wait_if_paused():
                    self._finish_stopped(done, total)
                    return
                self._set_job(
                    state="running",
                    message="正在创建知识库目录",
                    current=folder["rel_path"],
                    done=done,
                )
                self._migrate_folder(client, folder)
                done += 1
                self._set_job(done=done)

            for file_row in files:
                if self._wait_if_paused():
                    self._finish_stopped(done, total)
                    return
                self._set_job(
                    state="running",
                    message="正在上传并挂载文件",
                    current=file_row["rel_path"],
                    done=done,
                )
                self._migrate_file(client, file_row)
                done += 1
                self._set_job(done=done)

            summary = self.db.summary()
            failed = summary["files"].get("failed", {}).get("count", 0) + int(
                summary["folders"].get("failed", 0)
            )
            message = "迁移完成" if not failed else f"迁移完成，仍有 {failed} 个失败项"
            self._set_job(
                state="completed",
                message=message,
                current="",
                done=total,
                finished_at=now_iso(),
            )
            self.db.add_event("success" if not failed else "warning", message)
        except Exception as exc:
            self._set_job(
                state="failed",
                message=f"任务异常停止：{exc}",
                current="",
                done=done,
                finished_at=now_iso(),
            )
            self.db.add_event("error", f"任务异常停止：{exc}")

    def _finish_stopped(self, done: int, total: int) -> None:
        self._set_job(
            state="stopped",
            message="任务已停止，后续可以直接继续",
            current="",
            done=done,
            total=total,
            finished_at=now_iso(),
        )
        self.db.add_event("warning", "迁移任务由用户停止")

    def _existing_child(
        self,
        client: FeishuWikiClient,
        space_id: str,
        parent_token: str,
        title: str,
    ) -> dict | None:
        matches = [
            node
            for node in client.list_children(space_id, parent_token)
            if str(node.get("title") or "") == title
        ]
        if len(matches) > 1:
            raise FeishuError(f"目标位置存在多个同名节点“{title}”，无法安全复用")
        if matches and str(matches[0].get("obj_type") or "docx") != "docx":
            raise FeishuError(
                f"目标位置已有同名非目录节点“{title}”，请改名或更换空白目标节点"
            )
        return matches[0] if matches else None

    def _ensure_wrapper(self, client: FeishuWikiClient) -> None:
        target = self.db.get_meta("target", {})
        if not target.get("create_wrapper"):
            target["wrapper_node_token"] = target["node_token"]
            self.db.set_meta("target", target)
            return
        if target.get("wrapper_node_token"):
            return
        root_name = safe_remote_name(
            str(self.db.get_meta("source_root_name", "") or "本地目录")
        )
        existing = self._existing_child(
            client, target["space_id"], target["node_token"], root_name
        )
        node = existing or client.create_folder_node(
            target["space_id"], root_name, target["node_token"]
        )
        target["wrapper_node_token"] = str(node.get("node_token") or "")
        if not target["wrapper_node_token"]:
            raise FeishuError("根目录节点未返回 node_token")
        self.db.set_meta("target", target)
        action = "复用" if existing else "创建"
        self.db.add_event("success", f"{action}知识库根目录：{root_name}")

    def _migrate_folder(self, client: FeishuWikiClient, row: dict) -> None:
        self.db.update_row(
            "folders",
            row["rel_path"],
            status="working",
            error="",
            attempts=int(row["attempts"]) + 1,
        )
        try:
            parent_token = self.db.get_folder_token(row["parent_rel_path"])
            if not parent_token:
                raise FeishuError(f"父目录尚未创建：{row['parent_rel_path'] or '/'}")
            target = self.db.get_meta("target", {})
            existing = self._existing_child(
                client,
                target["space_id"],
                parent_token,
                row["remote_title"],
            )
            node = existing or client.create_folder_node(
                target["space_id"], row["remote_title"], parent_token
            )
            self.db.update_row(
                "folders",
                row["rel_path"],
                node_token=str(node.get("node_token") or ""),
                obj_token=str(node.get("obj_token") or ""),
                status="success",
                error="",
            )
            self.db.add_event(
                "success",
                ("复用目录：" if existing else "创建目录：") + row["rel_path"],
                row["rel_path"],
            )
        except Exception as exc:
            self.db.update_row(
                "folders", row["rel_path"], status="failed", error=str(exc)
            )
            self.db.add_event("error", f"目录创建失败：{exc}", row["rel_path"])

    def _migrate_file(self, client: FeishuWikiClient, row: dict) -> None:
        root = Path(str(self.db.get_meta("source_root", "")))
        local_path = root.joinpath(*row["rel_path"].split("/"))
        attempts = int(row["attempts"]) + 1
        try:
            if not local_path.is_file():
                raise FileNotFoundError(f"本地文件不存在：{local_path}")
            if local_path.stat().st_size <= 0:
                raise ValueError("飞书不支持上传 0 字节文件")
            parent_token = self.db.get_folder_token(row["parent_rel_path"])
            if not parent_token:
                raise FeishuError(
                    f"目标父目录未创建：{row['parent_rel_path'] or '/'}"
                )
            sha256 = str(row.get("sha256") or "")
            if not sha256:
                self.db.update_row(
                    "files",
                    row["rel_path"],
                    status="working",
                    attempts=attempts,
                    error="",
                )
                sha256 = file_sha256(local_path)
                self.db.update_row("files", row["rel_path"], sha256=sha256)

            file_token = str(row.get("file_token") or "")
            if not file_token:
                self.db.update_row("files", row["rel_path"], status="uploading")
                file_token = client.upload_file(local_path, row["remote_name"])
                self.db.update_row(
                    "files",
                    row["rel_path"],
                    file_token=file_token,
                    status="uploaded",
                )

            target = self.db.get_meta("target", {})
            self.db.update_row("files", row["rel_path"], status="mounting")
            wiki_token = client.mount_file(
                target["space_id"], file_token, parent_token
            )
            self.db.update_row(
                "files",
                row["rel_path"],
                wiki_token=wiki_token,
                status="success",
                error="",
            )
            self.db.add_event("success", "上传成功", row["rel_path"])
        except Exception as exc:
            self.db.update_row(
                "files",
                row["rel_path"],
                status="failed",
                error=str(exc),
                attempts=attempts,
            )
            self.db.add_event("error", f"文件迁移失败：{exc}", row["rel_path"])
