"""
B站视频爬虫 - 搜索关键词并下载视频片段
用于为混剪 pipeline 自动获取素材
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

import requests


@dataclass
class VideoInfo:
    """视频信息"""
    bvid: str
    title: str
    duration: str  # "7:22" 格式
    duration_seconds: float
    url: str
    description: str = ""
    author: str = ""


class BilibiliCrawler:
    """B站视频爬虫"""

    SEARCH_API = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://search.bilibili.com/",
    }

    def __init__(self, cache_dir: str = "var/cache/bilibili"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, keyword: str, max_results: int = 20) -> List[VideoInfo]:
        """搜索B站视频

        Args:
            keyword: 搜索关键词
            max_results: 最大返回数量

        Returns:
            视频信息列表
        """
        print(f"  搜索B站: {keyword}")

        results = []
        page = 1
        per_page = min(max_results, 50)  # B站API每页最多50条

        while len(results) < max_results:
            params = {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": per_page,
            }

            try:
                resp = requests.get(
                    self.SEARCH_API,
                    params=params,
                    headers=self.HEADERS,
                    timeout=10,
                    proxies={"http": None, "https": None},
                )
                data = resp.json()

                if data.get("code") != 0:
                    print(f"  搜索API返回错误: {data.get('message', 'unknown')}")
                    break

                video_list = data.get("data", {}).get("result", [])
                if not video_list:
                    break

                for v in video_list:
                    # 解析时长字符串 "7:22" -> 秒数
                    dur_str = v.get("duration", "0:00")
                    dur_sec = self._parse_duration(dur_str)

                    # 跳过太短（<10秒）或太长（>10分钟）的视频
                    if dur_sec < 10 or dur_sec > 600:
                        continue

                    info = VideoInfo(
                        bvid=v.get("bvid", ""),
                        title=self._strip_html(v.get("title", "")),
                        duration=dur_str,
                        duration_seconds=dur_sec,
                        url=self.VIDEO_URL.format(bvid=v.get("bvid", "")),
                        description=v.get("description", "")[:200],
                        author=v.get("author", ""),
                    )
                    results.append(info)

                    if len(results) >= max_results:
                        break

                page += 1
                time.sleep(0.5)  # 请求间隔

            except Exception as e:
                print(f"  搜索出错: {e}")
                break

        print(f"  找到 {len(results)} 个视频")
        return results

    def download(
        self,
        videos: List[VideoInfo],
        max_clips: int = 20,
        max_duration: float = 600,
    ) -> List[str]:
        """下载视频

        Args:
            videos: 视频信息列表
            max_clips: 最大下载数量
            max_duration: 单个视频最大时长（秒）

        Returns:
            本地文件路径列表
        """
        downloaded = []
        download_count = min(len(videos), max_clips)

        print(f"  准备下载 {download_count} 个视频...")

        for i, video in enumerate(videos[:download_count]):
            if video.duration_seconds > max_duration:
                print(f"  跳过 [{i+1}/{download_count}]: {video.title[:30]}... (太长: {video.duration})")
                continue

            # 检查缓存
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', video.title)[:60]
            output_path = self.cache_dir / f"{video.bvid}_{safe_name}.mp4"

            if output_path.exists():
                print(f"  缓存命中 [{i+1}/{download_count}]: {video.title[:30]}...")
                downloaded.append(str(output_path))
                continue

            print(f"  下载 [{i+1}/{download_count}]: {video.title[:40]}...")

            try:
                result = self._download_one(video.url, str(output_path))
                if result:
                    downloaded.append(result)
                time.sleep(1.5)  # 下载间隔，避免被封
            except Exception as e:
                print(f"    下载失败: {e}")

        print(f"  成功下载 {len(downloaded)} 个视频")
        return downloaded

    def search_and_download(
        self,
        keyword: str,
        max_clips: int = 20,
    ) -> List[str]:
        """搜索并下载（组合方法）

        Args:
            keyword: 搜索关键词
            max_clips: 最大下载数量

        Returns:
            本地视频文件路径列表
        """
        print(f"\n{'='*50}")
        print(f"B站素材爬取: {keyword}")
        print(f"{'='*50}")

        # 搜索
        videos = self.search(keyword, max_results=max_clips * 2)  # 多搜一些，过滤后用
        if not videos:
            print("  未找到视频")
            return []

        # 下载
        paths = self.download(videos, max_clips=max_clips)
        return paths

    def _download_one(self, url: str, output_path: str) -> Optional[str]:
        """用 yt-dlp 下载单个视频"""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": output_path,
            "socket_timeout": 30,
            "merge_output_format": "mp4",
            "retries": 3,
        }

        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return output_path if Path(output_path).exists() else None
        except ImportError:
            # 回退到命令行 yt-dlp
            return self._download_one_cli(url, output_path)

    def _download_one_cli(self, url: str, output_path: str) -> Optional[str]:
        """用 yt-dlp CLI 下载（回退方案）"""
        cmd = [
            "yt-dlp",
            "--quiet",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", output_path,
            url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and Path(output_path).exists():
                return output_path
            print(f"    yt-dlp 错误: {result.stderr[:200]}")
            return None
        except FileNotFoundError:
            print("    错误: yt-dlp 未安装，请运行 pip install yt-dlp")
            return None

    @staticmethod
    def _parse_duration(dur_str: str) -> float:
        """解析时长字符串 '7:22' -> 秒数"""
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
    def _strip_html(text: str) -> str:
        """去除HTML标签"""
        return re.sub(r'<[^>]+>', '', text)
