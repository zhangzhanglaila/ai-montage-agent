import sys

# Windows 控制台默认 GBK；标题里的「・」「🔥」等字符会让 print 抛异常，
# 进而把一次正常下载误判成“爬取失败”。这里统一改成可替换编码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from .src.bilibili_crawler import BilibiliCrawler, VideoInfo
from .src.bgm_crawler import BgmCrawler, BgmInfo
from .src.ytdlp_crawler import (
    YtdlpCrawler, YtdlpVideoInfo,
    DouyinCrawler, IxiguaCrawler, AcfunCrawler, VimeoCrawler,
    create_crawler,
)
from .src.quote_crawler import (
    QuoteCrawler, QuoteClip,
    YarnCrawler, PlayPhraseCrawler, QuoDBCrawler, ZhaotaiciCrawler,
    create_quote_crawler,
)

__all__ = [
    "BilibiliCrawler", "VideoInfo",
    "BgmCrawler", "BgmInfo",
    "YtdlpCrawler", "YtdlpVideoInfo",
    "DouyinCrawler", "IxiguaCrawler", "AcfunCrawler", "VimeoCrawler",
    "create_crawler",
    "QuoteCrawler", "QuoteClip",
    "YarnCrawler", "PlayPhraseCrawler", "QuoDBCrawler", "ZhaotaiciCrawler",
    "create_quote_crawler",
]
