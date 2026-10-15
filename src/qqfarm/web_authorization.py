"""检查归档中真正的登录时间，不把导出文件时间当成授权时间。"""

import hashlib
import time
from datetime import datetime
from urllib.parse import parse_qs

from qqfarm import wire
from qqfarm.capture import LOGIN_METHODS, frame_body, sessions
from qqfarm.config import FarmError


def inspect_authorization(archive, *, now=None):
    latest = None
    session_count = 0
    for candidate in sessions(archive, include_headers=True):
        latest = candidate
        session_count += 1
    if latest is None:
        raise FarmError("归档中没有完整的农场 WebSocket 会话。")
    _, url, _, frames = latest
    code = parse_qs(url.query).get("code", [""])[0]
    login_seq = None
    login_method = None
    login_succeeded = False
    captured_at = None
    for outgoing, raw, headers in frames:
        opcode, final, compressed, body = frame_body(raw)
        if opcode != 2 or not final or compressed:
            continue
        packet = wire.decode_gate(body)
        if (
            packet["service"] != "gamepb.userpb.UserService"
            or packet["method"] not in LOGIN_METHODS
        ):
            continue
        if outgoing and packet["type"] == 1:
            if login_seq is not None:
                raise FarmError("归档包含多个登录请求，请导出只含一次农场登录的会话。")
            login_seq = packet["seq"]
            login_method = packet["method"]
        elif (
            not outgoing
            and packet["type"] == 2
            and packet["method"] == login_method
            and login_seq is not None
            and packet["seq"] == login_seq
            and packet["error"] == 0
        ):
            basic = wire.parse(wire.data(wire.parse(packet["body"]), 1))
            login_succeeded = bool(wire.number(basic, 1))
            for name in ("DoneRead", "BeginSend", "DoneSend"):
                try:
                    stamp = datetime.fromisoformat(headers.get(name, ""))
                    if stamp.tzinfo is not None:
                        captured_at = stamp
                        break
                except ValueError:
                    continue
    current = time.time() if now is None else now
    return {
        "session_count": session_count,
        "latest_login_complete": bool(code and login_seq is not None and login_succeeded),
        "login_method": login_method,
        "captured_at": captured_at.isoformat() if captured_at else None,
        "age_seconds": int(current - captured_at.timestamp()) if captured_at else None,
        "authorization_fingerprint": hashlib.sha256(code.encode()).hexdigest()[:12]
        if code
        else None,
    }


def require_fresh_authorization(details, max_age):
    if not details["latest_login_complete"]:
        raise FarmError("最新会话没有完整成功登录；不会退回旧会话导出授权。")
    age = details["age_seconds"]
    if age is None:
        raise FarmError("归档缺少带时区的登录时间，无法确认授权新鲜程度。")
    if age < -60:
        raise FarmError("登录时间在未来，请检查电脑时钟和归档时间。")
    if age > max_age:
        raise FarmError(
            f"归档中的登录发生于 {details['captured_at']}，距今 {age} 秒，"
            f"超过导出时间窗口 {max_age} 秒。刚导出文件不代表新登录；请检查捕获是否有新连接。"
        )
