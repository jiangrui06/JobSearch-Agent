# JobHunter AI — BOSS直聘智能求职系统

> 自动采集 BOSS直聘 岗位，通过 AI Agent 对简历与岗位进行多维度匹配评分，生成个性化推荐报告，并支持自动投递。

---

## 目录

- [功能特性](#功能特性)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [使用方式](#使用方式)
- [首次登录 BOSS直聘](#首次登录-boss直聘)
- [跨机迁移指南](#跨机迁移指南)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [注意事项](#注意事项)

---

## 功能特性

- **岗位采集**：基于 DrissionPage 自动化浏览器，模拟真实用户搜索，支持分页抓取
- **AI 多维评分**：从技能匹配、经验相关性、薪资期望、发展前景四个维度评分
- **推荐分级**：强烈推荐 / 建议投递 / 可以考虑 / 不推荐
- **简历解析**：支持 PDF、DOCX、TXT 格式
- **结果可视化**：匹配度分布直方图 + Top 10 横向柱状图
- **AI 个性化招呼语**：根据简历和岗位要求自动生成差异化招呼语
- **自动投递**：对高匹配岗位自动打开详情页并发送招呼语
- **历史记录**：SQLite 持久化存储，支持查询、详情、删除
- **定时任务**：APScheduler 驱动，支持每日定时或单次执行
- **多入口**：FastAPI Web 界面 / Gradio 界面 / CLI 命令行

---

## 环境要求

- **操作系统**：Windows 10/11（推荐）、macOS、Linux
- **Python**：3.9 或更高版本
- **浏览器**：已安装 Google Chrome 或 Chromium（DrissionPage 会自动调用）
- **网络**：可访问 BOSS直聘 和所选 AI API 服务
- **AI API**：DeepSeek / 通义千问 / SenseNova 等 OpenAI 兼容接口的 API Key

---

## 快速开始

### 1. 克隆/复制项目

```bash
git clone <你的仓库地址> JobSearch-Agent
cd JobSearch-Agent/jobhunter_ai
```

### 2. 创建虚拟环境（推荐）

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
# 复制模板文件为正式配置文件
cp .env.example .env
```

用文本编辑器打开 `.env`，至少填写以下 **必填项**：

```ini
# AI API 配置（必填）
DEEPSEEK_API_KEY="sk-你的API密钥"
DEEPSEEK_API_BASE="https://token.sensenova.cn/v1"
AI_PROVIDER="sensenova"
AI_MODEL="sensenova-6.7-flash-lite"

# 简历路径（必填）
# 建议把简历放到 jobhunter_ai/data/resumes/ 目录下
RESUME_PATH="data/resumes/你的简历.docx"

# 求职偏好
SEARCH_KEYWORDS="Python开发,机器学习,数据挖掘"
SEARCH_CITY="上海"
```

> 路径建议使用正斜杠（`/`）或双反斜杠（`\\`），避免 Python 转义问题。

### 5. 放置简历

将你的简历文件（PDF / DOCX / TXT）放入：

```
jobhunter_ai/data/resumes/你的简历.docx
```

并确保 `.env` 中的 `RESUME_PATH` 指向该文件。

### 6. 启动

**方式一：FastAPI Web 界面（推荐，功能最全）**

```bash
cd jobhunter_ai
python -m src.web_app
```

浏览器会自动打开 `http://127.0.0.1:7861`。

**方式二：Gradio 界面**

```bash
cd jobhunter_ai
python -m src.app
```

访问 `http://127.0.0.1:7860`。

**方式三：命令行**

```bash
cd jobhunter_ai
python -m src.main --keyword "Python" --city 上海 --pages 3
```

---

## 使用方式

### FastAPI Web 界面

启动后打开 `http://127.0.0.1:7861`，界面包含三个标签页：

| 标签 | 功能 |
|------|------|
| **分析** | 输入关键词、城市、页数，上传简历，一键启动采集+评分 |
| **定时任务** | 创建、编辑、启用/禁用、立即执行、删除定时采集任务 |
| **历史记录** | 查看所有历史分析结果、详情、日志 |

### 命令行参数

```bash
python -m src.main [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-k, --keyword` | 搜索关键词，多个用逗号分隔 | `.env` 中的 `SEARCH_KEYWORDS` |
| `-c, --city` | 目标城市 | `.env` 中的 `SEARCH_CITY` |
| `-p, --pages` | 采集页数 | 3 |
| `-r, --resume` | 简历文件路径 | `.env` 中的 `RESUME_PATH` |
| `-t, --top` | 展示前 N 个结果 | 10 |
| `--headless` | 无头模式（不显示浏览器窗口） | 否 |

### 定时任务

在 Web 界面「定时任务」标签中：

1. 上传简历（简历会被保存到 `data/resumes/`）
2. 填写关键词、城市、执行时间、匹配度阈值
3. 选择是否自动投递
4. 创建任务

也可通过 API 手动触发：

```bash
curl -X POST http://127.0.0.1:7861/api/schedule/{task_id}/run
```

---

## 首次登录 BOSS直聘

由于 BOSS直聘 需要登录后才能搜索，首次运行时会弹出浏览器窗口：

1. 在弹出的浏览器中扫码或账号登录 BOSS直聘
2. 登录成功后，浏览器用户数据会持久化保存在 `data/browser_profile/`
3. 下次运行时会自动复用登录态，无需再次扫码

如果提示登录过期或无法搜索：

- 关闭程序
- 删除 `jobhunter_ai/data/browser_profile/` 目录
- 重新启动并扫码登录

---

## 跨机迁移指南

### 迁移前（旧电脑）

1. **不要提交 `.env` 文件**：里面包含你的 API Key 和本地路径
2. **不要提交 `data/browser_profile/`**：这是浏览器缓存，体积大且与机器绑定
3. **不要提交历史 CSV 和数据库**：迁移后可以在新电脑上重新生成
4. **只保留代码和简历模板**：建议把简历复制到 `data/resumes/` 后一起打包

### 迁移后（新电脑）

1. 复制项目文件夹到新电脑
2. 安装 Python 3.9+
3. 创建虚拟环境并安装依赖：
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```
4. 复制 `.env.example` 为 `.env`，填入新电脑的 API Key 和简历路径
5. 把简历放入 `data/resumes/`
6. 启动程序，重新扫码登录 BOSS直聘

---

## 常见问题

### 1. 启动时报错 `ModuleNotFoundError: No module named 'src'`

请确保在 `jobhunter_ai` 目录下运行：

```bash
cd jobhunter_ai
python -m src.web_app
```

不要从项目根目录 `JobSearch-Agent` 直接运行。

### 2. 简历加载失败

- 检查 `.env` 中 `RESUME_PATH` 是否指向正确文件
- 支持格式：`.pdf`、`.docx`、`.txt`
- 相对路径以 `jobhunter_ai` 目录为根，例如 `data/resumes/简历.docx`

### 3. 采集不到岗位数据

- 确认已登录 BOSS直聘
- 检查关键词和城市是否有效
- 如果浏览器被识别为自动化工具，尝试设置 `SHOW_BROWSER=true`
- 删除 `data/browser_profile/` 后重新登录

### 4. API 429 限流

- 降低 `.env` 中的 `MAX_WORKERS`（建议先声科技免费套餐设为 1-3）
- 增大 `API_DELAY`

### 5. 端口被占用

FastAPI 入口会自动在 7861-7870 范围内寻找可用端口，控制台会打印实际访问地址。

### 6. 自动投递失败

- 确保已登录 BOSS直聘
- 设置 `SHOW_BROWSER=true` 观察浏览器操作过程
- 检查日志中是否有 `未找到聊天输入框` 等提示

---

## 项目结构

```
JobSearch-Agent/
├── .gitignore                    # Git 忽略规则
├── README.md                     # 本文件
└── jobhunter_ai/
    ├── .env                      # 环境变量（本地配置，不提交）
    ├── .env.example              # 环境变量模板
    ├── requirements.txt          # Python 依赖
    ├── config/
    │   └── settings.py           # 配置加载
    ├── src/
    │   ├── web_app.py            # FastAPI Web 应用主入口（端口 7861）
    │   ├── app.py                # Gradio Web UI（端口 7860）
    │   ├── main.py               # CLI 入口
    │   ├── scheduler.py          # APScheduler 定时任务
    │   ├── database.py           # SQLite 持久化层
    │   ├── analyzer/
    │   │   └── agent.py          # AI Agent 评分引擎
    │   ├── scraper/
    │   │   └── boss_scraper.py   # BOSS直聘爬虫 + 自动投递
    │   ├── utils/
    │   │   └── resume_parser.py  # 简历解析
    │   └── templates/
    │       └── index.html        # FastAPI 前端页面
    └── data/
        ├── jobhunter.db          # SQLite 历史数据库（自动生成）
        ├── resumes/              # 上传的简历文件
        ├── browser_profile/      # 浏览器用户数据（自动生成，不提交）
        ├── logs/                 # 运行日志（自动生成）
        └── *.csv / *.png         # 历史结果文件（自动生成）
```

---

## 注意事项

1. **API Key 安全**：`.env` 文件包含 API Key，**不要提交到 Git**。项目已通过 `.gitignore` 忽略 `.env`。

2. **浏览器缓存**：`data/browser_profile/` 目录包含浏览器缓存、Cookie 和登录态，体积可能很大，**不要提交到 Git**。

3. **自动投递风险**：自动投递会向真实 HR 发送消息，请谨慎使用。建议在 `SHOW_BROWSER=true` 模式下先观察几次，确认行为正常后再开启全自动。

4. **法律与合规**：本工具仅用于个人求职辅助，请遵守 BOSS直聘 的使用条款，不要高频刷取或骚扰招聘方。

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 爬虫 | DrissionPage（Chromium 自动化）、jsonpath-ng |
| AI | OpenAI SDK（兼容 DeepSeek / 通义千问 / SenseNova） |
| 后端 | FastAPI + Uvicorn |
| 前端 | Bootstrap 5 + Font Awesome |
| 定时调度 | APScheduler |
| 数据库 | SQLite（WAL 模式） |
| 数据处理 | Pandas + Matplotlib |

---

如有问题，可查看 `jobhunter_ai/logs/` 目录下的日志文件排查。
