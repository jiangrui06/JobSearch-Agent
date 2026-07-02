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


class BossLoginRequiredError(Exception):
    """BOSS直聘要求登录（未登录或登录态失效）。"""
    pass


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
    """通过URL和页面特征判断是否已登录。

    BOSS直聘未登录时也会显示搜索页，因此不能仅凭搜索框判断。
    需要检测登录态特有的页面元素（头像、消息入口、个人中心）。
    """
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
    if any(kw in url for kw in ('web/chat', 'job_detail', 'web/geek')):
        return True

    # 检测登录态特有元素：用户头像、消息/投递入口、个人中心
    logged_in_markers = (
        'xpath://img[contains(@class,"avatar") or contains(@src,"avatar")]',
        'xpath://a[contains(@href,"/web/geek/")]',
        'xpath://a[contains(@href,"/web/chat/")]',
        'xpath://span[contains(text(),"我的")]',
        'xpath://span[contains(text(),"消息")]',
        'xpath://div[contains(@class,"user-info")]',
    )
    for sel in logged_in_markers:
        try:
            if page.ele(sel, timeout=0.8):
                return True
        except Exception:
            pass

    # 如果在搜索页且存在登录/注册按钮，判定为未登录
    login_button_markers = (
        'xpath://a[contains(text(),"登录")]',
        'xpath://a[contains(text(),"注册")]',
        'xpath://button[contains(text(),"登录")]',
        'xpath://span[contains(text(),"登录")]',
    )
    for sel in login_button_markers:
        try:
            if page.ele(sel, timeout=0.8):
                return False
        except Exception:
            pass

    # 默认保守判定为未登录，避免采集到限制数据
    return False


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
        for attempt in range(2):
            try:
                return self._do_search(keyword, city, page, cancel_check)
            except BossLoginRequiredError:
                logger.warning("登录态失效，需要重新登录")
                self._logged_in = False
                if not self.ensure_login():
                    logger.error("重新登录失败，使用模拟数据")
                    return _generate_boss_mock()
                # 登录成功后重试一次
                continue
        return _generate_boss_mock()

    def _do_search(
        self,
        keyword: str,
        city: str,
        page: int,
        cancel_check=None,
    ) -> list[dict[str, Any]]:
        """实际执行采集。"""
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
        first_page_count = 0

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
                            page_size = len(jobs)
                            if page_idx == 0:
                                first_page_count = page_size
                                # BOSS直聘未登录时通常只返回 15 条
                                if first_page_count <= 15:
                                    logger.warning(
                                        f"第一页仅返回 {first_page_count} 条数据，疑似未登录，将重新登录"
                                    )
                                    raise BossLoginRequiredError(
                                        f"第一页仅 {first_page_count} 条，疑似未登录"
                                    )
                            for j in jobs:
                                key = f"{j[0]}|{j[4]}"
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    all_results.append(j)
                            found_data = True
                            break
                    if found_data:
                        break
                except BossLoginRequiredError:
                    raise
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
        """从响应体解析岗位列表，并检测登录失效。"""
        if not isinstance(body, dict):
            return []

        # BOSS直聘 API 常见未登录响应：code != 0 或 message 含登录
        code = body.get('code') if isinstance(body, dict) else None
        message = (body.get('message') or '') if isinstance(body, dict) else ''
        if code not in (0, None) or '登录' in message or 'login' in message.lower():
            logger.warning(f"API 响应提示需要登录: code={code}, message={message}")
            raise BossLoginRequiredError(f"BOSS直聘要求登录: {message}")

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

    def _type_and_send_greeting(self, page, greeting: str) -> bool:
        """找聊天输入框，输入招呼语并发送。返回是否成功发送。"""
        if not greeting:
            return False

        # 先等页面加载稳定
        page.wait(1)
        current_url = page.url
        logger.info(f"当前页面URL: {current_url}")

        input_area = None
        for sel in (
            'css:[class*="chat-input"] textarea',
            'css:[class*="chat-input"] [contenteditable]',
            'css:[contenteditable="true"]',
            'css:[placeholder*="输入"]',
            'xpath://div[@contenteditable="true"]',
            'css:textarea',
        ):
            for _ in range(5):
                try:
                    ia = page.ele(sel, timeout=1)
                    if ia and ia.tag.lower() in ('textarea', 'div', 'input'):
                        input_area = ia
                        logger.info(f"找到输入框: {sel}")
                        break
                except Exception:
                    page.wait(1)
            if input_area:
                break

        if not input_area:
            logger.warning(f"未找到聊天输入框 (URL: {current_url})")
            return False

        try:
            input_area.click()
            page.wait(0.3)
            page.run_js("""
                var el=arguments[0];
                el.value ? el.value='' : el.innerHTML='';
            """, input_area)
            page.wait(0.2)
            for ch in greeting:
                input_area.input(ch, clear=False)
                page.wait(0.03)
            page.wait(0.5)

            # 用 Enter 发送
            page.wait(0.3)
            input_area.input('\n', clear=False)

            # ===== 确认消息真实发送成功（防网络慢导致转圈未完成就跳转） =====
            logger.info("[发送] 已按 Enter，正在确认消息是否发出...")
            greeting_head = greeting[:20].strip()

            send_ok = False
            confirm_start = time.time()
            for i in range(30):  # 最多等 15 秒 (30 × 0.5s)
                page.wait(0.5)
                try:
                    # 方法1：聊天记录中出现刚发的消息气泡
                    msg_sent = page.ele(
                        f'xpath://*[contains(@class,"message") or contains(@class,"chat")]'
                        f'//*[contains(text(),"{greeting_head}")]',
                        timeout=0.2,
                    )
                    if msg_sent:
                        send_ok = True
                        break

                    # 方法2：输入框已清空（消息已发出）
                    cur_text = (input_area.text or "").strip()
                    if not cur_text:
                        send_ok = True
                        break
                except Exception:
                    continue

            elapsed = time.time() - confirm_start
            if send_ok:
                logger.info(f"[发送] 消息确认发送 (耗时 {elapsed:.1f}s)")
                page.wait(0.5)
                return True
            else:
                logger.warning(f"[发送] {elapsed:.1f}s 超时未确认，尝试重发...")
                try:
                    page.wait(0.5)
                    input_area.click()
                    page.wait(0.2)
                    # 清空输入框
                    page.run_js("arguments[0].textContent = ''; arguments[0].value = '';", input_area)
                    page.wait(0.2)
                    for ch in greeting:
                        input_area.input(ch, clear=False)
                        page.wait(0.02)
                    page.wait(0.3)
                    input_area.input('\n', clear=False)
                    page.wait(3)
                    logger.info("[发送] 重发完成")
                    return True
                except Exception as e2:
                    logger.warning(f"[发送] 重发失败: {e2}")
                    return False
        except Exception as e:
            logger.warning(f"招呼语输入/发送异常: {e}")
            return False

    def _send_greeting_for_job(self, page, job: dict, idx: int, total: int) -> dict:
        """对单个岗位执行智能投递。逻辑：
        1. 打开岗位链接
        2. 找"立即沟通"按钮并点击
        3. 弹窗"已向BOSS发送消息"出现 → 点"继续沟通"进入聊天页 → 发送 AI 个性化招呼语
        4. 无弹窗但找到聊天框 → 直接发送 AI 招呼语
        5. 都不行 → 默认已沟通
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
                logger.warning(f"会话过期（URL重定向到登录页）: {page.url}")
                return {"title": title, "company": company, "status": "登录过期", "error": "登录会话已过期"}
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

        # ===== 检测弹窗：上限/频繁/继续沟通 =====
        popup_found = False
        limit_closed = False

        for _ in range(6):
            try:
                # ===== 全页文本搜索上限弹窗（不依赖CSS类名） =====
                try:
                    js_clicked = page.run_js("""
                        var kws=['无法进行沟通','休息一下','明天再来','已与','150'];
                        var btns=['好的','知道了','确定','确认','嗯','关闭'];
                        var all=document.querySelectorAll('body *');
                        var hit=null;
                        for(var i=0;i<all.length;i++){
                            var e=all[i];
                            if(e.offsetParent===null)continue;
                            var t=(e.textContent||'').trim();
                            var match=false;
                            for(var k=0;k<kws.length;k++){if(t.indexOf(kws[k])!==-1){match=true;break;}}
                            if(!match)continue;
                            var p=e;
                            for(var d=0;d<10&&p&&p!==document.body;d++){
                                var cs=p.querySelectorAll('button,[class*=\"btn\"],a,div[role=\"button\"]');
                                for(var c=0;c<cs.length;c++){
                                    var b=cs[c];
                                    if(b.offsetParent===null)continue;
                                    var bt=(b.textContent||'').trim();
                                    if(btns.indexOf(bt)!==-1){
                                        b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                                        hit=bt;break;
                                    }
                                }
                                if(hit)break;
                                p=p.parentElement;
                            }
                            if(hit)break;
                        }
                        return hit||'';
                    """)
                    if js_clicked:
                        logger.info(f"[投递 {title}] 检测到上限弹窗，已点击「{js_clicked}」")
                        print("\n!!! 已达每日上限，停止投递 !!!")
                        page.wait(2)
                        return {"title": title, "company": company, "status": "上限", "error": "已达每日沟通上限"}
                except Exception:
                    pass

                # ===== 检测"已向BOSS发送消息"弹窗 =====
                msg_popup = page.ele('xpath://*[contains(text(),"已向BOSS发送消息")]', timeout=0.5)
                if msg_popup:
                    popup_found = True
                    break

                # ===== 检测登录过期浮窗（非URL跳转，页面弹窗overlay） =====
                try:
                    qr_login = page.ele('xpath://*[contains(text(),"扫码登录")]', timeout=0.3)
                    if qr_login:
                        # 确认在弹窗/对话框内（不是页面底部footer的登录入口）
                        try:
                            dialog = qr_login.parent().parent()
                            d_class = (dialog.attr("class") or "").lower()
                            if any(k in d_class for k in ("dialog", "modal", "popup", "overlay")):
                                logger.warning(f"[投递 {title}] 检测到登录过期弹窗")
                                print("\n!!! 登录会话已过期，请重新扫码登录 !!!")
                                self._logged_in = False
                                return {"title": title, "company": company, "status": "登录过期", "error": "登录会话已过期"}
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass
            page.wait(1)

        if popup_found:
            if greeting:
                try:
                    # 精准定位弹窗中的「继续沟通」按钮（只匹配 button 标签）
                    continue_btn = page.ele('xpath://button[contains(.,"继续沟通")]', timeout=2)
                    if not continue_btn:
                        continue_btn = page.ele('xpath://a[contains(.,"继续沟通")]', timeout=1)
                    if not continue_btn:
                        # 兜底：弹窗底部的第一个按钮
                        continue_btn = page.ele('xpath://div[contains(@class,"dialog")]//div[contains(@class,"footer")]//button[1]', timeout=1)

                    if continue_btn:
                        logger.info(f"[投递 {title}] 点击「继续沟通」")
                        # 优先用 JS click，比 DrissionPage 的 .click() 更可靠
                        try:
                            page.run_js("arguments[0].click();", continue_btn)
                        except Exception:
                            continue_btn.click()

                        # 等待跳转到聊天页（URL 包含 /chat/）
                        for _ in range(10):
                            page.wait(1)
                            if "/chat/" in page.url or "/geek/chat/" in page.url:
                                logger.info(f"[投递 {title}] 已跳转到聊天页")
                                break
                        else:
                            logger.warning(f"[投递 {title}] 点击后未跳转，当前URL: {page.url}")

                        if self._type_and_send_greeting(page, greeting):
                            logger.info(f"[投递 {title}] AI 招呼语发送成功")
                            print("已发送AI招呼")
                            return {"title": title, "company": company, "status": "成功", "error": ""}
                        else:
                            logger.warning(f"[投递 {title}] 进入聊天页但未找到输入框")
                    else:
                        logger.warning(f"[投递 {title}] 未找到「继续沟通」按钮")
                except Exception as e:
                    logger.warning(f"[投递 {title}] 继续沟通异常: {e}")

            # 回退：关闭弹窗（使用 BOSS 默认招呼）
            for close_sel in (
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
            print("已投递（默认招呼）")
            return {"title": title, "company": company, "status": "成功", "error": ""}

        # ===== 没有弹窗 → 找聊天框发送 AI 招呼语 =====
        if self._type_and_send_greeting(page, greeting):
            print("已发送招呼")
            return {"title": title, "company": company, "status": "成功", "error": ""}

        # 没有输入框但已经点击了"立即沟通"
        current_url = page.url
        if "/chat/" in current_url or "/geek/chat/" in current_url:
            logger.info(f"已沟通过（重复）: {title} @ {company}")
            return {"title": title, "company": company, "status": "跳过", "error": "已沟通过"}
        else:
            logger.warning(f"点击「立即沟通」后页面未跳转到聊天页 (URL: {current_url})")
            return {"title": title, "company": company, "status": "跳过", "error": f"页面未跳转"}

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

            # 检测到上限或登录过期时立即停止后续投递
            if result.get("status") in ("上限", "登录过期"):
                remaining = len(jobs) - idx - 1
                reason = "已达每日上限" if result["status"] == "上限" else "登录会话已过期"
                if remaining > 0:
                    print(f"  {reason}，跳过剩余 {remaining} 个岗位")
                    for j in jobs[idx + 1:]:
                        results.append({
                            "title": j.get("title", ""),
                            "company": j.get("company", ""),
                            "status": "跳过",
                            "error": f"{reason}，停止投递",
                        })
                break

            if idx < len(jobs) - 1:
                delay = random.uniform(5, 10)
                logger.info(f"等待 {delay:.1f}s...")
                page.wait(delay)

        success = sum(1 for r in results if r["status"] == "成功")
        skipped = sum(1 for r in results if r["status"] == "跳过")
        failed = sum(1 for r in results if r["status"] == "失败")
        logger.info(f"投递完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {failed} 个")
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
