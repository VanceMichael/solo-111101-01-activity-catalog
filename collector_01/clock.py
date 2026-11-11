"""可注入的逻辑时钟。

离线重放不允许真实 sleep：所有等待只推进逻辑偏移，调度时刻可被测试精确断言。
"""

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, epoch=None):
        if epoch is None:
            epoch = datetime.now(timezone.utc)
        elif epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
        self.epoch = epoch.astimezone(timezone.utc)
        self.offset = 0.0

    def now(self) -> float:
        """逻辑秒数（单调，只增不减）。"""
        return self.offset

    def now_iso(self) -> str:
        moment = self.epoch + timedelta(seconds=self.offset)
        return moment.isoformat().replace("+00:00", "Z")

    def advance(self, seconds) -> str:
        seconds = float(seconds)
        if seconds < 0:
            raise ValueError("逻辑时钟不能回拨")
        self.offset += seconds
        return self.now_iso()
