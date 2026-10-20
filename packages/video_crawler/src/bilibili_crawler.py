"""
B站视频爬虫 - 搜索关键词并下载视频片段

下载策略（按稳定性排序）：
1. 官方 view/playurl API（durl 合并 MP4，必要时 DASH 音视频合并）
2. yt-dlp 兜底

yt-dlp 在部分网络环境会被 B 站返回 412；playurl API 更稳。
"""

import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    SEARCH_FALLBACK = "https://api.bilibili.com/x/web-interface/search/type"
    VIEW_API = "https://api.bilibili.com/x/web-interface/view"
    PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
    VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, cache_dir: str = "var/cache/bilibili"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(self.HEADERS)
        # 没有 buvid 时 playurl 更容易 412；生成一个游客 cookie 即可
        try:
            self.session.cookies.set("buvid3", uuid.uuid4().hex.upper() + "infoc")
            self.session.cookies.set("buvid4", uuid.uuid4().hex.upper() + "-1-0")
        except Exception:
            pass

    # ------------------------------------------------------------------ search
    def search(self, keyword: str, max_results: int = 20) -> List[VideoInfo]:
        """搜索B站视频（wbi/search 失败时自动降级到旧 search 接口）。"""
        print(f"  搜索B站: {keyword}")

        results: List[VideoInfo] = []
        page = 1
        per_page = min(max_results, 50)

        while len(results) < max_results:
            params = {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": per_page,
            }

            data = self._api_json(self.SEARCH_API, params)
            if not data or not data.get("result"):
                data = self._api_json(self.SEARCH_FALLBACK, params)

            video_list = (data or {}).get("result") or []
            if not video_list:
                break

            for v in video_list:
                dur_str = v.get("duration", "0:00")
                dur_sec = self._parse_duration(dur_str)
                if dur_sec < 10 or dur_sec > 600:
                    continue
                bvid = v.get("bvid", "")
                if not bvid:
                    continue
                results.append(VideoInfo(
                    bvid=bvid,
                    title=self._strip_html(v.get("title", "")),
                    duration=dur_str,
                    duration_seconds=dur_sec,
                    url=self.VIDEO_URL.format(bvid=bvid),
                    description=v.get("description", "")[:200],
                    author=v.get("author", ""),
                ))
                if len(results) >= max_results:
                    break

            page += 1
            time.sleep(0.5)

        print(f"  找到 {len(results)} 个视频")
        return results

    # ----------------------------------------------------------------- download
    def download(
        self,
        videos: List[VideoInfo],
        max_clips: int = 20,
        max_duration: float = 600,
    ) -> List[str]:
        """下载视频"""
        downloaded: List[str] = []
        download_count = min(len(videos), max_clips)
        print(f"  准备下载 {download_count} 个视频...")

        for i, video in enumerate(videos[:download_count]):
            if video.duration_seconds > max_duration:
                print(f"  跳过 [{i+1}/{download_count}]: {video.title[:30]}... (太长)")
                continue

            safe_name = re.sub(r'[<>:"/\\|?*]', '_', video.title)[:60]
            output_path = self.cache_dir / f"{video.bvid}_{safe_name}.mp4"
            if output_path.exists() and self._valid_media(str(output_path)):
                print(f"  缓存命中 [{i+1}/{download_count}]: {video.title[:30]}...")
                downloaded.append(str(output_path))
                continue

            print(f"  下载 [{i+1}/{download_count}]: {video.title[:40]}...")
            try:
                result = self._download_one(video.url, str(output_path))
                if result:
                    downloaded.append(result)
                time.sleep(1.0)
            except Exception as e:
                print(f"    下载失败: {e}")

        print(f"  成功下载 {len(downloaded)} 个视频")
        return downloaded

    def search_and_download(self, keyword: str, max_clips: int = 20) -> List[str]:
        """搜索并下载"""
        print(f"\n{'='*50}\nB站素材爬取: {keyword}\n{'='*50}")
        videos = self.search(keyword, max_results=max_clips * 2)
        if not videos:
            print("  未找到视频")
            return []
        return self.download(videos, max_clips=max_clips)

    def download_video(self, url: str, output_path: str) -> Optional[str]:
        """公开接口：下载单个 B 站/其他 URL 到指定路径。"""
        return self._download_one(url, output_path)

    # ------------------------------------------------------------- low level
    def _download_one(self, url: str, output_path: str) -> Optional[str]:
        """优先官方 playurl API，失败再回退 yt-dlp。"""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        bvid = self._extract_bvid(url)

        if bvid:
            view = self._api_json(self.VIEW_API, {"bvid": bvid})
            if view:
                cid = view.get("cid")
                pages = view.get("pages") or []
                if not cid and pages:
                    cid = pages[0].get("cid")
                if cid:
                    for qn in (64, 32, 16):
                        play = self._api_json(self.PLAYURL_API, {
                            "bvid": bvid, "cid": cid, "qn": qn,
                            "fnval": 1, "fourk": 0,
                        })
                        if not play or not play.get("durl"):
                            # 某些视频只给 DASH
                            play = self._api_json(self.PLAYURL_API, {
                                "bvid": bvid, "cid": cid, "qn": qn,
                                "fnval": 16, "fourk": 0,
                            })
                        if play and self._download_playurl(play, out):
                            if self._valid_media(str(out)):
                                return str(out)
                            print("    playurl 下载文件无效，重试...")
                        time.sleep(0.6)
            print("  playurl 不可用，回退 yt-dlp...")

        return self._download_with_ytdlp(url, str(out))

    def _download_playurl(self, play: Dict[str, Any], out: Path) -> bool:
        durl = play.get("durl") or []
        if durl:
            return self._download_durl(durl, out)
        dash = play.get("dash") or {}
        if dash:
            return self._download_dash(dash, out)
        return False

    def _download_durl(self, durl: List[Dict[str, Any]], out: Path) -> bool:
        parts: List[Path] = []
        urls = sorted(durl, key=lambda x: x.get("order", 0))
        for idx, seg in enumerate(urls):
            url = seg.get("url", "")
            if not url:
                self._cleanup(parts)
                return False
            part = out.with_name(f"{out.stem}.part{idx}{out.suffix}")
            if not self._download_stream(url, part):
                self._cleanup(parts + [part])
                return False
            parts.append(part)

        if not parts:
            return False
        if len(parts) == 1:
            parts[0].replace(out)
            return True

        list_file = out.with_name(f"{out.stem}.concat.txt")
        list_file.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
            encoding="utf-8",
        )
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", str(out)],
            capture_output=True,
        )
        self._cleanup(parts + [list_file])
        return r.returncode == 0 and out.exists()

    def _download_dash(self, dash: Dict[str, Any], out: Path) -> bool:
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        if not videos:
            return False

        # 优先 720P 以下、码率最高的视频轨
        candidates = [v for v in videos if (v.get("height") or 0) <= 720] or videos
        video = max(candidates, key=lambda v: (v.get("height") or 0,
                                               v.get("bandwidth") or 0))
        audio = max(audios, key=lambda a: a.get("bandwidth") or 0) if audios else None

        vpath = out.with_name(f"{out.stem}.video.m4s")
        apath = out.with_name(f"{out.stem}.audio.m4s")
        vurl = video.get("baseUrl") or (video.get("backupUrl") or [""])[0]
        if not vurl or not self._download_stream(vurl, vpath):
            self._cleanup([vpath])
            return False

        if audio:
            aurl = audio.get("baseUrl") or (audio.get("backupUrl") or [""])[0]
            if not aurl or not self._download_stream(aurl, apath):
                self._cleanup([vpath, apath])
                return False
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(vpath),
                   "-i", str(apath), "-c", "copy", "-shortest", str(out)]
        else:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(vpath),
                   "-c", "copy", str(out)]

        r = subprocess.run(cmd, capture_output=True)
        self._cleanup([vpath, apath])
        return r.returncode == 0 and out.exists()

    def _download_stream(self, url: str, path: Path, retries: int = 3) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(retries):
            tmp = path.with_name(path.name + ".part")
            try:
                with self.session.get(
                    url,
                    headers={"Referer": "https://www.bilibili.com/"},
                    stream=True,
                    timeout=90,
                ) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 20):
                            if chunk:
                                f.write(chunk)
                if tmp.stat().st_size < 5000:
                    raise IOError("文件过小")
                tmp.replace(path)
                return True
            except Exception as e:
                print(f"    下载片段失败({attempt+1}/{retries}): {e}")
                tmp.unlink(missing_ok=True)
                time.sleep(1.0 * (attempt + 1))
        return False

    def _download_with_ytdlp(self, url: str, output_path: str) -> Optional[str]:
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
            return output_path if self._valid_media(output_path) else None
        except ImportError:
            return self._download_one_cli(url, output_path)
        except Exception as e:
            print(f"    yt-dlp 失败: {e}")
            return None

    def _download_one_cli(self, url: str, output_path: str) -> Optional[str]:
        cmd = [
            "yt-dlp", "--quiet",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", output_path, url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode == 0 and self._valid_media(output_path):
                return output_path
            print(f"    yt-dlp CLI 错误: {(result.stderr or '')[:200]}")
            return None
        except FileNotFoundError:
            print("    错误: yt-dlp 未安装")
            return None

    # ------------------------------------------------------------------ utils
    def _api_json(self, url: str, params: Dict[str, Any],
                  retries: int = 3) -> Optional[Dict[str, Any]]:
        last = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=20)
                if r.status_code in (412, 429):
                    last = f"HTTP {r.status_code}"
                    time.sleep(1.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                payload = r.json()
                code = payload.get("code")
                if code == 0:
                    return payload.get("data") or {}
                last = payload.get("message") or f"code={code}"
                if code in (-412, -799, -509):
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last = e
                time.sleep(1.0 * (attempt + 1))
        print(f"  B站 API 失败: {last}")
        return None

    @staticmethod
    def _extract_bvid(url: str) -> Optional[str]:
        m = re.search(r"(BV[0-9A-Za-z]+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    def _valid_media(path: str) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=20,
            )
            return bool(r.stdout.strip()) and float(r.stdout.strip()) > 0.1
        except Exception:
            return False

    @staticmethod
    def _cleanup(paths: List[Path]) -> None:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _parse_duration(dur_str: str) -> float:
        parts = dur_str.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return 0.0
        except (ValueError, IndexError):
            return 0.0

    @staticmethod
    def _strip_html(text: str) -> str:
        return re.sub(r'<[^>]+>', '', text)
