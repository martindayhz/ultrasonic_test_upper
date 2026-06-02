from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import struct
from typing import Any

from protocol import hex_to_bytes, bytes_to_hex


@dataclass(frozen=True)
class RegisterDef:
    address: int
    size: int | None
    name: str
    access: str
    data_type: str
    description: str = ""
    enum: dict[int, str] | None = None
    scale: float | None = None
    unit: str = ""
    count: int | None = None
    signed: bool = False

    @property
    def address_label(self) -> str:
        return f"{self.address:04X}H"

    @property
    def can_read(self) -> bool:
        return "R" in self.access.upper()

    @property
    def can_write(self) -> bool:
        return "W" in self.access.upper()

    @property
    def combo_label(self) -> str:
        size = "N" if self.size is None else str(self.size)
        return f"{self.address_label}  {self.name}  {size}B  {self.access}"


def load_registers(config_path: str | Path = "registers.json") -> dict[int, RegisterDef]:
    registers = {reg.address: reg for reg in BUILTIN_REGISTERS}
    path = Path(config_path)
    if not path.exists():
        return registers
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload.get("registers", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("registers.json 必须是列表，或包含 registers 列表")
    for item in items:
        reg = _register_from_json(item, registers.get(_parse_address(item["address"])))
        registers[reg.address] = reg
    return registers


def parse_register_address(text: str) -> int:
    match = re.search(r"([0-9A-Fa-f]{1,4})\s*[Hh]?", text.strip())
    if not match:
        raise ValueError("请输入寄存器地址，例如 0001H")
    return _parse_address(match.group(1))


def make_unknown_register(address: int) -> RegisterDef:
    return RegisterDef(
        address=address,
        size=None,
        name="未知寄存器",
        access="R/W",
        data_type="raw",
        description="未在寄存器表中定义，读写均按原始 HEX 处理。",
    )


def decode_register_value(reg: RegisterDef, data: bytes) -> str:
    if not data:
        return "(无数据)"
    warning = ""
    if reg.size is not None and len(data) != reg.size:
        warning = f"长度异常：期望 {reg.size} 字节，实际 {len(data)} 字节；"
    try:
        decoded = _decode_by_type(reg, data)
    except Exception as exc:  # noqa: BLE001 - UI needs a useful fallback.
        decoded = f"解析失败：{exc}；原始 HEX：{bytes_to_hex(data)}"
    return warning + decoded


def encode_register_value(reg: RegisterDef, text: str) -> bytes:
    value = text.strip()
    if not value:
        raise ValueError("请输入写入值")
    if value.lower().startswith("hex:"):
        return _check_size(reg, hex_to_bytes(value[4:].strip()))
    if reg.data_type == "raw" or " " in value:
        return _check_size(reg, hex_to_bytes(value))
    if reg.data_type.startswith("array_") or reg.data_type in {"zero_drift", "tof_params", "amplitudes"}:
        parts = [part.strip() for part in re.split(r"[,，]", value) if part.strip()]
        if len(parts) <= 1:
            return _check_size(reg, hex_to_bytes(value))
        return _check_size(reg, _encode_number_array(reg, parts))
    if reg.data_type in {"u8", "u16", "u32", "u64", "i16", "i32"}:
        number = _parse_user_number(value, reg)
        return _check_size(reg, _encode_integer(number, reg))
    return _check_size(reg, hex_to_bytes(value))


def _decode_by_type(reg: RegisterDef, data: bytes) -> str:
    dtype = reg.data_type
    if dtype == "raw":
        return bytes_to_hex(data)
    if dtype == "bcd":
        return "".join(f"{byte >> 4:X}{byte & 0x0F:X}" for byte in data)
    if dtype in {"u8", "u16", "u32", "u64", "i16", "i32"}:
        value = _decode_integer(data, dtype)
        display = _format_scaled(value, reg)
        if reg.enum and value in reg.enum:
            display = f"{reg.enum[value]} ({value})"
        return display
    if dtype.startswith("array_"):
        base = dtype.removeprefix("array_")
        width = _int_width(base)
        values = [_decode_integer(data[i : i + width], base) for i in range(0, len(data), width)]
        return _format_array(values, reg)
    if dtype == "board_id":
        return _decode_board_id(data)
    if dtype == "temperature_pressure_enable":
        value = data[0]
        flags = []
        flags.append("温度传感器使能" if value & 0x01 else "温度传感器禁能")
        flags.append("压力传感器使能" if value & 0x10 else "压力传感器禁能")
        return f"0x{value:02X}，" + "，".join(flags)
    if dtype == "meter_status":
        return _decode_meter_status(int.from_bytes(data[:4], "big", signed=False))
    if dtype == "temperature_pressure_status":
        value = data[0]
        states = [
            f"温度{'异常' if value & 0x01 else '正常'}",
            f"压力{'异常' if value & 0x02 else '正常'}",
        ]
        return f"0x{value:02X}，" + "，".join(states)
    if dtype == "zero_drift":
        values = _unpack_int32_list(data)
        return "飞行时间差修正(ps): " + _join_values(values[:4]) + "；上下行飞行时间修正(ns): " + _join_values(values[4:8])
    if dtype == "tof_params":
        up = [int.from_bytes(data[i : i + 4], "big") for i in range(0, 16, 4)]
        down = [int.from_bytes(data[i : i + 4], "big") for i in range(16, 32, 4)]
        diff = [int.from_bytes(data[i : i + 4], "big", signed=True) for i in range(32, 48, 4)]
        return f"上行(ns): {_join_values(up)}；下行(ns): {_join_values(down)}；差值(ps): {_join_values(diff)}"
    if dtype == "amplitudes":
        values = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]
        labels = []
        for channel in range(4):
            base = channel * 2
            if base + 1 < len(values):
                labels.append(f"{channel + 1}声道 上{values[base]} 下{values[base + 1]}")
        return "；".join(labels)
    return bytes_to_hex(data)


def _encode_number_array(reg: RegisterDef, parts: list[str]) -> bytes:
    if reg.data_type.startswith("array_"):
        base = reg.data_type.removeprefix("array_")
        width = _int_width(base)
        signed = base.startswith("i")
        values = [_parse_plain_number(part) for part in parts]
        return b"".join(int(value).to_bytes(width, "big", signed=signed) for value in values)
    if reg.data_type == "amplitudes":
        return b"".join(_parse_plain_number(part).to_bytes(2, "big") for part in parts)
    values = [_parse_plain_number(part) for part in parts]
    return b"".join(int(value).to_bytes(4, "big", signed=True) for value in values)


def _decode_integer(data: bytes, dtype: str) -> int:
    width = _int_width(dtype)
    if len(data) < width:
        raise ValueError(f"{dtype} 需要 {width} 字节")
    signed = dtype.startswith("i")
    return int.from_bytes(data[:width], "big", signed=signed)


def _encode_integer(value: int, reg: RegisterDef) -> bytes:
    width = _int_width(reg.data_type)
    signed = reg.data_type.startswith("i")
    return int(value).to_bytes(width, "big", signed=signed)


def _int_width(dtype: str) -> int:
    return {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "i16": 2, "i32": 4}[dtype]


def _format_scaled(value: int, reg: RegisterDef) -> str:
    if reg.scale is None:
        return str(value)
    scaled = value * reg.scale
    return f"{scaled:g}{reg.unit} ({value})"


def _format_array(values: list[int], reg: RegisterDef) -> str:
    if reg.scale is None:
        return _join_values(values)
    return _join_values([f"{value * reg.scale:g}{reg.unit}" for value in values])


def _decode_board_id(data: bytes) -> str:
    raw = bytes_to_hex(data)
    if len(data) != 7:
        return raw
    year = _bcd_byte(data[1])
    month = _bcd_byte(data[2])
    serial = "".join(f"{byte >> 4:X}{byte & 0x0F:X}" for byte in data[3:7])
    return f"{raw}；年份后两位 {year:02d}，月份 {month:02d}，流水号 {serial}"


def _decode_meter_status(value: int) -> str:
    groups = [
        (1, 1, "采样超时异常"),
        (2, 5, "声道波形抓取异常"),
        (6, 9, "声道空管"),
        (10, 13, "声道固定点算法异常"),
        (14, 17, "声道飞行时间异常"),
        (18, 21, "声道飞行时间差异常"),
        (22, 25, "声道幅值异常"),
    ]
    parts = [f"0x{value:08X}"]
    for start, end, label in groups:
        active = [str(bit - start + 1) for bit in range(start, end + 1) if value & (1 << bit)]
        if active:
            parts.append(f"{label}: {','.join(active)}")
    study = (value >> 26) & 0b11
    parts.append("零漂自学习: " + {0: "初始化", 1: "成功", 2: "失败"}.get(study, "保留"))
    return "；".join(parts)


def _join_values(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values)


def _unpack_int32_list(data: bytes) -> list[int]:
    if len(data) % 4 != 0:
        raise ValueError("int32 数组长度必须是 4 的倍数")
    return [int.from_bytes(data[i : i + 4], "big", signed=True) for i in range(0, len(data), 4)]


def _parse_user_number(text: str, reg: RegisterDef) -> int:
    upper = text.strip().upper()
    if upper.startswith("DN"):
        return int(upper[2:])
    if upper.startswith("R") and upper[1:].isdigit():
        return int(upper[1:])
    if reg.enum:
        for number, label in reg.enum.items():
            if upper == label.upper():
                return number
    return _parse_plain_number(upper)


def _parse_plain_number(text: str) -> int:
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    if text.upper().endswith("H"):
        return int(text[:-1], 16)
    return int(text, 10)


def _check_size(reg: RegisterDef, data: bytes) -> bytes:
    if reg.size is not None and len(data) != reg.size:
        raise ValueError(f"{reg.name} 需要 {reg.size} 字节，当前 {len(data)} 字节")
    return data


def _register_from_json(item: dict[str, Any], base: RegisterDef | None) -> RegisterDef:
    address = _parse_address(item["address"])
    if base:
        reg = replace(base)
        updates = {
            "address": address,
            "size": item.get("size", reg.size),
            "name": item.get("name", reg.name),
            "access": item.get("access", reg.access),
            "data_type": item.get("type", reg.data_type),
            "description": item.get("description", reg.description),
            "scale": item.get("scale", reg.scale),
            "unit": item.get("unit", reg.unit),
            "count": item.get("count", reg.count),
            "signed": item.get("signed", reg.signed),
        }
        enum = item.get("enum")
        if enum is not None:
            updates["enum"] = {int(k, 0) if isinstance(k, str) else int(k): v for k, v in enum.items()}
        return replace(reg, **updates)
    enum_payload = item.get("enum")
    enum = None
    if isinstance(enum_payload, dict):
        enum = {int(k, 0) if isinstance(k, str) else int(k): str(v) for k, v in enum_payload.items()}
    return RegisterDef(
        address=address,
        size=item.get("size"),
        name=item.get("name", f"{address:04X}H"),
        access=item.get("access", "R/W"),
        data_type=item.get("type", "raw"),
        description=item.get("description", ""),
        enum=enum,
        scale=item.get("scale"),
        unit=item.get("unit", ""),
        count=item.get("count"),
        signed=item.get("signed", False),
    )


def _parse_address(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = value.strip().upper()
    if text.endswith("H"):
        text = text[:-1]
    if text.startswith("0X"):
        return int(text, 16)
    return int(text, 16)


def _bcd_byte(value: int) -> int:
    return (value >> 4) * 10 + (value & 0x0F)


BUILTIN_REGISTERS = [
    RegisterDef(0x0001, 2, "流道规格", "R/W", "u16", "口径，例如 DN15 写入 15 或 000F。", {15: "DN15", 20: "DN20"}),
    RegisterDef(0x0002, 2, "量程比", "R/W", "u16", "量程比，例如 R400 写入 400。", {250: "R250", 315: "R315", 400: "R400", 500: "R500", 630: "R630", 800: "R800", 1000: "R1000"}),
    RegisterDef(0x0003, 8, "始动流量", "R/W", "array_u32", "测试模式、标准模式始动流量；单位 0.01L/h。", scale=0.01, unit="L/h"),
    RegisterDef(0x0004, 1, "电子铅封", "R/W", "u8", "0x00 未封印；0x5A 封印。", {0x00: "未封印", 0x5A: "封印"}),
    RegisterDef(0x0005, 5, "软件版本", "R", "bcd", "BCD 码。"),
    RegisterDef(0x0006, 1, "温压使能", "R/W", "temperature_pressure_enable", "0x00 全禁能；0x01 温度；0x10 压力；0x11 全使能。"),
    RegisterDef(0x0007, 1, "扰流修正使能", "R/W", "u8", "0x00 禁能；0x01 使能。", {0x00: "禁能", 0x01: "使能"}),
    RegisterDef(0x0009, 1, "声道数", "R/W", "u8", "0x01~0x04。", {1: "1个声道", 2: "2个声道", 3: "3个声道", 4: "4个声道"}),
    RegisterDef(0x000A, 7, "板号地址", "R/W", "board_id", "7 字节地址编码。"),
    RegisterDef(0x000B, None, "读取存储数据", "R", "raw", "1字节长度 + 4字节存储地址 + N字节数据。"),
    RegisterDef(0x0102, 1, "工作模式", "R/W", "u8", "0x00 标准模式；0x01 测试模式。", {0x00: "标准模式", 0x01: "测试模式"}),
    RegisterDef(0x0103, 1, "触发零漂自学习", "R/W", "u8", "0x00 不启动；0x01 启动。", {0x00: "不启动", 0x01: "启动"}),
    RegisterDef(0x0104, 32, "零漂参数", "R/W", "zero_drift", "4个飞行时间差修正(ps) + 4个上下行飞行时间修正(ns)。"),
    RegisterDef(0x0106, 1, "零漂学习状态", "R", "u8", "0x00 初始化；0x01 成功；0x02 失败。", {0x00: "初始化", 0x01: "成功", 0x02: "失败"}),
    RegisterDef(0x0201, 40, "正向修正流量点", "R/W", "array_i32", "10个修正点，单位 0.01L/h。", scale=0.01, unit="L/h"),
    RegisterDef(0x0202, 40, "正向修正量", "R/W", "array_i32", "10个修正量，单位 1e-5。", scale=1e-5),
    RegisterDef(0x0203, 40, "反向修正流量点", "R/W", "array_i32", "10个修正点，单位 0.01L/h。", scale=0.01, unit="L/h"),
    RegisterDef(0x0204, 40, "反向修正量", "R/W", "array_i32", "10个修正量，单位 1e-5。", scale=1e-5),
    RegisterDef(0x0205, 4, "设备台差修正", "R/W", "i32", "单位 1e-5。", scale=1e-5),
    RegisterDef(0x0301, 48, "飞行时间参数", "R", "tof_params", "上行4个(ns)、下行4个(ns)、飞行时间差4个(ps)。"),
    RegisterDef(0x0302, 16, "声道幅值", "R", "amplitudes", "依次为1~4声道上、下行幅值。"),
    RegisterDef(0x0303, 1, "流向状态", "R", "u8", "0x00 初始化；0x01 正向；0x02 反向。", {0x00: "初始化数据", 0x01: "正向", 0x02: "反向"}),
    RegisterDef(0x0304, 8, "正累积", "R/W", "u64", "单位 1e-8m³。", scale=1e-8, unit="m³"),
    RegisterDef(0x0305, 8, "负累积", "R/W", "u64", "单位 1e-8m³。", scale=1e-8, unit="m³"),
    RegisterDef(0x0306, 8, "净累计", "R", "u64", "单位 1e-8m³。", scale=1e-8, unit="m³"),
    RegisterDef(0x0307, 4, "流量", "R", "i32", "单位 0.01L/h。", scale=0.01, unit="L/h"),
    RegisterDef(0x0308, 4, "计量状态字", "R", "meter_status", "按状态位解析异常。"),
    RegisterDef(0x0309, 1, "温压状态字", "R", "temperature_pressure_status", "bit0 温度异常；bit1 压力异常。"),
    RegisterDef(0x030A, 2, "温度", "R", "i16", "单位 0.01℃。", scale=0.01, unit="℃"),
    RegisterDef(0x030B, 2, "压力", "R", "i16", "单位 0.001MPa。", scale=0.001, unit="MPa"),
    RegisterDef(0x030C, 2, "压力采集周期", "R/W", "u16", "单位 min，范围 1~65535。", unit="min"),
    RegisterDef(0x1001, 2, "清除电子铅封", "R/W", "u16", "写入 0x55AA 清除电子封印。", {0x55AA: "清除电子封印"}),
    RegisterDef(0x1002, 6, "跳转升级", "W", "raw", "0x5A + 固定密码 43 48 49 4E 54。"),
]
