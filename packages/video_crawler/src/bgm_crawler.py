"""
BGM 爬虫 - 搜索并下载背景音乐

支持：
- B站音频搜索 + 提取 MP3（主力，国内可用）
- YouTube Music / Free Music Archive 搜索（yt-dlp 回退）
- 任意 URL 音频提取

用法：
    crawler = BgmCrawler()
    path = crawler.search_and_download("epic cinematic BGM")
    path = crawler.search_by_style("intense")  # 按风格自动搜索
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class BgmInfo:
    """BGM 信息"""
    bvid: str
    title: str
    duration: float  # 秒
    url: str
    author: str = ""


# 风格 → 搜索关键词映射
STYLE_KEYWORDS = {
    "dynamic": [
        "动感 BGM 纯音乐",
        "节奏感 纯音乐 卡点",
        "upbeat background music",
        "energetic instrumental",
        "电子音乐 节奏 卡点 BGM",
        "流行 混剪 BGM 纯音乐",
    ],
    "calm": [
        "舒缓 纯音乐 BGM",
        "钢琴 轻音乐 背景音乐",
        "calm piano background music",
        "relaxing instrumental music",
        "治愈系 纯音乐",
        "ambient chill BGM",
    ],
    "intense": [
        "高燃 BGM 纯音乐",
        "史诗 音乐 战斗 BGM",
        "epic cinematic music",
        "intense action background music",
        "燃向 混剪 音乐 纯音乐",
        "trailer music epic",
    ],
}


class BgmCrawler:
    """BGM 爬虫"""

    def __init__(self, cache_dir: str = "var/cache/bgm"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, keyword: str, max_results: int = 10) -> List[BgmInfo]:
        """在 B站搜索 BGM/音乐，自动尝试多个关键词变体"""
        # 生成搜索关键词变体
        kw_lower = keyword.lower()
        has_bgm = "bgm" in kw_lower or "音乐" in keyword
        has_pure = "纯音乐" in keyword

        search_variants = []
        if has_bgm or has_pure:
            search_variants.append(keyword)
        else:
            search_variants.append(f"{keyword} BGM")
            search_variants.append(f"{keyword} 纯音乐")
            search_variants.append(keyword)

        for search_kw in search_variants:
            print(f"  搜索 BGM: {search_kw}")
            results = self._search_bilibili(search_kw, max_results * 3)
            if results:
                results.sort(key=lambda r: r.duration)
                good = [r for r in results if 15 <= r.duration <= 600]
                if good:
                    print(f"  找到 {len(good)} 个 BGM")
                    return good[:max_results]
                print(f"  找到 {len(results)} 个 BGM（未过滤）")
                return results[:max_results]
            print(f"  未找到结果，尝试下一个关键词...")

        print("  所有关键词均未找到 BGM")
        return []

    def search_by_style(self, style: str, max_results: int = 5) -> List[BgmInfo]:
        """按风格自动搜索 BGM（遍历风格关键词直到找到）"""
        keywords = STYLE_KEYWORDS.get(style, STYLE_KEYWORDS["dynamic"])

        for kw in keywords:
            print(f"  尝试关键词: {kw}")
            results = self.search(kw, max_results=max_results)
            if results:
                return results
            time.sleep(0.5)

        return []

    def _search_bilibili(self, keyword: str, page_size: int = 30) -> List[BgmInfo]:
        """执行一次 B站搜索"""
        import requests

        api_url = "https://api.bilibili.com/x/web-interface/wbi/search/type"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://search.bilibili.com/",
        }
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": 1,
            "page_size": min(page_size, 50),
        }

        try:
            resp = requests.get(api_url, params=params, headers=headers, timeout=10,
                                proxies={"http": None, "https": None})
            data = resp.json()
            if data.get("code") != 0:
                print(f"  搜索失败: {data.get('message', 'unknown')}")
                return []

            results = []
            for v in data.get("data", {}).get("result", []):
                dur_str = v.get("duration", "0:00")
                dur_sec = self._parse_duration(dur_str)
                if dur_sec < 15 or dur_sec > 600:
                    continue

                title = self._strip_html(v.get("title", ""))
                # 过滤掉明显不是音乐的结果
                if any(kw in title for kw in ["直播", "教程", "教学", "解说", "评测", "vlog"]):
                    continue

                results.append(BgmInfo(
                    bvid=v.get("bvid", ""),
                    title=title,
                    duration=dur_sec,
                    url=f"https://www.bilibili.com/video/{v.get('bvid', '')}",
                    author=v.get("author", ""),
                ))

            return results

        except Exception as e:
            print(f"  搜索出错: {e}")
            return []

    def search_and_download(self, keyword: str, max_clips: int = 1) -> List[str]:
        """搜索并下载 BGM（返回 MP3 路径列表）"""
        print(f"\n{'='*50}")
        print(f"BGM 搜索下载: {keyword}")
        print(f"{'='*50}")

        bgms = self.search(keyword, max_results=max_clips * 3)
        if not bgms:
            print("  未找到 BGM")
            return []

        downloaded = []
        for i, bgm in enumerate(bgms):
            if len(downloaded) >= max_clips:
                break

            cache_path = self.cache_dir / f"{bgm.bvid}.mp3"

            if cache_path.exists():
                print(f"  [{i+1}] 已缓存: {bgm.title[:40]}")
                downloaded.append(str(cache_path))
                continue

            print(f"  [{i+1}] 下载: {bgm.title[:40]}... ({self._format_dur(bgm.duration)})")
            path = self._extract_audio(bgm.url, str(cache_path))
            if path:
                downloaded.append(path)
                print(f"       -> {Path(path).name}")
            time.sleep(1)

        print(f"  下载完成: {len(downloaded)} 个 BGM")
        return downloaded

    def search_and_download_by_style(self, style: str, max_clips: int = 1) -> List[str]:
        """按风格搜索并下载 BGM"""
        print(f"\n{'='*50}")
        print(f"BGM 搜索下载 (风格: {style})")
        print(f"{'='*50}")

        bgms = self.search_by_style(style, max_results=max_clips * 3)
        if not bgms:
            print("  未找到 BGM")
            return []

        downloaded = []
        for i, bgm in enumerate(bgms):
            if len(downloaded) >= max_clips:
                break

            cache_path = self.cache_dir / f"{bgm.bvid}.mp3"

            if cache_path.exists():
                print(f"  [{i+1}] 已缓存: {bgm.title[:40]}")
                downloaded.append(str(cache_path))
                continue

            print(f"  [{i+1}] 下载: {bgm.title[:40]}... ({self._format_dur(bgm.duration)})")
            path = self._extract_audio(bgm.url, str(cache_path))
            if path:
                downloaded.append(path)
                print(f"       -> {Path(path).name}")
            time.sleep(1)

        print(f"  下载完成: {len(downloaded)} 个 BGM")
        return downloaded

    def download_url(self, url: str) -> Optional[str]:
        """从任意 URL 提取音频（yt-dlp 支持的站点均可）"""
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        cache_path = self.cache_dir / f"url_{url_hash}.mp3"

        if cache_path.exists():
            print(f"  已缓存: {cache_path.name}")
            return str(cache_path)

        print(f"  提取音频: {url[:60]}...")
        return self._extract_audio(url, str(cache_path))

    def _extract_audio(self, url: str, output_path: str) -> Optional[str]:
        """用 yt-dlp 提取音频为 MP3"""
        cmd = [
            "yt-dlp", "--js-runtimes", "node",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", output_path,
            "--no-playlist",
            "--no-post-overwrites",
            url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and Path(output_path).exists():
                return output_path

            mp3_path = Path(output_path).with_suffix(".mp3")
            if mp3_path.exists():
                return str(mp3_path)

            stderr = result.stderr[:200] if result.stderr else ""
            print(f"       提取失败: {stderr}")
            return None
        except subprocess.TimeoutExpired:
            print("       提取超时")
            return None
        except FileNotFoundError:
            print("       yt-dlp 未安装")
            return None

    @staticmethod
    def _parse_duration(dur_str: str) -> float:
        parts = dur_str.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return 0.0
        except (ValueError, IndexError):
            return 0.0

    @staticmethod
    def _format_dur(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _strip_html(text: str) -> str:
        return re.sub(r'<[^>]+>', '', text)
