from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import sys
import time
from typing import Iterable


if sys.platform == "win32":
    import winreg
else:  # pragma: no cover - this application targets Windows.
    winreg = None


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
PURGE_RXCLEAR = 0x0008
PURGE_TXCLEAR = 0x0004

NOPARITY = 0
ODDPARITY = 1
EVENPARITY = 2
MARKPARITY = 3
SPACEPARITY = 4

ONESTOPBIT = 0
ONE5STOPBITS = 1
TWOSTOPBITS = 2


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


class DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("fBinary", wintypes.DWORD, 1),
        ("fParity", wintypes.DWORD, 1),
        ("fOutxCtsFlow", wintypes.DWORD, 1),
        ("fOutxDsrFlow", wintypes.DWORD, 1),
        ("fDtrControl", wintypes.DWORD, 2),
        ("fDsrSensitivity", wintypes.DWORD, 1),
        ("fTXContinueOnXoff", wintypes.DWORD, 1),
        ("fOutX", wintypes.DWORD, 1),
        ("fInX", wintypes.DWORD, 1),
        ("fErrorChar", wintypes.DWORD, 1),
        ("fNull", wintypes.DWORD, 1),
        ("fRtsControl", wintypes.DWORD, 2),
        ("fAbortOnError", wintypes.DWORD, 1),
        ("fDummy2", wintypes.DWORD, 17),
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", wintypes.BYTE),
        ("Parity", wintypes.BYTE),
        ("StopBits", wintypes.BYTE),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


@dataclass
class SerialConfig:
    port: str
    baudrate: int = 2400
    bytesize: int = 8
    parity: str = "E"
    stopbits: str = "1"
    timeout_ms: int = 1000


def list_serial_ports() -> list[str]:
    if sys.platform != "win32" or winreg is None:
        return []
    ports: list[str] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return []
    with key:
        index = 0
        while True:
            try:
                _name, value, _type = winreg.EnumValue(key, index)
            except OSError:
                break
            ports.append(str(value))
            index += 1
    return sorted(set(ports), key=_port_sort_key)


class SerialPort:
    def __init__(self, config: SerialConfig):
        self.config = config
        self.handle: int | None = None

    @property
    def is_open(self) -> bool:
        return self.handle not in (None, INVALID_HANDLE_VALUE)

    def open(self) -> None:
        if sys.platform != "win32":
            raise OSError("Win32 串口后端只能在 Windows 上运行")
        if self.is_open:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        path = self._device_path(self.config.port)
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise_win_error(f"无法打开串口 {self.config.port}")
        self.handle = handle
        try:
            self._configure(kernel32)
            self._set_timeouts(kernel32, self.config.timeout_ms)
            kernel32.PurgeComm(self.handle, PURGE_RXCLEAR | PURGE_TXCLEAR)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self.is_open:
            self.handle = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(self.handle)
        self.handle = None

    def write(self, data: bytes | bytearray | Iterable[int]) -> int:
        self._ensure_open()
        payload = bytes(data)
        written = wintypes.DWORD(0)
        ok = ctypes.WinDLL("kernel32", use_last_error=True).WriteFile(
            self.handle,
            payload,
            len(payload),
            ctypes.byref(written),
            None,
        )
        if not ok:
            raise_win_error("串口写入失败")
        return int(written.value)

    def read(self, size: int = 4096) -> bytes:
        self._ensure_open()
        buffer = ctypes.create_string_buffer(size)
        read_count = wintypes.DWORD(0)
        ok = ctypes.WinDLL("kernel32", use_last_error=True).ReadFile(
            self.handle,
            buffer,
            size,
            ctypes.byref(read_count),
            None,
        )
        if not ok:
            raise_win_error("串口读取失败")
        return buffer.raw[: read_count.value]

    def transact(self, request: bytes, wait_ms: int) -> bytes:
        self._ensure_open()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PurgeComm(self.handle, PURGE_RXCLEAR | PURGE_TXCLEAR)
        self.write(request)
        time.sleep(max(0, wait_ms) / 1000)
        chunks: list[bytes] = []
        quiet_deadline = time.monotonic() + 0.2
        while True:
            chunk = self.read(4096)
            if chunk:
                chunks.append(chunk)
                quiet_deadline = time.monotonic() + 0.2
                continue
            if time.monotonic() >= quiet_deadline:
                break
        return b"".join(chunks)

    def _configure(self, kernel32: ctypes.WinDLL) -> None:
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not kernel32.GetCommState(self.handle, ctypes.byref(dcb)):
            raise_win_error("读取串口配置失败")
        dcb.BaudRate = int(self.config.baudrate)
        dcb.ByteSize = int(self.config.bytesize)
        dcb.Parity = _parity_value(self.config.parity)
        dcb.StopBits = _stopbits_value(self.config.stopbits)
        dcb.fBinary = 1
        dcb.fParity = 0 if dcb.Parity == NOPARITY else 1
        dcb.fOutxCtsFlow = 0
        dcb.fOutxDsrFlow = 0
        dcb.fOutX = 0
        dcb.fInX = 0
        dcb.fDtrControl = 1
        dcb.fRtsControl = 1
        if not kernel32.SetCommState(self.handle, ctypes.byref(dcb)):
            raise_win_error("设置串口配置失败")

    def _set_timeouts(self, kernel32: ctypes.WinDLL, timeout_ms: int) -> None:
        timeouts = COMMTIMEOUTS(
            ReadIntervalTimeout=50,
            ReadTotalTimeoutMultiplier=0,
            ReadTotalTimeoutConstant=max(1, int(timeout_ms)),
            WriteTotalTimeoutMultiplier=0,
            WriteTotalTimeoutConstant=max(1, int(timeout_ms)),
        )
        if not kernel32.SetCommTimeouts(self.handle, ctypes.byref(timeouts)):
            raise_win_error("设置串口超时失败")

    def _ensure_open(self) -> None:
        if not self.is_open:
            raise OSError("串口未打开")

    @staticmethod
    def _device_path(port: str) -> str:
        port = port.strip()
        if not port:
            raise ValueError("请选择串口")
        if port.upper().startswith("\\\\.\\"):
            return port
        return rf"\\.\{port}"


def _parity_value(value: str) -> int:
    parity = value.strip().upper()
    mapping = {
        "N": NOPARITY,
        "NONE": NOPARITY,
        "无": NOPARITY,
        "E": EVENPARITY,
        "EVEN": EVENPARITY,
        "偶": EVENPARITY,
        "O": ODDPARITY,
        "ODD": ODDPARITY,
        "奇": ODDPARITY,
        "M": MARKPARITY,
        "S": SPACEPARITY,
    }
    if parity not in mapping:
        raise ValueError(f"不支持的校验位：{value}")
    return mapping[parity]


def _stopbits_value(value: str) -> int:
    stopbits = str(value).strip()
    mapping = {"1": ONESTOPBIT, "1.5": ONE5STOPBITS, "2": TWOSTOPBITS}
    if stopbits not in mapping:
        raise ValueError(f"不支持的停止位：{value}")
    return mapping[stopbits]


def _port_sort_key(port: str) -> tuple[str, int]:
    head = "".join(ch for ch in port if not ch.isdigit())
    digits = "".join(ch for ch in port if ch.isdigit())
    return (head, int(digits or 0))


def raise_win_error(message: str) -> None:
    error_code = ctypes.get_last_error()
    raise OSError(error_code, f"{message}：{ctypes.FormatError(error_code)}")
