"""定时任务调度器：支持定时爬取+自动投递。

使用 APScheduler 实现，支持每天/仅一次执行。
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

# 全局调度器实例
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()

# 任务存储（内存+文件）
SCHEDULE_FILE = DATA_DIR / "scheduled_tasks.json"


def get_scheduler() -> BackgroundScheduler:
    """获取或创建调度器实例。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler()
            _scheduler.start()
            logger.info("定时任务调度器已启动")
        return _scheduler


def stop_scheduler() -> None:
    """停止调度器。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            _scheduler.shutdown()
            _scheduler = None
            logger.info("定时任务调度器已停止")


def _load_tasks() -> list[dict]:
    """从文件加载任务列表。"""
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载任务文件失败: {e}")
    return []


def _save_tasks(tasks: list[dict]) -> None:
    """保存任务列表到文件。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存任务文件失败: {e}")


def _run_scheduled_task(task_id: str, config: dict) -> None:
    """执行定时任务：爬取 -> 评分 -> 自动投递。"""
    import io

    logger.info(f"[定时任务 {task_id}] 开始执行")

    # 捕获本任务的日志到内存
    log_buf = io.StringIO()
    buf_handler = logging.StreamHandler(log_buf)
    buf_handler.setLevel(logging.INFO)
    buf_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(buf_handler)
    db_run_id = None

    def _get_log_text() -> str:
        return log_buf.getvalue()

    try:
        from src.scraper.boss_scraper import BossZhipinScraper
        from src.analyzer.agent import JobAgent
        from src.utils.resume_parser import parse_resume

        keyword = config.get("keyword", "")
        city = config.get("city", "上海")
        pages = config.get("pages", 3)
        resume_path = config.get("resume_path", "")
        auto_send = config.get("auto_send", True)
        min_score = config.get("min_score", 70)
        recommendations = config.get("recommendations", ["强烈推荐", "建议投递"])

        # 1. 爬取
        with BossZhipinScraper() as scraper:
            jobs = scraper.search(keyword=keyword, city=city, page=pages)

        if not jobs:
            logger.warning(f"[定时任务 {task_id}] 未采集到数据")
            _update_task_status(task_id, "error", "未采集到数据")
            return

        # 2. 评分
        agent = JobAgent()
        resume_text = ""

        # 任务优先使用自身配置的简历，若为空则尝试 .env 的默认简历路径
        if not resume_path or not Path(resume_path).exists():
            from config.settings import RESUME_PATH as default_resume
            if default_resume and Path(default_resume).exists():
                resume_path = default_resume
                logger.info(f"[定时任务 {task_id}] 使用 .env 默认简历: {default_resume}")

        if resume_path and Path(resume_path).exists():
            try:
                resume_text = parse_resume(resume_path)
                logger.info(f"[定时任务 {task_id}] 简历加载成功 ({len(resume_text)} 字符)")
            except Exception as e:
                logger.warning(f"[定时任务 {task_id}] 简历解析失败: {e}")
        else:
            logger.warning(f"[定时任务 {task_id}] 未配置简历，所有岗位将被标记为不推荐")

        import pandas as pd
        df = pd.DataFrame(jobs)
        df = agent.analyze_jobs(df, resume_text)
        df = df.sort_values("match_score", ascending=False)

        # 3. 自动投递
        send_stats = {"success": 0, "total": 0}
        if auto_send:
            sendable = df[
                (df["recommendation"].isin(recommendations)) &
                (df["match_score"] >= min_score)
            ].to_dict("records")

            if sendable:
                logger.info(f"[定时任务 {task_id}] 自动投递 {len(sendable)} 个岗位")
                try:
                    with BossZhipinScraper() as scraper:
                        scraper._logged_in = False
                        if not scraper.ensure_login():
                            logger.warning(f"[定时任务 {task_id}] 未检测到登录态，跳过自动投递")
                        else:
                            results = scraper.send_greetings(sendable)
                            send_stats["success"] = sum(1 for r in results if r["status"] == "成功")
                            send_stats["total"] = len(results)
                            skipped = sum(1 for r in results if r["status"] == "跳过")
                            failed = sum(1 for r in results if r["status"] == "失败")
                            logger.info(f"[定时任务 {task_id}] 投递完成: 成功 {send_stats['success']}/{send_stats['total']} (跳过 {skipped}, 失败 {failed})")
                except Exception as e:
                    logger.exception(f"[定时任务 {task_id}] 自动投递异常: {e}")
            else:
                logger.info(f"[定时任务 {task_id}] 没有符合投递条件的岗位")

        # 4. 保存结果到数据库（显示在历史记录中）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = DATA_DIR / f"scheduled_{task_id}_{ts}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"[定时任务 {task_id}] 结果已保存: {csv_path}")

        # 计算统计数据
        total = len(df)
        avg_score = round(float(df["match_score"].mean()), 1) if total > 0 else 0.0
        max_score = int(df["match_score"].max()) if total > 0 else 0
        min_score_val = int(df["match_score"].min()) if total > 0 else 0
        count_strong = int((df["recommendation"] == "强烈推荐").sum())
        count_recommend = int((df["recommendation"] == "建议投递").sum())
        count_consider = int((df["recommendation"] == "可以考虑").sum())
        count_skip = int((df["recommendation"] == "不推荐").sum())

        # 构造前端表格数据
        table = []
        for rank, (_, row) in enumerate(df.iterrows(), 1):
            table.append({
                "rank": rank,
                "title": str(row.get("title", "")),
                "company": str(row.get("company", "")),
                "city": str(row.get("city", city)),
                "salary": str(row.get("salary", "")),
                "link": str(row.get("link", "")),
                "description": str(row.get("description", "")),
                "requirements": str(row.get("requirements", "")),
                "match_score": int(row.get("match_score", 0)),
                "match_reason": str(row.get("match_reason", "")),
                "recommendation": str(row.get("recommendation", "可以考虑")),
                "recommendation_reason": str(row.get("recommendation_reason", "")),
                "greeting": str(row.get("greeting", "")),
            })

        # 写入数据库
        try:
            from src.database import save_run, save_jobs
            import uuid as _uuid
            db_task_id = f"scheduled_{task_id}_{ts}_{_uuid.uuid4().hex[:6]}"
            resume_filename = Path(resume_path).name if resume_path else ""
            run_data = {
                "task_id": db_task_id,
                "keyword": keyword,
                "city": city,
                "pages": pages,
                "model": "sensenova",
                "status": "completed",
                "total_jobs": total,
                "avg_score": avg_score,
                "max_score": max_score,
                "min_score": min_score_val,
                "count_strong": count_strong,
                "count_recommend": count_recommend,
                "count_consider": count_consider,
                "count_skip": count_skip,
                "csv_path": str(csv_path),
                "log_path": "",
                "chart_b64": "",
                "error_message": "",
                "resume_filename": resume_filename,
                "created_at": datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat(),
            }
            db_run_id = save_run(run_data)
            save_jobs(db_run_id, table)
        except Exception as e:
            logger.warning(f"[定时任务 {task_id}] 数据库保存失败: {e}")

        # 更新任务状态
        _update_task_status(task_id, "completed", f"执行成功，采集 {total} 个岗位")

    except Exception as e:
        logger.exception(f"[定时任务 {task_id}] 执行失败: {e}")
        _update_task_status(task_id, "error", str(e))
    finally:
        # 保存日志文件
        try:
            log_text = _get_log_text()
            if log_text.strip():
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_path = LOG_DIR / f"scheduled_{task_id}_{ts}.log"
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(log_text)
                # 把 log_path 更新到数据库记录
                if db_run_id is not None:
                    try:
                        from src.database import update_run
                        update_run(db_run_id, log_path=str(log_path))
                    except Exception:
                        pass
        except Exception:
            pass
        # 移除日志缓冲 handler
        logger.removeHandler(buf_handler)


def _update_task_status(task_id: str, status: str, message: str = "") -> None:
    """更新任务状态。"""
    tasks = _load_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            t["last_status"] = status
            t["last_message"] = message
            t["last_run"] = datetime.now().isoformat()
            break
    _save_tasks(tasks)


def add_task(
    name: str,
    keyword: str,
    city: str,
    pages: int,
    schedule_type: str,  # "daily" | "once"
    schedule_time: str,  # "HH:MM" 格式
    resume_path: str = "",
    resume_id: str = "",
    auto_send: bool = True,
    min_score: int = 70,
    recommendations: list[str] = None,
) -> dict:
    """添加定时任务。

    Args:
        name: 任务名称
        keyword: 搜索关键词
        city: 城市
        pages: 采集页数
        schedule_type: "daily" 每天执行 / "once" 仅执行一次
        schedule_time: 执行时间 "HH:MM"
        resume_path: 简历路径
        auto_send: 是否自动投递
        min_score: 最低匹配度
        recommendations: 可投递的推荐等级

    Returns:
        任务信息 dict
    """
    task_id = str(uuid.uuid4())[:8]
    hour, minute = map(int, schedule_time.split(":"))

    task = {
        "id": task_id,
        "name": name,
        "keyword": keyword,
        "city": city,
        "pages": pages,
        "schedule_type": schedule_type,
        "schedule_time": schedule_time,
        "resume_path": resume_path,
        "resume_id": resume_id,
        "auto_send": auto_send,
        "min_score": min_score,
        "recommendations": recommendations or ["强烈推荐", "建议投递"],
        "enabled": True,
        "created_at": datetime.now().isoformat(),
        "last_status": "pending",
        "last_message": "",
        "last_run": None,
    }

    # 保存到文件
    tasks = _load_tasks()
    tasks.append(task)
    _save_tasks(tasks)

    # 添加到调度器
    scheduler = get_scheduler()
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        # 仅执行一次：设置为今天的指定时间，如果已过则设为明天
        now = datetime.now()
        run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_time <= now:
            from datetime import timedelta
            run_time += timedelta(days=1)
        trigger = DateTrigger(run_date=run_time)

    scheduler.add_job(
        func=_run_scheduled_task,
        trigger=trigger,
        id=task_id,
        args=[task_id, task],
        replace_existing=True,
    )
    logger.info(f"定时任务已添加: {name} ({schedule_type} {schedule_time})")

    return task


def remove_task(task_id: str) -> bool:
    """删除定时任务。"""
    tasks = _load_tasks()
    tasks = [t for t in tasks if t.get("id") != task_id]
    _save_tasks(tasks)

    scheduler = get_scheduler()
    try:
        scheduler.remove_job(task_id)
    except Exception:
        pass

    logger.info(f"定时任务已删除: {task_id}")
    return True


def toggle_task(task_id: str, enabled: bool) -> bool:
    """启用/禁用定时任务。"""
    tasks = _load_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            t["enabled"] = enabled
            break
    _save_tasks(tasks)

    scheduler = get_scheduler()
    try:
        job = scheduler.get_job(task_id)
        if job:
            if enabled:
                job.resume()
            else:
                job.pause()
    except Exception:
        pass

    return True


def update_task(task_id: str, updates: dict) -> Optional[dict]:
    """更新定时任务配置。

    Args:
        task_id: 任务 ID
        updates: 要更新的字段（name, keyword, city, pages, schedule_type,
                 schedule_time, resume_path, resume_id, auto_send, min_score, recommendations）

    Returns:
        更新后的任务 dict，任务不存在返回 None
    """
    tasks = _load_tasks()
    task = None
    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if task is None:
        logger.warning(f"更新任务失败，不存在: {task_id}")
        return None

    # 允许更新的字段
    allowed_fields = {
        "name", "keyword", "city", "pages", "schedule_type",
        "schedule_time", "resume_path", "resume_id", "auto_send",
        "min_score", "recommendations",
    }
    for key, value in updates.items():
        if key in allowed_fields:
            task[key] = value

    _save_tasks(tasks)

    # 重新注册调度任务（可能改了时间/类型）
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(task_id)
    except Exception:
        pass

    schedule_type = task.get("schedule_type", "daily")
    schedule_time = task.get("schedule_time", "09:00")
    hour, minute = map(int, schedule_time.split(":"))

    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        now = datetime.now()
        run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_time <= now:
            from datetime import timedelta
            run_time += timedelta(days=1)
        trigger = DateTrigger(run_date=run_time)

    scheduler.add_job(
        func=_run_scheduled_task,
        trigger=trigger,
        id=task_id,
        args=[task_id, task],
        replace_existing=True,
    )
    logger.info(f"定时任务已更新: {task.get('name')} ({schedule_type} {schedule_time})")
    return task


def list_tasks() -> list[dict]:
    """获取所有定时任务。"""
    return _load_tasks()


def init_scheduler() -> None:
    """初始化：加载已有任务到调度器。"""
    tasks = _load_tasks()
    scheduler = get_scheduler()

    for task in tasks:
        if not task.get("enabled", True):
            continue

        task_id = task["id"]
        schedule_type = task.get("schedule_type", "once")
        schedule_time = task.get("schedule_time", "09:00")
        hour, minute = map(int, schedule_time.split(":"))

        if schedule_type == "daily":
            trigger = CronTrigger(hour=hour, minute=minute)
        else:
            # 一次性任务如果已过期则跳过
            last_run = task.get("last_run")
            if last_run:
                logger.info(f"一次性任务 {task_id} 已执行过，跳过")
                continue
            now = datetime.now()
            run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_time <= now:
                from datetime import timedelta
                run_time += timedelta(days=1)
            trigger = DateTrigger(run_date=run_time)

        scheduler.add_job(
            func=_run_scheduled_task,
            trigger=trigger,
            id=task_id,
            args=[task_id, task],
            replace_existing=True,
        )
        logger.info(f"已恢复定时任务: {task.get('name')} ({schedule_type} {schedule_time})")
