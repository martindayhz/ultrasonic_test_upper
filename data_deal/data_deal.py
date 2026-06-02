import pandas as pd
import matplotlib
matplotlib.use('TkAgg') 
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector
import struct
import re
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
import os
import traceback
import math
import numpy as np

# --- 配置 Matplotlib 全局渲染加速 ---
plt.rcParams['path.simplify'] = True           
plt.rcParams['path.simplify_threshold'] = 1.0  
plt.rcParams['agg.path.chunksize'] = 10000     

# --- 核心算法：向量化滑动去极值均值滤波 ---
def fast_rolling_trimmed_mean(series, window=200, trim_ratio=0.2):
    arr = series.values
    n = len(arr)
    res = np.empty(n)
    
    if n == 0: return series
    
    for i in range(1, min(window, n + 1)):
        sub_arr = arr[:i]
        trim = int(i * trim_ratio)
        if trim > 0:
            res[i-1] = np.mean(np.sort(sub_arr)[trim:-trim])
        else:
            res[i-1] = np.mean(sub_arr)
            
    if n <= window:
        return pd.Series(res, index=series.index)
        
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(arr, window_shape=window)
    windows_sorted = np.sort(windows, axis=1)
    
    trim = int(window * trim_ratio)
    trimmed_means = np.mean(windows_sorted[:, trim:-trim], axis=1)
    res[window-1:] = trimmed_means
    
    return pd.Series(res, index=series.index)


class UltrasoundMasterAppV39:
    def __init__(self, root):
        self.root = root
        self.root.title("超声波数据解析上位机 V39.0 - 坐标轴自定义版")
        self.root.geometry("1400x550") # 加宽以容纳新的横纵坐标列
        
        self.raw_data_bytes = b""
        self.df = None
        self.current_folder = ""
        self.current_filename = "未加载文件"
        
        # --- 1. 协议配置区域 ---
        config_frame = tk.LabelFrame(root, text="协议帧定义 (Figure ID 相同则合并显示；双击任意行修改详细图表属性)", padx=10, pady=10)
        config_frame.pack(fill="x", padx=10, pady=5)
        
        top_settings = tk.Frame(config_frame)
        top_settings.pack(fill="x", pady=5)
        tk.Label(top_settings, text="帧头(Hex):").grid(row=0, column=0)
        self.ent_head = tk.Entry(top_settings, width=12); self.ent_head.insert(0, "FEFEFEFE")
        self.ent_head.grid(row=0, column=1, padx=5)
        
        tk.Label(top_settings, text="总帧长(Byte):").grid(row=0, column=2)
        self.ent_len = tk.Entry(top_settings, width=8); self.ent_len.insert(0, "64")
        self.ent_len.grid(row=0, column=3, padx=5)

        tk.Button(top_settings, text="💾 导出配置", command=self.export_config).grid(row=0, column=4, padx=15)
        tk.Button(top_settings, text="📂 导入配置", command=self.import_config).grid(row=0, column=5)

        # --- 核心新增：增加 xlabel 和 ylabel 列 ---
        cols = ("name", "offset", "size", "type", "scale", "unit", "fig_id", "title", "xlabel", "ylabel")
        self.tree = ttk.Treeview(config_frame, columns=cols, show='headings', height=6)
        self.tree.heading("name", text="变量名称"); self.tree.heading("offset", text="偏移")
        self.tree.heading("size", text="长度"); self.tree.heading("type", text="数据类型")
        self.tree.heading("scale", text="缩放系数"); self.tree.heading("unit", text="单位")
        self.tree.heading("fig_id", text="Fig ID"); self.tree.heading("title", text="图表大标题")
        self.tree.heading("xlabel", text="横轴(X)名称"); self.tree.heading("ylabel", text="纵轴(Y)名称")
        
        widths = {"name": 100, "offset": 50, "size": 50, "type": 150, "scale": 60, "unit": 60, "fig_id": 50, "title": 180, "xlabel": 100, "ylabel": 100}
        for c in cols: self.tree.column(c, width=widths.get(c, 100), anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        
        # 绑定双击事件打开综合编辑面板
        self.tree.bind("<Double-1>", self.on_double_click)

        btn_edit_pnl = tk.Frame(config_frame)
        btn_edit_pnl.pack(side="right", padx=5)
        tk.Button(btn_edit_pnl, text="新增字段", command=self.add_row, width=10).pack(pady=2)
        tk.Button(btn_edit_pnl, text="删除选中", command=self.del_row, width=10).pack(pady=2)
        
        self.init_defaults()

        # --- 2. 控制与交互分析区域 ---
        ctrl_frame = tk.Frame(root, pady=10)
        ctrl_frame.pack(fill="x")

        info_frame = tk.Frame(ctrl_frame)
        info_frame.pack(side="top", fill="x", padx=20)
        
        tk.Label(info_frame, text="选择文件:", font=("Arial", 10, "bold")).pack(side="left")
        self.cmb_files = ttk.Combobox(info_frame, state="readonly", width=65, font=("Arial", 9))
        self.cmb_files.pack(side="left", padx=10)
        self.cmb_files.bind("<<ComboboxSelected>>", self.on_file_selected)
        
        flow_frame = tk.Frame(info_frame)
        flow_frame.pack(side="right", padx=5)
        tk.Label(flow_frame, text="声程(mm)=", font=("Arial", 9, "bold")).pack(side="left")
        self.ent_path = tk.Entry(flow_frame, width=5, font=("Arial", 9)); self.ent_path.insert(0, "72.3"); self.ent_path.pack(side="left", padx=(0, 8))
        tk.Label(flow_frame, text="角度系数=", font=("Arial", 9, "bold")).pack(side="left")
        self.ent_angle = tk.Entry(flow_frame, width=5, font=("Arial", 9)); self.ent_angle.insert(0, "2"); self.ent_angle.pack(side="left", padx=(0, 8))
        tk.Label(flow_frame, text="半径(mm)=", font=("Arial", 9, "bold")).pack(side="left")
        self.ent_radius = tk.Entry(flow_frame, width=5, font=("Arial", 9)); self.ent_radius.insert(0, "5"); self.ent_radius.pack(side="left", padx=(0, 15))
        tk.Label(flow_frame, text="零点=", font=("Arial", 9, "bold"), fg="#1976D2").pack(side="left")
        self.ent_zero = tk.Entry(flow_frame, width=6, font=("Arial", 9)); self.ent_zero.insert(0, "0.0"); self.ent_zero.pack(side="left")

        btns_frame = tk.Frame(ctrl_frame)
        btns_frame.pack(side="top", pady=10)
        self.btn_read = tk.Button(btns_frame, text="1. 打开文件夹", command=self.load_folder, bg="#9E9E9E", fg="white", width=14, font=('Arial', 10, 'bold'))
        self.btn_read.pack(side="left", padx=8)
        self.btn_parse = tk.Button(btns_frame, text="2. 执行解析", command=self.parse_data, bg="#4CAF50", fg="white", width=14, font=('Arial', 10, 'bold'), state="disabled")
        self.btn_parse.pack(side="left", padx=8)
        self.btn_plot = tk.Button(btns_frame, text="3. 智能绘图显示", command=self.smart_plot, bg="#2196F3", fg="white", width=14, font=('Arial', 10, 'bold'), state="disabled")
        self.btn_plot.pack(side="left", padx=8)
        self.btn_flow = tk.Button(btns_frame, text="4. 框选计算流量", command=self.interactive_flow_analysis, bg="#FF9800", fg="white", width=14, font=('Arial', 10, 'bold'), state="disabled")
        self.btn_flow.pack(side="left", padx=8)
        self.btn_save_csv = tk.Button(btns_frame, text="💾 5. 导出CSV", command=self.save_csv, bg="#607D8B", fg="white", width=14, font=('Arial', 10, 'bold'), state="disabled")
        self.btn_save_csv.pack(side="left", padx=8)

    def init_defaults(self):
        # 默认增加 X 轴 "数据帧" 和 Y 轴自动带单位的设定
        defaults = [
            ("声道", "4", "1", "unsigned char (B)", "1", "-", "0", "声道监控", "数据帧", "声道号"),
            ("dtof", "5", "4", "signed int (>i)", "1", "ps", "2", "飞行时间差趋势", "数据帧", "时间差 (ps)"),
            ("up_tof", "9", "4", "unsigned int (>I)", "1", "ns", "1", "上下行TOF对比", "数据帧", "时间 (ns)"),
            ("down_tof", "13", "4", "unsigned int (>I)", "1", "ns", "1", "上下行TOF对比", "数据帧", "时间 (ns)"),
            ("amplitude", "17", "2", "unsigned short (>H)", "1", "AD", "3", "幅值监控", "数据帧", "幅值 (AD)")
        ]
        for row in defaults: self.tree.insert("", "end", values=row)

    # --- 核心重构：双击弹出综合属性编辑面板 ---
    def on_double_click(self, event):
        item = self.tree.selection()[0]
        vals = list(self.tree.item(item, 'values'))
        
        # 确保旧配置导入时补齐 10 个元素
        while len(vals) < 10:
            if len(vals) == 8: vals.extend(["数据帧", f"数值 ({vals[5]})"])
            else: vals.append("")
            
        top = tk.Toplevel(self.root)
        top.title(f"编辑变量属性: {vals[0]}")
        top.geometry("400x450")
        top.grab_set() # 模态窗口
        
        frame = tk.Frame(top, padx=20, pady=20)
        frame.pack(fill="both", expand=True)
        
        entries = {}
        labels = [
            ("变量名称 (name):", 0), ("偏移量 (offset):", 1), ("字节长度 (size):", 2),
            ("缩放系数 (scale):", 4), ("物理单位 (unit):", 5), ("显示组号 (fig_id):", 6),
            ("图表大标题 (title):", 7), ("横轴名称 (xlabel):", 8), ("纵轴名称 (ylabel):", 9)
        ]
        
        row = 0
        for text, idx in labels:
            tk.Label(frame, text=text, font=("微软雅黑", 9)).grid(row=row, column=0, sticky="e", pady=5)
            ent = tk.Entry(frame, width=25)
            ent.insert(0, vals[idx])
            ent.grid(row=row, column=1, pady=5, padx=10)
            entries[idx] = ent
            row += 1
            
        # 数据类型单独用下拉框
        tk.Label(frame, text="数据类型 (type):", font=("微软雅黑", 9, "bold"), fg="#D32F2F").grid(row=row, column=0, sticky="e", pady=5)
        types = [
            "unsigned char (B)", "unsigned short (>H)", "unsigned int (>I)", "signed int (>i)", "float (>f)",
            "signed long long (>q)", "unsigned long long (>Q)", "double (>d)"
        ]
        cb_type = ttk.Combobox(frame, values=types, state="readonly", width=23)
        cb_type.set(vals[3])
        cb_type.grid(row=row, column=1, pady=5, padx=10)
        
        def save_changes():
            new_vals = [
                entries[0].get(), entries[1].get(), entries[2].get(), cb_type.get(),
                entries[4].get(), entries[5].get(), entries[6].get(), entries[7].get(),
                entries[8].get(), entries[9].get()
            ]
            self.tree.item(item, values=new_vals)
            top.destroy()
            
        tk.Button(frame, text="保存修改", command=save_changes, bg="#4CAF50", fg="white", font=("微软雅黑", 10, "bold"), width=15).grid(row=row+1, column=0, columnspan=2, pady=20)


    def update_titles_with_filename(self):
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, 'values'))
            clean_title = vals[7].split(' - ')[-1] 
            vals[7] = f"[{self.current_filename}] - {clean_title}"
            self.tree.item(item, values=vals)

    def load_folder(self):
        folder_path = filedialog.askdirectory(title="选择包含 TXT 数据的文件夹")
        if not folder_path: return
        self.current_folder = folder_path
        txt_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.txt')]
        if not txt_files:
            messagebox.showwarning("提示", "所选文件夹中没有找到 TXT 文件！")
            return
        self.cmb_files['values'] = txt_files
        self.cmb_files.current(0)
        self.on_file_selected()

    def on_file_selected(self, event=None):
        filename = self.cmb_files.get()
        if not filename: return
        path = os.path.join(self.current_folder, filename)
        self.current_filename = filename
        self.update_titles_with_filename()
        
        try:
            temp_bytes = bytearray()
            hex_left = ""
            hex_re = re.compile(r'[^0-9a-fA-F]') 
            
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk: break
                    clean = hex_left + hex_re.sub('', chunk) 
                    if len(clean) % 2 != 0: 
                        hex_left = clean[-1]
                        clean = clean[:-1]
                    else: 
                        hex_left = ""
                    if clean: temp_bytes.extend(bytes.fromhex(clean))
                    
            self.raw_data_bytes = bytes(temp_bytes)
            self.df = None 
            self.btn_parse.config(state="normal")
            self.btn_plot.config(state="disabled")
            self.btn_flow.config(state="disabled")
            self.btn_save_csv.config(state="disabled")
            self.parse_data() 
        except Exception as e: messagebox.showerror("文件读取错误", str(e))

    def parse_data(self):
        if not self.raw_data_bytes: return
        try:
            head = bytes.fromhex(self.ent_head.get().strip())
            length = int(self.ent_len.get())
            head_len = len(head)
            
            rules = []
            for i in self.tree.get_children():
                v = self.tree.item(i)['values']
                fmt = re.search(r'\((.*?)\)', str(v[3])).group(1)
                
                try: scale_val = float(v[4])
                except ValueError: scale_val = 1.0
                
                rules.append({'name':str(v[0]), 'off':int(v[1]), 'size':int(v[2]), 'fmt':fmt, 'scale':scale_val})
            
            results = []
            cur = 0
            bytes_len = len(self.raw_data_bytes)
            valid_channels = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08 , 0x09 , 0x0A , 0x0B , 0x0C , 0x0D , 0x0E , 0x0F , 0x10 , 0x11 , 0x12 , 0x13 , 0x14 , 0x15 , 0x16 , 0x17 , 0x18 , 0x19 , 0x1A , 0x1B , 0x1C , 0x1D , 0x1E , 0x1F ,}
            
            while cur <= bytes_len - length:
                idx = self.raw_data_bytes.find(head, cur)
                if idx == -1 or idx > bytes_len - length: break
                
                channel_byte = self.raw_data_bytes[idx + head_len]
                if channel_byte not in valid_channels:
                    cur = idx + 1 
                    continue
                    
                frame = self.raw_data_bytes[idx : idx + length]
                row_data = {}
                for r in rules:
                    try: 
                        raw_val = struct.unpack(r['fmt'], frame[r['off']:r['off']+r['size']])[0]
                        row_data[r['name']] = raw_val * r['scale']
                    except: 
                        row_data[r['name']] = 0
                        
                results.append(row_data)
                cur = idx + length
                
            self.df = pd.DataFrame(results)
            self.btn_plot.config(state="normal")
            self.btn_flow.config(state="normal")
            self.btn_save_csv.config(state="normal")
        except Exception as e: messagebox.showerror("解析失败", str(e))

    def save_csv(self):
        if self.df is None or self.df.empty:
            messagebox.showwarning("提示", "当前没有可导出的解析数据！")
            return
            
        base_name = os.path.splitext(self.current_filename)[0]
        default_name = f"{base_name}_解析结果.csv"
        
        path = filedialog.asksaveasfilename(
            title="保存解析数据",
            defaultextension=".csv", 
            initialfile=default_name, 
            filetypes=[("CSV 表格文件", "*.csv")]
        )
        
        if path:
            try:
                self.df.to_csv(path, index=False, encoding='utf-8-sig')
                messagebox.showinfo("成功", f"全量数据已成功保存至：\n{path}")
            except Exception as e:
                messagebox.showerror("导出失败", f"保存 CSV 文件时发生错误：\n{str(e)}")

    def smart_plot(self):
        try:
            if self.df is None or self.df.empty: return
            plt.close('all')
            plt.rcParams['font.sans-serif'] = ['SimHei']; plt.rcParams['axes.unicode_minus'] = False
            plt.rcParams['lines.linewidth'] = 0.75 
            
            groups = {}
            for i in self.tree.get_children():
                v = list(self.tree.item(i)['values'])
                
                # 兼容机制：补齐列数
                while len(v) < 10:
                    if len(v) == 8: v.extend(["数据帧", f"数值 ({v[5]})"])
                    else: v.append("")

                fid = str(v[6]).strip()
                if fid == "0" or fid.lower() == "no": continue
                title_str = str(v[7])
                
                if fid not in groups: 
                    groups[fid] = {
                        'fields': [], 
                        'plot_title': title_str,
                        'xlabel': str(v[8]), # 取首个变量的 x/y 轴标签
                        'ylabel': str(v[9])
                    }
                groups[fid]['fields'].append({'name':str(v[0]), 'unit':str(v[5])})

            for fid, g in groups.items():
                plt.figure(f"组{fid}", figsize=(6, 5))
                for f in g['fields']:
                    data = self.df[f['name']]
                    if 'dtof' in f['name'].lower():
                        plt.plot(data, color='blue', alpha=0.4, label='原始', linewidth=0.4)
                        filtered_data = fast_rolling_trimmed_mean(data, window=200, trim_ratio=0.2)
                        plt.plot(filtered_data, color='black', label='去极值滑动均值(200点)')
                    else:
                        plt.plot(data, label=f['name'])
                        
                # --- 核心更新：使用用户自定义的 xlabel 和 ylabel ---
                plt.xlabel(g['xlabel'])
                plt.ylabel(g['ylabel'])
                plt.title(g['plot_title'])
                plt.legend(); plt.grid(True, alpha=0.4)
                
            plt.tight_layout()
            plt.show()
        except Exception:
            messagebox.showerror("绘图错误", traceback.format_exc())

    def interactive_flow_analysis(self):
        if self.df is None or self.df.empty: return
        
        col_dtof, col_up, col_down = None, None, None
        
        # 尝试通过配置表找到对应的自定义标签
        dtof_ylabel, tof_ylabel = "时间差 (ps)", "时间 (ns)"
        xlabel_common = "数据帧"
        
        for i in self.tree.get_children():
            v = list(self.tree.item(i)['values'])
            while len(v) < 10:
                if len(v) == 8: v.extend(["数据帧", f"数值 ({v[5]})"])
                else: v.append("")
                
            name = str(v[0]).lower()
            if 'dtof' in name:
                col_dtof = str(v[0])
                xlabel_common = str(v[8])
                dtof_ylabel = str(v[9])
            elif 'up_tof' in name:
                col_up = str(v[0])
                tof_ylabel = str(v[9])
            elif 'down_tof' in name:
                col_down = str(v[0])

        # 如果配置表没找到，按底层字段去搜
        if not col_dtof:
            for col in self.df.columns:
                if 'dtof' in col.lower(): col_dtof = col; break
        if not col_up:
            for col in self.df.columns:
                if 'up_tof' in col.lower(): col_up = col; break
        if not col_down:
            for col in self.df.columns:
                if 'down_tof' in col.lower(): col_down = col; break

        if not (col_dtof and col_up and col_down):
            messagebox.showerror("匹配失败", "未在数据中找到 dtof, up_tof, down_tof 变量！")
            return

        flow_win = tk.Toplevel(self.root)
        flow_win.title(f"流量框选计算 - 批处理模式")
        flow_win.geometry("1200x750") 
        flow_win.configure(bg="white")

        plt.rcParams['font.sans-serif'] = ['SimHei']; plt.rcParams['axes.unicode_minus'] = False
        
        filtered_dtof_series = fast_rolling_trimmed_mean(self.df[col_dtof], window=200, trim_ratio=0.2)
        
        top_frame = tk.Frame(flow_win, bg="white")
        top_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        log_frame = tk.Frame(flow_win, bg="white")
        log_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=False, padx=10, pady=5)

        plot_frame = tk.Frame(top_frame, bg="white")
        plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        fig = Figure(figsize=(8, 5.5), dpi=100) 
        fig.subplots_adjust(hspace=0.3, top=0.92, bottom=0.08, left=0.1, right=0.95) 
        
        ax1 = fig.add_subplot(211)
        ax2 = fig.add_subplot(212, sharex=ax1)
        
        ax1.plot(self.df[col_dtof], color='blue', alpha=0.5, linewidth=0.5, label='dTOF原始')
        ax1.plot(filtered_dtof_series, color='black', label='滑动均值去极值(200点)', linewidth=1.5)
        ax1.set_title(f"飞行时间差 dTOF - {self.current_filename}", fontsize=11)
        # --- 核心更新：交互绘图界面也应用用户自定义标签 ---
        ax1.set_ylabel(dtof_ylabel)
        ax1.legend(loc='upper right'); ax1.grid(True, alpha=0.3)

        ax2.plot(self.df[col_up], label='Up TOF', linewidth=0.75)
        ax2.plot(self.df[col_down], label='Down TOF', linewidth=0.75)
        ax2.set_title("上下行飞行时间", fontsize=11)
        ax2.set_ylabel(tof_ylabel)
        ax2.set_xlabel(xlabel_common)
        ax2.legend(loc='upper right'); ax2.grid(True, alpha=0.3)

        canvas = FigureCanvasTkAgg(fig, master=plot_frame)
        canvas.draw()
        
        toolbar = NavigationToolbar2Tk(canvas, plot_frame)
        toolbar.update()
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.pan_active = False
        self.pan_ref_x = None
        self.pan_ref_y = None
        self.pan_ax = None
        self.pan_xlim_start = None
        self.pan_ylim_start = None

        def on_scroll(event):
            if event.inaxes not in [ax1, ax2]: return
            if toolbar.mode != '': return 

            ax = event.inaxes
            base_scale = 1.2
            if event.button == 'up': scale_factor = 1 / base_scale
            elif event.button == 'down': scale_factor = base_scale
            else: return

            xdata, ydata = event.xdata, event.ydata
            if xdata is None or ydata is None: return

            scale_x, scale_y = True, True
            if event.key in ['control', 'ctrl', 'x']: scale_y = False
            elif event.key in ['shift', 'y']: scale_x = False

            if scale_x:
                xlim = ax.get_xlim()
                new_width = (xlim[1] - xlim[0]) * scale_factor
                relx = (xlim[1] - xdata) / (xlim[1] - xlim[0])
                ax.set_xlim([xdata - new_width * (1 - relx), xdata + new_width * relx])

            if scale_y:
                ylim = ax.get_ylim()
                new_height = (ylim[1] - ylim[0]) * scale_factor
                rely = (ylim[1] - ydata) / (ylim[1] - ylim[0])
                ax.set_ylim([ydata - new_height * (1 - rely), ydata + new_height * rely])

            fig.canvas.draw_idle()
            toolbar.push_current()

        def on_press(event):
            if toolbar.mode != '': return 
            if event.button == 1 and event.inaxes in [ax1, ax2]: 
                self.pan_active = True
                self.pan_ref_x = event.x
                self.pan_ref_y = event.y
                self.pan_ax = event.inaxes
                self.pan_xlim_start = self.pan_ax.get_xlim()
                self.pan_ylim_start = self.pan_ax.get_ylim()

        def on_release(event):
            if event.button == 1 and getattr(self, 'pan_active', False): 
                self.pan_active = False
                toolbar.push_current()

        def on_motion(event):
            if not getattr(self, 'pan_active', False) or self.pan_ax is None: return
            if toolbar.mode != '': return
            if event.button == 1 and event.inaxes == self.pan_ax:
                inv = self.pan_ax.transData.inverted()
                data_x0, data_y0 = inv.transform((self.pan_ref_x, self.pan_ref_y))
                data_x1, data_y1 = inv.transform((event.x, event.y))
                dx = data_x0 - data_x1
                dy = data_y0 - data_y1
                self.pan_ax.set_xlim([self.pan_xlim_start[0] + dx, self.pan_xlim_start[1] + dx])
                self.pan_ax.set_ylim([self.pan_ylim_start[0] + dy, self.pan_ylim_start[1] + dy])
                fig.canvas.draw_idle()

        fig.canvas.mpl_connect('scroll_event', on_scroll)
        fig.canvas.mpl_connect('button_press_event', on_press)
        fig.canvas.mpl_connect('button_release_event', on_release)
        fig.canvas.mpl_connect('motion_notify_event', on_motion)

        right_frame = tk.Frame(top_frame, width=330, bg="white")
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(15, 0))
        right_frame.pack_propagate(False)

        tk.Label(right_frame, text="📊 实时计算分析报告", font=("微软雅黑", 9, "bold"), fg="#2C3E50", bg="white").pack(side=tk.TOP, anchor="w", pady=(5, 2))
        
        action_btns_frame = tk.Frame(right_frame, bg="white")
        action_btns_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 2))
        
        def append_to_log():
            if self.current_summary_str:
                log_text.insert(tk.END, self.current_summary_str + "\n")
                log_text.see(tk.END)
            else:
                messagebox.showwarning("提示", "请先在图上拉框计算出一组数据！")
                
        tk.Button(action_btns_frame, text="⬇️ 记录当前结果到下方日志", command=append_to_log, bg="#FF9800", fg="white", font=("Arial", 9, "bold"), height=2).pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        tk.Button(action_btns_frame, text="🗑️ 清空历史日志", command=lambda: log_text.delete(2.0, tk.END), bg="#EEEEEE", font=("Arial", 9)).pack(side=tk.TOP, fill=tk.X)

        self.use_filtered_dtof = tk.BooleanVar(value=False)
        cb_filter = tk.Checkbutton(right_frame, text="采用 [滑动均值去极值(200点)]", variable=self.use_filtered_dtof, font=("微软雅黑", 8, "bold"), fg="#D32F2F", bg="white")
        cb_filter.pack(side=tk.BOTTOM, anchor="w", pady=(2, 2))

        realtime_text = tk.Text(right_frame, font=("微软雅黑", 7), bg="#F8F9FA", relief=tk.FLAT, borderwidth=1, padx=8, pady=8)
        realtime_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        
        realtime_text.tag_configure("header", font=("微软雅黑", 8, "bold"), foreground="#0056b3", spacing1=2, spacing3=1)
        realtime_text.tag_configure("label", font=("微软雅黑", 7), foreground="#555555", spacing1=0)
        realtime_text.tag_configure("value", font=("Consolas", 8, "bold"), foreground="#D32F2F") 
        realtime_text.tag_configure("divider", foreground="#CCCCCC", justify="center", spacing1=2, spacing3=2)
        realtime_text.tag_configure("q_title", font=("微软雅黑", 8, "bold"), foreground="#1976D2", justify="center", spacing1=1)
        realtime_text.tag_configure("q_value", font=("Consolas", 11, "bold"), foreground="#28A745", justify="center", spacing1=1)
        realtime_text.tag_configure("guide", font=("微软雅黑", 7), foreground="#666666", spacing1=1)

        realtime_text.insert(tk.END, "💡 丝滑手势操作指南：\n", "header")
        realtime_text.insert(tk.END, "1. [纯滚轮] 以鼠标为中心双轴缩放。\n", "guide")
        realtime_text.insert(tk.END, "   [Ctrl+滚轮] 仅横向缩放 (X轴)。\n", "guide")
        realtime_text.insert(tk.END, "   [Shift+滚轮] 仅纵向缩放 (Y轴)。\n", "guide")
        realtime_text.insert(tk.END, "2. [左键] 按住随意拖拽平移波形。\n", "guide")
        realtime_text.insert(tk.END, "3. [右键] 框选平稳区间生成报告。\n", "guide")
        realtime_text.insert(tk.END, "4. [点击房子图标] 瞬间复原全部视图。\n", "guide")

        self.sel_state = {'idx_min': 0, 'idx_max': 0, 'selected': False}
        self.current_summary_str = "" 

        def execute_calculation(*args):
            if not self.sel_state['selected']: return
                
            idx_min, idx_max = self.sel_state['idx_min'], self.sel_state['idx_max']
            sub_df = self.df.iloc[idx_min:idx_max]
            sub_filtered = filtered_dtof_series.iloc[idx_min:idx_max]
            
            if self.use_filtered_dtof.get():
                mean_dtof = sub_filtered.mean(); dtof_src = "滤波去极值"
            else:
                mean_dtof = sub_df[col_dtof].mean(); dtof_src = "原始数据"
                
            mean_up = sub_df[col_up].mean()
            mean_down = sub_df[col_down].mean()
            
            try: path_len = float(self.ent_path.get())
            except: path_len = 72.3
            try: angle_coeff = float(self.ent_angle.get())
            except: angle_coeff = 2.0
            try: radius = float(self.ent_radius.get())
            except: radius = 5.0
            k_val = (path_len / angle_coeff) * math.pi * (radius ** 2) * 3600
            
            try: zero_point = float(self.ent_zero.get())
            except: zero_point = 0.0
                
            corrected_dtof = mean_dtof - zero_point
                
            if mean_up * mean_down != 0: Q = k_val * corrected_dtof / (mean_up * mean_down)
            else: Q = 0

            realtime_text.delete(1.0, tk.END)

            def insert_kv(label, val, unit=""):
                realtime_text.insert(tk.END, f"  {label:<8}", "label")
                realtime_text.insert(tk.END, f"{val}", "value")
                realtime_text.insert(tk.END, f" {unit}\n", "label")

            realtime_text.insert(tk.END, "🎯【框选区间】\n", "header")
            realtime_text.insert(tk.END, f"  {idx_min}", "value")
            realtime_text.insert(tk.END, " ~ ", "label")
            realtime_text.insert(tk.END, f"{idx_max}", "value")
            realtime_text.insert(tk.END, " 帧\n", "label")

            realtime_text.insert(tk.END, "⚙️【物理参数】\n", "header")
            insert_kv("声程:", path_len, "mm")
            insert_kv("角度:", angle_coeff)
            insert_kv("半径:", radius, "mm")
            insert_kv("计算k:", f"{k_val:.2f}")

            realtime_text.insert(tk.END, "⏱️【飞行时间差 (dTOF)】\n", "header")
            realtime_text.insert(tk.END, f"  来源:    {dtof_src}\n", "label")
            insert_kv("求均值:", f"{mean_dtof:.4f}")
            insert_kv("去零点:", f"{zero_point:.4f}")
            insert_kv("修正后:", f"{corrected_dtof:.4f}")

            realtime_text.insert(tk.END, "⏳【飞行时间 (TOF)】\n", "header")
            insert_kv("Up均值:", f"{mean_up:.4f}")
            insert_kv("Dn均值:", f"{mean_down:.4f}")

            realtime_text.insert(tk.END, "\n" + "-"*35 + "\n", "divider")
            realtime_text.insert(tk.END, "🚀 计算流量 Q (m³/h)\n", "q_title")
            realtime_text.insert(tk.END, f"{Q:.6f}\n", "q_value")

            self.current_summary_str = f"{self.current_filename}\t{idx_min}~{idx_max}\t{dtof_src}\t{mean_dtof:.4f}\t{zero_point}\t{corrected_dtof:.4f}\t{k_val:.2f}\t{Q:.6f}"

        cb_filter.config(command=execute_calculation)

        log_label = tk.Label(log_frame, text="📝 流量计算汇总日志 (全选复制至 Excel，制表符自动分列):", font=("微软雅黑", 9, "bold"), fg="#1976D2", bg="white")
        log_label.pack(anchor="w")
        
        log_text = tk.Text(log_frame, height=6, font=("Consolas", 10), bg="#FFFDE7", relief=tk.SOLID, borderwidth=1, padx=10, pady=5)
        log_text.pack(fill=tk.BOTH, expand=True)
        header = "文件名\t选区帧数\t数据源\t原始dTOF\t零点\t修正dTOF\tk系数\t计算流量(Q)\n"
        log_text.insert(tk.END, header)

        def onselect(xmin, xmax):
            idx_min, idx_max = max(0, int(xmin)), min(len(self.df)-1, int(xmax))
            if idx_min >= idx_max: return
            self.sel_state.update({'idx_min': idx_min, 'idx_max': idx_max, 'selected': True})
            execute_calculation()

        self.span1 = SpanSelector(ax1, onselect, 'horizontal', useblit=True, props=dict(alpha=0.2, facecolor='red'), button=3)
        self.span2 = SpanSelector(ax2, onselect, 'horizontal', useblit=True, props=dict(alpha=0.2, facecolor='red'), button=3)

    # --- 核心更新：导出/导入配置的全面向下兼容 ---
    def export_config(self):
        config = {"head": self.ent_head.get(), "length": self.ent_len.get(), "fields": [self.tree.item(i)['values'] for i in self.tree.get_children()]}
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            with open(path, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)

    def import_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path: return
        with open(path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            self.ent_head.delete(0, tk.END); self.ent_head.insert(0, config['head'])
            self.ent_len.delete(0, tk.END); self.ent_len.insert(0, config['length'])
            for i in self.tree.get_children(): self.tree.delete(i)
            for row in config['fields']: 
                row_list = list(row)
                # 向下兼容：如果导入的是只有 7 列的老旧版本配置
                if len(row_list) == 7:
                    row_list.insert(4, "1") # 补上 scale
                    row_list.extend(["数据帧", f"数值 ({row_list[5]})"]) # 补上 x/y label
                # 向下兼容：如果导入的是只有 8 列的版本 (带了 scale，但没 label)
                elif len(row_list) == 8:
                    row_list.extend(["数据帧", f"数值 ({row_list[5]})"])
                    
                self.tree.insert("", "end", values=row_list)

    def add_row(self):
        title = f"[{self.current_filename}] - 新增趋势图"
        self.tree.insert("", "end", values=("新变量", "20", "4", "unsigned int (>I)", "1", "单位", "4", title, "数据帧", "数值"))
        
    def del_row(self):
        for s in self.tree.selection(): self.tree.delete(s)

if __name__ == "__main__":
    root = tk.Tk(); app = UltrasoundMasterAppV39(root); root.mainloop()