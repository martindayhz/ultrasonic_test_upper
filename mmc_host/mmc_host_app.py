from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from protocol import (
    CMD_SLAVE_READ_RESPONSE,
    CMD_SLAVE_WRITE_RESPONSE,
    build_meter_number_request,
    build_meter_read_frame,
    build_meter_write_frame,
    build_passthrough_frame,
    bytes_to_hex,
    extract_meter_payload,
    hex_to_bytes,
    parse_meter_frame,
    parse_meter_number_response,
)
from registers import (
    decode_register_value,
    encode_register_value,
    load_registers,
    make_unknown_register,
    parse_register_address,
)
from serial_win32 import SerialConfig, SerialPort, list_serial_ports


class MMC_HOSTApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("透传计量上位机")
        self.minsize(980, 700)
        self.geometry("1120x760")

        self.registers = load_registers(Path(__file__).with_name("registers.json"))
        self.serial_port: SerialPort | None = None

        self.port_var = tk.StringVar()
        self.baudrate_var = tk.StringVar(value="2400")
        self.bytesize_var = tk.StringVar(value="8")
        self.parity_var = tk.StringVar(value="E")
        self.stopbits_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="1000")
        self.meter_number_var = tk.StringVar()
        self.use_passthrough_var = tk.BooleanVar(value=True)
        self.sequence_var = tk.StringVar(value="1")
        self.register_var = tk.StringVar()
        self.write_value_var = tk.StringVar(value="15")
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()
        self.refresh_ports()
        self._select_default_register()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        serial_frame = ttk.LabelFrame(self, text="串口")
        serial_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        for col in range(14):
            serial_frame.columnconfigure(col, weight=0)
        serial_frame.columnconfigure(13, weight=1)

        ttk.Label(serial_frame, text="端口").grid(row=0, column=0, padx=6, pady=8)
        self.port_combo = ttk.Combobox(serial_frame, textvariable=self.port_var, width=12, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=4, pady=8)
        ttk.Button(serial_frame, text="刷新", command=self.refresh_ports).grid(row=0, column=2, padx=4, pady=8)

        ttk.Label(serial_frame, text="波特率").grid(row=0, column=3, padx=(16, 4), pady=8)
        ttk.Combobox(
            serial_frame,
            textvariable=self.baudrate_var,
            width=8,
            values=("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"),
        ).grid(row=0, column=4, padx=4, pady=8)

        ttk.Label(serial_frame, text="数据位").grid(row=0, column=5, padx=(12, 4), pady=8)
        ttk.Combobox(serial_frame, textvariable=self.bytesize_var, width=4, values=("7", "8")).grid(row=0, column=6, padx=4, pady=8)

        ttk.Label(serial_frame, text="校验位").grid(row=0, column=7, padx=(12, 4), pady=8)
        ttk.Combobox(serial_frame, textvariable=self.parity_var, width=5, values=("E", "N", "O")).grid(row=0, column=8, padx=4, pady=8)

        ttk.Label(serial_frame, text="停止位").grid(row=0, column=9, padx=(12, 4), pady=8)
        ttk.Combobox(serial_frame, textvariable=self.stopbits_var, width=5, values=("1", "1.5", "2")).grid(row=0, column=10, padx=4, pady=8)

        ttk.Label(serial_frame, text="等待ms").grid(row=0, column=11, padx=(12, 4), pady=8)
        ttk.Entry(serial_frame, textvariable=self.timeout_var, width=7).grid(row=0, column=12, padx=4, pady=8)
        self.open_button = ttk.Button(serial_frame, text="打开串口", command=self.toggle_serial)
        self.open_button.grid(row=0, column=13, padx=8, pady=8, sticky="e")

        meter_frame = ttk.LabelFrame(self, text="表号与透传")
        meter_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
        meter_frame.columnconfigure(4, weight=1)
        ttk.Button(meter_frame, text="读取表号", command=self.read_meter_number).grid(row=0, column=0, padx=6, pady=8)
        ttk.Label(meter_frame, text="表号").grid(row=0, column=1, padx=(10, 4), pady=8)
        ttk.Entry(meter_frame, textvariable=self.meter_number_var, width=26).grid(row=0, column=2, padx=4, pady=8)
        ttk.Checkbutton(meter_frame, text="包透传协议", variable=self.use_passthrough_var).grid(row=0, column=3, padx=14, pady=8)
        ttk.Label(meter_frame, text="序号").grid(row=0, column=4, sticky="e", padx=(10, 4), pady=8)
        ttk.Entry(meter_frame, textvariable=self.sequence_var, width=6).grid(row=0, column=5, padx=6, pady=8)

        operation_frame = ttk.LabelFrame(self, text="寄存器读写")
        operation_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        operation_frame.columnconfigure(1, weight=1)

        ttk.Label(operation_frame, text="寄存器").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.register_combo = ttk.Combobox(operation_frame, textvariable=self.register_var, values=self._register_labels())
        self.register_combo.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        self.register_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_register_change())
        self.register_combo.bind("<FocusOut>", lambda _event: self.on_register_change())

        self.read_button = ttk.Button(operation_frame, text="读取", command=self.read_register)
        self.read_button.grid(row=0, column=2, padx=6, pady=8)
        ttk.Label(operation_frame, text="写入值").grid(row=0, column=3, padx=(12, 4), pady=8)
        ttk.Entry(operation_frame, textvariable=self.write_value_var, width=24).grid(row=0, column=4, padx=4, pady=8)
        self.write_button = ttk.Button(operation_frame, text="写入", command=self.write_register)
        self.write_button.grid(row=0, column=5, padx=6, pady=8)

        self.register_desc_var = tk.StringVar(value="")
        ttk.Label(operation_frame, textvariable=self.register_desc_var, wraplength=900, foreground="#444").grid(
            row=1, column=0, columnspan=6, padx=6, pady=(0, 8), sticky="ew"
        )

        content_frame = ttk.Frame(self)
        content_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=6)
        content_frame.columnconfigure(0, weight=1)
        content_frame.rowconfigure(1, weight=1)

        result_frame = ttk.LabelFrame(content_frame, text="解析结果")
        result_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        result_frame.columnconfigure(0, weight=1)
        self.result_text = scrolledtext.ScrolledText(result_frame, height=8, wrap="word")
        self.result_text.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        log_frame = ttk.LabelFrame(content_frame, text="收发日志")
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        log_buttons = ttk.Frame(log_frame)
        log_buttons.grid(row=1, column=0, sticky="e", padx=6, pady=(0, 6))
        ttk.Button(log_buttons, text="清空日志", command=self.clear_log).grid(row=0, column=0, padx=4)
        ttk.Button(log_buttons, text="导出日志", command=self.export_log).grid(row=0, column=1, padx=4)

        status = ttk.Label(self, textvariable=self.status_var, anchor="w")
        status.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))

    def _register_labels(self) -> list[str]:
        return [self.registers[address].combo_label for address in sorted(self.registers)]

    def _select_default_register(self) -> None:
        labels = self._register_labels()
        if labels:
            self.register_var.set(labels[0])
        self.on_register_change()

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        self.set_status("已刷新串口列表" if ports else "未发现串口，可手动排查设备连接")

    def toggle_serial(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            self.serial_port = None
            self.open_button.configure(text="打开串口")
            self.set_status("串口已关闭")
            self.log("串口已关闭")
            return
        try:
            config = self._serial_config()
            port = SerialPort(config)
            port.open()
        except Exception as exc:  # noqa: BLE001 - show exact serial error to user.
            messagebox.showerror("串口错误", str(exc))
            self.set_status("打开串口失败")
            return
        self.serial_port = port
        self.open_button.configure(text="关闭串口")
        self.set_status(f"已打开 {config.port}，{config.baudrate}/{config.bytesize}/{config.parity}/{config.stopbits}")
        self.log(f"串口已打开：{config.port}，{config.baudrate}/{config.bytesize}/{config.parity}/{config.stopbits}")

    def read_meter_number(self) -> None:
        request = build_meter_number_request()
        response = self._transact(request)
        if response is None:
            return
        if not response:
            self.show_result("读取表号无回包")
            return
        try:
            parsed = parse_meter_number_response(response)
        except Exception as exc:  # noqa: BLE001
            self.show_result(f"表号解析失败：{exc}\n原始应答：{bytes_to_hex(response)}")
            return
        self.meter_number_var.set(bytes_to_hex(parsed.meter_number))
        lines = [
            "表号读取成功",
            f"表号：{bytes_to_hex(parsed.meter_number)}",
            f"校验：{'正常' if parsed.checksum_ok else '异常'}",
        ]
        lines.extend(parsed.warnings)
        self.show_result("\n".join(lines))
        self.log("表号：" + bytes_to_hex(parsed.meter_number))

    def read_register(self) -> None:
        try:
            reg = self.selected_register()
            meter_frame = build_meter_read_frame(reg.address)
            request = self.wrap_if_needed(meter_frame)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取参数错误", str(exc))
            return
        response = self._transact(request)
        if response is None:
            return
        if not response:
            self.show_result("读取寄存器无回包")
            return
        try:
            frame, outer_lines = self.parse_response_meter_frame(response, CMD_SLAVE_READ_RESPONSE)
            decoded = decode_register_value(reg, frame.data)
            lines = outer_lines + [
                "读寄存器应答",
                f"功能码：{frame.command:02X} ({'应答主站读取' if frame.command == CMD_SLAVE_READ_RESPONSE else '不匹配'})",
                f"寄存器：{frame.register_address:04X}H",
                f"原始数据：{bytes_to_hex(frame.data)}",
                f"解析值：{decoded}",
                f"计量帧校验：{'正常' if frame.checksum_ok else '异常'}",
            ]
            if frame.register_address != reg.address:
                lines.append(f"警告：应答寄存器 {frame.register_address:04X}H 与请求 {reg.address:04X}H 不一致")
            lines.extend(frame.warnings)
            self.show_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self.show_result(f"读应答解析失败：{exc}\n原始应答：{bytes_to_hex(response)}")

    def write_register(self) -> None:
        try:
            reg = self.selected_register()
            if not reg.can_write:
                raise ValueError(f"{reg.address_label} {reg.name} 不支持写入")
            payload = encode_register_value(reg, self.write_value_var.get())
            meter_frame = build_meter_write_frame(reg.address, payload)
            request = self.wrap_if_needed(meter_frame)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("写入参数错误", str(exc))
            return
        response = self._transact(request)
        if response is None:
            return
        if not response:
            self.show_result("写寄存器无回包")
            return
        try:
            frame, outer_lines = self.parse_response_meter_frame(response, CMD_SLAVE_WRITE_RESPONSE)
            ok = frame.command == CMD_SLAVE_WRITE_RESPONSE and frame.register_address == reg.address
            lines = outer_lines + [
                "写寄存器应答",
                f"功能码：{frame.command:02X} ({'从站设置参数响应' if frame.command == CMD_SLAVE_WRITE_RESPONSE else '不匹配'})",
                f"寄存器：{frame.register_address:04X}H",
                f"写入数据：{bytes_to_hex(payload)}",
                f"结果：{'设置成功' if ok else '需检查应答'}",
                f"计量帧校验：{'正常' if frame.checksum_ok else '异常'}",
            ]
            if frame.data:
                lines.append(f"应答附加数据：{bytes_to_hex(frame.data)}")
            if frame.register_address != reg.address:
                lines.append(f"警告：应答寄存器 {frame.register_address:04X}H 与请求 {reg.address:04X}H 不一致")
            lines.extend(frame.warnings)
            self.show_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self.show_result(f"写应答解析失败：{exc}\n原始应答：{bytes_to_hex(response)}")

    def parse_response_meter_frame(self, response: bytes, expected_command: int):
        payload, outer = extract_meter_payload(response)
        lines: list[str] = []
        if outer:
            lines.extend(
                [
                    "透传外层应答",
                    f"表号：{bytes_to_hex(outer.meter_number)}",
                    f"控制码：{outer.control:02X}",
                    f"透传长度：{outer.passthrough_length}",
                    f"外层校验：{'正常' if outer.checksum_ok else '异常'}",
                ]
            )
            lines.extend(outer.warnings)
        frame = parse_meter_frame(payload, expected_command=expected_command)
        return frame, lines

    def wrap_if_needed(self, meter_frame: bytes) -> bytes:
        if not self.use_passthrough_var.get():
            return meter_frame
        meter_number = hex_to_bytes(self.meter_number_var.get())
        if len(meter_number) != 7:
            raise ValueError("包透传协议时，表号必须为 7 字节；请先读取表号或手动输入")
        sequence = int(self.sequence_var.get().strip() or "1", 0)
        return build_passthrough_frame(meter_frame, meter_number, sequence=sequence)

    def selected_register(self):
        address = parse_register_address(self.register_var.get())
        return self.registers.get(address, make_unknown_register(address))

    def on_register_change(self) -> None:
        try:
            reg = self.selected_register()
            desc = f"{reg.address_label} {reg.name}，{reg.access}，类型 {reg.data_type}，长度 {'N' if reg.size is None else reg.size} 字节。{reg.description}"
            self.register_desc_var.set(desc)
            self.read_button.configure(state="normal" if reg.can_read else "disabled")
            self.write_button.configure(state="normal" if reg.can_write else "disabled")
        except Exception:
            self.register_desc_var.set("可输入未知寄存器地址，例如 1234H；未知寄存器按原始 HEX 处理。")
            self.read_button.configure(state="normal")
            self.write_button.configure(state="normal")

    def _serial_config(self) -> SerialConfig:
        return SerialConfig(
            port=self.port_var.get().strip(),
            baudrate=int(self.baudrate_var.get()),
            bytesize=int(self.bytesize_var.get()),
            parity=self.parity_var.get().strip().upper(),
            stopbits=self.stopbits_var.get().strip(),
            timeout_ms=int(self.timeout_var.get()),
        )

    def _transact(self, request: bytes) -> bytes | None:
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("串口未打开", "请先打开串口")
            return None
        wait_ms = int(self.timeout_var.get())
        self.log("TX: " + bytes_to_hex(request))
        try:
            response = self.serial_port.transact(request, wait_ms)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("通信失败", str(exc))
            self.set_status("通信失败")
            return None
        self.log("RX: " + (bytes_to_hex(response) if response else "(无回包)"))
        self.set_status("通信完成")
        return response

    def show_result(self, text: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, text)
        self.result_text.configure(state="disabled")
        self.log("解析结果：\n" + text)

    def log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.insert(tk.END, f"[{timestamp}] {text}\n")
        self.log_text.see(tk.END)

    def clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def export_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            filetypes=(("Text", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        Path(path).write_text(self.log_text.get("1.0", tk.END), encoding="utf-8")
        self.set_status(f"日志已导出：{path}")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def destroy(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        super().destroy()


def main() -> None:
    app = MMC_HOSTApp()
    app.mainloop()


if __name__ == "__main__":
    main()
