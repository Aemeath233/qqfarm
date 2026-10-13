"""本客户端使用的 Protobuf 子集：明确字段编码、受限解码，不执行输入内容。"""

from collections import defaultdict

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def varint(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        raise ProtocolError("无效整数。")
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def read_varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ProtocolError("整数被截断。")
        value = data[offset]
        offset += 1
        if shift == 63 and value > 1:
            raise ProtocolError("整数超过 64 位。")
        result |= (value & 127) << shift
        if not value & 128:
            return result, offset
    raise ProtocolError("无效整数编码。")


def uint(tag, value):
    return varint(tag << 3) + varint(value)


def blob(tag, value: bytes):
    return varint(tag << 3 | 2) + varint(len(value)) + value


def text(tag, value: str):
    return blob(tag, value.encode("utf-8"))


def packed(tag, values):
    return blob(tag, b"".join(varint(value) for value in values))


def parse(data: bytes):
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("消息超过大小限制。")
    fields = defaultdict(list)
    offset = count = 0
    while offset < len(data):
        count += 1
        if count > 10000:
            raise ProtocolError("消息字段过多。")
        key, offset = read_varint(data, offset)
        tag, kind = key >> 3, key & 7
        if not tag or tag >= 1 << 29:
            raise ProtocolError("无效字段标识。")
        if kind == 0:
            value, offset = read_varint(data, offset)
        elif kind in {1, 2, 5}:
            length = 8 if kind == 1 else 4
            if kind == 2:
                length, offset = read_varint(data, offset)
            if length > len(data) - offset:
                raise ProtocolError("字段被截断。")
            value, offset = data[offset : offset + length], offset + length
        else:
            raise ProtocolError("不支持的字段类型。")
        fields[tag].append(value)
    return fields


def number(fields, tag, default=0):
    value = fields.get(tag, [default])[0]
    if not isinstance(value, int):
        raise ProtocolError("字段整数类型不匹配。")
    return value


def data(fields, tag, default=b""):
    value = fields.get(tag, [default])[0]
    if not isinstance(value, bytes):
        raise ProtocolError("字段二进制类型不匹配。")
    return value


def string(fields, tag, default=""):
    try:
        return data(fields, tag, default.encode()).decode("utf-8")
    except UnicodeError:
        raise ProtocolError("文本字段格式无效。") from None


def encode_gate(service, method, seq, server_seq, body):
    meta = text(1, service) + text(2, method) + uint(3, 1) + uint(4, seq)
    if server_seq:
        meta += uint(5, server_seq)
    return blob(1, meta) + (blob(2, body) if body else b"")


def decode_gate(payload):
    outer = parse(payload)
    meta = parse(data(outer, 1))
    return {
        "service": string(meta, 1),
        "method": string(meta, 2),
        "type": number(meta, 3),
        "seq": number(meta, 4),
        "server_seq": number(meta, 5),
        "error": number(meta, 6),
        "body": data(outer, 2),
    }
