"""AI Agent: 对 BOSS直聘岗位进行匹配度评分并给出个性化推荐。"""

import json
import logging
import re
from typing import Any, Optional

import pandas as pd

from config.settings import (
    AI_MODEL,
    DEEPSEEK_API_BASE,
    DEEPSEEK_API_KEY,
    SALARY_EXPECTATION,
)
from src.utils.resume_parser import parse_resume

logger = logging.getLogger(__name__)

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

**必须输出严格的 JSON 格式**（不要包含 markdown 代码块标记）：
{
  "skill_match": 85,
  "experience_match": 70,
  "salary_match": 90,
  "growth_potential": 75,
  "match_score": 80,
  "match_reason": "您的 Python 技能与岗位高度匹配（85分），...",
  "recommendation": "建议投递",
  "recommendation_reason": "技能匹配度高，薪资在期望范围内，建议投递。"
}
"""


class JobAgent:
    """AI Agent：使用 DeepSeek 对岗位进行评分和推荐。"""

    def __init__(self):
        self._client = None
        self._init_client()

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
        """为 DataFrame 中每个岗位进行评分并生成推荐。

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

        scores: list[int] = []
        reasons: list[str] = []
        recs: list[str] = []
        rec_reasons: list[str] = []

        for idx, row in df.iterrows():
            job_desc = (
                f"职位：{row.get('title', '')}\n"
                f"公司：{row.get('company', '')}\n"
                f"地点：{row.get('city', '')}\n"
                f"薪资：{row.get('salary', '')}\n"
                f"要求：{row.get('requirements', '')}"
            )

            result = self._analyze_single(job_desc, resume_text)
            scores.append(result.get("match_score", 0))
            reasons.append(result.get("match_reason", ""))
            recs.append(result.get("recommendation", "可以考虑"))
            rec_reasons.append(result.get("recommendation_reason", ""))

            logger.info(
                f"[{idx + 1}/{len(df)}] {row.get('title', '')} @ "
                f"{row.get('company', '')} -> {scores[-1]}分 - {recs[-1]}"
            )

        df["match_score"] = scores
        df["match_reason"] = reasons
        df["recommendation"] = recs
        df["recommendation_reason"] = rec_reasons
        return df

    def _analyze_single(
        self, job_description: str, resume_content: str
    ) -> dict[str, Any]:
        """对单个岗位进行分析。"""
        user_prompt = (
            f"## 简历内容\n{resume_content[:4000]}\n\n"
            f"## 岗位描述\n{job_description[:2000]}\n\n"
            f"## 薪资期望\n{SALARY_EXPECTATION if SALARY_EXPECTATION else '未明确'}\n\n"
            "请按系统提示的 JSON 格式输出评分结果。"
        )

        try:
            response = self._client.chat.completions.create(
                model=AI_MODEL if AI_MODEL else "deepseek-chat",
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return self._parse_response(response.choices[0].message.content)
        except Exception as e:
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
