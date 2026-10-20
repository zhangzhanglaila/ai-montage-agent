"""
通用 yt-dlp 爬虫 - 支持 YouTube/Dailymotion/抖音/西瓜视频等

基于 yt-dlp，支持所有 yt-dlp 能解析的网站。
搜索功能：YouTube 用 ytsearch，Dailymotion 用 API，其余站点暂不支持搜索（可直接下载链接）。
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class YtdlpVideoInfo:
    """视频信息"""
    id: str
    title: str
    duration: float  # 秒
    url: str
    uploader: str = ""
    description: str = ""


def _safe_print(text: str):
    """Windows 安全打印（过滤 emoji 等特殊字符）"""
    import sys
    clean = text.encode("gbk", errors="replace").decode("gbk", errors="replace")
    try:
        print(clean)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((clean + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


class YtdlpCrawler:
    """基于 yt-dlp 的通用视频爬虫"""

    # yt-dlp 支持的常用站点
    SUPPORTED_SITES = {
        "youtube": "https://www.youtube.com",
        "dailymotion": "https://www.dailymotion.com",
        "douyin": "https://www.douyin.com",
        "ixigua": "https://www.ixigua.com",
        "acfun": "https://www.acfun.cn",
        "vimeo": "https://vimeo.com",
    }

    # 支持原生搜索的站点
    SEARCHABLE_SITES = {"youtube", "dailymotion"}

    def __init__(
        self,
        output_dir: str = "var/cache/ytdlp",
        max_duration: float = 600.0,
        min_duration: float = 5.0,
        proxy: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration = max_duration
        self.min_duration = min_duration
        self.proxy = proxy

    def _get_ytdlp_cmd(self) -> list:
        return ["yt-dlp", "--js-runtimes", "node"]

    def search_and_download(
        self,
        keyword: str,
        max_clips: int = 20,
        site: str = "youtube",
    ) -> List[str]:
        """搜索并下载视频"""
        base_url = self.SUPPORTED_SITES.get(site)
        if not base_url:
            _safe_print(f"不支持的站点: {site}，支持: {', '.join(self.SUPPORTED_SITES.keys())}")
            return []

        _safe_print(f"  搜索 {site}: {keyword}")

        # 构建搜索 URL
        if site == "youtube":
            search_url = f"ytsearch{max_clips}:{keyword}"
        elif site == "dailymotion":
            # Dailymotion 不支持 ytsearch，用 API 搜索
            videos = self._search_dailymotion_api(keyword, max_clips)
            if not videos:
                _safe_print("  未找到视频")
                return []
            _safe_print(f"  找到 {len(videos)} 个视频")
            return self._download_list(videos, max_clips)
        else:
            _safe_print(f"  {site} 不支持关键词搜索，需要直接提供视频 URL")
            return []

        # YouTube 搜索
        videos = self._search_videos(search_url, max_clips)
        if not videos:
            _safe_print("  未找到视频")
            return []

        _safe_print(f"  找到 {len(videos)} 个视频")
        return self._download_list(videos, max_clips)

    def _search_dailymotion_api(self, keyword: str, max_results: int) -> List[YtdlpVideoInfo]:
        """用 Dailymotion API 搜索视频"""
        import requests

        api_url = "https://api.dailymotion.com/videos"
        params = {
            "search": keyword,
            "limit": min(max_results, 100),
            "fields": "id,title,duration,url,owner.screenname",
            "sort": "relevance",
        }

        try:
            resp = requests.get(api_url, params=params, timeout=15,
                                proxies={"http": None, "https": None})
            data = resp.json()
            videos = []
            for v in data.get("list", []):
                vid = YtdlpVideoInfo(
                    id=v.get("id", ""),
                    title=v.get("title", ""),
                    duration=v.get("duration", 0) or 0,
                    url=v.get("url", ""),
                    uploader=v.get("owner.screenname", ""),
                )
                if vid.url and vid.duration >= self.min_duration and vid.duration <= self.max_duration:
                    videos.append(vid)
            return videos
        except Exception as e:
            _safe_print(f"  Dailymotion API 搜索失败: {e}")
            return []

    def download_urls(self, urls: List[str], max_clips: int = 20) -> List[str]:
        """直接下载给定 URL 列表"""
        videos = []
        for url in urls:
            vid_id = re.sub(r'[^\w]', '_', url.split("/")[-1])[:40]
            videos.append(YtdlpVideoInfo(id=vid_id, title=vid_id, duration=0, url=url))
        return self._download_list(videos, max_clips)

    def _download_list(self, videos: List[YtdlpVideoInfo], max_clips: int) -> List[str]:
        """下载视频列表"""
        downloaded = []
        for i, video in enumerate(videos):
            if len(downloaded) >= max_clips:
                break

            cache_path = self.output_dir / f"{video.id}.mp4"
            if cache_path.exists():
                _safe_print(f"  [{i+1}/{len(videos)}] 已缓存: {video.title[:30]}")
                downloaded.append(str(cache_path))
                continue

            if video.duration > 0 and (video.duration < self.min_duration or video.duration > self.max_duration):
                continue

            _safe_print(f"  [{i+1}/{len(videos)}] 下载: {video.title[:40]}...")
            path = self._download_video(video.url, str(cache_path))
            if path:
                downloaded.append(path)
            time.sleep(1)

        _safe_print(f"  下载完成: {len(downloaded)}/{len(videos)}")
        return downloaded

    def _search_videos(self, search_url: str, max_results: int) -> List[YtdlpVideoInfo]:
        """用 yt-dlp 搜索视频列表"""
        cmd = self._get_ytdlp_cmd() + [
            "--flat-playlist",
            "--dump-json",
            "--no-download",
            "--playlist-end", str(max_results),
        ]
        if self.proxy:
            cmd.extend(["--proxy", self.proxy])
        cmd.append(search_url)

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="ignore")
                _safe_print(f"  yt-dlp 搜索失败: {stderr[:200]}")
                return []

            videos = []
            for line in result.stdout.decode("utf-8", errors="ignore").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    vid = YtdlpVideoInfo(
                        id=data.get("id", ""),
                        title=data.get("title", ""),
                        duration=data.get("duration", 0) or 0,
                        url=data.get("url") or data.get("webpage_url") or "",
                        uploader=data.get("uploader", ""),
                    )
                    if vid.url:
                        videos.append(vid)
                except json.JSONDecodeError:
                    continue
            return videos
        except subprocess.TimeoutExpired:
            _safe_print("  yt-dlp 搜索超时")
            return []
        except FileNotFoundError:
            _safe_print("  yt-dlp 未安装，请运行: pip install yt-dlp")
            return []

    def _download_video(self, url: str, output_path: str) -> Optional[str]:
        """下载单个视频"""
        cmd = self._get_ytdlp_cmd() + [
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", output_path,
            "--no-playlist",
        ]
        if self.proxy:
            cmd.extend(["--proxy", self.proxy])
        cmd.append(url)

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode == 0 and Path(output_path).exists():
                return output_path
            return None
        except subprocess.TimeoutExpired:
            return None


class DouyinCrawler(YtdlpCrawler):
    """抖音视频爬虫"""
    def search_and_download(self, keyword: str, max_clips: int = 20) -> List[str]:
        _safe_print("  抖音不支持关键词搜索，需要直接提供视频链接")
        return []


class IxiguaCrawler(YtdlpCrawler):
    """西瓜视频爬虫"""
    def search_and_download(self, keyword: str, max_clips: int = 20) -> List[str]:
        _safe_print("  西瓜视频不支持关键词搜索，需要直接提供视频链接")
        return []


class AcfunCrawler(YtdlpCrawler):
    """AcFun 视频爬虫"""
    def search_and_download(self, keyword: str, max_clips: int = 20) -> List[str]:
        _safe_print("  AcFun 不支持关键词搜索，需要直接提供视频链接")
        return []


class VimeoCrawler(YtdlpCrawler):
    """Vimeo 视频爬虫"""
    def search_and_download(self, keyword: str, max_clips: int = 20) -> List[str]:
        _safe_print("  Vimeo 不支持关键词搜索，需要直接提供视频链接")
        return []


def create_crawler(source: str, **kwargs) -> YtdlpCrawler:
    """工厂函数 - 创建对应源的爬虫"""
    crawlers = {
        "youtube": YtdlpCrawler,
        "dailymotion": YtdlpCrawler,
        "douyin": DouyinCrawler,
        "ixigua": IxiguaCrawler,
        "acfun": AcfunCrawler,
        "vimeo": VimeoCrawler,
    }
    cls = crawlers.get(source)
    if not cls:
        raise ValueError(f"不支持的源: {source}，支持: {', '.join(crawlers.keys())}")
    return cls(**kwargs)
