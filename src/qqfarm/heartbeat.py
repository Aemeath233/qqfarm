"""业务心跳的节奏与统计，不保存玩家标识或授权凭证。"""

import statistics
import time
from collections import deque

INTERVAL = 25.0  # 当前客户端抓包中的业务心跳周期。
ACK_TIMEOUT = 12.0  # 本地保护值，不是已确认的服务端踢下线阈值。


def next_deadline(previous, now, interval=INTERVAL):
    following = previous + interval
    # 事件循环长时间暂停后不密集补发，重新安排一个周期。
    return following if following > now else now + interval


class HeartbeatStats:
    def __init__(self):
        self.started = time.monotonic()
        self.sent_count = self.ack_count = 0
        self.last_sent = self.last_ack = None
        self.intervals = deque(maxlen=3600)
        self.rtts = deque(maxlen=3600)
        self.lateness = deque(maxlen=3600)

    def sent(self, stamp, due):
        if self.last_sent is not None:
            self.intervals.append(stamp - self.last_sent)
        self.sent_count += 1
        self.last_sent = stamp
        self.lateness.append(max(0, stamp - due))

    def acknowledged(self, sent, received):
        self.ack_count += 1
        self.last_ack = received
        self.rtts.append(received - sent)

    def snapshot(self):
        def describe(values):
            if not values:
                return None
            return {
                "samples": len(values),
                "min": round(min(values), 4),
                "median": round(statistics.median(values), 4),
                "max": round(max(values), 4),
            }

        return {
            "elapsed_seconds": round(time.monotonic() - self.started, 2),
            "sent": self.sent_count,
            "acknowledged": self.ack_count,
            "interval_seconds": describe(self.intervals),
            "rtt_seconds": describe(self.rtts),
            "schedule_lateness_seconds": describe(self.lateness),
            "seconds_since_last_ack": (
                round(time.monotonic() - self.last_ack, 2) if self.last_ack is not None else None
            ),
        }
