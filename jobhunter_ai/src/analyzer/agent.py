"""AI Agent: 对 BOSS直聘岗位进行匹配度评分并给出个性化推荐。"""

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config.settings import (
    AI_MODEL,
    DATA_DIR,
    DEEPSEEK_API_BASE,
    DEEPSEEK_API_KEY,
    SALARY_EXPECTATION,
    MAX_WORKERS,
)
from src.utils.resume_parser import parse_resume

logger = logging.getLogger(__name__)

# 请求超时（秒）
REQUEST_TIMEOUT = 60

SYSTEM_PROMPT = """你是一名顶级的 HR 专家和职业顾问，擅长分析职位描述与候选人简历之间的匹配度。

请按以下流程分析并输出 JSON：

1. **解析简历**：提取核心技能、工作年限、主要成就和求职意向。
2. **分析岗位**：识别关键职责、硬技能要求和软技能要求。
3. **评分维度**：
   - 技能匹配度 (0-100)：技能与岗位要求的匹配程度
   - 经验相关性 (0-100)：过往经验与岗位核心职责的相关性
   - 薪资期望 (0-100)：薪资期望是否在岗位范围内
   - 发展前景 (0-100)：该岗位的职业成长空间
4. **综合评分**：基于以上加权计算总分 (0-100 整数)。
5. **匹配理由**：用 2-3 句中文简要说明得分原因。
6. **推荐等级**：根据综合判断给出推荐等级：
   - "强烈推荐"：高度匹配，建议优先投递
   - "建议投递"：匹配度较高，值得申请
   - "可以考虑"：部分匹配，可作为备选
   - "不推荐"：匹配度低，不建议浪费精力
7. **推荐理由**：简要说明为什么给出这个推荐等级。
	8. **投递招呼语**：生成一段 30-60 字的个性化打招呼文案，用于 BOSS直聘 投递时发送给 HR。
	   要求：① 必须提到**具体公司名和岗位名称**，让 HR 感觉你不是海投的；
	   ② 说明为什么投递这个岗位（结合你的背景和岗位需求，让人感受到你是认真考虑过的）；
	   ③ 突出 1-2 个与岗位最相关的技能或经验亮点，让 HR 一眼看到你的价值；
	   ④ 语气自然真诚，像是在和一个真实的人对话，拒绝"您好，我对贵岗位很感兴趣"这种套话；
	   ⑤ 每个岗位的招呼语都要有差异化，不要用固定模板。

**必须输出严格的 JSON 格式**（不要包含 markdown 代码块标记）：
{
  "skill_match": 85,
  "experience_match": 70,
  "salary_match": 90,
  "growth_potential": 75,
  "match_score": 80,
  "match_reason": "您的 Python 技能与岗位高度匹配（85分），...",
  "recommendation": "建议投递",
  "recommendation_reason": "技能匹配度高，薪资在期望范围内，建议投递。",
  "greeting": "您好，看到贵团队在招Python后端。我3年Python经验，主力语言就是Python，之前负责过日活百万的API系统，正好和岗位要求匹配。想了解一下这个机会，期待您的回复！"
}
"""


def _make_cache_key(job_desc: str, resume_text: str, model: str) -> str:
    """生成缓存 key（基于岗位描述+简历前500字+模型名）。"""
    content = f"{model}|{job_desc}|{resume_text[:500]}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


class _ScoreCache:
    """线程安全的文件缓存，避免对相同岗位重复请求 LLM。"""

    def __init__(self, cache_dir: Path):
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 内存缓存，减少磁盘 IO
        self._mem: dict[str, dict] = {}

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        # 先查内存
        with self._lock:
            if key in self._mem:
                return self._mem[key]
        # 再查磁盘
        path = self._path(key)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                with self._lock:
                    self._mem[key] = data
                return data
            except Exception:
                return None
        return None

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            self._mem[key] = value
        try:
            with open(self._path(key), "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"缓存写入失败: {e}")

    def clear_memory(self) -> None:
        """清理内存缓存（不影响磁盘）。"""
        with self._lock:
            self._mem.clear()


class _RateLimiter:
    """滑动窗口限流器：跟踪最近 60s 内的请求次数，超过 RPM 限制时主动等待。"""

    def __init__(self, max_rpm: int = 30, window: int = 60):
        self.max_rpm = max_rpm
        self.window = window
        self._lock = threading.Lock()
        self._timestamps: list[float] = []

    def acquire(self):
        """等待直到可以安全发送下一个请求。"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            if len(self._timestamps) >= self.max_rpm:
                # RPM 配额已满，等待最早的时间戳过期
                wait = self._timestamps[0] + self.window - now
                if wait > 0:
                    logger.warning(f"RPM 配额已用 {len(self._timestamps)}/{self.max_rpm}，等待 {wait:.1f}s")
                    time.sleep(wait)
                    now = time.time()
                    cutoff = now - self.window
                    self._timestamps = [t for t in self._timestamps if t > cutoff]

            self._timestamps.append(now)


class JobAgent:
    """AI Agent：使用 DeepSeek 对岗位进行评分和推荐。"""

    def __init__(self):
        self._client = None
        self._cache = _ScoreCache(DATA_DIR / "score_cache")
        self._init_client()
        # 滑动窗口限流器（跟踪 RPM，动态调整请求速度）
        self._rate_limiter = _RateLimiter(max_rpm=30, window=60)

    def _init_client(self):
        """初始化 DeepSeek 客户端。"""
        if not DEEPSEEK_API_KEY:
            logger.error("未配置 DEEPSEEK_API_KEY，请检查 .env 文件")
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_API_BASE,
                timeout=REQUEST_TIMEOUT,
            )
            logger.info(f"已初始化 DeepSeek 客户端（{DEEPSEEK_API_BASE}）")
        except Exception as e:
            logger.error(f"DeepSeek 客户端初始化失败: {e}")

    def load_resume(self, path: str) -> Optional[str]:
        """加载简历文件内容。"""
        if not path:
            logger.warning("未指定简历路径")
            return None
        try:
            text = parse_resume(path)
            logger.info(f"简历加载成功（{len(text)} 字符）")
            return text
        except Exception as e:
            logger.warning(f"简历加载失败: {e}")
            return None

    def analyze_jobs(
        self, df: pd.DataFrame, resume_text: str
    ) -> pd.DataFrame:
        """为 DataFrame 中每个岗位进行评分并生成推荐（并发 + 缓存 + 限流）。

        Args:
            df: 包含岗位信息的 DataFrame
            resume_text: 简历文本

        Returns:
            新增 match_score, match_reason, recommendation, recommendation_reason 列的 DataFrame
        """
        if df.empty:
            logger.warning("DataFrame 为空，跳过分析。")
            df["match_score"] = pd.Series(dtype=int)
            df["match_reason"] = pd.Series(dtype=str)
            df["recommendation"] = pd.Series(dtype=str)
            df["recommendation_reason"] = pd.Series(dtype=str)
            return df

        if self._client is None:
            logger.error("DeepSeek 客户端未初始化，无法进行分析")
            df["match_score"] = 0
            df["match_reason"] = "AI 客户端未初始化"
            df["recommendation"] = "不推荐"
            df["recommendation_reason"] = "系统配置错误，无法进行分析"
            return df

        if not resume_text or len(resume_text.strip()) < 50:
            logger.warning("简历内容为空或过短，跳过 LLM 评分，全部标记为不推荐（防止缓存污染）")
            df["match_score"] = 0
            df["match_reason"] = "未配置简历或简历内容为空，无法进行匹配度评分"
            df["recommendation"] = "不推荐"
            df["recommendation_reason"] = "请先在设置中上传简历并绑定到定时任务"
            df["greeting"] = ""
            return df

        total = len(df)
        logger.info(f"开始评分: 共 {total} 个岗位, 并发 {MAX_WORKERS}, 缓存目录 {self._cache._dir}")
        start_time = time.time()

        # 构造任务列表
        tasks = []
        for idx, row in df.iterrows():
            job_desc = (
                f"职位：{row.get('title', '')}\n"
                f"公司：{row.get('company', '')}\n"
                f"地点：{row.get('city', '')}\n"
                f"薪资：{row.get('salary', '')}\n"
                f"要求：{row.get('requirements', '')}"
            )
            tasks.append((idx, job_desc, resume_text))

        # 并发执行
        results_map: dict[int, dict[str, Any]] = {}
        cache_hits = 0
        cache_misses = 0
        completed = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_idx: dict = {}
            for idx, job_desc, resume in tasks:
                cache_key = _make_cache_key(job_desc, resume, AI_MODEL or "default")
                cached = self._cache.get(cache_key)
                if cached is not None:
                    results_map[idx] = cached
                    cache_hits += 1
                    completed += 1
                    continue
                cache_misses += 1
                future = executor.submit(
                    self._analyze_single_with_cache,
                    idx,
                    job_desc,
                    resume,
                    cache_key,
                )
                future_to_idx[future] = idx

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results_map[idx] = future.result()
                except Exception as e:
                    logger.error(f"岗位 {idx} 评分异常: {e}")
                    results_map[idx] = {
                        "match_score": 0,
                        "match_reason": f"并发评分异常: {e}",
                        "recommendation": "不推荐",
                        "recommendation_reason": "系统异常",
                        "greeting": "",
                    }
                completed += 1
                if completed % 5 == 0 or completed == total:
                    elapsed = time.time() - start_time
                    logger.info(f"评分进度: {completed}/{total} ({elapsed:.1f}s)")

        elapsed = time.time() - start_time
        logger.info(
            f"评分完成: 总计 {total} 个, 缓存命中 {cache_hits}, "
            f"请求 {cache_misses}, 耗时 {elapsed:.1f}s, "
            f"平均 {elapsed/max(1, cache_misses):.1f}s/请求"
        )

        # 按原始顺序组装结果
        scores: list[int] = []
        reasons: list[str] = []
        recs: list[str] = []
        rec_reasons: list[str] = []
        greetings: list[str] = []

        for idx, _ in enumerate(df.iterrows()):
            result = results_map.get(idx, {})
            scores.append(result.get("match_score", 0))
            reasons.append(result.get("match_reason", ""))
            recs.append(result.get("recommendation", "可以考虑"))
            rec_reasons.append(result.get("recommendation_reason", ""))
            greeting = result.get("greeting", "")
            if not greeting:
                greeting = f"您好，我对贵公司的{df.iloc[idx].get('title', '该岗位')}很感兴趣，希望能进一步沟通。"
            greetings.append(greeting)

        df["match_score"] = scores
        df["match_reason"] = reasons
        df["recommendation"] = recs
        df["recommendation_reason"] = rec_reasons
        df["greeting"] = greetings
        return df

    def _throttle(self):
        """限流：滑动窗口 RPM 控制，超出时主动等待。"""
        self._rate_limiter.acquire()

    def _analyze_single_with_cache(
        self, idx: int, job_description: str, resume_content: str, cache_key: str
    ) -> dict[str, Any]:
        """带缓存的单个岗位分析（供线程池调用）。"""
        self._throttle()
        result = self._analyze_single(job_description, resume_content)
        self._cache.set(cache_key, result)
        return result

    def _analyze_single(
        self, job_description: str, resume_content: str
    ) -> dict[str, Any]:
        """对单个岗位进行分析（含 429 重试）。"""
        user_prompt = (
            f"## 简历内容\n{resume_content[:4000]}\n\n"
            f"## 岗位描述\n{job_description[:2000]}\n\n"
            f"## 薪资期望\n{SALARY_EXPECTATION if SALARY_EXPECTATION else '未明确'}\n\n"
            "请按系统提示的 JSON 格式输出评分结果。"
        )

        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                response = self._client.chat.completions.create(
                    model=AI_MODEL if AI_MODEL else "deepseek-chat",
                    temperature=0.5,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                return self._parse_response(response.choices[0].message.content)
            except Exception as e:
                err_str = str(e)
                is_429 = '429' in err_str or 'rpm exhausted' in err_str or 'quota_exceeded' in err_str
                if is_429 and attempt < max_attempts - 1:
                    wait = (attempt + 1) * 5  # 5s, 10s, 15s
                    logger.warning(f"RPM 限流，{wait}s 后重试 ({attempt + 2}/{max_attempts})...")
                    time.sleep(wait)
                    continue
                logger.error(f"DeepSeek 调用失败: {e}")
                return {
                    "skill_match": 0,
                    "experience_match": 0,
                    "salary_match": 0,
                    "growth_potential": 0,
                    "match_score": 0,
                    "match_reason": f"AI 评分失败: {e}",
                    "recommendation": "不推荐",
                    "recommendation_reason": "AI 分析失败，无法给出推荐",
                }

    def _parse_response(self, text: str) -> dict[str, Any]:
        """从 LLM 响应中解析 JSON。"""
        json_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL
        )
        json_str = json_match.group(1) if json_match else text.strip()

        start = json_str.find("{")
        end = json_str.rfind("}")
        if start != -1 and end != -1:
            json_str = json_str[start: end + 1]

        try:
            result = json.loads(json_str)
            # 确保包含推荐字段
            if "recommendation" not in result:
                result["recommendation"] = self._score_to_rec(result.get("match_score", 0))
            if "recommendation_reason" not in result:
                result["recommendation_reason"] = result.get("match_reason", "")
            return result
        except json.JSONDecodeError:
            logger.warning(f"LLM 返回非 JSON 格式，尝试容错解析: {text[:100]}")
            return {
                "match_score": 0,
                "match_reason": text[:200],
                "recommendation": "可以考虑",
                "recommendation_reason": "AI 响应解析失败",
            }

    @staticmethod
    def _score_to_rec(score: int) -> str:
        if score >= 85:
            return "强烈推荐"
        elif score >= 70:
            return "建议投递"
        elif score >= 50:
            return "可以考虑"
        return "不推荐"
