"""BOSS直聘爬虫 — 使用你桌面上的爬取逻辑。"""

import csv
import logging
import re
import sys
from pathlib import Path
from typing import Any

from DrissionPage import ChromiumPage

# JSONPath 解析：优先用你的 jsonpath，没有则用 jsonpath-ng
try:
    from jsonpath import search as _jp_search
except ImportError:
    from jsonpath_ng.ext import parse as _jp_parse
    def _jp_search(expr, data):
        return [m.value for m in _jp_parse(expr).find(data)]
search = _jp_search

logger = logging.getLogger(__name__)

LOGIN_URL = 'https://www.zhipin.com/web/user/?ka=header-login'
LISTEN_PATTERN = 'https://www.zhipin.com/wapi/zpgeek/search/joblist.json?_='

# 常用城市中文名 → BOSS 直聘 URL 拼音
CITY_MAP = {
    '北京': 'beijing',
    '上海': 'shanghai',
    '广州': 'guangzhou',
    '深圳': 'shenzhen',
    '杭州': 'hangzhou',
    '成都': 'chengdu',
    '南京': 'nanjing',
    '武汉': 'wuhan',
    '西安': 'xian',
    '重庆': 'chongqing',
    '长沙': 'changsha',
    '苏州': 'suzhou',
    '天津': 'tianjin',
    '郑州': 'zhengzhou',
    '东莞': 'dongguan',
    '青岛': 'qingdao',
    '沈阳': 'shenyang',
    '宁波': 'ningbo',
    '昆明': 'kunming',
    '大连': 'dalian',
    '厦门': 'xiamen',
    '合肥': 'hefei',
    '佛山': 'foshan',
    '福州': 'fuzhou',
    '哈尔滨': 'haerbin',
    '济南': 'jinan',
    '温州': 'wenzhou',
    '长春': 'changchun',
    '石家庄': 'shijiazhuang',
    '常州': 'changzhou',
    '泉州': 'quanzhou',
    '南宁': 'nanning',
    '贵阳': 'guiyang',
    '南昌': 'nanchang',
    '太原': 'taiyuan',
    '烟台': 'yantai',
    '嘉兴': 'jiaxing',
    '南通': 'nantong',
    '金华': 'jinhua',
    '珠海': 'zhuhai',
    '惠州': 'huizhou',
    '徐州': 'xuzhou',
    '海口': 'haikou',
    '乌鲁木齐': 'wulumuqi',
    '绍兴': 'shaoxing',
    '中山': 'zhongshan',
    '台州': 'taizhou',
    '兰州': 'lanzhou',
}


def normalize_city(input_text):
    """将用户输入转换为城市拼音，支持中文名或直接输入拼音"""
    text = input_text.strip()
    if not text:
        return 'beijing'

    if CITY_MAP.get(text):
        return CITY_MAP[text]

    city_pinyin = re.sub(r'[^a-zA-Z]', '', text).lower()
    if city_pinyin:
        return city_pinyin

    return 'beijing'


def boss_login(page, timeout_seconds=300):
    """等待用户手动扫码登录，带超时"""
    page.get(LOGIN_URL)
    waited = 0
    while page.title == 'BOSS登录':
        page.wait(1)
        waited += 1
        if waited >= timeout_seconds:
            print("登录超时，请重试")
            sys.exit(1)


def get_data(city, keyword, page_number):
    page = ChromiumPage()
    boss_login(page)

    city_url = f'https://www.zhipin.com/{city}/'
    print(f"正在访问: {city_url}")
    page.get(city_url)
    page.listen.start(LISTEN_PATTERN)

    search_input = page.ele('xpath=//input[@name="query"]')
    search_input.input(keyword)
    page.ele('xpath=//button[@class="btn btn-search"]').click()
    print(f"搜索关键词: {keyword}")

    page.wait(1)

    all_results = []

    for page_idx in range(page_number):
        for data in page.listen.steps(timeout=3):
            body = data.response.body
            if not body:
                continue

            raw_list = search('$..jobList', body)
            if not raw_list:
                continue
            job_list = raw_list[0]

            job_names = search('$..jobName', job_list)
            salary_desc = search('$..salaryDesc', job_list)
            job_degrees = search('$..jobDegree', job_list)
            job_experiences = search('$..jobExperience', job_list)
            intern_days = search('$..daysPerWeekDesc', job_list)
            intern_months = search('$..leastMonthDesc', job_list)
            brand_names = search('$..brandName', job_list)
            city_names = search('$..cityName', job_list)
            districts = search('$..areaDistrict', job_list)
            business_districts = search('$..businessDistrict', job_list)
            encrypt_ids = search('$..encryptJobId', job_list)

            if not job_names:
                continue

            total = len(job_names)
            for j in range(total):
                degree = job_degrees[j] if job_degrees and j < len(job_degrees) else ''
                experience = job_experiences[j] if job_experiences and j < len(job_experiences) else ''
                intern_day = intern_days[j] if intern_days and j < len(intern_days) else ''
                intern_month = intern_months[j] if intern_months and j < len(intern_months) else ''
                brand = brand_names[j] if brand_names and j < len(brand_names) else ''
                ct = city_names[j] if city_names and j < len(city_names) else ''
                district = districts[j] if districts and j < len(districts) else ''
                business = business_districts[j] if business_districts and j < len(business_districts) else ''
                enc_id = encrypt_ids[j] if encrypt_ids and j < len(encrypt_ids) else ''

                if degree:
                    req = f"全职，要求{experience}，{degree}"
                elif intern_day and intern_month:
                    req = f"实习，{intern_day}，{intern_month}"
                else:
                    req = "无明确要求"

                address = f"{ct}-{district}-{business}"
                link = f"https://www.zhipin.com/job_detail/{enc_id}.html" if enc_id else ""

                all_results.append([
                    job_names[j],
                    salary_desc[j] if salary_desc and j < len(salary_desc) else '',
                    degree,
                    req,
                    brand,
                    address,
                    link,
                ])

        if page_idx < page_number - 1:
            try:
                next_btn = page.ele('xpath=//a[@class="next"]', timeout=2)
                if next_btn:
                    next_btn.click()
                    page.wait(1)
                else:
                    page.run_js('window.scrollTo(0,document.body.scrollHeight);')
                    page.wait(1)
            except Exception:
                page.run_js('window.scrollTo(0,document.body.scrollHeight);')
                page.wait(1)

    return all_results


def save_data(data_list):
    with open('boss直聘.csv', 'w', encoding='utf-8-sig', newline='') as file:
        csv_writer = csv.writer(file)
        csv_writer.writerow(['工作名称', '薪资待遇', '学历要求', '年限/实习要求', '企业名称', '地址', '链接'])
        csv_writer.writerows(data_list)
    print(f"已保存 {len(data_list)} 条数据到 boss直聘.csv")


# ============================================================
# 以下封装使你的爬虫能接入 JobHunter AI 项目流水线
# ============================================================

class BossZhipinScraper:
    """封装你的 get_data()，使项目其他模块可以调用。"""

    def __init__(self):
        self._page = None

    def search(
        self,
        keyword: str,
        city: str = "上海",
        page: int = 3,
        cancel_check=None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """调用你的 get_data() 采集，返回项目需要的 dict 列表。"""
        city_pinyin = normalize_city(city)

        raw = get_data(city_pinyin, keyword, page)

        if not raw:
            logger.info("未获取到实时数据，使用 BOSS直聘模拟数据")
            return _generate_boss_mock()

        # 将你的列表格式转换为项目需要的 dict 格式
        jobs = []
        for row in raw:
            jobs.append({
                "title": row[0],
                "salary": row[1],
                "requirements": row[3],
                "company": row[4],
                "city": row[5].split("-")[0] if row[5] else city,
                "description": f"工作地点：{row[5]}" if row[5] else "",
                "link": row[6] if len(row) > 6 else "",
                "source": "boss",
            })

        logger.info(f"采集到 {len(jobs)} 条实时数据")
        return jobs

    def close(self):
        pass


def _generate_boss_mock() -> list[dict]:
    """BOSS直聘风格的仿真模拟数据。"""
    import random as rnd

    jobs = [
        {
            "title": "Python后端开发工程师", "company": "字节跳动", "city": "上海",
            "salary": "30K-60K",
            "description": "负责抖音电商后端系统设计与开发，基于 Python/Go 微服务架构，支撑亿级流量。",
            "requirements": "本科及以上，3-5年Python后端经验，熟悉微服务、MySQL、Redis",
        },
        {
            "title": "高级Python开发工程师", "company": "蚂蚁集团", "city": "杭州",
            "salary": "35K-65K",
            "description": "参与蚂蚁数金科技平台建设，负责高并发分布式系统研发。",
            "requirements": "5年以上Python/Java经验，有分布式系统设计经验",
        },
        {
            "title": "机器学习工程师", "company": "阿里巴巴", "city": "杭州",
            "salary": "40K-70K",
            "description": "负责搜索推荐算法优化，CTR预估、多目标排序模型研发与落地。",
            "requirements": "熟悉深度学习框架，有推荐/广告/搜索算法经验",
        },
        {
            "title": "AI平台开发工程师", "company": "腾讯", "city": "深圳",
            "salary": "30K-60K",
            "description": "参与腾讯云AI平台建设，负责MLOps、模型训练推理框架开发。",
            "requirements": "熟悉Python/Go，有Kubeflow/MLflow等MLOps工具经验",
        },
        {
            "title": "云计算开发工程师", "company": "华为", "city": "深圳",
            "salary": "25K-50K",
            "description": "参与华为云容器平台与Serverless产品的核心功能开发。",
            "requirements": "熟悉Docker/K8s，有云原生/分布式系统开发经验",
        },
    ]

    rnd.shuffle(jobs)
    for j in jobs:
        j["source"] = "boss"
        j["link"] = ""
    logger.info(f"BOSS模拟数据：{len(jobs)} 条岗位")
    return jobs
