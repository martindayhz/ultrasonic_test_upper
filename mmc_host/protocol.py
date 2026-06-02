from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


METER_NUMBER_REQUEST = bytes.fromhex(
    "68 10 AA AA AA AA AA AA AA 03 03 0A 81 02 B1 16"
)

CMD_MASTER_READ = 0x01
CMD_SLAVE_READ_RESPONSE = 0x81
CMD_MASTER_WRITE = 0x02
CMD_SLAVE_WRITE_RESPONSE = 0x82


@dataclass
class MeterNumberResponse:
    raw: bytes
    meter_number: bytes
    checksum_ok: bool
    expected_checksum: int
    checksum: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class MeterFrame:
    raw: bytes
    prefix: bytes
    length: int
    control: int
    address_byte: int
    ci: int
    dif: int
    command: int
    register_address: int
    data: bytes
    checksum: int
    expected_checksum: int
    checksum_ok: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class PassthroughFrame:
    raw: bytes
    prefix: bytes
    meter_type: int
    meter_number: bytes
    control: int
    data_length: int
    command_code: bytes
    sequence: int
    marker: int
    passthrough_length: int
    payload: bytes
    checksum: int
    expected_checksum: int
    checksum_ok: bool
    warnings: list[str] = field(default_factory=list)


def add8(data: Iterable[int]) -> int:
    return sum(data) & 0xFF


def bytes_to_hex(data: bytes | bytearray | Iterable[int]) -> str:
    return " ".join(f"{byte:02X}" for byte in bytes(data))


def hex_to_bytes(text: str) -> bytes:
    value = text.strip()
    if not value:
        return b""
    value = value.replace("0x", "").replace("0X", "")
    tokens = re.split(r"[\s,;:，；、-]+", value)
    out: list[int] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if not re.fullmatch(r"[0-9a-fA-F]+", token):
            raise ValueError(f"HEX 输入包含非法字符：{token}")
        if len(token) == 1:
            out.append(int(token, 16))
            continue
        if len(token) % 2 != 0:
            raise ValueError(f"HEX 输入长度必须为偶数：{token}")
        for pos in range(0, len(token), 2):
            out.append(int(token[pos : pos + 2], 16))
    return bytes(out)


def build_meter_number_request() -> bytes:
    return METER_NUMBER_REQUEST


def parse_meter_number_response(raw: bytes | str) -> MeterNumberResponse:
    data = _coerce_bytes(raw)
    start = _find_outer_start(data)
    if start < 0:
        raise ValueError("未找到表号应答起始符 68")
    if start + 16 > len(data):
        raise ValueError("表号应答长度不足")
    frame = data[start : start + 16]
    if frame[0] != 0x68 or frame[1] != 0x10 or frame[-1] != 0x16:
        raise ValueError("表号应答帧格式不正确")
    meter_number = frame[2:9]
    checksum = frame[-2]
    expected = add8(frame[:-2])
    warnings: list[str] = []
    prefix = data[:start]
    if prefix:
        warnings.append(f"忽略前导字节：{bytes_to_hex(prefix)}")
    if checksum != expected:
        warnings.append(f"表号应答校验异常：收到 {checksum:02X}，应为 {expected:02X}")
    return MeterNumberResponse(
        raw=frame,
        meter_number=meter_number,
        checksum_ok=checksum == expected,
        expected_checksum=expected,
        checksum=checksum,
        warnings=warnings,
    )


def build_meter_read_frame(register_address: int) -> bytes:
    return _build_meter_frame(CMD_MASTER_READ, register_address, b"")


def build_meter_write_frame(register_address: int, register_data: bytes) -> bytes:
    if not register_data:
        raise ValueError("写寄存器必须提供数据")
    return _build_meter_frame(CMD_MASTER_WRITE, register_address, register_data)


def _build_meter_frame(command: int, register_address: int, register_data: bytes) -> bytes:
    if not 0 <= register_address <= 0xFFFF:
        raise ValueError("寄存器地址必须在 0000H~FFFFH 范围内")
    body = bytes(
        [
            0x53,
            0xFE,
            0x51,
            0x0F,
            command,
            (register_address >> 8) & 0xFF,
            register_address & 0xFF,
        ]
    ) + bytes(register_data)
    frame = bytes([0x68, len(body), len(body), 0x68]) + body
    return frame + bytes([add8(body), 0x16])


def build_passthrough_frame(
    meter_frame: bytes,
    meter_number: bytes,
    sequence: int = 1,
    meter_type: int = 0x10,
) -> bytes:
    if len(meter_number) != 7:
        raise ValueError("透传协议表号必须为 7 字节")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("序号必须为 0~255")
    meter_frame = bytes(meter_frame)
    pass_len = len(meter_frame)
    data_field = (
        bytes([0xBB, 0x19, sequence, 0x01, pass_len & 0xFF, (pass_len >> 8) & 0xFF])
        + meter_frame
    )
    head = bytes([0x68, meter_type]) + meter_number + bytes([0x04, len(data_field)])
    frame = head + data_field
    return frame + bytes([add8(frame), 0x16])


def parse_passthrough_response(raw: bytes | str) -> PassthroughFrame:
    data = _coerce_bytes(raw)
    start = _find_outer_start(data)
    if start < 0:
        raise ValueError("未找到透传应答起始符 68")
    if start + 13 > len(data):
        raise ValueError("透传应答长度不足")
    data_length = data[start + 10]
    end_index = start + 11 + data_length + 1
    if end_index >= len(data):
        raise ValueError("透传应答数据域长度超过实际数据")
    frame = data[start : end_index + 1]
    if frame[-1] != 0x16:
        raise ValueError("透传应答结束符不是 16")
    field = frame[11 : 11 + data_length]
    if len(field) < 6:
        raise ValueError("透传应答数据域长度不足")
    pass_len = field[4] | (field[5] << 8)
    payload = field[6 : 6 + pass_len]
    warnings: list[str] = []
    if data[:start]:
        warnings.append(f"忽略前导字节：{bytes_to_hex(data[:start])}")
    if len(payload) != pass_len:
        warnings.append(f"透传计量帧长度不一致：声明 {pass_len}，实际 {len(payload)}")
    checksum = frame[-2]
    expected = add8(frame[:-2])
    if checksum != expected:
        warnings.append(f"透传外层校验异常：收到 {checksum:02X}，应为 {expected:02X}")
    return PassthroughFrame(
        raw=frame,
        prefix=data[:start],
        meter_type=frame[1],
        meter_number=frame[2:9],
        control=frame[9],
        data_length=data_length,
        command_code=field[0:2],
        sequence=field[2],
        marker=field[3],
        passthrough_length=pass_len,
        payload=payload,
        checksum=checksum,
        expected_checksum=expected,
        checksum_ok=checksum == expected,
        warnings=warnings,
    )


def parse_meter_frame(raw: bytes | str, expected_command: int | None = None) -> MeterFrame:
    data = _coerce_bytes(raw)
    start = _find_meter_start(data)
    if start < 0:
        raise ValueError("未找到计量帧起始结构 68 L L 68")
    length = data[start + 1]
    total_len = 4 + length + 2
    frame = data[start : start + total_len]
    if len(frame) != total_len:
        raise ValueError("计量帧长度不足")
    if frame[2] != length or frame[3] != 0x68:
        raise ValueError("计量帧长度字节或第二起始符错误")
    if frame[-1] != 0x16:
        raise ValueError("计量帧结束符不是 16")
    body = frame[4 : 4 + length]
    if len(body) < 7:
        raise ValueError("计量帧数据域长度不足")
    checksum = frame[-2]
    expected = add8(body)
    warnings: list[str] = []
    prefix = data[:start]
    if prefix:
        warnings.append(f"忽略计量帧前导字节：{bytes_to_hex(prefix)}")
    if checksum != expected:
        warnings.append(f"计量帧校验异常：收到 {checksum:02X}，应为 {expected:02X}")
    command = body[4]
    if expected_command is not None and command != expected_command:
        warnings.append(f"功能码不匹配：收到 {command:02X}，期望 {expected_command:02X}")
    if body[3] != 0x0F:
        warnings.append(f"DIF 不是扩展指令 0F：{body[3]:02X}")
    return MeterFrame(
        raw=frame,
        prefix=prefix,
        length=length,
        control=body[0],
        address_byte=body[1],
        ci=body[2],
        dif=body[3],
        command=command,
        register_address=(body[5] << 8) | body[6],
        data=body[7:],
        checksum=checksum,
        expected_checksum=expected,
        checksum_ok=checksum == expected,
        warnings=warnings,
    )


def extract_meter_payload(raw: bytes | str) -> tuple[bytes, PassthroughFrame | None]:
    data = _coerce_bytes(raw)
    try:
        outer = parse_passthrough_response(data)
    except ValueError:
        return data, None
    return outer.payload, outer


def _coerce_bytes(raw: bytes | str) -> bytes:
    if isinstance(raw, bytes):
        return raw
    return hex_to_bytes(raw)


def _find_outer_start(data: bytes) -> int:
    for index, byte in enumerate(data):
        if byte == 0x68 and index + 1 < len(data) and data[index + 1] == 0x10:
            return index
    return -1


def _find_meter_start(data: bytes) -> int:
    for index in range(0, max(0, len(data) - 5)):
        if data[index] != 0x68:
            continue
        length = data[index + 1]
        if data[index + 2] != length or data[index + 3] != 0x68:
            continue
        total_len = 4 + length + 2
        if index + total_len <= len(data) and data[index + total_len - 1] == 0x16:
            return index
    return -1
