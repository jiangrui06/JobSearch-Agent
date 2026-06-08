"""JobHunter AI — Web UI (Gradio 界面)"""

import io
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import gradio as gr

import pandas as pd

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.settings import DATA_DIR
from src.analyzer.agent import JobAgent
from src.scraper.boss_scraper import BossZhipinScraper

logger = logging.getLogger(__name__)

# ---------- 核心逻辑 ----------

def run_analysis(keyword, city, pages, resume_path, progress=gr.Progress()):
    """执行采集->评分->推荐全流程，返回 (日志文本, 结果DataFrame, 统计, 图表路径)。"""
    log_capture = io.StringIO()
    _orig_stdout = sys.stdout
    sys.stdout = log_capture

    def emit(line=""):
        print(line)
        sys.stdout.flush()

    try:
        # ---- 阶段1: 采集 ----
        emit(f"[{datetime.now():%H:%M:%S}] === 阶段1/2: 采集 BOSS直聘数据 ===")
        emit(f"关键词: {keyword}  |  城市: {city}  |  页数: {pages}")
        progress(0.05, desc="正在登录BOSS直聘...")

        scraper = BossZhipinScraper()
        try:
            all_raw = scraper.search(keyword=keyword, city=city, page=int(pages))
        finally:
            scraper.close()

        if not all_raw:
            emit("未采集到数据，流程终止。")
            df_empty = pd.DataFrame(columns=["title","company","city","salary","match_score","recommendation"])
            sys.stdout = _orig_stdout
            return log_capture.getvalue(), df_empty, {}, None

        df = pd.DataFrame(all_raw)
        emit(f"采集完成: {len(df)} 条岗位数据\n")

        # ---- 阶段2: AI 评分 ----
        emit(f"[{datetime.now():%H:%M:%S}] === 阶段2/2: AI Agent 评分 ===")
        progress(0.2, desc="AI Agent 正在分析匹配度...")

        agent = JobAgent()
        resume_text = ""
        if resume_path:
            resume_text = agent.load_resume(resume_path) or ""
        if not resume_text:
            emit("(未加载简历，评分将不够精准)")

        df = agent.analyze_jobs(df, resume_text)
        df = df.sort_values("match_score", ascending=False).reset_index(drop=True)

        # ---- 统计信息 ----
        rec_order = ["强烈推荐", "建议投递", "可以考虑", "不推荐"]
        stats = {
            "总岗位数": len(df),
            "平均匹配度": round(df["match_score"].mean(), 1),
            "最高匹配度": int(df["match_score"].max()),
            "最低匹配度": int(df["match_score"].min()),
        }
        for rec in rec_order:
            count = len(df[df["recommendation"] == rec])
            stats[rec] = count

        emit(f"\n匹配概览: 平均{stats['平均匹配度']}分, 最高{stats['最高匹配度']}分")

        # ---- 保存CSV ----
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = DATA_DIR / f"boss_jobs_{ts}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        emit(f"结果已保存: {csv_path}")

        # ---- 生成图表 ----
        chart_path = None
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False

            chart_path = str(DATA_DIR / f"match_distribution_{ts}.png")

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
                top10["title"].apply(lambda x: (str(x)[:15] + "..") if len(str(x)) > 15 else x)
            )
            axes[1].set_xlabel("匹配度")
            axes[1].set_title("Top 10 匹配度")
            axes[1].set_xlim(0, 105)
            for i, v in enumerate(top10["match_score"]):
                axes[1].text(v + 1, i, str(v), va="center")

            plt.tight_layout()
            plt.savefig(chart_path, dpi=150, bbox_inches="tight")
            plt.close()
            emit(f"图表已保存: {chart_path}")
        except Exception as e:
            emit(f"图表生成跳过: {e}")

        progress(1.0, desc="完成")

    finally:
        sys.stdout = _orig_stdout

    return log_capture.getvalue(), df, stats, chart_path


def launch_browser_for_login():
    """在新线程中打开浏览器以供登录。"""
    def _open():
        from DrissionPage import ChromiumPage
        p = ChromiumPage()
        p.get("https://www.zhipin.com/web/user/?ka=header-login")
        while True:
            time.sleep(10)

    t = threading.Thread(target=_open, daemon=True)
    t.start()
    time.sleep(3)
    return "浏览器已打开，请在浏览器中扫码/账号登录 BOSS直聘。登录后可关闭该浏览器窗口。"


# ---------- Gradio UI ----------


def create_ui():
    """构建并返回 Gradio Blocks 应用。"""
    css = """
    .app-header { text-align: center; margin-bottom: 20px; }
    .app-header h1 { font-size: 28px; color: #1a73e8; margin-bottom: 5px; }
    .app-header p { color: #666; font-size: 14px; }
    .stat-box { text-align: center; padding: 12px; background: #f8f9fa; border-radius: 8px; }
    .stat-box .num { font-size: 28px; font-weight: bold; color: #1a73e8; }
    .stat-box .label { font-size: 12px; color: #666; }
    """

    with gr.Blocks(css=css, title="JobHunter AI - BOSS直聘智能求职系统",
                   theme=gr.themes.Soft(primary_hue="blue")) as demo:

        gr.HTML("""
        <div class="app-header">
            <h1>JobHunter AI</h1>
            <p>BOSS直聘 智能求职系统 — 自动采集 + AI 评分 + 个性化推荐</p>
        </div>
        """)

        with gr.Row(equal_height=True):
            with gr.Column(scale=2):
                keyword = gr.Textbox(label="关键词", value="Python",
                                     placeholder="多个关键词用逗号分隔")
                pages = gr.Slider(minimum=1, maximum=10, value=3, step=1,
                                  label="采集页数")
            with gr.Column(scale=2):
                city = gr.Textbox(label="城市", value="上海", placeholder="如 北京、上海、杭州")
                resume = gr.Textbox(label="简历路径", value="resume.txt",
                                    placeholder=".pdf 或 .txt 文件路径")

        with gr.Row():
            login_btn = gr.Button("打开浏览器登录", variant="secondary", size="sm")
            analyze_btn = gr.Button("开始采集分析", variant="primary", size="lg")

        login_msg = gr.Textbox(label="登录状态", interactive=False)

        with gr.Row():
            with gr.Column(scale=1):
                log_output = gr.Textbox(label="运行日志", lines=12, max_lines=20, interactive=False)

            with gr.Column(scale=2):
                results_table = gr.Dataframe(
                    label="岗位推荐结果",
                    headers=["序号", "职位", "公司", "城市", "薪资", "匹配度", "推荐等级", "推荐理由"],
                    datatype=["number", "str", "str", "str", "str", "number", "str", "str"],
                    wrap=True,
                )

        with gr.Row():
            stats_json = gr.JSON(label="统计概览")

        with gr.Row():
            chart_img = gr.Image(label="匹配度分布图", type="filepath")

        # ---------- 按钮事件 ----------

        login_btn.click(
            fn=launch_browser_for_login,
            outputs=login_msg,
        )

        def _run_wrapper(kw, c, p, r, progress=gr.Progress()):
            log, df, stats, chart = run_analysis(kw, c, p, r, progress)
            if df.empty:
                return log, [], stats, None

            # 格式化表格
            table_data = []
            for idx, row in df.iterrows():
                table_data.append([
                    idx + 1,
                    row.get("title", ""),
                    row.get("company", ""),
                    row.get("city", ""),
                    row.get("salary", ""),
                    row.get("match_score", 0),
                    row.get("recommendation", ""),
                    row.get("recommendation_reason", "")[:60],
                ])

            return log, table_data, stats, chart

        analyze_btn.click(
            fn=_run_wrapper,
            inputs=[keyword, city, pages, resume],
            outputs=[log_output, results_table, stats_json, chart_img],
        )

    return demo


if __name__ == "__main__":
    demo = create_ui()
    demo.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=True,
        inbrowser=True,
    )
