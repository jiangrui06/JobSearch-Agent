"""JobHunter AI — BOSS直聘智能求职系统。

采集 BOSS直聘岗位 -> AI Agent 评分 -> 输出个性化推荐。

使用方法：
    python -m src.main --keyword "Python" --city 上海 --resume resume.txt
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.settings import (
    DATA_DIR,
    GENERATE_CHART,
    LOG_DIR,
    LOG_LEVEL,
    RESUME_PATH,
    SAVE_CSV,
    SEARCH_CITY,
    SEARCH_KEYWORDS,
    SHOW_BROWSER,
    TOP_N_RESULTS,
)
from src.analyzer.agent import JobAgent
from src.scraper.boss_scraper import BossZhipinScraper

logger = logging.getLogger(__name__)

# 推荐等级颜色标签（控制台使用）
REC_LABELS = {
    "强烈推荐": "[+++]",
    "建议投递": "[++] ",
    "可以考虑": "[+]  ",
    "不推荐": "[-]  ",
}


def setup_logging():
    """配置日志输出到文件与控制台。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"jobhunter_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="JobHunter AI - BOSS直聘智能求职系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--keyword", "-k",
        default=SEARCH_KEYWORDS,
        help=f"搜索关键词（默认: {SEARCH_KEYWORDS}）",
    )
    parser.add_argument(
        "--city", "-c",
        default=SEARCH_CITY,
        help=f"目标城市（默认: {SEARCH_CITY}）",
    )
    parser.add_argument(
        "--pages", "-p",
        type=int,
        default=3,
        help="采集页数（默认: 3）",
    )
    parser.add_argument(
        "--resume", "-r",
        default=RESUME_PATH,
        help="简历文件路径（.pdf 或 .txt）",
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=TOP_N_RESULTS,
        help=f"展示前 N 个结果（默认: {TOP_N_RESULTS}）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（不显示浏览器窗口）",
    )
    return parser.parse_args()


def run_pipeline(args) -> pd.DataFrame:
    """执行 BOSS直聘采集 -> AI 分析流水线。

    Returns:
        带有评分和推荐的 DataFrame
    """
    # ---------- 阶段一：BOSS直聘数据采集 ----------
    print("\n" + "=" * 60)
    print("  JobHunter AI - BOSS直聘智能求职系统")
    print("=" * 60)

    print(f"\n[阶段 1/2] 正在从 BOSS直聘采集岗位数据...")
    print(f"  关键词: {args.keyword}")
    print(f"  城市: {args.city}")
    print(f"  页数: {args.pages}")
    if args.headless:
        print("  浏览器模式: 无头（静默）")
    else:
        print("  浏览器模式: 可见（需要手动登录）")

    keywords = [kw.strip() for kw in args.keyword.split(",")]
    combined_keyword = " ".join(keywords)

    with BossZhipinScraper() as scraper:
        all_raw = scraper.search(
            keyword=combined_keyword,
            city=args.city,
            page=args.pages,
        )

    if not all_raw:
        print("\n  未采集到数据，流程终止。")
        return pd.DataFrame()

    df = pd.DataFrame(all_raw)
    print(f"\n  采集完成: {len(df)} 条岗位数据\n")

    # ---------- 阶段二：AI Agent 评分与推荐 ----------
    print(f"[阶段 2/2] AI Agent 正在分析匹配度...")

    resume_text = ""
    if args.resume:
        agent = JobAgent()
        resume_text = agent.load_resume(args.resume) or ""

    if not resume_text:
        print("  (警告: 未加载简历，评分将不够精准)")

    agent = JobAgent()
    df = agent.analyze_jobs(df, resume_text)

    # 排序
    df = df.sort_values("match_score", ascending=False).reset_index(drop=True)
    return df


def output_results(df: pd.DataFrame, top_n: int):
    """输出结果：控制台展示 + CSV 保存 + 图表。"""
    if df.empty:
        print("没有结果可展示。")
        return

    # ---------- 按推荐等级分组展示 ----------
    print("\n" + "=" * 70)
    print("推荐岗位")
    print("=" * 70)

    rec_order = ["强烈推荐", "建议投递", "可以考虑", "不推荐"]
    shown = 0
    for rec_level in rec_order:
        group = df[df["recommendation"] == rec_level]
        if group.empty:
            continue

        print(f"\n  {REC_LABELS.get(rec_level, '')} {rec_level} ({len(group)} 个)")
        print("  " + "-" * 50)

        for _, row in group.iterrows():
            if shown >= top_n:
                break
            print(f"  #{shown + 1}  {row.get('title', 'N/A')}")
            print(f"      公司: {row.get('company', 'N/A')}  |  地点: {row.get('city', 'N/A')}")
            print(f"      薪资: {row.get('salary', 'N/A')}")
            print(f"      匹配度: {row.get('match_score', 'N/A')}/100")
            print(f"      理由: {row.get('match_reason', 'N/A')}")
            if row.get("recommendation_reason"):
                print(f"      推荐: {row.get('recommendation_reason', '')}")
            shown += 1

    # ---------- 统计概览 ----------
    print(f"\n" + "=" * 70)
    print("匹配概览")
    print(f"  总岗位数: {len(df)}")
    print(f"  平均匹配度: {df['match_score'].mean():.1f}")
    print(f"  最高匹配度: {df['match_score'].max()}")
    print(f"  最低匹配度: {df['match_score'].min()}")

    # 推荐等级分布
    for rec_level in rec_order:
        count = len(df[df["recommendation"] == rec_level])
        if count > 0:
            print(f"  {rec_level}: {count} 个")

    # ---------- 保存 CSV ----------
    if SAVE_CSV:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = DATA_DIR / f"boss_jobs_{timestamp}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"\n已保存: {csv_path}")

    # ---------- 图表 ----------
    if GENERATE_CHART and "match_score" in df.columns:
        _generate_chart(df)


def _generate_chart(df: pd.DataFrame):
    """生成匹配分数分布图。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chart_path = DATA_DIR / f"match_distribution_{timestamp}.png"

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
            top10["title"].apply(lambda x: x[:15] + ".." if len(str(x)) > 15 else x)
        )
        axes[1].set_xlabel("匹配度")
        axes[1].set_title("Top 10")
        axes[1].set_xlim(0, 105)
        for i, v in enumerate(top10["match_score"]):
            axes[1].text(v + 1, i, str(v), va="center")

        plt.tight_layout()
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"图表已保存: {chart_path}")
    except ImportError:
        logger.warning("matplotlib 未安装，跳过图表")
    except Exception as e:
        logger.warning(f"图表生成失败: {e}")


def main():
    log_file = setup_logging()
    args = parse_args()

    print(f"\n日志文件: {log_file}")

    df = run_pipeline(args)
    output_results(df, args.top)

    # ---------- 半自动投递 ----------
    if not df.empty and "greeting" in df.columns:
        interactive_send(df)

    print(f"\n流程完成。日志: {log_file}\n")


def interactive_send(df: pd.DataFrame):
    """评分完成后，询问用户是否批量投递招呼语。"""
    # 只考虑推荐等级较高的岗位
    sendable = df[df["recommendation"].isin(["强烈推荐", "建议投递"])].copy()
    if sendable.empty:
        return

    print("\n" + "=" * 70)
    print("是否对推荐岗位批量投递招呼语？")
    print("(系统将打开浏览器，逐个发送 AI 生成的个性化招呼语)")
    choice = input("  输入 y 确认投递，按回车跳过: ").strip().lower()
    if choice != "y":
        print("已跳过投递。")
        return

    # 确保 greeting 列存在
    if "greeting" not in sendable.columns:
        sendable["greeting"] = "您好，我对贵岗位很感兴趣，希望能进一步沟通。"

    print("\n以下岗位将进行投递：")
    print("-" * 70)
    for idx, (_, row) in enumerate(sendable.iterrows(), 1):
        greeting = row.get("greeting", "")
        print(f"  #{idx} {row.get('title', '')} @ {row.get('company', '')}")
        print(f"      招呼语: {greeting[:60]}{'...' if len(greeting) > 60 else ''}")
        print()

    confirm = input("  确认投递以上岗位？(y/回车取消): ").strip().lower()
    if confirm != "y":
        print("已取消投递。")
        return

    # 转为字典列表传给 scraper
    jobs_to_send = []
    for _, row in sendable.iterrows():
        jobs_to_send.append({
            "title": row.get("title", ""),
            "link": row.get("link", ""),
            "greeting": row.get("greeting", ""),
        })

    print("\n打开浏览器进行投递...")
    print("请在浏览器中扫码登录 BOSS直聘（如已登录则自动继续）")
    with BossZhipinScraper() as scraper:
        results = scraper.send_greetings(jobs_to_send)
        success = sum(1 for r in results if r["status"] == "成功")
        skipped = sum(1 for r in results if r["status"] == "跳过")
        failed = sum(1 for r in results if r["status"] == "失败")
        print(f"\n投递汇总: 成功 {success} / 跳过 {skipped} / 失败 {failed}")
        for r in results:
            if r["status"] != "成功":
                print(f"  {r['title']}: {r['status']} - {r.get('error', '')}")


if __name__ == "__main__":
    main()
