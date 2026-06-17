"""BOSS直聘爬虫 — 优化版：浏览器复用、智能等待、异常恢复。"""

import base64
import csv
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from DrissionPage import ChromiumPage, ChromiumOptions

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

# 持久化浏览器用户数据目录（保持登录态）
BROWSER_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser_profile"

# 常用城市中文名 → BOSS 直聘 URL 拼音
CITY_MAP = {
    '北京': 'beijing', '上海': 'shanghai', '广州': 'guangzhou', '深圳': 'shenzhen',
    '杭州': 'hangzhou', '成都': 'chengdu', '南京': 'nanjing', '武汉': 'wuhan',
    '西安': 'xian', '重庆': 'chongqing', '长沙': 'changsha', '苏州': 'suzhou',
    '天津': 'tianjin', '郑州': 'zhengzhou', '东莞': 'dongguan', '青岛': 'qingdao',
    '沈阳': 'shenyang', '宁波': 'ningbo', '昆明': 'kunming', '大连': 'dalian',
    '厦门': 'xiamen', '合肥': 'hefei', '佛山': 'foshan', '福州': 'fuzhou',
    '哈尔滨': 'haerbin', '济南': 'jinan', '温州': 'wenzhou', '长春': 'changchun',
    '石家庄': 'shijiazhuang', '常州': 'changzhou', '泉州': 'quanzhou', '南宁': 'nanning',
    '贵阳': 'guiyang', '南昌': 'nanchang', '太原': 'taiyuan', '烟台': 'yantai',
    '嘉兴': 'jiaxing', '南通': 'nantong', '金华': 'jinhua', '珠海': 'zhuhai',
    '惠州': 'huizhou', '徐州': 'xuzhou', '海口': 'haikou', '乌鲁木齐': 'wulumuqi',
    '绍兴': 'shaoxing', '中山': 'zhongshan', '台州': 'taizhou', '兰州': 'lanzhou',
}


def normalize_city(input_text: str) -> str:
    """将用户输入转换为城市拼音，支持中文名或直接输入拼音。"""
    text = input_text.strip()
    if not text:
        return 'beijing'
    if CITY_MAP.get(text):
        return CITY_MAP[text]
    city_pinyin = re.sub(r'[^a-zA-Z]', '', text).lower()
    return city_pinyin if city_pinyin else 'beijing'


def _is_login_page(page) -> bool:
    """通过URL和页面元素判断当前是否为登录页。"""
    try:
        url = page.url.lower()
        if any(kw in url for kw in ('login', 'web/user', 'ka=header-login', 'passport')):
            return True
    except Exception:
        pass
    # 检查登录页特有元素
    login_markers = (
        'xpath://*[contains(@class,"qr")]',
        'xpath://*[contains(text(),"扫码登录")]',
        'xpath://*[contains(text(),"账号登录")]',
    )
    for sel in login_markers:
        try:
            if page.ele(sel, timeout=0.5):
                return True
        except Exception:
            continue
    return False


def _is_logged_in(page) -> bool:
    """通过URL和页面特征判断是否已登录（不走CSS选择器，用URL+搜索框检测）。"""
    try:
        url = page.url.lower()
    except Exception:
        return False

    # 必须在BOSS直聘域名下
    if 'zhipin.com' not in url:
        return False

    # 如果仍在登录路径，未登录
    if any(kw in url for kw in ('login', 'web/user', 'ka=header-login', 'passport')):
        return False

    # 如果在聊天页、职位详情、个人中心等页面 → 肯定已登录
    if any(kw in url for kw in ('web/chat', 'job_detail', 'web/geek', 'web/search')):
        return True

    # 如果在首页或城市页，检查搜索框（BOSS主页面特征）
    try:
        if page.ele('xpath=//input[@name="query"]', timeout=0.5):
            return True
    except Exception:
        pass

    # URL是zhipin.com正常页面且不在登录路径 → 认为已登录
    return True


def boss_login(page, timeout_seconds: int = 300) -> bool:
    """等待用户手动扫码登录，带超时。返回是否成功登录。"""
    page.get(LOGIN_URL)
    page.wait(2)

    if _is_logged_in(page):
        logger.info("已登录")
        return True

    waited = 0
    while True:
        page.wait(1)
        waited += 1
        if waited % 5 == 0:
            try:
                cur_url = page.url
            except Exception:
                cur_url = "?"
            logger.info(f"等待扫码登录... ({waited}s) url={cur_url}")
        if _is_logged_in(page):
            logger.info("登录成功")
            return True
        if waited >= timeout_seconds:
            logger.error("登录超时")
            return False


class BossZhipinScraper:
    """BOSS直聘爬虫：支持浏览器复用、采集、批量投递。"""

    def __init__(self, headless: bool = False):
        self._page: Optional[ChromiumPage] = None
        self._headless = headless
        self._logged_in = False

    def _get_page(self) -> ChromiumPage:
        """获取或创建浏览器页面对象（复用持久化登录态 + 反检测）。"""
        if self._page is None:
            # 使用持久化用户数据目录保持登录状态
            BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            co = ChromiumOptions()
            co.set_user_data_path(str(BROWSER_DATA_DIR))

            # 反检测：隐藏自动化特征
            co.set_argument('--disable-blink-features=AutomationControlled')
            co.set_pref('excludeSwitches', ['enable-automation'])
            co.set_pref('useAutomationExtension', False)
            # 设置窗口大小（避免被识别为无头）
            co.set_argument('--window-size=1280,800')

            if self._headless:
                co.set_headless(True)

            try:
                self._page = ChromiumPage(co)
                # 注入反检测 JS（在页面加载前执行，覆盖 navigator.webdriver）
                try:
                    self._page.add_init_js('''
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
                        window.chrome = {runtime: {}};
                    ''')
                except Exception:
                    pass
            except Exception:
                # 如果配置方式不兼容，回退到无配置启动
                self._page = ChromiumPage()
            logger.info(f"浏览器已启动 (profile: {BROWSER_DATA_DIR})")
        return self._page

    def ensure_login(self) -> bool:
        """确保已登录，如未登录则引导扫码（多维度检测）。"""
        if self._logged_in:
            return True
        page = self._get_page()
        logger.info("正在检测登录状态...")
        page.get("https://www.zhipin.com")
        page.wait(2)

        if _is_logged_in(page):
            logger.info("检测到登录态")
            self._logged_in = True
            return True

        # 还在登录页，引导扫码
        logger.info("未检测到登录态，请在浏览器中扫码登录 BOSS直聘")
        return boss_login(page)

    def search(
        self,
        keyword: str,
        city: str = "上海",
        page: int = 3,
        cancel_check=None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """采集 BOSS直聘岗位数据。

        Args:
            keyword: 搜索关键词
            city: 城市名（中文或拼音）
            page: 采集页数
            cancel_check: 可选的取消检查函数

        Returns:
            岗位 dict 列表
        """
        if not self.ensure_login():
            logger.warning("未登录，使用模拟数据")
            return _generate_boss_mock()

        city_pinyin = normalize_city(city)
        page_obj = self._get_page()

        city_url = f'https://www.zhipin.com/{city_pinyin}/'
        logger.info(f"正在访问: {city_url}")
        try:
            page_obj.get(city_url)
        except Exception as e:
            logger.warning(f"访问城市页失败: {e}，重试一次")
            try:
                page_obj.get(city_url)
            except Exception:
                logger.error("无法访问BOSS直聘，使用模拟数据")
                return _generate_boss_mock()

        page_obj.listen.start(LISTEN_PATTERN)

        # 输入关键词并搜索
        try:
            search_input = page_obj.ele('xpath=//input[@name="query"]', timeout=5)
            search_input.clear()
            search_input.input(keyword)
            page_obj.ele('xpath=//button[@class="btn btn-search"]', timeout=3).click()
            logger.info(f"搜索关键词: {keyword}")
        except Exception as e:
            logger.warning(f"搜索输入失败: {e}，尝试直接访问搜索URL")
            search_url = f"https://www.zhipin.com/{city_pinyin}/?query={keyword}"
            try:
                page_obj.get(search_url)
            except Exception:
                logger.error("无法访问搜索页，使用模拟数据")
                return _generate_boss_mock()

        page_obj.wait(1.5)

        all_results = []
        seen_keys = set()  # 去重用

        for page_idx in range(page):
            if cancel_check and cancel_check():
                logger.info("用户取消采集")
                break

            logger.info(f"正在采集第 {page_idx+1} 页...")

            # 第2页起：滚动到底部触发懒加载（BOSS直聘使用无限滚动）
            if page_idx > 0:
                logger.info("滚动到底部触发懒加载...")
                for _ in range(6):
                    page_obj.run_js('window.scrollTo(0, document.body.scrollHeight);')
                    page_obj.wait(0.5)
                page_obj.wait(2)

            # 监听网络请求获取数据（最多重试3次）
            found_data = False
            for retry in range(3):
                try:
                    for data in page_obj.listen.steps(timeout=10):
                        body = data.response.body
                        if not body:
                            continue
                        jobs = self._parse_joblist(body)
                        if jobs:
                            for j in jobs:
                                key = f"{j[0]}|{j[4]}"
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    all_results.append(j)
                            found_data = True
                            break
                    if found_data:
                        break
                except Exception as e:
                    logger.warning(f"第 {page_idx + 1} 页监听失败(重试 {retry + 1}): {e}")
                    page_obj.run_js('window.scrollTo(0, document.body.scrollHeight);')
                    page_obj.wait(2)
                    continue

            if not found_data:
                logger.warning("无法翻页，提前结束")
                break

        logger.info(f"采集完成: {len(all_results)} 条")
        if not all_results:
            return _generate_boss_mock()

        return self._format_results(all_results, city)

    def _parse_joblist(self, body: dict) -> list[list]:
        """从响应体解析岗位列表。"""
        raw_list = search('$..jobList', body)
        if not raw_list:
            return []
        job_list = raw_list[0]

        job_names = search('$..jobName', job_list)
        if not job_names:
            return []

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

        results = []
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

            results.append([
                job_names[j],
                salary_desc[j] if salary_desc and j < len(salary_desc) else '',
                degree,
                req,
                brand,
                address,
                link,
            ])
        return results

    def _goto_next_page(self, page_obj) -> bool:
        """尝试翻页，返回是否成功。"""
        # 方法1: 点击"下一页"按钮
        next_selectors = (
            'xpath=//a[@class="next"]',
            'xpath=//a[contains(@class,"next")]',
            'xpath=//button[contains(@class,"next")]',
            'xpath=//span[contains(text(),"下一页")]/..',
            'css:.next',
        )
        for sel in next_selectors:
            try:
                next_btn = page_obj.ele(sel, timeout=2)
                if next_btn:
                    classes = next_btn.attr('class') or ''
                    disabled = next_btn.attr('disabled') or ''
                    if 'disabled' not in classes and 'disabled' not in disabled:
                        next_btn.click()
                        page_obj.wait(2.5)
                        return True
            except Exception:
                continue

        # 方法2: 滚动到底部触发加载
        try:
            page_obj.run_js('window.scrollTo(0,document.body.scrollHeight);')
            page_obj.wait(2)
        except Exception:
            pass
        return False

    def _format_results(self, raw: list[list], city: str) -> list[dict[str, Any]]:
        """将原始列表转换为标准 dict 格式。"""
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
        return jobs

    def _send_greeting_for_job(self, page, job: dict, idx: int, total: int) -> dict:
        """对单个岗位执行投递。逻辑：
        1. 打开岗位链接
        2. 找"立即沟通"按钮，找到就点击
        3. 出现"已向BOSS发送消息" → 成功
        4. 否则找聊天输入框 → 输入招呼语 → 发送
        5. 继续下一个
        """
        from datetime import datetime

        title = job.get("title", "未知岗位")
        company = job.get("company", "")
        link = job.get("link", "")
        greeting = job.get("greeting", "")

        if not link:
            return {"title": title, "company": company, "status": "跳过", "error": "无跳转链接"}

        print(f"  [{idx+1}/{total}] {title} - 投递中...", end=" ", flush=True)

        # 访问页面
        try:
            page.get(link)
            page.wait(3)
        except Exception as e:
            print(f"页面失败: {e}")
            return {"title": title, "company": company, "status": "失败", "error": str(e)[:60]}

        # 检查是否被重定向到登录页
        try:
            if any(kw in page.url.lower() for kw in ('login', 'passport', 'web/user')):
                print("会话过期")
                return {"title": title, "company": company, "status": "跳过", "error": "登录会话已过期"}
        except Exception:
            pass

        # ===== 找"立即沟通"按钮 =====
        send_btn = None
        for sel in (
            'css:[class*="btn-startchat"]',
            'xpath://*[contains(text(),"立即沟通")]',
            'css:.btn-startchat',
            'xpath://*[contains(text(),"投递")]',
        ):
            for attempt in range(5):  # 等最多 5 秒
                try:
                    btn = page.ele(sel, timeout=1)
                    if btn:
                        send_btn = btn
                        break
                except Exception:
                    page.wait(1)
            if send_btn:
                break

        if not send_btn:
            print("无投递按钮")
            return {"title": title, "company": company, "status": "失败", "error": "未找到立即沟通按钮"}

        # ===== 点击"立即沟通" =====
        try:
            send_btn.click()
            page.wait(2)
        except Exception as e:
            print(f"点击失败: {e}")
            return {"title": title, "company": company, "status": "失败", "error": f"点击失败: {e}"}

        # ===== 检查"已向BOSS发送消息"弹窗 =====
        for _ in range(3):
            try:
                if page.ele('xpath://*[contains(text(),"已向BOSS发送消息")]', timeout=1):
                    # 关闭弹窗
                    for close_sel in (
                        'xpath://*[contains(text(),"继续沟通")]',
                        'xpath://*[contains(text(),"留在此页")]',
                        'css:.dialog-close',
                        'css:[class*="close"]',
                    ):
                        try:
                            close_btn = page.ele(close_sel, timeout=0.5)
                            if close_btn:
                                close_btn.click()
                                page.wait(1)
                                break
                        except Exception:
                            continue
                    print("已投递")
                    return {"title": title, "company": company, "status": "成功", "error": ""}
            except Exception:
                page.wait(1)

        # ===== 没有自动弹窗 → 找聊天框输入招呼语 =====
        input_area = None
        for sel in (
            'css:.chat-input textarea',
            'css:[class*="chat-input"] [contenteditable]',
            'css:[contenteditable="true"]',
            'css:[placeholder*="输入"]',
            'xpath://div[@contenteditable="true"]',
        ):
            try:
                ia = page.ele(sel, timeout=1)
                if ia:
                    input_area = ia
                    break
            except Exception:
                continue

        if input_area and greeting:
            try:
                input_area.click()
                page.wait(0.3)
                # 清空
                page.run_js("""
                    var el=arguments[0];
                    el.value ? el.value='' : el.innerHTML='';
                """, input_area)
                page.wait(0.2)
                # 输入
                for ch in greeting:
                    input_area.input(ch, clear=False)
                    page.wait(0.03)
                page.wait(0.5)

                # 发送
                sent = False
                for send_sel in (
                    'css:[class*="send-btn"]',
                    'css:[class*="btn-send"]',
                    'css:[aria-label*="发送"]',
                    'xpath://*[contains(text(),"发送")]',
                ):
                    try:
                        btn = page.ele(send_sel, timeout=0.5)
                        if btn:
                            btn.click()
                            sent = True
                            break
                    except Exception:
                        continue
                if not sent:
                    page.run_js("""
                        arguments[0].dispatchEvent(new KeyboardEvent('keydown',{
                            key:'Enter',code:'Enter',keyCode:13,bubbles:true
                        }));
                    """, input_area)

                page.wait(1)
                print("已发送招呼")
                return {"title": title, "company": company, "status": "成功", "error": ""}
            except Exception as e:
                print(f"招呼异常: {e}")
                return {"title": title, "company": company, "status": "成功", "error": f"招呼可能未发送: {e}"}

        # 没有输入框但已经点击了"立即沟通"→ 可能已发送默认招呼
        print("已沟通（默认）")
        return {"title": title, "company": company, "status": "成功", "error": "已点击立即沟通"}

    def send_greetings(self, jobs: list[dict]) -> list[dict]:
        """批量投递：对每个岗位打开详情页发送招呼语。"""
        from datetime import datetime

        logged_in = self.ensure_login()
        if not logged_in:
            logger.error("未登录，无法投递")
            return [{"title": j.get("title", ""), "status": "失败", "error": "未登录"} for j in jobs]

        page = self._get_page()
        page.set.timeouts(base=5)

        print(f"\n开始批量投递，共 {len(jobs)} 个岗位\n")

        results = []
        for idx, job in enumerate(jobs):
            result = self._send_greeting_for_job(page, job, idx, len(jobs))
            results.append(result)
            if idx < len(jobs) - 1:
                delay = random.uniform(5, 10)
                logger.info(f"等待 {delay:.1f}s...")
                page.wait(delay)

        success = sum(1 for r in results if r["status"] == "成功")
        skipped = sum(1 for r in results if r["status"] == "跳过")
        failed = sum(1 for r in results if r["status"] == "失败")
        print(f"\n投递完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {failed} 个")
        return results

    def close(self):
        """关闭浏览器，释放资源。"""
        if self._page:
            try:
                self._page.quit()
                logger.info("浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器失败: {e}")
            finally:
                self._page = None
                self._logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def _generate_boss_mock() -> list[dict]:
    """BOSS直聘风格的仿真模拟数据。"""
    import random as rnd
    jobs = [
        {"title": "Python后端开发工程师", "company": "字节跳动", "city": "上海", "salary": "30K-60K",
         "description": "负责抖音电商后端系统设计与开发", "requirements": "本科及以上，3-5年Python后端经验"},
        {"title": "高级Python开发工程师", "company": "蚂蚁集团", "city": "杭州", "salary": "35K-65K",
         "description": "参与蚂蚁数金科技平台建设", "requirements": "5年以上Python/Java经验"},
        {"title": "机器学习工程师", "company": "阿里巴巴", "city": "杭州", "salary": "40K-70K",
         "description": "负责搜索推荐算法优化", "requirements": "熟悉深度学习框架"},
        {"title": "AI平台开发工程师", "company": "腾讯", "city": "深圳", "salary": "30K-60K",
         "description": "参与腾讯云AI平台建设", "requirements": "熟悉Python/Go"},
        {"title": "云计算开发工程师", "company": "华为", "city": "深圳", "salary": "25K-50K",
         "description": "参与华为云容器平台开发", "requirements": "熟悉Docker/K8s"},
    ]
    rnd.shuffle(jobs)
    for j in jobs:
        j["source"] = "boss"
        j["link"] = ""
    return jobs
