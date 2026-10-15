import json
import os
import re
from collections import Counter
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from qqfarm import wire
from qqfarm.audit import tag
from qqfarm.config import FarmError

LOGIN_METHODS = {"Login", "PlayerLogin"}
RECORD_HEADER = re.compile(rb"(Request|Response)-Length:[ \t]*(\d+)\r\n")


def records(payload, *, include_headers=False):
    offset = 0
    while offset < len(payload):
        while offset < len(payload) and payload[offset] in {10, 13}:
            offset += 1
        if offset == len(payload):
            return
        match = RECORD_HEADER.match(payload, offset)
        end = payload.find(b"\r\n\r\n", offset)
        if not match or end < 0 or end - offset > 8192:
            raise FarmError("不支持的 SAZ WebSocket 记录格式。")
        length = int(match[2])
        end += 4
        frame = payload[end : end + length]
        if len(frame) != length:
            raise FarmError("SAZ WebSocket 记录被截断。")
        if include_headers:
            headers = {}
            for row in payload[offset : end - 4].split(b"\r\n"):
                key, colon, value = row.partition(b":")
                if colon and key in {b"DoneRead", b"BeginSend", b"DoneSend"}:
                    headers[key.decode("ascii")] = value.strip().decode("ascii")
            yield match[1] == b"Request", frame, headers
        else:
            yield match[1] == b"Request", frame
        offset = end + length


def frame_body(raw):
    if len(raw) < 2:
        raise FarmError("帧被截断。")
    opcode, final, compressed = raw[0] & 15, bool(raw[0] & 128), bool(raw[0] & 64)
    size, offset = raw[1] & 127, 2
    if size in {126, 127}:
        count = 2 if size == 126 else 8
        if len(raw) < offset + count:
            raise FarmError("帧长度被截断。")
        size, offset = int.from_bytes(raw[offset : offset + count], "big"), offset + count
    mask = raw[offset : offset + 4] if raw[1] & 128 else b""
    if raw[1] & 128 and len(mask) != 4:
        raise FarmError("帧掩码被截断。")
    offset += 4 if mask else 0
    if offset + size != len(raw):
        raise FarmError("帧长度不匹配。")
    body = raw[offset:]
    if mask:
        body = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
    return opcode, final, compressed, body


def sessions(archive_path, *, include_headers=False):
    source = BytesIO(archive_path) if isinstance(archive_path, bytes) else archive_path
    with ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > 10000:
            raise FarmError("SAZ 包含过多记录，请仅导出最近一次农场登录。")
        names = {member.filename for member in members}
        if len(names) != len(members):
            raise FarmError("SAZ 包含重名记录，请重新导出。")
        entries = [n for n in names if re.fullmatch(r"raw/\d+_c.txt", n)]
        if sum(archive.getinfo(name).file_size for name in entries) > 8 * 1024 * 1024:
            raise FarmError("SAZ 请求记录过多，请仅导出最近一次农场登录。")
        selected_bytes = 0
        selected_sessions = 0
        for name in sorted(entries, key=lambda n: int(n.split("/")[1].split("_")[0])):
            if archive.getinfo(name).file_size > 65536:
                continue
            headers = archive.read(name).split(b"\r\n")
            request = headers[0].split(b" ", 2)
            if len(request) != 3 or request[0] != b"GET":
                continue
            url = urlsplit(request[1].decode("ascii"))
            if url.hostname != "gate-obt.nqf.qq.com" or url.path != "/prod/ws":
                continue
            websocket = name.replace("_c.txt", "_w.txt")
            if websocket not in names:
                continue
            if archive.getinfo(websocket).file_size > 32 * 1024 * 1024:
                raise FarmError("捕获会话过大，请导出较短的最新登录会话。")
            selected_sessions += 1
            selected_bytes += archive.getinfo(websocket).file_size
            if selected_sessions > 64 or selected_bytes > 64 * 1024 * 1024:
                raise FarmError("农场会话记录过多，请仅导出最近一次登录。")
            pairs = {}
            for header in headers[1:]:
                key, colon, value = header.partition(b":")
                if colon:
                    pairs[key.decode("ascii", "ignore").lower()] = value.strip().decode("utf-8")
            yield (
                name,
                url,
                pairs,
                list(records(archive.read(websocket), include_headers=include_headers)),
            )


def authorization_from_capture(path, crypto, *, max_age=None):
    """解析用户自己的 SAZ 登录帧；将临时授权留在内存返回。"""
    if max_age is not None:
        from qqfarm.web_authorization import inspect_authorization, require_fresh_authorization

        require_fresh_authorization(inspect_authorization(path), max_age)
    latest = None
    for candidate in sessions(path):
        latest = candidate
    if latest is None:
        raise FarmError("归档中没有完整的农场登录会话。")
    _, url, headers, frames = latest
    query = parse_qs(url.query)
    code = query.get("code", [""])[0]
    if not code:
        raise FarmError("捕获的连接没有登录 code。")
    template = None
    login_seq = None
    login_method = None
    owner = 0
    friend_open_ids = []
    for outbound, raw in frames:
        opcode, final, compressed, payload = frame_body(raw)
        if opcode != 2 or not final or compressed:
            continue
        packet = wire.decode_gate(payload)
        if (
            outbound
            and packet["service"] == "gamepb.friendpb.FriendService"
            and packet["method"] == "SyncAll"
            and packet["type"] == 1
        ):
            values = wire.parse(crypto.decrypt(packet["body"])).get(2, [])
            if any(not isinstance(value, bytes) for value in values):
                raise FarmError("好友同步资料格式异常。")
            friend_open_ids = list(dict.fromkeys(value.decode("utf-8") for value in values))
            if len(friend_open_ids) > 500 or any(
                not value or len(value) > 512 or any(ord(char) < 32 for char in value)
                for value in friend_open_ids
            ):
                raise FarmError("好友同步资料异常。")
        if (
            packet["service"] != "gamepb.userpb.UserService"
            or packet["method"] not in LOGIN_METHODS
        ):
            continue
        if outbound and packet["type"] == 1:
            if template is not None:
                raise FarmError("归档包含多个登录请求，请导出只含一次农场登录的 Fiddler 会话。")
            login_seq = packet["seq"]
            login_method = packet["method"]
            template = crypto.decrypt(packet["body"])
            request = wire.parse(template)
            if login_method == "PlayerLogin":
                device = wire.parse(wire.data(request, 3))
                if (
                    wire.string(device, 1) != query.get("ver", [""])[0]
                    or wire.string(device, 2) != query.get("os", ["Windows"])[0]
                ):
                    raise FarmError("新版登录模板的版本或系统信息与 SAZ 不一致。")
        elif not outbound and packet["type"] == 2:
            if (
                packet["method"] != login_method
                or packet["seq"] != login_seq
                or packet["error"] != 0
            ):
                continue
            if (
                code.startswith("-")
                or len(code) > 8192
                or any(char.isspace() or ord(char) < 32 for char in code)
            ):
                raise FarmError("Fiddler 会话中的临时授权格式不正确。")
            if not template:
                raise FarmError("登录回复没有对应的客户端模板。")
            basic = wire.parse(wire.data(wire.parse(packet["body"]), 1))
            owner = wire.number(basic, 1)
    if not template or not owner:
        raise FarmError("归档没有完整的成功登录及客户端模板。")
    profile = {
        "code": code,
        "login_method": login_method,
        "version": query.get("ver", [""])[0],
        "os": query.get("os", ["Windows"])[0],
        "platform": query.get("platform", ["qq"])[0],
        "user_agent": headers.get("user-agent", ""),
        "origin": headers.get("origin", "https://gate-obt.nqf.qq.com"),
        "account_tag": tag(owner),
        "friend_open_ids": friend_open_ids,
        "template": template,
    }
    return profile


def settings_from_capture(path: Path, paths, *, max_age=300):
    """解析新鲜 SAZ 登录资料到内存，不落盘保存临时授权和设备模板。"""
    from qqfarm.crypto import GameCrypto, bundled_wasm

    crypto = GameCrypto(bundled_wasm(paths.root / "tsdk.wasm"))
    profile = authorization_from_capture(path, crypto, max_age=max_age)
    return replace(
        paths.game_settings(profile["code"]),
        client_version=profile["version"],
        login_method=profile.get("login_method", "Login"),
        os_tag=profile["os"],
        platform=profile["platform"],
        user_agent=profile["user_agent"],
        origin=profile["origin"],
        login_template=profile["template"],
        friend_open_ids=tuple(profile["friend_open_ids"]),
    )


def import_capture(path: Path, crypto, target: Path):
    profile = authorization_from_capture(path, crypto)
    template = profile.pop("template")
    profile.pop("friend_open_ids", None)
    target.mkdir(parents=True, exist_ok=True)
    (target / "login-template.bin").write_bytes(template)
    (target / "session.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    for name in ("login-template.bin", "session.json"):
        os.chmod(target / name, 0o600)
    return {
        "account": profile["account_tag"],
        "version": profile["version"],
        "template_imported": True,
    }


def prove_capture(path: Path, crypto):
    count = 0
    methods = set()
    for _, _, _, frames in sessions(path):
        for outbound, raw in frames:
            opcode, final, compressed, payload = frame_body(raw)
            if not outbound or opcode != 2 or not final or compressed:
                continue
            packet = wire.decode_gate(payload)
            plain = crypto.decrypt(packet["body"])
            wire.parse(plain)
            if crypto.encrypt(plain) != packet["body"]:
                raise FarmError("请求正文与当前加密实现不一致。")
            count += 1
            if packet["method"] in {"BuyGoods", "Plant", "Harvest", "RemovePlant", "Fertilize"}:
                methods.add(packet["method"])
    if not count:
        raise FarmError("没有可校验的客户端请求。")
    return {"cipher_packets_verified": count, "operations_verified": sorted(methods)}


def _field_value_shape(value, depth):
    if isinstance(value, int):
        return {"type": "varint"}
    if not value:
        return {"type": "bytes"}
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text and all(char.isprintable() or char in "\r\n\t" for char in text):
        return {"type": "text"}
    if depth < 4 and len(value) <= 65536:
        try:
            nested = wire.parse(value)
        except wire.ProtocolError:
            nested = None
        if nested:
            return {"type": "message_candidate", "fields": _message_shape(value, depth + 1)}
    return {"type": "bytes"}


def _message_shape(payload, depth=0):
    try:
        fields = wire.parse(payload)
    except wire.ProtocolError:
        return {"parseable": False}
    shape = []
    for field_tag, values in sorted(fields.items()):
        variants = Counter(
            json.dumps(
                _field_value_shape(value, depth),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for value in values
        )
        shape.append(
            {
                "tag": field_tag,
                "occurrences": len(values),
                "variants": [
                    {**json.loads(signature), "occurrences": count}
                    for signature, count in sorted(variants.items())
                ],
            }
        )
    return {"parseable": True, "fields": shape}


def _schema_samples(counter, limit=12):
    samples = [
        {"samples": count, "fields": json.loads(signature)}
        for signature, count in counter.most_common(limit)
    ]
    return samples, sum(counter.values()) - sum(item["samples"] for item in samples)


def inspect_interfaces(path: Path, crypto):
    """梳理自有 SAZ 中观察到的 RPC 与 protobuf 字段形状，不输出字段值。"""
    interfaces, notifications = {}, Counter()
    session_count = 0

    def stats_for(key):
        return interfaces.setdefault(
            key,
            {
                "requests": 0,
                "responses": 0,
                "matched_responses": 0,
                "unmatched_responses": 0,
                "reply_errors": Counter(),
                "request_shapes": Counter(),
                "response_shapes": Counter(),
            },
        )

    for _, _, _, frames in sessions(path):
        session_count += 1
        pending = {}
        for outbound, raw in frames:
            opcode, final, compressed, payload = frame_body(raw)
            if opcode != 2 or not final or compressed:
                continue
            packet = wire.decode_gate(payload)
            message_type = packet["type"]
            if message_type == 1 and outbound:
                service, method = packet["service"], packet["method"]
                if len(service) > 160 or len(method) > 160:
                    raise FarmError("捕获消息中的服务名或方法名过长。")
                key = service, method
                stats = stats_for(key)
                body = crypto.decrypt(packet["body"]) if packet["body"] else b""
                shape = json.dumps(
                    _message_shape(body),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stats["requests"] += 1
                stats["request_shapes"][shape] += 1
                pending[packet["seq"]] = key
            elif message_type == 2 and not outbound:
                service, method = packet["service"], packet["method"]
                if len(service) > 160 or len(method) > 160:
                    raise FarmError("捕获消息中的服务名或方法名过长。")
                key = service, method
                stats = stats_for(key)
                stats["responses"] += 1
                stats["reply_errors"][packet["error"]] += 1
                if pending.get(packet["seq"]) == key:
                    stats["matched_responses"] += 1
                    del pending[packet["seq"]]
                else:
                    stats["unmatched_responses"] += 1
                if packet["body"]:
                    shape = json.dumps(
                        _message_shape(packet["body"]),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    stats["response_shapes"][shape] += 1
            elif message_type == 3 and not outbound:
                event = wire.parse(packet["body"])
                name = wire.string(event, 1) or "(未命名)"
                notifications[name if len(name) <= 160 else "(超长事件名)"] += 1

    if not session_count:
        raise FarmError("归档中没有完整的农场 WebSocket 会话。")

    rows = []
    for (service, method), stats in sorted(interfaces.items()):
        request_shapes, omitted_requests = _schema_samples(stats["request_shapes"])
        response_shapes, omitted_responses = _schema_samples(stats["response_shapes"])
        rows.append(
            {
                "service": service,
                "method": method,
                "requests": stats["requests"],
                "responses": stats["responses"],
                "matched_responses": stats["matched_responses"],
                "unanswered_requests": stats["requests"] - stats["matched_responses"],
                "unmatched_responses": stats["unmatched_responses"],
                "reply_errors": {
                    str(code): count for code, count in sorted(stats["reply_errors"].items())
                },
                "request_schemas": request_shapes,
                "omitted_request_schema_samples": omitted_requests,
                "response_schemas": response_shapes,
                "omitted_response_schema_samples": omitted_responses,
            }
        )
    return {
        "sessions": session_count,
        "request_count": sum(row["requests"] for row in rows),
        "response_count": sum(row["responses"] for row in rows),
        "unanswered_request_count": sum(row["unanswered_requests"] for row in rows),
        "unmatched_response_count": sum(row["unmatched_responses"] for row in rows),
        "interfaces": rows,
        "notifications": dict(sorted(notifications.items())),
        "note": (
            "仅包含接口名、消息字段号、类型与计数；不导出授权、账号标识或字段值。"
            "message_candidate 表示字节字段可按 protobuf 解析，需结合客户端行为确认。"
        ),
    }
