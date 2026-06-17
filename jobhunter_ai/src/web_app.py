"""JobHunter AI — Web 应用 (FastAPI)

提供简历上传、配置需求、切换模型、结果展示等功能。
"""

import io
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.settings import DATA_DIR, LOG_DIR, AI_MODEL
from src.analyzer.agent import JobAgent
from src.scraper.boss_scraper import BossZhipinScraper

logger = logging.getLogger(__name__)


def setup_web_logging() -> Path:
    """配置 web 模式的文件日志。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"web_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file

# ---------- FastAPI ----------

app = FastAPI(title="JobHunter AI", description="BOSS直聘智能求职系统")

# 任务存储
tasks: dict[str, dict[str, Any]] = {}

# 可用模型
AVAILABLE_MODELS = [
    {"id": "sensenova-6.7-flash-lite", "name": "SenseNova 6.7 Flash Lite"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "sensenova-u1-fast", "name": "SenseNova U1 Fast"},
    {"id": "custom", "name": "自定义模型"},
]

# ---------- 简历管理 ----------

RESUME_DIR = DATA_DIR / "resumes"
RESUMES_FILE = RESUME_DIR / "resumes.json"


def _ensure_resume_dir():
    """确保简历存储目录存在。"""
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    if not RESUMES_FILE.exists():
        RESUMES_FILE.write_text("[]", encoding="utf-8")


def _load_resumes() -> list[dict]:
    """加载简历列表。"""
    _ensure_resume_dir()
    try:
        return json.loads(RESUMES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_resumes(resumes: list[dict]) -> None:
    """保存简历列表。"""
    _ensure_resume_dir()
    RESUMES_FILE.write_text(
        json.dumps(resumes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _add_resume(filename: str, file_path: str, file_size: int) -> dict:
    """添加一条简历记录。"""
    resumes = _load_resumes()
    record = {
        "id": str(uuid.uuid4())[:8],
        "name": filename,
        "path": file_path,
        "size": file_size,
        "created_at": datetime.now().isoformat(),
    }
    resumes.append(record)
    _save_resumes(resumes)
    return record


def _delete_resume(rid: str) -> bool:
    """删除简历记录及文件。"""
    resumes = _load_resumes()
    for r in resumes:
        if r.get("id") == rid:
            # 删除文件
            fpath = Path(r.get("path", ""))
            if fpath.exists():
                fpath.unlink(missing_ok=True)
            resumes.remove(r)
            _save_resumes(resumes)
            return True
    return False


# ---------- 后台任务 ----------


def _run_analysis(
    task_id: str,
    keyword: str,
    city: str,
    pages: int,
    model: str,
    resume_path: str,
):
    """后台执行采集 + 评分流程。"""
    import builtins

    log_buf = io.StringIO()
    _orig_print = builtins.print

    def _print(*args, **kwargs):
        kwargs["file"] = kwargs.get("file", log_buf)
        _orig_print(*args, **kwargs)
        tasks[task_id]["log"] = log_buf.getvalue()

    def _is_cancelled() -> bool:
        return tasks.get(task_id, {}).get("cancel_requested", False)

    builtins.print = _print

    try:
        tasks[task_id]["status"] = "running"

        _print(f"[{datetime.now():%H:%M:%S}] === 阶段1/2: 采集 BOSS直聘数据 ===")
        _print(f"关键词: {keyword}  |  城市: {city}  |  页数: {pages}")
        tasks[task_id]["progress"] = 5

        # 如果用户选了自定义模型，临时切换
        if model and model != AI_MODEL and model != "custom":
            os.environ["AI_MODEL"] = model
        elif model == "custom":
            pass  # 保留 .env 中的设置

        with BossZhipinScraper() as scraper:
            all_raw = scraper.search(keyword=keyword, city=city, page=pages, cancel_check=_is_cancelled)

        if _is_cancelled():
            _print("\n用户取消了操作。")
            tasks[task_id]["status"] = "cancelled"
            builtins.print = _orig_print
            return

        if not all_raw:
            _print("未采集到数据，流程终止。")
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = "未采集到数据"
            builtins.print = _orig_print
            return

        df = pd.DataFrame(all_raw)
        _print(f"采集完成: {len(df)} 条岗位数据")
        tasks[task_id]["progress"] = 30

        # ---- 阶段2: AI 评分 ----
        _print(f"\n[{datetime.now():%H:%M:%S}] === 阶段2/2: AI Agent 评分 ===")
        tasks[task_id]["progress"] = 35

        if _is_cancelled():
            _print("\n用户取消了操作。")
            tasks[task_id]["status"] = "cancelled"
            builtins.print = _orig_print
            return

        agent = JobAgent()
        resume_text = ""
        if resume_path:
            try:
                from src.utils.resume_parser import parse_resume
                resume_text = parse_resume(resume_path)
                _print(f"简历加载成功 ({len(resume_text)} 字符)")
            except Exception as e:
                _print(f"简历加载失败: {e}")

        df = agent.analyze_jobs(df, resume_text)
        df = df.sort_values("match_score", ascending=False).reset_index(drop=True)
        _print(f"评分完成: {len(df)} 个岗位")
        tasks[task_id]["progress"] = 70

        # ---- 统计 ----
        rec_order = ["强烈推荐", "建议投递", "可以考虑", "不推荐"]
        stats = {
            "total": len(df),
            "avg_score": round(float(df["match_score"].mean()), 1),
            "max_score": int(df["match_score"].max()),
            "min_score": int(df["match_score"].min()),
        }
        counts = {}
        for rec in rec_order:
            c = len(df[df["recommendation"] == rec])
            counts[rec] = c
        stats["counts"] = counts

        # ---- 表格数据 ----
        table = []
        for idx, row in df.iterrows():
            table.append({
                "rank": idx + 1,
                "title": str(row.get("title", "")),
                "company": str(row.get("company", "")),
                "city": str(row.get("city", "")),
                "salary": str(row.get("salary", "")),
                "link": str(row.get("link", "")),
                "description": str(row.get("description", "")),
                "requirements": str(row.get("requirements", "")),
                "score": int(row.get("match_score", 0)),
                "match_score": int(row.get("match_score", 0)),
                "recommendation": str(row.get("recommendation", "")),
                "reason": str(row.get("match_reason", "")),
                "match_reason": str(row.get("match_reason", "")),
                "rec_reason": str(row.get("recommendation_reason", "")),
                "greeting": str(row.get("greeting", "")),
                "recommendation_reason": str(row.get("recommendation_reason", "")),
            })

        # ---- 图表 (base64) ----
        chart_b64 = _generate_chart_base64(df)

        # ---- 保存 CSV ----
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = DATA_DIR / f"boss_jobs_{ts}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        _print(f"结果已保存: {csv_path}")

        # ---- 保存日志文件 ----
        log_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"analysis_{task_id}_{log_ts}.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(log_buf.getvalue())

        tasks[task_id].update({
            "status": "completed",
            "progress": 100,
            "result": {
                "stats": stats,
                "table": table,
                "chart": chart_b64,
                "csv": str(csv_path),
            },
        })
        _print(f"\n[{datetime.now():%H:%M:%S}] 分析完成")

        # ---- 写入数据库 ----
        try:
            from src.database import save_run, save_jobs
            run_data = {
                "task_id": task_id,
                "keyword": keyword,
                "city": city,
                "pages": pages,
                "model": model,
                "status": "completed",
                "total_jobs": stats["total"],
                "avg_score": stats["avg_score"],
                "max_score": stats["max_score"],
                "min_score": stats["min_score"],
                "count_strong": counts.get("强烈推荐", 0),
                "count_recommend": counts.get("建议投递", 0),
                "count_consider": counts.get("可以考虑", 0),
                "count_skip": counts.get("不推荐", 0),
                "csv_path": str(csv_path),
                "log_path": str(log_path),
                "chart_b64": chart_b64 or "",
                "error_message": "",
                "resume_filename": Path(resume_path).name if resume_path else "",
                "created_at": datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat(),
            }
            db_run_id = save_run(run_data)
            save_jobs(db_run_id, table)
        except Exception as e:
            _print(f"数据库保存失败: {e}")

    except Exception as e:
        import traceback
        _print(f"\n错误: {e}")
        _print(traceback.format_exc())
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)

        # 错误时也保存日志
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / f"analysis_{task_id}_error.log"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(log_buf.getvalue())
        except Exception:
            pass
    finally:
        builtins.print = _orig_print



def _run_send_greetings(task_id: str, jobs: list[dict]):
    """\u5728\u540e\u53f0\u7ebf\u7a0b\u4e2d\u6279\u91cf\u6295\u9012\uff08\u81ea\u52a8\u590d\u7528\u6301\u4e45\u5316\u767b\u5f55\u6001\uff09\u3002"""
    import builtins
    import random

    log_buf = io.StringIO()
    _orig_print = builtins.print

    def _print(*args, **kwargs):
        kwargs["file"] = kwargs.get("file", log_buf)
        _orig_print(*args, **kwargs)
        tasks[task_id]["log"] = log_buf.getvalue()

    builtins.print = _print

    try:
        tasks[task_id]["status"] = "running"
        tasks[task_id]["progress"] = 5

        sendable = [j for j in jobs if j.get("recommendation") in ("\u5f3a\u70c8\u63a8\u8350", "\u5efa\u8bae\u6295\u9012")]
        if not sendable:
            _print("\u6ca1\u6709\u7b26\u5408\u6295\u9012\u6761\u4ef6\u7684\u5c97\u4f4d\uff08\u9700\u8981 \u5f3a\u70c8\u63a8\u8350 \u6216 \u5efa\u8bae\u6295\u9012\uff09")
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["progress"] = 100
            tasks[task_id]["result"] = {"results": []}
            builtins.print = _orig_print
            return

        _print(f"\u5f00\u59cb\u6279\u91cf\u6295\u9012 {len(sendable)} \u4e2a\u5c97\u4f4d...\n")
        _print("\u6d4f\u89c8\u5668\u5df2\u6253\u5f00\uff0c\u5c1d\u8bd5\u4f7f\u7528\u5df2\u4fdd\u5b58\u7684\u767b\u5f55\u6001...")
        _print("\u5982\u679c\u63d0\u793a\u767b\u5f55\uff0c\u8bf7\u5728\u6d4f\u89c8\u5668\u4e2d\u626b\u7801\u5b8c\u6210\u767b\u5f55\uff08\u767b\u5f55\u540e\u53ea\u9700\u4e00\u6b21\uff0c\u540e\u7eed\u81ea\u52a8\u590d\u7528\uff09\n")

        from src.scraper.boss_scraper import BossZhipinScraper
        with BossZhipinScraper() as scraper:
            results = scraper.send_greetings(sendable)
            success = sum(1 for r in results if r["status"] == "\u6210\u529f")
            skipped = sum(1 for r in results if r["status"] == "\u8df3\u8fc7")
            failed = sum(1 for r in results if r["status"] == "\u5931\u8d25")
            _print(f"\n\u6295\u9012\u5b8c\u6210: \u6210\u529f {success} \u4e2a / \u8df3\u8fc7 {skipped} \u4e2a / \u5931\u8d25 {failed} \u4e2a")
            for r in results:
                if r["status"] != "\u6210\u529f":
                    _print(f"  {r['title']}: {r['status']} - {r.get('error', '')}")

        tasks[task_id].update({
            "status": "completed",
            "progress": 100,
            "result": {"results": results},
        })
        _print("\n\u6295\u9012\u6d41\u7a0b\u7ed3\u675f")
    except Exception as e:
        import traceback
        _print(f"\n\u6295\u9012\u5f02\u5e38: {e}")
        _print(traceback.format_exc())
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(e)
    finally:
        builtins.print = _orig_print

def _generate_chart_base64(df: pd.DataFrame) -> Optional[str]:
    """生成匹配度分布图，返回 base64 编码的 PNG。"""
    try:
        import base64
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        axes[0].hist(df["match_score"], bins=10, color="#4CAF50", alpha=0.7, edgecolor="white")
        axes[0].axvline(df["match_score"].mean(), color="red", linestyle="--",
                        label=f"平均 {df['match_score'].mean():.1f}")
        axes[0].set_xlabel("匹配度")
        axes[0].set_ylabel("岗位数量")
        axes[0].set_title("匹配度分布")
        axes[0].legend()
        axes[0].grid(axis="y", alpha=0.3)

        top10 = df.head(10).sort_values("match_score")
        colors = plt.cm.RdYlGn(top10["match_score"] / 100)
        axes[1].barh(range(len(top10)), top10["match_score"].values, color=colors)
        axes[1].set_yticks(range(len(top10)))
        axes[1].set_yticklabels(
            top10["title"].apply(lambda x: (str(x)[:12] + "..") if len(str(x)) > 12 else x)
        )
        axes[1].set_xlabel("匹配度")
        axes[1].set_title("Top 10")
        axes[1].set_xlim(0, 105)
        for i, v in enumerate(top10["match_score"]):
            axes[1].text(v + 1, i, str(v), va="center")

        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception as e:
        logger.warning(f"图表生成失败: {e}")
        return None


# ---------- API 路由 ----------




@app.post("/api/send-greetings")
async def start_send_greetings(request: dict):
    """start batch delivery task, receives {jobs: [...]}"""
    import uuid
    jobs = request.get("jobs", [])
    if not jobs:
        return JSONResponse({"error": "jobs list is empty"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {
        "status": "queued",
        "progress": 0,
        "log": "",
        "result": None,
        "error": None,
    }

    thread = threading.Thread(
        target=_run_send_greetings,
        args=(task_id, jobs),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"task_id": task_id})

@app.get("/favicon.ico")
async def favicon():
    """返回一个简单的 SVG favicon。"""
    return HTMLResponse(
        content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="6" fill="#1a73e8"/>'
                '<text x="16" y="23" font-size="20" text-anchor="middle" fill="white">'
                'J</text></svg>',
        media_type="image/svg+xml",
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回主页面 HTML。"""
    from fastapi.responses import HTMLResponse
    template_path = Path(__file__).parent / "templates" / "index.html"
    content = template_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/models")
async def list_models():
    """返回可用模型列表。"""
    return JSONResponse(AVAILABLE_MODELS)


@app.post("/api/analyze")
async def start_analysis(
    keyword: str = Form(...),
    city: str = Form("上海"),
    pages: int = Form(3),
    model: str = Form("deepseek-chat"),
    resume: Optional[UploadFile] = File(None),
):
    """启动分析任务，返回 task_id。"""
    task_id = str(uuid.uuid4())[:8]

    # 保存上传的简历
    resume_path = ""
    if resume and resume.filename:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(resume.filename).suffix or ".txt"
        save_path = DATA_DIR / f"resume_{task_id}{ext}"
        content = await resume.read()
        save_path.write_bytes(content)
        resume_path = str(save_path)

    tasks[task_id] = {
        "status": "queued",
        "progress": 0,
        "log": "",
        "result": None,
        "error": None,
    }

    thread = threading.Thread(
        target=_run_analysis,
        args=(task_id, keyword, city, pages, model, resume_path),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"task_id": task_id})


# ---------- 简历 API ----------


@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    """上传简历文件，保存到简历库。"""
    if not file.filename:
        return JSONResponse({"error": "no_file"}, status_code=400)

    ext = Path(file.filename).suffix or ".txt"
    if ext.lower() not in (".pdf", ".docx", ".doc", ".txt"):
        return JSONResponse({"error": "不支持的格式，仅支持 PDF/DOCX/TXT"}, status_code=400)

    _ensure_resume_dir()
    save_name = f"{str(uuid.uuid4())[:8]}_{file.filename}"
    save_path = RESUME_DIR / save_name
    content = await file.read()
    save_path.write_bytes(content)

    record = _add_resume(
        filename=file.filename,
        file_path=str(save_path.resolve()),
        file_size=len(content),
    )
    return JSONResponse({"resume": record})


@app.get("/api/resumes")
async def list_resumes():
    """获取所有已上传的简历。"""
    return JSONResponse({"resumes": _load_resumes()})


@app.delete("/api/resume/{resume_id}")
async def delete_resume(resume_id: str):
    """删除简历。"""
    ok = _delete_resume(resume_id)
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"success": True})


# ---------- 取消任务 ----------


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    """取消正在运行的任务。"""
    task = tasks.get(task_id)
    if task is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if task["status"] in ("queued", "running"):
        task["cancel_requested"] = True
        return JSONResponse({"status": "cancelling"})
    return JSONResponse({"status": task["status"]})


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    """查询任务状态和结果。"""
    task = tasks.get(task_id)
    if task is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse({
        "status": task["status"],
        "progress": task["progress"],
        "log": task.get("log", ""),
        "result": task.get("result"),
        "error": task.get("error"),
    })


# ---------- 历史 API ----------


@app.get("/api/history")
async def list_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    keyword: str = Query(""),
    city: str = Query(""),
):
    """分页获取历史运行记录。"""
    from src.database import get_runs
    runs, total = get_runs(page=page, per_page=per_page, keyword=keyword, city=city)
    return JSONResponse({"runs": runs, "total": total, "page": page, "per_page": per_page})


@app.get("/api/history/{run_id}")
async def get_history_detail(run_id: int):
    """获取单次历史运行的详情（含岗位列表）。"""
    from src.database import get_run, get_run_jobs
    run = get_run(run_id)
    if run is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    jobs = get_run_jobs(run_id)
    # 数据库字段名 → 前端期望的字段名
    for j in jobs:
        j["score"] = j.pop("match_score", 0)
    return JSONResponse({"run": run, "jobs": jobs})


@app.get("/api/history/{run_id}/log")
async def get_history_log(run_id: int):
    """读取历史运行的日志文件内容。"""
    from src.database import get_run
    run = get_run(run_id)
    if run is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    log_path = (run.get("log_path") or "").strip()
    if log_path and Path(log_path).exists():
        log_text = Path(log_path).read_text(encoding="utf-8")
    else:
        log_text = "(日志文件不存在)"
    return JSONResponse({"log": log_text})


@app.delete("/api/history/{run_id}")
async def delete_history(run_id: int):
    """删除一条历史运行记录。"""
    from src.database import delete_run
    success = delete_run(run_id)
    if not success:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"success": True})


# ---------- 定时任务 API ----------

@app.get("/api/schedule")
async def list_scheduled_tasks():
    """获取所有定时任务。"""
    from src.scheduler import list_tasks
    return JSONResponse({"tasks": list_tasks()})


@app.post("/api/schedule")
async def create_scheduled_task(request: dict):
    """创建定时任务。

    请求体示例:
    {
        "name": "每日投递",
        "keyword": "Python",
        "city": "上海",
        "pages": 3,
        "schedule_type": "daily",  // "daily" 或 "once"
        "schedule_time": "09:00",
        "resume_path": "",
        "resume_id": "",           // 或指定已上传简历的 ID
        "auto_send": true,
        "min_score": 70,
        "recommendations": ["强烈推荐", "建议投递"]
    }
    """
    from src.scheduler import add_task

    required = ["name", "keyword", "city", "schedule_type", "schedule_time"]
    for field in required:
        if field not in request:
            return JSONResponse({"error": f"missing_field: {field}"}, status_code=400)

    # 如果传了 resume_id，解析为实际路径
    resume_path = request.get("resume_path", "")
    resume_id = request.get("resume_id", "")
    if resume_id and not resume_path:
        resumes = _load_resumes()
        match = next((r for r in resumes if r.get("id") == resume_id), None)
        if match:
            resume_path = match.get("path", "")
        else:
            return JSONResponse({"error": f"简历不存在: {resume_id}"}, status_code=400)

    task = add_task(
        name=request["name"],
        keyword=request["keyword"],
        city=request["city"],
        pages=request.get("pages", 3),
        schedule_type=request["schedule_type"],
        schedule_time=request["schedule_time"],
        resume_path=resume_path,
        resume_id=resume_id,
        auto_send=request.get("auto_send", True),
        min_score=request.get("min_score", 70),
        recommendations=request.get("recommendations", ["强烈推荐", "建议投递"]),
    )
    return JSONResponse({"task": task})


@app.post("/api/schedule/{task_id}/toggle")
async def toggle_scheduled_task(task_id: str, request: dict):
    """启用/禁用定时任务。"""
    from src.scheduler import toggle_task
    enabled = request.get("enabled", True)
    toggle_task(task_id, enabled)
    return JSONResponse({"success": True, "enabled": enabled})


@app.delete("/api/schedule/{task_id}")
async def delete_scheduled_task(task_id: str):
    """删除定时任务。"""
    from src.scheduler import remove_task
    remove_task(task_id)
    return JSONResponse({"success": True})


@app.post("/api/schedule/{task_id}/run")
async def run_scheduled_task_now(task_id: str):
    """立即执行一次定时任务。"""
    from src.scheduler import list_tasks, _run_scheduled_task
    tasks = list_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        return JSONResponse({"error": "not_found"}, status_code=404)

    # 在后台线程执行
    thread = threading.Thread(
        target=_run_scheduled_task,
        args=(task_id, task),
        daemon=True,
    )
    thread.start()
    return JSONResponse({"success": True, "message": "任务已触发"})


# ---------- 启动 ----------

if __name__ == "__main__":
    setup_web_logging()
    from src.database import init_db
    init_db(DATA_DIR)

    # 初始化定时任务调度器
    from src.scheduler import init_scheduler
    init_scheduler()

    port = 7860
    # 端口被占用时自动找可用端口
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            import webbrowser
            print(f"端口 {port} 已被占用，尝试端口 {port + 1}")
            port = 7861
    url = f"http://127.0.0.1:{port}"
    print(f"启动服务: {url}")
    import webbrowser
    webbrowser.open(url)
    uvicorn.run(app, host="0.0.0.0", port=port)
