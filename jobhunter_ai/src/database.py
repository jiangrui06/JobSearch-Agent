"""SQLite 持久化层：存储分析历史记录（优化版：连接池 + 批量写入）。"""

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

DB_PATH: Optional[Path] = None
_write_lock = threading.Lock()
# 连接池：每个线程复用自己的连接
_conn_pool: dict[int, sqlite3.Connection] = {}
_pool_lock = threading.Lock()


def _get_thread_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（连接池）。"""
    if DB_PATH is None:
        raise RuntimeError("database not initialized, call init_db() first")
    tid = threading.current_thread().ident
    if tid not in _conn_pool or _conn_pool[tid] is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")  # 平衡性能与安全性
        with _pool_lock:
            _conn_pool[tid] = conn
    return _conn_pool[tid]


def get_connection() -> sqlite3.Connection:
    """获取数据库连接（优先连接池）。"""
    return _get_thread_conn()


def close_all_connections() -> None:
    """关闭所有连接池中的连接（应用退出时调用）。"""
    with _pool_lock:
        for tid, conn in list(_conn_pool.items()):
            try:
                conn.close()
            except Exception:
                pass
            _conn_pool[tid] = None
        _conn_pool.clear()


def init_db(db_dir: Path) -> None:
    """初始化数据库，建表（幂等）。"""
    global DB_PATH
    db_dir.mkdir(parents=True, exist_ok=True)
    DB_PATH = db_dir / "jobhunter.db"

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT    NOT NULL UNIQUE,
            keyword     TEXT    NOT NULL,
            city        TEXT    NOT NULL,
            pages       INTEGER NOT NULL DEFAULT 3,
            model       TEXT    NOT NULL DEFAULT 'deepseek-chat',
            status      TEXT    NOT NULL DEFAULT 'completed',
            total_jobs  INTEGER DEFAULT 0,
            avg_score   REAL    DEFAULT 0.0,
            max_score   INTEGER DEFAULT 0,
            min_score   INTEGER DEFAULT 0,
            count_strong    INTEGER DEFAULT 0,
            count_recommend INTEGER DEFAULT 0,
            count_consider  INTEGER DEFAULT 0,
            count_skip      INTEGER DEFAULT 0,
            csv_path    TEXT,
            log_path    TEXT,
            chart_b64   TEXT,
            error_message TEXT,
            resume_filename TEXT,
            created_at  TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS analysis_jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id              INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
            rank                INTEGER NOT NULL,
            title               TEXT    NOT NULL,
            company             TEXT    NOT NULL,
            city                TEXT    NOT NULL,
            salary              TEXT,
            link                TEXT,
            description         TEXT,
            requirements        TEXT,
            match_score         INTEGER NOT NULL,
            match_reason        TEXT,
            recommendation      TEXT    NOT NULL,
            recommendation_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_runs_created_at ON analysis_runs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON analysis_jobs(run_id);
    """)
    conn.commit()
    conn.close()


# ---------- 写操作 ----------


def save_run(run_data: dict[str, Any]) -> int:
    """插入一条运行记录，返回自增 ID。"""
    with _write_lock:
        conn = get_connection()
        cur = conn.execute(
            """INSERT INTO analysis_runs
               (task_id, keyword, city, pages, model, status,
                total_jobs, avg_score, max_score, min_score,
                count_strong, count_recommend, count_consider, count_skip,
                csv_path, log_path, chart_b64, error_message, resume_filename,
                created_at, completed_at)
               VALUES
               (:task_id, :keyword, :city, :pages, :model, :status,
                :total_jobs, :avg_score, :max_score, :min_score,
                :count_strong, :count_recommend, :count_consider, :count_skip,
                :csv_path, :log_path, :chart_b64, :error_message, :resume_filename,
                :created_at, :completed_at)""",
            run_data,
        )
        run_id = cur.lastrowid
        conn.commit()
        return run_id


def save_jobs(run_id: int, jobs: list[dict[str, Any]]) -> None:
    """批量插入岗位数据（使用 executemany）。"""
    if not jobs:
        return
    with _write_lock:
        conn = get_connection()
        rows = [
            (
                run_id,
                j.get("rank", 0),
                j.get("title", ""),
                j.get("company", ""),
                j.get("city", ""),
                j.get("salary", ""),
                j.get("link", ""),
                j.get("description", ""),
                j.get("requirements", ""),
                j.get("match_score", 0),
                j.get("match_reason", ""),
                j.get("recommendation", "可以考虑"),
                j.get("recommendation_reason", ""),
            )
            for j in jobs
        ]
        conn.executemany(
            """INSERT INTO analysis_jobs
               (run_id, rank, title, company, city, salary, link,
                description, requirements, match_score, match_reason,
                recommendation, recommendation_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()


def update_run(run_id: int, **kwargs: Any) -> None:
    """更新运行记录指定字段。"""
    if not kwargs:
        return
    with _write_lock:
        conn = get_connection()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [run_id]
        conn.execute(f"UPDATE analysis_runs SET {sets} WHERE id = ?", vals)
        conn.commit()


def delete_run(run_id: int) -> bool:
    """删除运行记录及关联岗位（CASCADE）。返回是否删除了行。"""
    with _write_lock:
        conn = get_connection()
        cur = conn.execute("DELETE FROM analysis_runs WHERE id = ?", (run_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


# ---------- 读操作 ----------


def get_run(run_id: int) -> Optional[dict[str, Any]]:
    """获取单条运行记录。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def get_runs(
    page: int = 1,
    per_page: int = 20,
    keyword: str = "",
    city: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """分页获取运行记录列表（按创建时间倒序）。返回 (列表, 总数)。"""
    conn = get_connection()

    where_parts: list[str] = []
    params: list[Any] = []

    if keyword:
        where_parts.append("keyword LIKE ?")
        params.append(f"%{keyword}%")
    if city:
        where_parts.append("city LIKE ?")
        params.append(f"%{city}%")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM analysis_runs {where_sql}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT id, task_id, keyword, city, pages, model, status, "
        f"total_jobs, avg_score, max_score, min_score, "
        f"count_strong, count_recommend, count_consider, count_skip, "
        f"csv_path, log_path, resume_filename, created_at, completed_at, "
        f"error_message "
        f"FROM analysis_runs {where_sql} ORDER BY created_at DESC "
        f"LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return [dict(r) for r in rows], total


def get_run_jobs(run_id: int) -> list[dict[str, Any]]:
    """获取某次运行的所有岗位（按 rank 排序）。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM analysis_jobs WHERE run_id = ? ORDER BY rank",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]
