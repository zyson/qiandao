# -*- coding: utf-8 -*-
"""
实现搜书吧论坛登入和发布空间动态
"""
import os
import re
import sys
from copy import copy
import time
import logging

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from requests.adapters import HTTPAdapter
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

import urllib3

disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

ch = logging.StreamHandler(stream=sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

# 连接与读取分别设限，避免网络波动时把 Action 挂死到超时
DEFAULT_TIMEOUT = (10, 30)

# 站点用 403 页承载 meta refresh 跳转，所以 403 不当作失败
TOLERATED_STATUS = (403,)

RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4
RETRY_BACKOFF = 5

LOGINHASH_RE = re.compile(r'<div id="main_messaqge_(.+?)">')
FORMHASH_RE = re.compile(r'<input type="hidden" name="formhash" value="(.+?)" />')

# 发帖成功后间隔。站点对两次发布有冷却（实测 waittime 可达 55s），90s 留足余量
POST_INTERVAL = 90
# 发帖失败后间隔。不重试，只等待后继续下一帖，避免重复发帖
POST_FAIL_INTERVAL = 65
# 失败的判据关键词：成功时响应 CDATA 为空，而"操作成功"实际从不出现
POST_FAILURE_HINTS = ('抱歉', '太快', '失败', '错误')
# 失败时从响应里取服务端要求的等待秒数
WAITTIME_RE = re.compile(r"'waittime':'(\d+)'")


class UnexpectedPageError(RuntimeError):
    """响应不是预期页面（网络波动返回错误页，或站点结构变更）。"""

    def __init__(self, what, url=None, status=None, body=None):
        self.what = what
        self.url = url
        self.status = status
        self.body = body
        super().__init__(self._message())

    def _message(self):
        parts = [f'未能解析{self.what}']
        if self.url:
            parts.append(f'url={self.url}')
        if self.status is not None:
            parts.append(f'status={self.status}')
        parts.append(f'响应片段={snippet(self.body)}')
        return '，'.join(parts)


def snippet(text, limit=200):
    flat = ' '.join(text.split()) if text else ''
    if not flat:
        return '<空响应>'
    return flat[:limit] + ('…' if len(flat) > limit else '')


def build_session():
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=1.5,
                  status_forcelist=RETRY_STATUS, allowed_methods=frozenset(['GET']),
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def fetch(url, session=None, check_status=False):
    """GET 并返回响应。

    check_status 默认 False：站点会把正文完好的页面配 404/403 这类状态码返回
    （例如发现页就是 404 + 完整 HTML），所以抓取解析类请求不能按状态码判失败，
    只能交给解析层判断——这正是原实现的行为。状态码只用于展示排查。
    """
    getter = session.get if session else requests.get
    response = getter(url, timeout=DEFAULT_TIMEOUT, verify=False)
    if check_status and response.status_code not in TOLERATED_STATUS:
        response.raise_for_status()
    return response


def with_retries(what, action, attempts=MAX_ATTEMPTS, backoff=RETRY_BACKOFF):
    """重试可恢复的失败（网络波动、错误页），耗尽后抛出最后一次异常。"""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (UnexpectedPageError, requests.RequestException) as e:
            last = e
            if attempt == attempts:
                break
            delay = backoff * attempt
            logger.warning(f'{what}第 {attempt}/{attempts} 次失败：{e}，{delay}s 后重试')
            time.sleep(delay)
    raise last


def first_group(pattern, what, text, url=None, status=None):
    """正则取值，未命中时抛 UnexpectedPageError 而不是返回 None。"""
    match = pattern.search(text or '')
    if match is None:
        raise UnexpectedPageError(what, url, status, text)
    return match.group(1)


def post_message(response, limit=120):
    """发帖响应的服务端原文：剥掉 script 与 XML 标签，只留提示文字。"""
    text = response.text or ''
    if not text.strip():
        return '<空响应>'
    # 先取出 CDATA 内文——否则 <![CDATA[ 会被后面的去标签规则当成标签吃掉
    text = re.sub(r'<!\[CDATA\[([\s\S]*?)\]\]>', r'\1', text)
    # 再剥 script（里面是重复的 JS 提示）与其余标签
    text = re.sub(r'<script[\s\S]*?</script>', ' ', text)
    text = re.sub(r'<[^>]*>', ' ', text)
    flat = ' '.join(text.split())
    if not flat:
        return '<空响应(无文字)>'
    return flat[:limit] + ('…' if len(flat) > limit else '')


def post_wait_seconds(response):
    """服务端要求的等待秒数；没有则 None。"""
    match = WAITTIME_RE.search(response.text or '')
    return int(match.group(1)) if match else None


def post_failed(response):
    """发帖是否失败。

    成功时 CDATA 为空、且响应里从不含"操作成功"，所以正向匹配不可用；
    改为检测服务端给出的失败提示词。
    """
    text = response.text or ''
    return any(hint in text for hint in POST_FAILURE_HINTS)


def get_refresh_url(url: str):
    """返回 meta refresh 指向的地址。

    失败时抛 UnexpectedPageError，不再返回 None —— 否则调用方会拿 None
    拼出 `https://None/`，报错信息完全掩盖真实的网络问题。
    """
    response = fetch(url, check_status=True)
    soup = BeautifulSoup(response.text, 'html.parser')
    meta_tags = soup.find_all('meta', {'http-equiv': 'refresh'})

    if not meta_tags:
        raise UnexpectedPageError('meta refresh 标签', url, response.status_code, response.text)

    content = meta_tags[0].get('content', '')
    if 'url=' not in content:
        raise UnexpectedPageError('meta refresh 的 url=', url, response.status_code, response.text)

    redirect_url = content.split('url=')[1].strip()
    logger.info(f"Redirecting to: {redirect_url}")
    return redirect_url


def get_url(url: str):
    """返回发现页地址。失败时抛 UnexpectedPageError，不返回 None。

    该页常以 404 状态码返回但正文完整，所以不做状态码判定。
    """
    response = fetch(url)
    soup = BeautifulSoup(response.content, 'html.parser')

    for link in soup.find_all('a', href=True):
        if link.text == "搜书吧":
            return link['href']
    raise UnexpectedPageError('“搜书吧”链接', url, response.status_code, response.text)


class SouShuBaClient:

    def __init__(self, hostname: str, username: str, password: str, questionid: str = '0', answer: str = None,
                 proxies: dict | None = None):
        self.session: requests.Session = build_session()
        self.hostname = hostname
        self.username = username
        self.password = password
        self.questionid = questionid
        self.answer = answer
        self._common_headers = {
            "Host": f"{ hostname }",
            "Connection": "keep-alive",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,cn;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        self.proxies = proxies

    def login_form_hash(self):
        login_url = f'https://{self.hostname}/member.php?mod=logging&action=login'
        response = fetch(login_url, self.session)
        rst = response.text
        loginhash = first_group(LOGINHASH_RE, 'loginhash', rst, login_url, response.status_code)
        formhash = first_group(FORMHASH_RE, 'formhash', rst, login_url, response.status_code)
        return loginhash, formhash

    def login(self):
        """Login with username and password"""
        loginhash, formhash = with_retries('获取登录 formhash', self.login_form_hash)
        login_url = f'https://{self.hostname}/member.php?mod=logging&action=login&loginsubmit=yes' \
                    f'&handlekey=register&loginhash={loginhash}&inajax=1'


        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/'
        payload = {
            'formhash': formhash,
            'referer': f'https://{self.hostname}/',
            'username': self.username,
            'password': self.password,
            'questionid': self.questionid,
            'answer': self.answer
        }

        resp = self.session.post(login_url, proxies=self.proxies, data=payload, headers=headers, verify=False,
                                 timeout=DEFAULT_TIMEOUT)
        if resp.status_code == 200:
            logger.info(f'Welcome {self.username}!')
        else:
            raise ValueError('Verify Failed! Check your username and password!')

    def credit(self):
        credit_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu"
        credit_rst = fetch(credit_url, self.session).text

        # 解析 XML，提取 CDATA
        try:
            root = ET.fromstring(str(credit_rst))
        except ET.ParseError as e:
            raise UnexpectedPageError('积分 XML', credit_url, body=credit_rst) from e

        cdata_content = root.text
        if not cdata_content:
            raise UnexpectedPageError('积分 CDATA 内容', credit_url, body=credit_rst)

        # 使用 BeautifulSoup 解析 CDATA 内容
        cdata_soup = BeautifulSoup(cdata_content, features="lxml")
        hcredit_2 = cdata_soup.find("span", id="hcredit_2")
        if hcredit_2 is None:
            raise UnexpectedPageError('积分 #hcredit_2', credit_url, body=cdata_content)

        return hcredit_2.string

    def space_form_hash(self):
        home_url = f'https://{self.hostname}/home.php'
        response = fetch(home_url, self.session)
        return first_group(FORMHASH_RE, 'formhash', response.text, home_url, response.status_code)

    def space(self):
        formhash = with_retries('获取空间 formhash', self.space_form_hash)
        space_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=doing&handlekey=doing&inajax=1"

        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/home.php'

        for x in range(5):
            payload = {
                "message": "嘿嘿赚银币 {0} 次".format(x + 1).encode("GBK"),
                "addsubmit": "true",
                "spacenote": "true",
                "referer": "home.php",
                "formhash": formhash
            }
            resp = self.session.post(space_url, proxies=self.proxies, data=payload, headers=headers, verify=False,
                                     timeout=DEFAULT_TIMEOUT)
            if post_failed(resp):
                wait = post_wait_seconds(resp)
                logger.warning(f'{self.username} post {x + 1}nd failed：{post_message(resp)}'
                               f'（waittime={wait}，等待 {POST_FAIL_INTERVAL}s）')
                time.sleep(POST_FAIL_INTERVAL)
            else:
                logger.info(f'{self.username} post {x + 1}nd successfully!')
                time.sleep(POST_INTERVAL)


if __name__ == '__main__':
    try:
        redirect_url = with_retries(
            '获取刷新地址',
            lambda: get_refresh_url('http://' + os.environ.get('SOUSHUBA_HOSTNAME', 'www.soushu2035.com')))
        time.sleep(2)
        redirect_url2 = with_retries('获取跳转地址', lambda: get_refresh_url(redirect_url))
        url = with_retries('获取发现页地址', lambda: get_url(redirect_url2))
        logger.info(f'{url}')
        hostname = urlparse(url).hostname
        if not hostname:
            raise UnexpectedPageError('主机名', url)
        client = SouShuBaClient(hostname,
                                os.environ.get('SOUSHUBA_USERNAME', "USERNAME"),
                                os.environ.get('SOUSHUBA_PASSWORD', "PASSWORD"))
        client.login()
        client.space()
        try:
            credit = client.credit()
            logger.info(f'{client.username} have {credit} coins!')
        except (UnexpectedPageError, requests.RequestException) as e:
            # 积分只是展示，失败不应让本次签到判为失败
            logger.warning(f'获取积分失败（不影响签到）：{e}')
    except Exception as e:
        logger.exception("执行发生异常")
        sys.exit(1)
