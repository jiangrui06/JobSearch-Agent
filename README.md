# JobHunter AI — BOSS直聘智能求职系统

自动采集 BOSS直聘岗位数据，通过 AI Agent 进行简历匹配度评分，生成个性化推荐报告。

## 架构概览

```
你
 │
 ├─ CLI  (python -m src.main)
 ├─ Web  (FastAPI + Bootstrap)
 └─ UI  (Gradio)
 │
 └─── JobHunter AI ───┬── Scraper ─── BOSS直聘
                      │
                      ├── AI Agent ─── DeepSeek / 通义千问
                      │
                      ├── Resume Parser ─── PDF / DOCX / TXT
                      │
                      └── Database ─── SQLite (分析历史)
```

## 功能

- **岗位采集** — 基于 DrissionPage 自动化浏览器，支持分页抓取 BOSS直聘 岗位
- **AI 评分** — 从技能匹配、经验相关性、薪资期望、发展前景四个维度评分
- **推荐分级** — 强烈推荐 / 建议投递 / 可以考虑 / 不推荐
- **简历解析** — 支持 PDF、DOCX、TXT 格式
- **结果可视化** — 匹配度分布直方图 + Top 10 横向柱状图
- **历史记录** — SQLite 持久化，支持查询、详情、删除
- **多出口** — CLI / FastAPI Web / Gradio 三种使用方式

## 快速开始

### 1. 配置环境变量

复制 `.env` 文件，配置 API Key：

```bash
# AI API（至少配置一个）
DEEPSEEK_API_KEY="your-api-key"
DEEPSEEK_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
AI_MODEL="qwen-plus"         # qwen-plus / qwen-max / qwen-turbo

# 求职偏好
SEARCH_KEYWORDS="Python开发,机器学习,数据挖掘"
SEARCH_CITY="上海"
```

项目使用阿里云百炼平台的 OpenAI 兼容接口，`qwen-plus` 模型性价比最高。

### 2. 安装依赖

```bash
cd jobhunter_ai
pip install -r requirements.txt
```

### 3. 运行

**CLI 模式：**

```bash
python -m src.main --keyword "Python" --city 上海 --resume resume.txt
```

**Web 模式 (FastAPI)：**

```bash
python -m src.web_app
# 访问 http://127.0.0.1:7860
```

**Gradio 模式：**

```bash
python -m src.app
# 访问 http://127.0.0.1:7860
```

## 项目结构

```
jobhunter_ai/
├── .env                      # 环境变量（API Key、求职偏好）
├── requirements.txt          # Python 依赖
├── resume.txt                # 示例简历
├── config/
│   └── settings.py           # 配置加载
├── data/
│   ├── jobhunter.db          # SQLite 历史数据库
│   └── browser_profile/      # 浏览器缓存（自动生成）
├── src/
│   ├── main.py               # CLI 入口
│   ├── app.py                # Gradio Web UI
│   ├── web_app.py            # FastAPI Web 应用
│   ├── database.py           # SQLite 持久化层
│   ├── analyzer/
│   │   └── agent.py          # AI Agent 评分引擎
│   ├── scraper/
│   │   └── boss_scraper.py   # BOSS直聘爬虫
│   ├── utils/
│   │   └── resume_parser.py  # 简历解析（PDF/DOCX/TXT）
│   └── templates/
│       └── index.html        # FastAPI 前端页面
└── logs/                     # 运行日志（自动生成）
```

## AI 评分维度

| 维度 | 权重 | 说明 |
|------|------|------|
| 技能匹配度 | 0-100 | 技能与岗位要求的匹配程度 |
| 经验相关性 | 0-100 | 过往经验与岗位核心职责的相关性 |
| 薪资期望 | 0-100 | 薪资期望是否在岗位范围内 |
| 发展前景 | 0-100 | 该岗位的职业成长空间 |

最终综合评分后，系统自动给出推荐等级。

## 技术栈

- **爬虫** — DrissionPage（自动化浏览器）、jsonpath-ng
- **AI** — OpenAI SDK（兼容 DeepSeek / 通义千问 / Claude）
- **后端** — FastAPI + Uvicorn / Gradio
- **前端** — Bootstrap 5 + Font Awesome
- **数据库** — SQLite（WAL 模式）
- **数据处理** — Pandas + Matplotlib
