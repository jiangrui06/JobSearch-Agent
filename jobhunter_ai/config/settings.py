"""用户配置：API密钥、求职偏好、爬虫设置。"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

# ==================== AI API 配置 ====================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek").lower()
AI_MODEL = os.getenv("AI_MODEL", "deepseek-chat")

# ==================== 简历路径 ====================
RESUME_PATH = os.getenv("RESUME_PATH", "")

# ==================== 求职偏好 ====================
SEARCH_KEYWORDS = os.getenv("SEARCH_KEYWORDS", "Python开发,机器学习,数据挖掘")
SEARCH_CITY = os.getenv("SEARCH_CITY", "上海")
SALARY_EXPECTATION = os.getenv("SALARY_EXPECTATION", "")

# ==================== BOSS直聘爬虫配置 ====================
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "true").lower() == "true"

# ==================== 输出配置 ====================
TOP_N_RESULTS = int(os.getenv("TOP_N_RESULTS", "10"))
SAVE_CSV = os.getenv("SAVE_CSV", "true").lower() == "true"
GENERATE_CHART = os.getenv("GENERATE_CHART", "true").lower() == "true"

# ==================== 日志配置 ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
