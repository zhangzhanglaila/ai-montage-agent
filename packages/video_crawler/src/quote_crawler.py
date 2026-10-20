"""
台词搜片段爬虫 - 通过电影台词搜索视频片段

支持站点：
- PlayPhrase (playphrase.me) - 英文台词搜片段，API 直接获取视频 URL ✅
- QuoDB (quodb.com) - 英文台词搜片段，提供电影名+时间码 ✅
- YARN (getyarn.io) - Cloudflare 封锁，暂不可用 ❌
- 找台词网 (zhaotaici.com) - 域名 SSL 错误，暂不可用 ❌

需要安装 playwright: pip install playwright（使用系统已安装的 Chrome 浏览器）
"""

import json
import re
import time
from pathlib import Path
from typing import List
from dataclasses import dataclass


@dataclass
class QuoteClip:
    """台词片段信息"""
    quote: str           # 台词文本
    movie: str           # 电影名
    year: str = ""       # 年份
    start_time: float = 0.0  # 开始时间（秒）
    end_time: float = 0.0    # 结束时间（秒）
    video_url: str = ""  # 视频 URL
    source: str = ""     # 来源站点


def _check_playwright():
    """检查 Playwright 是否可用"""
    try:
        from playwright.sync_api import sync_playwright
        return True
    except ImportError:
        return False


def _launch_browser(playwright, headless=True):
    """启动 Chrome 浏览器（隐身模式绕过反爬检测）"""
    browser = playwright.chromium.launch(
        headless=headless,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )
    return browser


def _new_stealth_page(browser):
    """创建带反检测的页面"""
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    )
    return page


class QuoteCrawler:
    """台词搜片段爬虫基类"""

    def __init__(self, output_dir: str = "var/cache/quotes", headless: bool = True):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless

    def search(self, query: str, max_results: int = 10) -> List[QuoteClip]:
        raise NotImplementedError

    def download_clips(self, clips: List[QuoteClip], max_clips: int = 10) -> List[str]:
        raise NotImplementedError

    def search_and_download(self, keyword: str, max_clips: int = 10) -> List[str]:
        clips = self.search(keyword, max_results=max_clips * 2)
        if not clips:
            return []
        return self.download_clips(clips[:max_clips], max_clips)


# ============================================================
# PlayPhrase - 通过 API 搜索，直接获取视频 URL
# ============================================================
class PlayPhraseCrawler(QuoteCrawler):
    """PlayPhrase (playphrase.me) 台词搜片段

    使用 PlayPhrase 内部 API 搜索，返回的每条结果包含：
    - text: 台词文本
    - video-url: 直接 MP4 下载链接（Wasabi S3）
    - video-info.info: 电影/剧集名 + 时间码
    """

    BASE_URL = "https://www.playphrase.me"

    def search(self, query: str, max_results: int = 10) -> List[QuoteClip]:
        if not _check_playwright():
            print("  需要安装 playwright: pip install playwright")
            return []

        from playwright.sync_api import sync_playwright

        clips = []
        api_data = None

        with sync_playwright() as p:
            browser = _launch_browser(p, self.headless)
            page = _new_stealth_page(browser)

            # 拦截 API 响应
            def on_response(response):
                nonlocal api_data
                if "/api/v1/phrases/search?" in response.url:
                    try:
                        ct = response.headers.get("content-type", "")
                        if response.status == 200 and "json" in ct:
                            data = response.json()
                            if isinstance(data, dict) and "phrases" in data:
                                api_data = data
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                page.goto(self.BASE_URL, timeout=30000)
                page.wait_for_timeout(5000)

                search_input = page.query_selector("input[type='text']:visible")
                if not search_input:
                    print("  PlayPhrase: 找不到搜索框")
                    return []

                search_input.fill(query)
                search_input.press("Enter")
                page.wait_for_timeout(8000)

                if not api_data:
                    print("  PlayPhrase: 未捕获到 API 响应")
                    return []

                phrases = api_data.get("phrases", [])
                for ph in phrases[:max_results]:
                    quote = ph.get("text", "")
                    video_url = ph.get("video-url", "")
                    video_info = ph.get("video-info", {})
                    info_str = video_info.get("info", "")

                    # 解析电影名和年份: "Movie Name (2020) [00:19:48]"
                    movie = info_str
                    year = ""
                    m = re.search(r"\((\d{4})\)", info_str)
                    if m:
                        year = m.group(1)
                        movie = info_str[: m.start()].strip()

                    # 解析时间码: [00:19:48]
                    start_sec = 0.0
                    tc = re.search(r"\[(\d+):(\d+):(\d+)\]", info_str)
                    if tc:
                        h, mi, s = int(tc.group(1)), int(tc.group(2)), int(tc.group(3))
                        start_sec = h * 3600 + mi * 60 + s

                    if quote:
                        clips.append(QuoteClip(
                            quote=quote,
                            movie=movie,
                            year=year,
                            start_time=start_sec,
                            video_url=video_url,
                            source="playphrase",
                        ))

            except Exception as e:
                print(f"  PlayPhrase 搜索失败: {e}")
            finally:
                browser.close()

        return clips

    def download_clips(self, clips: List[QuoteClip], max_clips: int = 10) -> List[str]:
        """下载 PlayPhrase 视频片段（直接 MP4 URL）"""
        import requests

        downloaded = []
        for i, clip in enumerate(clips[:max_clips]):
            if not clip.video_url:
                continue

            safe_name = re.sub(r'[^\w\s-]', '', clip.quote[:30]).strip().replace(' ', '_')
            cache_path = self.output_dir / f"playphrase_{i:04d}_{safe_name}.mp4"
            if cache_path.exists():
                downloaded.append(str(cache_path))
                print(f"  [{i+1}] 已缓存: {clip.quote[:30]}...")
                continue

            try:
                resp = requests.get(
                    clip.video_url, timeout=30, stream=True,
                    proxies={"http": None, "https": None},
                )
                resp.raise_for_status()
                with open(cache_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                downloaded.append(str(cache_path))
                print(f"  [{i+1}] 下载成功: {clip.quote[:30]}... | {clip.movie}")
            except Exception as e:
                print(f"  [{i+1}] 下载失败: {e}")

            time.sleep(0.5)

        return downloaded


# ============================================================
# QuoDB - 网页抓取，提供台词+电影+时间码
# ============================================================
class QuoDBCrawler(QuoteCrawler):
    """QuoDB (quodb.com) 英文台词搜片段

    QuoDB 不直接提供视频下载，但提供精确的电影名+时间码，
    可配合其他视频源使用。
    """

    BASE_URL = "https://www.quodb.com"

    def search(self, query: str, max_results: int = 10) -> List[QuoteClip]:
        if not _check_playwright():
            print("  需要安装 playwright: pip install playwright")
            return []

        from playwright.sync_api import sync_playwright

        clips = []
        with sync_playwright() as p:
            browser = _launch_browser(p, self.headless)
            page = browser.new_page()

            try:
                url = f"{self.BASE_URL}/search/{query}"
                page.goto(url, timeout=30000)
                page.wait_for_timeout(3000)

                # QuoDB 结构: table.result_table > tbody > tr
                rows = page.query_selector_all("table.result_table tr")
                for row in rows[:max_results]:
                    try:
                        text = row.inner_text().strip()
                        if not text:
                            continue

                        # 格式: "01:02:39 I love you.\n\nThe Dark Knight (2008)\nFavorite ..."
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        if len(lines) < 2:
                            continue

                        # 第一行: 时间码 + 台词
                        first_line = lines[0]
                        tc_match = re.match(r"(\d{2}):(\d{2}):(\d{2})\s+(.*)", first_line)
                        if not tc_match:
                            continue

                        h, mi, s = int(tc_match.group(1)), int(tc_match.group(2)), int(tc_match.group(3))
                        start_sec = h * 3600 + mi * 60 + s
                        quote = tc_match.group(4).strip()

                        # 第二行: 电影名 (年份)
                        movie_line = lines[1]
                        movie = movie_line
                        year = ""
                        m = re.search(r"\((\d{4})\)", movie_line)
                        if m:
                            year = m.group(1)
                            movie = movie_line[: m.start()].strip()

                        # 从 a.title 链接获取更准确的电影名
                        title_el = row.query_selector("a.title")
                        if title_el:
                            movie = title_el.inner_text().strip() or movie

                        if quote:
                            clips.append(QuoteClip(
                                quote=quote,
                                movie=movie,
                                year=year,
                                start_time=start_sec,
                                source="quodb",
                            ))
                    except Exception:
                        continue
            except Exception as e:
                print(f"  QuoDB 搜索失败: {e}")
            finally:
                browser.close()

        return clips

    def download_clips(self, clips: List[QuoteClip], max_clips: int = 10) -> List[str]:
        print("  QuoDB 提供台词+时间码，不直接提供视频，需配合其他视频源使用")
        return []


# ============================================================
# YARN - Cloudflare 封锁，暂不可用
# ============================================================
class YarnCrawler(QuoteCrawler):
    """YARN (getyarn.io) 台词搜片段

    注意: getyarn.io 全站 Cloudflare 反爬封锁，当前不可用。
    """

    BASE_URL = "https://getyarn.io"

    def search(self, query: str, max_results: int = 10) -> List[QuoteClip]:
        print("  YARN: 被 Cloudflare 封锁，暂不可用")
        return []

    def download_clips(self, clips: List[QuoteClip], max_clips: int = 10) -> List[str]:
        return []


# ============================================================
# 找台词网 - SSL 错误，暂不可用
# ============================================================
class ZhaotaiciCrawler(QuoteCrawler):
    """找台词网 (zhaotaici.com) 中文台词搜片段

    注意: 域名 SSL 证书错误，站点可能已下线或迁移，当前不可用。
    """

    BASE_URL = "https://www.zhaotaici.com"

    def search(self, query: str, max_results: int = 10) -> List[QuoteClip]:
        print("  找台词网: SSL 错误，站点可能已下线，暂不可用")
        return []

    def download_clips(self, clips: List[QuoteClip], max_clips: int = 10) -> List[str]:
        return []


def create_quote_crawler(source: str, **kwargs) -> QuoteCrawler:
    """工厂函数 - 创建对应源的台词爬虫"""
    crawlers = {
        "yarn": YarnCrawler,
        "playphrase": PlayPhraseCrawler,
        "quodb": QuoDBCrawler,
        "zhaotaici": ZhaotaiciCrawler,
    }
    cls = crawlers.get(source)
    if not cls:
        raise ValueError(f"不支持的源: {source}，支持: {', '.join(crawlers.keys())}")
    return cls(**kwargs)
