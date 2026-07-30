from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        bootstrap = sqlite3.connect(self.path, timeout=30)
        try:
            bootstrap.execute("PRAGMA journal_mode=WAL")
            bootstrap.execute("PRAGMA busy_timeout=30000")
        finally:
            bootstrap.close()
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._local.connection = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            self._local.connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS folders (
                    rel_path TEXT PRIMARY KEY,
                    parent_rel_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    remote_title TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    node_token TEXT NOT NULL DEFAULT '',
                    obj_token TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    seen_scan TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS files (
                    rel_path TEXT PRIMARY KEY,
                    parent_rel_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    remote_name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    offline INTEGER NOT NULL DEFAULT 0,
                    file_token TEXT NOT NULL DEFAULT '',
                    wiki_token TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    seen_scan TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    rel_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_folders_status
                    ON folders(status, depth, rel_path);
                CREATE INDEX IF NOT EXISTS idx_files_status
                    ON files(status, rel_path);
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON events(id DESC);
                """
            )
            conn.execute(
                "UPDATE folders SET status='pending' WHERE status='working'"
            )
            conn.execute(
                "UPDATE files SET status='pending' WHERE status IN ('working','uploading','mounting')"
            )

    def set_meta(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, payload),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def upsert_folder(self, item: dict, scan_id: str) -> None:
        stamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO folders(
                    rel_path,parent_rel_path,name,remote_title,depth,
                    seen_scan,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    parent_rel_path=excluded.parent_rel_path,
                    name=excluded.name,
                    remote_title=excluded.remote_title,
                    depth=excluded.depth,
                    seen_scan=excluded.seen_scan,
                    updated_at=excluded.updated_at,
                    status=CASE
                        WHEN folders.status='missing' THEN 'pending'
                        ELSE folders.status
                    END,
                    error=CASE
                        WHEN folders.status='missing' THEN ''
                        ELSE folders.error
                    END
                """,
                (
                    item["rel_path"],
                    item["parent_rel_path"],
                    item["name"],
                    item["remote_title"],
                    item["depth"],
                    scan_id,
                    stamp,
                ),
            )

    def upsert_file(self, item: dict, scan_id: str) -> None:
        stamp = now_iso()
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT size,mtime_ns,status FROM files WHERE rel_path=?",
                (item["rel_path"],),
            ).fetchone()
            changed = bool(
                old
                and old["status"] == "success"
                and (
                    int(old["size"]) != int(item["size"])
                    or int(old["mtime_ns"]) != int(item["mtime_ns"])
                )
            )
            conn.execute(
                """
                INSERT INTO files(
                    rel_path,parent_rel_path,name,remote_name,size,mtime_ns,
                    offline,seen_scan,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    parent_rel_path=excluded.parent_rel_path,
                    name=excluded.name,
                    remote_name=excluded.remote_name,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    offline=excluded.offline,
                    seen_scan=excluded.seen_scan,
                    updated_at=excluded.updated_at,
                    status=CASE
                        WHEN files.status='missing' THEN 'pending'
                        ELSE files.status
                    END,
                    error=CASE
                        WHEN files.status='missing' THEN ''
                        ELSE files.error
                    END
                """,
                (
                    item["rel_path"],
                    item["parent_rel_path"],
                    item["name"],
                    item["remote_name"],
                    item["size"],
                    item["mtime_ns"],
                    int(item.get("offline", False)),
                    scan_id,
                    stamp,
                ),
            )
            if changed:
                conn.execute(
                    """
                    UPDATE files SET status='changed',
                        error='æœ¬åœ°æ–‡ä»¶åœ¨æˆåŠŸè¿ç§»åŽå‘ç”Ÿå˜åŒ–ï¼Œæœªè‡ªåŠ¨è¦†ç›–é£žä¹¦å†…å®¹'
                    WHERE rel_path=?
                    """,
                    (item["rel_path"],),
                )

    def finish_scan(self, scan_id: str) -> None:
        stamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE folders SET status='missing',updated_at=?
                WHERE seen_scan<>? AND status<>'missing'
                """,
                (stamp, scan_id),
            )
            conn.execute(
                """
                UPDATE files SET status='missing',
                    error='æœ¬åœ°æºæ–‡ä»¶å·²ä¸å­˜åœ¨',updated_at=?
                WHERE seen_scan<>? AND status<>'missing'
                """,
                (stamp, scan_id),
            )

    def reset_inventory(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM folders")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM meta WHERE key NOT LIKE 'oauth_%'")

    def summary(self) -> dict:
        with self.connect() as conn:
            folder_rows = conn.execute(
                "SELECT status,COUNT(*) n FROM folders GROUP BY status"
            ).fetchall()
            file_rows = conn.execute(
                """
                SELECT status,COUNT(*) n,COALESCE(SUM(size),0) bytes,
                       COALESCE(SUM(offline),0) offline
                FROM files GROUP BY status
                """
            ).fetchall()
        folders = {row["status"]: row["n"] for row in folder_rows}
        files = {
            row["status"]: {
                "count": row["n"],
                "bytes": row["bytes"],
                "offline": row["offline"],
            }
            for row in file_rows
        }
        return {
            "folders": folders,
            "files": files,
            "folder_total": sum(folders.values()),
            "file_total": sum(v["count"] for v in files.values()),
            "byte_total": sum(v["bytes"] for v in files.values()),
            "offline_total": sum(v["offline"] for v in files.values()),
        }

    def rows(self, table: str, statuses: tuple[str, ...], limit: int = 0) -> list[dict]:
        if table not in {"folders", "files"}:
            raise ValueError("unsupported table")
        placeholders = ",".join("?" for _ in statuses)
        order = "depth,rel_path" if table == "folders" else "rel_path"
        sql = f"SELECT * FROM {table} WHERE status IN ({placeholders}) ORDER BY {order}"
        params: list[Any] = list(statuses)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def update_row(self, table: str, rel_path: str, **fields: Any) -> None:
        if table not in {"folders", "files"}:
            raise ValueError("unsupported table")
        allowed = {
            "folders": {"node_token", "obj_token", "status", "error", "attempts"},
            "files": {
                "sha256",
                "fim}ãO-¢G§²ÚîÆ­yÕÁ¤ œ½…Á¤½Ñ…É•Ðœ°A=MPœ±íÝ¥­¥}ÕÉ°±É•…Ñ•}ÝÉ…ÁÁ•Èè É•…Ñ”µÝÉ…ÁÁ•Èœ¤¹¡•­•‘ô¤ì(€€€Ñ½…ÍÐ Ÿžn»š‚¦ª3¢¾š"C–*¾òhœ¬¡È¹Ñ¥Ñ±•ññÈ¹¹½‘•}Ñ½­•¸¤¤í…Ý…¥ÐÉ•™É•Í  ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ Ÿžn»š‚¦ª3¢¾–’Ç¢Ò—¾òhœ­”¹µ•ÍÍ…”¥ô)ô)…Íå¹Œ™Õ¹Ñ¥½¸ÍÑ…ÉÑ5¥É…Ñ¥½¸ ¥ì(€½¹ÍÐ™¥±•Ìõ9Õµ‰•È¡ÍÑ…Ñ”¹ÍÕµµ…Éäü¹™¥±•}Ñ½Ñ…±ñðÀ¤ì(€¥˜ …™¥±•Ì¥É•ÑÕÉ¸Ñ½…ÍÐ Ÿ¢¾ß–#š&¯š>?šr³–rÃžn»–öTœ¤ì(€¥˜ …ÍÑ…Ñ”¹½…ÕÑ ü¹…ÕÑ¡½É¥é•¥É•ÑÕÉ¸Ñ½…ÍÐ Ÿ¢¾ß–#–º3š"C¦Ž{’æ˜=ÕÑ ƒš:#švœ¤ì(€¥˜ …ÍÑ…Ñ”¹Ñ…É•Ðü¹ÍÁ…•}¥¥É•ÑÕÉ¸Ñ½…ÍÐ Ÿ¢¾ß–#¦ª3¢¾žn»š‚ž~—¢¾–êLœ¤ì(€¥˜ …½¹™¥É´¡ƒ–6Ï–Â¢þžžì€‘í™¥±•Ì¹Ñ½1½…±•MÑÉ¥¹œ ¥ôƒ’â«šZ’îÛ¾ò3–æÛ–r£ž~—¢¾–êO’â·–"o–îëžn»–öW¢*ž
çŽ	q¹q»–ÞËš"C–*¦†ç’òk¢«–*£¢ÞÏ¢þ¾ò3šb¿–B›žîŸžî·¾ò}€¤¥É•ÑÕÉ¸ì(€ÑÉåí…Ý…¥Ð…Á¤ œ½…Á¤½µ¥É…Ñ¥½¸½ÍÑ…ÉÐœ°A=MPœ±íô¤íÑ½…ÍÐ Ÿ¢þžžï’îï–*‡–ÞË–B¿–* œ¤í…Ý…¥ÐÉ•™É•Í  ¥õ…Ñ ¡”¥íÑ½…ÍÐ¡”¹µ•ÍÍ…”¥ô)ô)…Íå¹Œ™Õ¹Ñ¥½¸©½‰Ñ¥½¸¡…Ñ¥½¸¥íÑÉåí…Ý…¥Ð…Á¤ œ½…Á¤½µ¥É…Ñ¥½¸¼œ­…Ñ¥½¸°A=MPœ±íô¤í…Ý…¥ÐÉ•™É•Í  ¥õ…Ñ ¡”¥íÑ½…ÍÐ¡”¹µ•ÍÍ…”¥õô)…Íå¹Œ™Õ¹Ñ¥½¸É•ÑÉå…¥±• ¥íÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µ¥É…Ñ¥½¸½É•ÑÉäœ°A=MPœ±íô¤íÑ½…ÍÐ¡ƒ–ÞË¦7šZÃš:K¦b¾òkžn»–öT€‘íÈ¹™½±‘•ÉÍ÷¾ò3šZ’îØ€‘íÈ¹™¥±•Íõ€¤í…Ý…¥ÐÉ•™É•Í  ¥õ…Ñ ¡”¥íÑ½…ÍÐ¡”¹µ•ÍÍ…”¥õô)É•™É•Í  ¤íÍ•Ñ%¹Ñ•ÉÙ…°¡É•™É•Í °ÈÔÀÀ¤ì(ð½ÍÉ¥ÁÐø(ð½‰½‘äø(ð½¡Ñµ°ø