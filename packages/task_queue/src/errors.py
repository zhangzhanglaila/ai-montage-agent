"""任务执行异常。"""

from __future__ import annotations


class TaskError(Exception):
    """处理器主动抛出的失败。

    通过 ``retryable`` 区分"可重试的偶发故障"（网络抖动、临时占用）与
    "重试也没用的确定性失败"（参数非法、素材搜不到）。后者不消耗重试次数，
    直接落 ``error``，避免白白重跑一遍重活。
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = bool(retryable)
