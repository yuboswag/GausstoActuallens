"""
action_gui.py
Action_a 图形用户界面：参数配置、运行控制、实时日志显示。

运行方式：
    python action_gui.py
"""

import datetime
import io
import json
import os
import queue
import re
import sys
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, filedialog
import customtkinter as ctk
from pathlib import Path

from config import (_DEFAULT_GROUPS, _AUTO_SAVE_FILE,
                    _parse_structure, _parse_list_str, _parse_floats,
                    _parse_cemented_pairs, _parse_melt_filter)


# ══════════════════════════════════════════════════════════════════════
#  字体检测（中文显示兼容）
# ══════════════════════════════════════════════════════════════════════
def _detect_cjk_font():
    """返回当前平台可用的中文字体名称。"""
    try:
        import tkinter.font as tkfont
        _tmp = tk.Tk()
        _tmp.withdraw()
        available = set(tkfont.families())
        _tmp.destroy()
        for name in ('Microsoft YaHei', 'SimHei', 'PingFang SC',
                     'Hiragino Sans GB', 'WenQuanYi Micro Hei', 'Noto Sans CJK SC'):
            if name in available:
                return name
    except Exception:
        pass
    return 'TkDefaultFont'


# ══════════════════════════════════════════════════════════════════════
#  stdout 重定向：将 print 输出写入 queue.Queue（GUI 终端）
#  同时全量落盘到日志文件，并按"候选详细块"做静默过滤
# ══════════════════════════════════════════════════════════════════════

# 进入静默的起始模式（候选详细报告块的开头）
_MUTE_START_PATTERNS = (
    re.compile(r'^\s*【步骤5c】\s*EFL 缩放修正'),
    re.compile(r'^\s*正在计算\s*G\d+\s*候选'),    # 候选枚举循环：单个候选块也静默
    re.compile(r'^\s*\[G\d+\]\s*最终候选列表'),   # G? 多样性筛选纯光学排名表
    re.compile(r'^\s*★\s*第一名详情'),            # 第一名详情块（玻璃属性、约束验证等）
    re.compile(r'^\s*初始结构验证报告'),           # 初始结构验证报告块
)

# 退出静默的恢复模式（任一匹配则解除静默且该行显示）
_RESUME_PATTERNS = (
    re.compile(r'^\s*\[多样性筛选\]'),              # 多样性筛选汇总（候选枚举结束）
    re.compile(r'^\s*组元参数[::]'),                # 下一组开始
    re.compile(r'^\s*搜索完成[::]'),
    re.compile(r'^\s*两阶段筛选[::]'),
    re.compile(r'^\s*\[AUTO\]\s*G\d+\s*自动衔接'),  # 入选方案摘要（关键决策结果）
    re.compile(r'^\s*\[load_zoom_configs'),         # 系统级阶段
    re.compile(r'^\s*\[correct_zoom_spacings'),
    re.compile(r'^\s*面序列索引'),
    re.compile(r'^\s*像差地图'),
    re.compile(r'^\s*各面贡献分布'),
    re.compile(r'^\s*诊断报告'),
    re.compile(r'^\s*诊断汇总'),
    re.compile(r'^#{3,}'),                          # ### 章节
    re.compile(r'^═{3,}'),                          # ══ 章节
    re.compile(r'WARNING|ERROR|Traceback|Warning|❌|错误|异常'),
)

# 限时恢复模式：在静默期间命中后短暂恢复 N 行，N 行结束自动重新静默
# 用于在被静默的大块内部保留少量关键诊断行
_LIMITED_RESUME_PATTERNS = (
    (re.compile(r'^\s*【V2】\s*近轴 EFL 自洽性'), 5),  # V2 标题 + 4 行数据
    (re.compile(r'^\s*Petzval 半径'),               2),  # Petzval 2 行
)

# 排名表 Top-N 截断：识别到"排名"表头后，仅保留前 N 条
_RANKING_HEADER_PATTERN = re.compile(r'^\s+排名\s+\S')
_RANKING_ROW_PATTERN = re.compile(r'^\s+\d+\.\s+\S')
_RANKING_KEEP_N = 10


class _QueueWriter(io.TextIOBase):
    """
    将写入重定向到 queue.Queue（GUI 显示），同时全量落盘到日志文件。

    过滤策略：默认放行；识别到"候选详细报告块"起始时进入静默，
    遇到下一个"安全恢复点"时退出。完整内容始终写文件，
    GUI 终端只看到过滤后的精简流。
    """

    def __init__(self, q: queue.Queue, log_path: str | None = None):
        self._q = q
        self._log_fp = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                self._log_fp = open(log_path, 'w', encoding='utf-8', buffering=1)
            except Exception:
                self._log_fp = None
        self._muted = False
        self._resume_count = 0       # 限时恢复倒计时：>0 时强制显示该行
        self._line_buf = ''
        # 排名表 top-N 截断状态
        self._in_ranking = False
        self._ranking_count = 0
        self._ranking_seen_row = False
        self._ranking_truncated = False
        # 连续空行压缩状态
        self._last_was_blank = False

    @staticmethod
    def _match_any(line: str, patterns) -> bool:
        return any(p.search(line) for p in patterns)

    def write(self, msg: str) -> int:
        if not msg:
            return 0
        # 全量落盘（不受过滤影响）
        if self._log_fp is not None:
            try:
                self._log_fp.write(msg)
            except Exception:
                pass
        # 按行处理：先拼上次残留，再切分；最后一段（无尾换行）入缓冲
        buf = self._line_buf + msg
        parts = buf.split('\n')
        self._line_buf = parts[-1]
        lines = parts[:-1]
        for raw in lines:
            line = raw + '\n'
            # 限时恢复倒计时进行中：强制显示该行
            if self._resume_count > 0:
                self._q.put(line)
                self._resume_count -= 1
                if self._resume_count == 0:
                    self._muted = True   # 倒计时结束，重回静默
                continue
            # 静默期内：检查限时恢复触发（关键诊断行短暂出来）
            if self._muted:
                hit_limited = False
                for pat, n in _LIMITED_RESUME_PATTERNS:
                    if pat.search(line):
                        self._q.put(line)
                        self._resume_count = n - 1   # 本行已显示
                        if self._resume_count == 0:
                            # n=1 的情况：显示一行后立刻回静默（保持 muted=True）
                            pass
                        else:
                            self._muted = False  # 进入倒计时窗口（计数为 0 时重新静默）
                        hit_limited = True
                        break
                if hit_limited:
                    continue
            # 恢复点优先：解除静默并显示该行
            if self._muted and self._match_any(line, _RESUME_PATTERNS):
                self._muted = False
                self._q.put(line)
                self._last_was_blank = False
                continue
            # 起始点：进入静默，本行不显示（块的开头是模板，无信息量）
            if not self._muted and self._match_any(line, _MUTE_START_PATTERNS):
                self._muted = True
                continue
            # 排名表 Top-N 截断（unmuted 时生效）
            if not self._muted:
                if self._in_ranking:
                    if _RANKING_ROW_PATTERN.match(line):
                        self._ranking_seen_row = True
                        self._ranking_count += 1
                        if self._ranking_count > _RANKING_KEEP_N:
                            if not self._ranking_truncated:
                                self._q.put(f'   ... (省略 Top {_RANKING_KEEP_N} 之后的条目，详见日志文件)\n')
                                self._ranking_truncated = True
                                self._last_was_blank = False
                            continue
                    elif self._ranking_seen_row:
                        # 已见过 row 但本行不再匹配 → 退出排名模式
                        self._in_ranking = False
                        self._ranking_count = 0
                        self._ranking_seen_row = False
                        self._ranking_truncated = False
                # 检测进入排名模式
                if _RANKING_HEADER_PATTERN.match(line):
                    self._in_ranking = True
                    self._ranking_count = 0
                    self._ranking_seen_row = False
                    self._ranking_truncated = False
                # 连续空行压缩（>=2 连续 → 1）
                if line.strip() == '':
                    if self._last_was_blank:
                        continue
                    self._last_was_blank = True
                else:
                    self._last_was_blank = False
                self._q.put(line)
        return len(msg)

    def flush(self):
        if self._log_fp is not None:
            try:
                self._log_fp.flush()
            except Exception:
                pass

    def close_log(self):
        # flush 残留缓冲行（如有）
        if self._line_buf:
            if self._log_fp is not None:
                try:
                    self._log_fp.write(self._line_buf)
                except Exception:
                    pass
            if not self._muted:
                self._q.put(self._line_buf)
            self._line_buf = ''
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None


# ══════════════════════════════════════════════════════════════════════
#  可折叠参数区块组件
# ══════════════════════════════════════════════════════════════════════
class CollapsibleSection(ctk.CTkFrame):
    """
    可折叠参数区块。

    用法：
        section = CollapsibleSection(parent, title="全局参数", default_open=True)
        section.pack(fill='x', padx=4, pady=4)
        # 往 section.content 里添加控件
        ctk.CTkLabel(section.content, text="xxx").pack()
    """

    def __init__(self, parent, title: str, default_open: bool = True, **kwargs):
        super().__init__(parent, **kwargs)

        # 标题行：点击切换折叠
        self._header = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        self._header.pack(fill='x')

        self._arrow_label = ctk.CTkLabel(self._header, text="▼" if default_open else "▶",
                                          width=20, font=("", 12))
        self._arrow_label.pack(side='left', padx=(8, 4))

        self._title_label = ctk.CTkLabel(self._header, text=title,
                                          font=("", 13, "bold"))
        self._title_label.pack(side='left')

        # 右侧摘要（可选，外部通过 set_summary 更新）
        self._summary_label = ctk.CTkLabel(self._header, text="",
                                            text_color="gray", font=("", 11))
        self._summary_label.pack(side='right', padx=8)

        # 内容区
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self._is_open = default_open
        if default_open:
            self.content.pack(fill='x', padx=8, pady=(0, 8))

        # 绑定点击事件（标题行整体可点击）
        for widget in (self._header, self._arrow_label, self._title_label):
            widget.bind("<Button-1>", self._toggle)

    def _toggle(self, event=None):
        self._is_open = not self._is_open
        if self._is_open:
            self.content.pack(fill='x', padx=8, pady=(0, 8))
            self._arrow_label.configure(text="▼")
        else:
            self.content.forget()
            self._arrow_label.configure(text="▶")

    def set_summary(self, text: str):
        """设置折叠时标题右侧的灰色摘要文本。"""
        self._summary_label.configure(text=text)


# ══════════════════════════════════════════════════════════════════════
#  主 GUI 类
# ══════════════════════════════════════════════════════════════════════
class ActionGUI:
    """Action_a 图形用户界面。"""

    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("正组补偿变焦镜头初始结构设计工具 (Action_a)")
        self.root.geometry("1400x900")
        self.root.minsize(1100, 700)

        self._font_ui   = (_detect_cjk_font(), 9)
        self._font_bold = (_detect_cjk_font(), 9, 'bold')
        self._font_mono = ('Consolas', 9)

        self._log_queue: queue.Queue = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._running = False

        # 用于存储各 Tab 的控件变量
        self._group_vars: list[dict] = []

        self._build_ui()
        self._load_auto_save()
        self._auto_load_csv_on_startup()

    # ──────────────────────────────────────────────────────────────────
    #  UI 构建
    # ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # 顶层分为左右两列
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # 左侧参数面板（带垂直滚动）
        left_outer = ctk.CTkFrame(self.root)
        left_outer.grid(row=0, column=0, sticky='nsew', padx=(6, 3), pady=6)
        left_outer.rowconfigure(0, weight=1)   # 滚动区行可伸缩
        left_outer.rowconfigure(1, weight=0)   # 固定底部行不伸缩
        left_outer.columnconfigure(0, weight=1)

        self._left_scroll = ctk.CTkScrollableFrame(left_outer, width=420)
        self._left_scroll.grid(row=0, column=0, sticky='nsew')

        # 右侧日志面板
        right_frame = ctk.CTkFrame(self.root)
        right_frame.grid(row=0, column=1, sticky='nsew', padx=(3, 6), pady=6)
        right_frame.rowconfigure(1, weight=1)
        right_frame.rowconfigure(2, weight=0)
        right_frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(right_frame, text="运行日志", font=self._font_bold).grid(
            row=0, column=0, sticky='w', padx=6, pady=(6, 2))
        log_log_btns = ctk.CTkFrame(right_frame, fg_color='transparent')
        log_log_btns.grid(row=0, column=1, sticky='e', padx=6)
        ctk.CTkButton(log_log_btns, text="复制全部", width=80,
                      command=self._copy_log).pack(side='left', padx=2)
        ctk.CTkButton(log_log_btns, text="清空", width=80,
                      command=self._clear_log).pack(side='left', padx=2)

        log_container = ctk.CTkFrame(right_frame, fg_color='transparent')
        log_container.grid(row=1, column=0, columnspan=2, sticky='nsew',
                           padx=6, pady=(0, 6))
        log_container.rowconfigure(0, weight=1)
        log_container.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_container, font=self._font_mono,
                                wrap='none', state='normal',
                                undo=False,
                                bg='#1e1e1e', fg='#d4d4d4',
                                insertbackground='white',
                                selectbackground='#264f78')
        log_vsb = tk.Scrollbar(log_container, command=self.log_text.yview,
                               bg='#2b2b3b', troughcolor='#1e1e2e')
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_vsb.grid(row=0, column=1, sticky='ns')

        # 右键菜单
        log_menu = tk.Menu(self.log_text, tearoff=0,
                           bg='#2b2b3b', fg='#ccccdd',
                           activebackground='#3b3b5b', activeforeground='white')
        log_menu.add_command(label="复制选中", command=lambda: self.log_text.event_generate('<<Copy>>'))
        log_menu.add_command(label="全选", command=lambda: self.log_text.tag_add('sel', '1.0', 'end'))
        log_menu.add_separator()
        log_menu.add_command(label="清空日志", command=self._clear_log)
        self.log_text.bind('<Button-3>', lambda e: log_menu.tk_popup(e.x_root, e.y_root))

        # ── 固定底部操作区（不随左侧滚动区滚动）──────────────────
        fixed_bottom = ctk.CTkFrame(left_outer, fg_color='transparent')
        fixed_bottom.grid(row=1, column=0, sticky='ew', padx=4, pady=4)
        fixed_bottom.columnconfigure((0, 1, 2, 3, 4), weight=1)

        self.btn_run = ctk.CTkButton(fixed_bottom, text="▶ 运行",
                                     command=self._run,
                                     fg_color="#7c83ff",
                                     hover_color="#6a70dd")
        self.btn_run.grid(row=0, column=0, padx=2, sticky='ew')
        self.btn_stop = ctk.CTkButton(fixed_bottom, text="⏹ 停止",
                                      command=self._stop, state='disabled')
        self.btn_stop.grid(row=0, column=1, padx=2, sticky='ew')
        ctk.CTkButton(fixed_bottom, text="💾 保存",
                      command=self._save_config).grid(row=0, column=2, padx=2, sticky='ew')
        ctk.CTkButton(fixed_bottom, text="📂 加载",
                      command=self._load_config).grid(row=0, column=3, padx=2, sticky='ew')
        self.btn_zemax = ctk.CTkButton(fixed_bottom, text="📐 导入 Zemax",
                                       command=self._on_import_zemax,
                                       state='disabled')
        self.btn_zemax.grid(row=0, column=4, padx=2, sticky='ew')

        self.progress = ctk.CTkProgressBar(fixed_bottom, mode='indeterminate')
        self.progress.grid(row=1, column=0, columnspan=5, sticky='ew', pady=(4, 0))

        # ── 状态栏（日志面板底部）────────────────────────────────
        status_bar = ctk.CTkFrame(right_frame, height=28, corner_radius=0)
        status_bar.grid(row=2, column=0, columnspan=2, sticky='ew')
        status_bar.grid_propagate(False)

        self._status_dot = ctk.CTkLabel(status_bar, text="●", font=("", 10),
                                         text_color="gray50", width=20)
        self._status_dot.pack(side='left', padx=(8, 0))
        self._status_text = ctk.CTkLabel(status_bar, text="就绪",
                                          font=("", 11), text_color="gray")
        self._status_text.pack(side='left', padx=4)

        self._timer_label = ctk.CTkLabel(status_bar, text="",
                                          font=("", 11), text_color="gray")
        self._timer_label.pack(side='right', padx=8)

        self._timer_start = None
        self._timer_after_id = None

        # 填充左侧内容
        self._build_left_panel()

    # ──────────────────────────────────────────────────────────────────
    #  左侧面板内容
    # ──────────────────────────────────────────────────────────────────
    def _build_left_panel(self):
        p = self._left_scroll
        p.columnconfigure(0, weight=1)
        row = 0

        # ── 标题 ──────────────────────────────────────────────────────
        ctk.CTkLabel(p, text="Action_a 参数配置",
                     font=(_detect_cjk_font(), 11, 'bold')).grid(
            row=row, column=0, columnspan=2, pady=(0, 6), sticky='w')
        row += 1

        # ── 全局参数 ──────────────────────────────────────────────────
        glob_section = CollapsibleSection(p, title="全局参数", default_open=True)
        glob_section.grid(row=row, column=0, columnspan=2, sticky='ew', pady=4)
        row += 1
        glob_frame = glob_section.content
        glob_frame.columnconfigure(1, weight=1)

        self._var_xlsx    = tk.StringVar()
        self._var_lam_s   = tk.StringVar(value='450')
        self._var_lam_r   = tk.StringVar(value='550')
        self._var_lam_l   = tk.StringVar(value='850')
        self._var_melt    = tk.StringVar(value='MA')
        self._var_top_n   = tk.StringVar(value='10')
        self._var_workers = tk.StringVar(value='4')

        def _xlsx_row(parent, r):
            ctk.CTkLabel(parent, text="玻璃库 xlsx").grid(row=r, column=0, sticky='w', pady=2)
            _fr = ctk.CTkFrame(parent, fg_color='transparent')
            _fr.grid(row=r, column=1, sticky='ew', pady=2)
            _fr.columnconfigure(0, weight=1)
            ctk.CTkEntry(_fr, textvariable=self._var_xlsx).grid(row=0, column=0, sticky='ew')
            ctk.CTkButton(_fr, text="浏览...", width=48,
                          command=self._browse_xlsx).grid(row=0, column=1, padx=(2, 0))

        _xlsx_row(glob_frame, 0)

        for r, (lbl, var) in enumerate([
            ("短波 λ_s (nm)",  self._var_lam_s),
            ("参考 λ_ref (nm)", self._var_lam_r),
            ("长波 λ_l (nm)",  self._var_lam_l),
            ("熔炼过滤",        self._var_melt),
            ("top_n（保留候选数）", self._var_top_n),
            ("并行进程数",      self._var_workers),
        ], start=1):
            ctk.CTkLabel(glob_frame, text=lbl).grid(row=r, column=0, sticky='w', pady=2)
            ctk.CTkEntry(glob_frame, textvariable=var, width=112).grid(
                row=r, column=1, sticky='w', pady=2)

        # ── 组元配置 Tabview ─────────────────────────────────────────
        ctk.CTkLabel(p, text="组元配置", font=self._font_bold).grid(
            row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        row += 1
        grp_outer = ctk.CTkFrame(p, border_width=1, corner_radius=8)
        grp_outer.grid(row=row, column=0, columnspan=2, sticky='ew', pady=4, padx=4)
        row += 1

        self._group_nb = ctk.CTkTabview(grp_outer)
        self._group_nb.pack(fill='both', expand=True)

        for gi, gdef in enumerate(_DEFAULT_GROUPS):
            self._add_group_tab(gi, gdef)

        # ── 系统参数 ──────────────────────────────────────────────────
        sys_section = CollapsibleSection(p, title="系统级分析参数", default_open=False)
        sys_section.grid(row=row, column=0, columnspan=2, sticky='ew', pady=4)
        row += 1
        sys_frame = sys_section.content
        sys_frame.columnconfigure(1, weight=1)

        self._var_gap_csv   = tk.StringVar()
        self._var_gap_cols  = tk.StringVar(
            value='d1 (G1-G2间距) (mm),d2 (G2-G3间距) (mm),d3 (G3-G4间距) (mm)')
        self._var_stop_gi   = tk.StringVar(value='auto')
        self._var_stop_off  = tk.StringVar(value='0')
        self._var_fnum_w    = tk.StringVar(value='4.0')
        self._var_fnum_t    = tk.StringVar(value='5.6')
        self._var_sensor    = tk.StringVar(value='7.6')
        self._var_sys_srch  = tk.StringVar(value='30')
        self._var_sys_cand  = tk.StringVar(value='10')
        self._var_bfd_actual = tk.StringVar(value='8.0')


        # 像差权重
        self._var_wSI   = tk.StringVar(value='5.0')
        self._var_wSII  = tk.StringVar(value='5.0')
        self._var_wSIII = tk.StringVar(value='3.0')
        self._var_wSIV  = tk.StringVar(value='1.0')
        self._var_wSV   = tk.StringVar(value='0.1')
        self._var_wCI   = tk.StringVar(value='2.0')
        self._var_wCII  = tk.StringVar(value='2.0')

        def _csv_row_sys(parent, r):
            ctk.CTkLabel(parent, text="组间间距 CSV").grid(row=r, column=0, sticky='w', pady=2)
            _fr = ctk.CTkFrame(parent, fg_color='transparent')
            _fr.grid(row=r, column=1, sticky='ew', pady=2)
            _fr.columnconfigure(0, weight=1)
            ctk.CTkEntry(_fr, textvariable=self._var_gap_csv).grid(row=0, column=0, sticky='ew')
            ctk.CTkButton(_fr, text="浏览...", width=48,
                          command=self._browse_gap_csv).grid(row=0, column=1, padx=(2, 0))

        _csv_row_sys(sys_frame, 0)

        ctk.CTkLabel(sys_frame, text="间距列名（逗号分隔）").grid(row=1, column=0, sticky='w', pady=2)
        ctk.CTkEntry(sys_frame, textvariable=self._var_gap_cols).grid(
            row=1, column=1, sticky='ew', pady=2)

        # 光阑组元索引（Combobox，支持 auto 自动从 CSV 元数据读取）
        ctk.CTkLabel(sys_frame, text="光阑组元索引").grid(row=2, column=0, sticky='w', pady=2)
        _f_stop = ctk.CTkFrame(sys_frame, fg_color='transparent')
        _f_stop.grid(row=2, column=1, sticky='w', pady=2)
        ctk.CTkComboBox(_f_stop, variable=self._var_stop_gi,
                        values=["auto", "0", "1", "2", "3"],
                        width=80).pack(side='left')
        ctk.CTkLabel(_f_stop, text="auto=从CSV读取  0=G1…3=G4",
                     text_color='gray').pack(side='left', padx=(4, 0))

        for r, (lbl, var, hint) in enumerate([
            ("光阑面内偏移",  self._var_stop_off, "0=第一面 -1=最后一面"),
            ("F/# 广角端",   self._var_fnum_w,   ""),
            ("F/# 长焦端",   self._var_fnum_t,   "空/'None'=固定F数"),
            ("传感器对角线(mm)", self._var_sensor,""),
            ("系统搜索候选数",self._var_sys_srch, "auto模式"),
            ("系统保留候选数",self._var_sys_cand, "auto模式"),
            ("法兰距 bfd_actual (mm)", self._var_bfd_actual, "G4 后顶点→传感器物理距离"),
        ], start=3):
            ctk.CTkLabel(sys_frame, text=lbl).grid(row=r, column=0, sticky='w', pady=2)
            _f = ctk.CTkFrame(sys_frame, fg_color='transparent')
            _f.grid(row=r, column=1, sticky='w', pady=2)
            ctk.CTkEntry(_f, textvariable=var, width=80).pack(side='left')
            if hint:
                ctk.CTkLabel(_f, text=hint,
                             text_color='gray').pack(side='left', padx=(4, 0))

        # 像差权重（折叠式）
        self._weights_visible = tk.BooleanVar(value=False)
        _w_toggle = ctk.CTkCheckBox(
            sys_frame, text="展开像差权重",
            variable=self._weights_visible,
            command=self._toggle_weights)
        _w_toggle.grid(row=11, column=0, columnspan=2, sticky='w', pady=(6, 0))

        self._weights_frame = ctk.CTkFrame(sys_frame, fg_color='transparent')
        self._weights_frame.grid(row=12, column=0, columnspan=2, sticky='ew')
        self._weights_frame.grid_remove()

        for i, (key, var) in enumerate([
            ('SI（球差）',   self._var_wSI),
            ('SII（彗差）',  self._var_wSII),
            ('SIII（像散）', self._var_wSIII),
            ('SIV（场曲）',  self._var_wSIV),
            ('SV（畸变）',   self._var_wSV),
            ('CI（轴色差）', self._var_wCI),
            ('CII（垂轴色差）', self._var_wCII),
        ]):
            ctk.CTkLabel(self._weights_frame, text=key).grid(
                row=i, column=0, sticky='w', padx=(8, 4))
            ctk.CTkEntry(self._weights_frame, textvariable=var, width=64).grid(
                row=i, column=1, sticky='w', pady=1)


    # ──────────────────────────────────────────────────────────────────
    #  组元 Tab 构建
    # ──────────────────────────────────────────────────────────────────
    def _add_group_tab(self, gi: int, gdef: dict):
        tab_name = f" {gdef['name']} "
        self._group_nb.add(tab_name)
        tab = self._group_nb.tab(tab_name)
        tab.columnconfigure(0, weight=1)

        vars_dict = {}
        self._group_vars.append(vars_dict)

        def _make_var(key, default=''):
            v = tk.StringVar(value=str(gdef.get(key, default)))
            vars_dict[key] = v
            return v

        def _make_bool(key, default=False):
            v = tk.BooleanVar(value=bool(gdef.get(key, default)))
            vars_dict[key] = v
            return v

        def _labeled_entry(parent, label, key, default='', width=100, hint=''):
            """上方小字 label + 下方输入框的竖向组合；复用已存在的 var。"""
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            ctk.CTkLabel(frame, text=label, font=("", 11),
                          text_color="gray").pack(anchor='w')
            v = vars_dict[key] if key in vars_dict else _make_var(key, default)
            ctk.CTkEntry(frame, textvariable=v, width=width).pack(fill='x')
            if hint:
                ctk.CTkLabel(frame, text=hint, font=("", 10),
                              text_color="gray50").pack(anchor='w')
            return frame, v

        # 隐藏的 name 变量（tab 名已显示组名，无需输入框）
        _make_var('name', gdef['name'])

        row = 0

        # ═══ 基本参数（3 列）═══════════════════════════════════════
        basic_frame = ctk.CTkFrame(tab, fg_color="transparent")
        basic_frame.grid(row=row, column=0, sticky='ew', padx=8, pady=(8, 4))
        basic_frame.columnconfigure((0, 1, 2), weight=1)
        row += 1

        f1, _ = _labeled_entry(basic_frame, "焦距 f (mm)", 'f_group', gdef['f_group'])
        f1.grid(row=0, column=0, sticky='ew', padx=(0, 4))

        f2, _ = _labeled_entry(basic_frame, "口径 D (mm)", 'D', gdef['D'])
        f2.grid(row=0, column=1, sticky='ew', padx=4)

        f3, _ = _labeled_entry(basic_frame, "最小片焦距 (mm)", 'min_f_mm', gdef['min_f_mm'])
        f3.grid(row=0, column=2, sticky='ew', padx=(4, 0))

        # ═══ 镜片结构 ══════════════════════════════════════════════
        struct_section = ctk.CTkFrame(tab, fg_color="transparent")
        struct_section.grid(row=row, column=0, sticky='ew', padx=8, pady=4)
        row += 1

        ctk.CTkLabel(struct_section, text="镜片结构", font=("", 11),
                      text_color="gray").pack(anchor='w')

        # 彩色标签可视化行
        vis_frame = ctk.CTkFrame(struct_section, fg_color="transparent")
        vis_frame.pack(fill='x', pady=(2, 4))
        self._make_structure_tags(vis_frame, gdef.get('structure', 'pos'),
                                   gdef.get('cemented_pairs', ''))

        # structure 输入框
        struct_var = _make_var('structure', gdef['structure'])
        ctk.CTkEntry(struct_section, textvariable=struct_var,
                      placeholder_text="pos,neg,pos,pos").pack(fill='x')

        # cemented_pairs var（注册到 vars_dict，供 trace 和胶合间距区复用）
        cem_var = _make_var('cemented_pairs', gdef['cemented_pairs'])

        def _on_struct_change(*_):
            for w in vis_frame.winfo_children():
                w.destroy()
            self._make_structure_tags(vis_frame, struct_var.get(), cem_var.get())

        struct_var.trace_add('write', _on_struct_change)
        cem_var.trace_add('write', _on_struct_change)

        # 开关行：apo + 允许重复
        opts_frame = ctk.CTkFrame(struct_section, fg_color="transparent")
        opts_frame.pack(fill='x', pady=(4, 0))

        apo_var = _make_bool('apo', gdef.get('apo', False))
        ctk.CTkCheckBox(opts_frame, text="apo", variable=apo_var,
                         width=60).pack(side='left')

        dup_var = _make_bool('allow_duplicate', gdef.get('allow_duplicate', True))
        ctk.CTkCheckBox(opts_frame, text="允许重复玻璃", variable=dup_var,
                         width=120).pack(side='left', padx=(8, 0))

        # ═══ 胶合与间距（2 列）═════════════════════════════════════
        cem_space_frame = ctk.CTkFrame(tab, fg_color="transparent")
        cem_space_frame.grid(row=row, column=0, sticky='ew', padx=8, pady=4)
        cem_space_frame.columnconfigure((0, 1), weight=1)
        row += 1

        # cemented_pairs 已在 vars_dict 中，_labeled_entry 会复用
        fc1, _ = _labeled_entry(cem_space_frame, "胶合对", 'cemented_pairs',
                                 gdef['cemented_pairs'], hint='如 (1,2),(2,3)，空=无')
        fc1.grid(row=0, column=0, sticky='ew', padx=(0, 4))

        fc2, _ = _labeled_entry(cem_space_frame, "片间距 (mm)", 'spacings_mm',
                                 gdef['spacings_mm'], hint='胶合面处填0')
        fc2.grid(row=0, column=1, sticky='ew', padx=(4, 0))

        # 自动片间距开关：勾选后忽略手输内容，由 edge_geometry 自动计算
        auto_sp_var = _make_bool('_spacing_auto', gdef.get('_spacing_auto', False))
        ctk.CTkCheckBox(cem_space_frame, text="自动片间距（忽略手填）",
                         variable=auto_sp_var, width=180).grid(
                         row=1, column=1, sticky='w', padx=(4, 0), pady=(2, 0))

        # ═══ 结构约束（4 列紧凑）═══════════════════════════════════
        cons_frame = ctk.CTkFrame(tab, fg_color="transparent")
        cons_frame.grid(row=row, column=0, sticky='ew', padx=8, pady=4)
        cons_frame.columnconfigure((0, 1, 2, 3), weight=1)
        row += 1

        for ci, (lbl, key, dv) in enumerate([
            ("最小R (mm)",  'min_r_mm',       gdef['min_r_mm']),
            ("正镜边厚",    't_edge_min',     gdef['t_edge_min']),
            ("负镜中厚",    't_center_min',   gdef['t_center_min']),
            ("胶合总厚",    't_cemented_min', gdef['t_cemented_min']),
        ]):
            fc, _ = _labeled_entry(cons_frame, lbl, key, dv, width=70)
            fc.grid(row=0, column=ci, sticky='ew',
                    padx=(0 if ci == 0 else 2, 0 if ci == 3 else 2))

        # ═══ 可折叠：高级参数（玻璃池/焦距约束/变焦列前缀）═══════════
        step2_section = CollapsibleSection(
            tab, title="高级参数（玻璃池/约束）", default_open=False)
        step2_section.grid(row=row, column=0, sticky='ew', padx=4, pady=4)
        row += 1
        s2 = step2_section.content

        # 不常用参数（glass_roles / max_f_mm / zoom_csv_group）
        misc_frame = ctk.CTkFrame(s2, fg_color="transparent")
        misc_frame.pack(fill='x', pady=(4, 0))
        misc_frame.columnconfigure((0, 1, 2), weight=1)

        fm1, _ = _labeled_entry(misc_frame, "玻璃角色", 'glass_roles',
                                  gdef['glass_roles'], hint='pos/neg/any')
        fm1.grid(row=0, column=0, sticky='ew', padx=(0, 4))

        fm2, _ = _labeled_entry(misc_frame, "最大片焦距", 'max_f_mm',
                                  gdef['max_f_mm'], hint='空=无限制')
        fm2.grid(row=0, column=1, sticky='ew', padx=4)

        fm3, _ = _labeled_entry(misc_frame, "CSV列前缀", 'zoom_csv_group',
                                  gdef['zoom_csv_group'], hint='定焦组留空')
        fm3.grid(row=0, column=2, sticky='ew', padx=(4, 0))

    def _make_structure_tags(self, parent, structure_str: str, cemented_str: str):
        """在 parent 中创建镜片结构的彩色可视化标签。"""
        parts = [p.strip() for p in structure_str.split(',') if p.strip()]
        for p in parts:
            if p in ('pos', '+'):
                text, fg, bg = "+", "#4499ff", "#1a2a44"
            elif p in ('neg', '-'):
                text, fg, bg = "−", "#ff6666", "#441a1a"
            else:
                text, fg, bg = p, "gray", "gray20"
            ctk.CTkLabel(parent, text=f" {text} ", font=("", 12, "bold"),
                          text_color=fg, fg_color=bg,
                          corner_radius=10, width=32, height=24).pack(side='left', padx=2)
        if cemented_str.strip():
            ctk.CTkLabel(parent, text=f"胶合: {cemented_str}", font=("", 11),
                          text_color="gray").pack(side='left', padx=(8, 0))

    def _toggle_weights(self):
        if self._weights_visible.get():
            self._weights_frame.grid()
        else:
            self._weights_frame.grid_remove()

    # ──────────────────────────────────────────────────────────────────
    #  浏览文件
    # ──────────────────────────────────────────────────────────────────
    def _browse_xlsx(self):
        path = filedialog.askopenfilename(
            title="选择玻璃库 xlsx 文件",
            filetypes=[("Excel 文件", "*.xlsx;*.xls"), ("所有文件", "*.*")])
        if path:
            self._var_xlsx.set(path)

    def _parse_gauss_csv_header(self, path: str) -> dict:
        """解析 Gaussianoptics 导出的 CSV 文件头部 # KEY=VALUE 行。

        Returns:
            dict[str, str]：包含所有解析到的 KEY=VALUE 对（VALUE 为字符串）。
            文件不存在或读取失败时返回空 dict。
        """
        result = {}
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith('#'):
                        break  # # 头结束，进入数据表
                    body = line.lstrip('#').strip()
                    if '=' in body:
                        k, v = body.split('=', 1)
                        result[k.strip()] = v.strip()
        except (OSError, UnicodeDecodeError):
            pass
        return result

    def _apply_gauss_header_to_groups(self, header: dict) -> int:
        """把 # 头中的 F_G1~F_G4 / D_G1~D_G4 填入 _group_vars。

        Returns:
            int：成功填入的字段数（用于日志诊断）。
        """
        filled = 0
        for gi in range(min(4, len(self._group_vars))):
            gv = self._group_vars[gi]
            f_key = f'F_G{gi+1}'
            d_key = f'D_G{gi+1}'
            if f_key in header and 'f_group' in gv:
                gv['f_group'].set(header[f_key])
                filled += 1
            if d_key in header and 'D' in gv:
                gv['D'].set(header[d_key])
                filled += 1
        return filled

    def _auto_load_csv_on_startup(self) -> None:
        """启动时自动加载 CSV header 元数据（焦距/口径）。

        路径优先级：
          1. _var_gap_csv 中已有的路径（由 _load_auto_save 从 _AUTO_SAVE_FILE 恢复）
          2. fallback：action_gui.py 同目录下的 111.csv
        都不可用时静默跳过，不阻断 GUI 启动。
        """
        csv_path = self._var_gap_csv.get().strip()
        if not csv_path or not Path(csv_path).exists():
            default = Path(__file__).parent / '111.csv'
            if not default.exists():
                return
            csv_path = str(default.resolve())
            self._var_gap_csv.set(csv_path)
            self._log(f"[启动] 未找到上次的 CSV 路径,使用默认: {csv_path}\n")
        try:
            header = self._parse_gauss_csv_header(csv_path)
            n = self._apply_gauss_header_to_groups(header)
        except Exception as e:
            self._log(f"[启动] CSV header 自动加载失败: {e}\n")
            return
        if n > 0:
            self._log(f"[启动] 已从 {Path(csv_path).name} 自动填入 {n} 项组焦距/口径\n")

    def _browse_gap_csv(self):
        path = filedialog.askopenfilename(
            title="选择组间间距 CSV 文件",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if path:
            self._var_gap_csv.set(path)
            # 自动解析 # 头并填入各组焦距/口径
            header = self._parse_gauss_csv_header(path)
            n = self._apply_gauss_header_to_groups(header)
            if n > 0:
                self._log(f"已从 {Path(path).name} 自动填入 {n} 项组焦距/口径\n")

    # ──────────────────────────────────────────────────────────────────
    #  参数收集 & 验证
    # ──────────────────────────────────────────────────────────────────
    def _collect_all_params(self) -> dict:
        """从 GUI 控件收集所有参数，组装为 run_action_a_pipeline 所需的 params 字典。"""
        groups = []
        for gi, gv in enumerate(self._group_vars):
            def _sv(key, default=''):
                v = gv.get(key)
                if v is None:
                    return default
                return v.get() if isinstance(v, (tk.StringVar,)) else str(v.get())

            def _bv(key, default=False):
                v = gv.get(key)
                if v is None:
                    return default
                return bool(v.get())

            # 解析 nd_vals：格式 "GlassA:1.700,GlassB:1.650" 或空
            nd_raw = _sv('nd_vals', '').strip()
            nd_dict = {}
            if nd_raw:
                for part in nd_raw.split(','):
                    if ':' in part:
                        k, val = part.split(':', 1)
                        try:
                            nd_dict[k.strip()] = float(val.strip())
                        except ValueError:
                            pass

            glass_names_raw = _sv('glass_names', '').strip()
            focal_raw       = _sv('focal_lengths_mm', '').strip()
            vgen_raw        = _sv('vgen_list', '').strip()
            struct_raw      = _sv('structure', 'pos').strip()
            roles_raw       = _sv('glass_roles', '').strip()
            cem_raw         = _sv('cemented_pairs', '').strip()
            sp_raw          = _sv('spacings_mm', '').strip()
            sp_auto         = _bv('_spacing_auto', False)

            groups.append({
                'name':            _sv('name', f'G{gi+1}'),
                'zoom_csv_group':  _sv('zoom_csv_group', '') or None,
                'f_group':         float(_sv('f_group', '0')),
                'D':               float(_sv('D', '10')),
                'structure':       _parse_structure(struct_raw),
                'glass_roles':     _parse_list_str(roles_raw) if roles_raw else None,
                'apo':             _bv('apo', False),
                'cemented_pairs':  _parse_cemented_pairs(cem_raw),
                'spacings_mm':     None if sp_auto else (_parse_floats(sp_raw) if sp_raw else []),
                'min_f_mm':        float(_sv('min_f_mm')) if _sv('min_f_mm').strip() else None,
                'max_f_mm':        float(_sv('max_f_mm')) if _sv('max_f_mm').strip() else None,
                'allow_duplicate': _bv('allow_duplicate', True),
                'min_r_mm':        float(_sv('min_r_mm', '20')),
                't_edge_min':      float(_sv('t_edge_min', '1.0')),
                't_center_min':    float(_sv('t_center_min', '1.5')),
                't_cemented_min':  float(_sv('t_cemented_min', '3.0')),
                'glass_names':     _parse_list_str(glass_names_raw) if glass_names_raw else [],
                'focal_lengths_mm':_parse_floats(focal_raw) if focal_raw else [],
                'vgen_list':       _parse_floats(vgen_raw) if vgen_raw else [],
                'nd_vals':         nd_dict,
                'target_f_mm':     float(_sv('f_group', '0')),
            })

        # 系统参数
        fnum_tele_raw = self._var_fnum_t.get().strip()
        fnum_tele = None if fnum_tele_raw.lower() in ('', 'none') \
                    else float(fnum_tele_raw)

        # 间距列名
        gap_cols_raw = self._var_gap_cols.get().strip()
        gap_cols = [c.strip() for c in gap_cols_raw.split(',') if c.strip()] \
                   if gap_cols_raw else []

        # 光阑组元索引：'auto' → None（由 run_action_a_pipeline 从 CSV 元数据读取）
        _stop_gi_raw = self._var_stop_gi.get().strip()
        _stop_group_idx = None if _stop_gi_raw == 'auto' else int(_stop_gi_raw)

        weights = {
            'SI':   float(self._var_wSI.get()),
            'SII':  float(self._var_wSII.get()),
            'SIII': float(self._var_wSIII.get()),
            'SIV':  float(self._var_wSIV.get()),
            'SV':   float(self._var_wSV.get()),
            'CI':   float(self._var_wCI.get()),
            'CII':  float(self._var_wCII.get()),
        }

        return {
            'run_mode':          'auto',
            'glass_xlsx':        self._var_xlsx.get().strip(),
            'lam_short_nm':      float(self._var_lam_s.get()),
            'lam_ref_nm':        float(self._var_lam_r.get()),
            'lam_long_nm':       float(self._var_lam_l.get()),
            'melt_filter':       _parse_melt_filter(self._var_melt.get()),
            'top_n':             int(self._var_top_n.get()),
            'system_search_n':   int(self._var_sys_srch.get()),
            'system_cand_n':     int(self._var_sys_cand.get()),
            'n_workers':         int(self._var_workers.get()),
            'phi_scan_steps':    20,
            'optical_percentile':30,
            'tol_disp':          1e-4,
            'w_apo':             2000.0,
            'tol_phi':           1e-5,
            's_zoom_csv':        None,
            'groups':            groups,
            'system': {
                'gap_csv':        self._var_gap_csv.get().strip() or None,
                'gap_columns':    gap_cols,
                'stop_group_idx': _stop_group_idx,
                'stop_offset':    int(self._var_stop_off.get()),
                'fnum_wide':      float(self._var_fnum_w.get()),
                'fnum_tele':      fnum_tele,
                'sensor_diag_mm': float(self._var_sensor.get()),
                'bfd_actual':     float(self._var_bfd_actual.get()),
                'weights':        weights,
            },
        }

    def _validate_params(self, params: dict) -> list:
        """基本参数校验，返回错误信息列表（空列表表示校验通过）。"""
        errors = []
        xlsx = params.get('glass_xlsx', '')
        if not xlsx:
            errors.append("请选择玻璃库 xlsx 文件。")
        elif not Path(xlsx).exists():
            errors.append(f"玻璃库文件不存在：{xlsx}")

        for gi, g in enumerate(params.get('groups', [])):
            if not g['structure']:
                errors.append(f"G{gi+1}：结构（structure）不能为空。")

        return errors

    # ──────────────────────────────────────────────────────────────────
    #  配置保存 / 加载
    # ──────────────────────────────────────────────────────────────────
    def _params_to_json_safe(self, params: dict) -> dict:
        """将 params 转为 JSON 可序列化格式（主要处理 tuple → list）。"""
        import copy
        p = copy.deepcopy(params)
        for g in p.get('groups', []):
            cp = g.get('cemented_pairs')
            if cp:
                g['cemented_pairs'] = [list(x) for x in cp]
        return p

    def _collect_gui_raw(self) -> dict:
        """收集 GUI 原始字符串（用于 JSON 保存，方便还原）。"""
        raw = {
            'glass_xlsx': self._var_xlsx.get(),
            'lam_short_nm': self._var_lam_s.get(),
            'lam_ref_nm':   self._var_lam_r.get(),
            'lam_long_nm':  self._var_lam_l.get(),
            'melt_filter':  self._var_melt.get(),
            'top_n':        self._var_top_n.get(),
            'n_workers':    self._var_workers.get(),
            'sys_search_n': self._var_sys_srch.get(),
            'sys_cand_n':   self._var_sys_cand.get(),
            'gap_csv':      self._var_gap_csv.get(),
            'gap_cols':     self._var_gap_cols.get(),
            'stop_gi':      self._var_stop_gi.get(),
            'stop_off':     self._var_stop_off.get(),
            'fnum_w':       self._var_fnum_w.get(),
            'fnum_t':       self._var_fnum_t.get(),
            'sensor':       self._var_sensor.get(),
            'bfd_actual':   self._var_bfd_actual.get(),
            'wSI':   self._var_wSI.get(),
            'wSII':  self._var_wSII.get(),
            'wSIII': self._var_wSIII.get(),
            'wSIV':  self._var_wSIV.get(),
            'wSV':   self._var_wSV.get(),
            'wCI':   self._var_wCI.get(),
            'wCII':  self._var_wCII.get(),
            'groups': [],
        }
        _str_keys = [
            'name', 'f_group', 'D', 'structure', 'glass_roles', 'cemented_pairs',
            'spacings_mm', 'zoom_csv_group', 'min_f_mm', 'max_f_mm',
            'min_r_mm', 't_edge_min', 't_center_min', 't_cemented_min',
            'glass_names', 'focal_lengths_mm', 'vgen_list', 'nd_vals',
        ]
        _bool_keys = ['apo', 'allow_duplicate', '_spacing_auto']
        for gv in self._group_vars:
            gd = {}
            for k in _str_keys:
                v = gv.get(k)
                gd[k] = v.get() if v else ''
            for k in _bool_keys:
                v = gv.get(k)
                gd[k] = bool(v.get()) if v else False
            raw['groups'].append(gd)
        return raw

    def _apply_raw_config(self, raw: dict):
        """将 JSON 原始配置还原到 GUI 控件。"""
        def _set(var, key):
            val = raw.get(key)
            if val is not None:
                var.set(str(val))

        _set(self._var_xlsx,   'glass_xlsx')
        _set(self._var_lam_s,  'lam_short_nm')
        _set(self._var_lam_r,  'lam_ref_nm')
        _set(self._var_lam_l,  'lam_long_nm')
        _set(self._var_melt,   'melt_filter')
        _set(self._var_top_n,  'top_n')
        _set(self._var_workers,'n_workers')
        _set(self._var_sys_srch,'sys_search_n')
        _set(self._var_sys_cand,'sys_cand_n')
        _set(self._var_gap_csv,'gap_csv')
        _set(self._var_gap_cols,'gap_cols')
        _set(self._var_stop_gi,'stop_gi')
        _set(self._var_stop_off,'stop_off')
        _set(self._var_fnum_w, 'fnum_w')
        _set(self._var_fnum_t, 'fnum_t')
        _set(self._var_sensor, 'sensor')
        _set(self._var_bfd_actual,'bfd_actual')
        _set(self._var_wSI,    'wSI')
        _set(self._var_wSII,   'wSII')
        _set(self._var_wSIII,  'wSIII')
        _set(self._var_wSIV,   'wSIV')
        _set(self._var_wSV,    'wSV')
        _set(self._var_wCI,    'wCI')
        _set(self._var_wCII,   'wCII')

        _str_keys = [
            'name', 'f_group', 'D', 'structure', 'glass_roles', 'cemented_pairs',
            'spacings_mm', 'zoom_csv_group', 'min_f_mm', 'max_f_mm',
            'min_r_mm', 't_edge_min', 't_center_min', 't_cemented_min',
            'glass_names', 'focal_lengths_mm', 'vgen_list', 'nd_vals',
        ]
        _bool_keys = ['apo', 'allow_duplicate', '_spacing_auto']
        for gi, gd in enumerate(raw.get('groups', [])):
            if gi >= len(self._group_vars):
                break
            gv = self._group_vars[gi]
            for k in _str_keys:
                v = gv.get(k)
                if v and k in gd:
                    v.set(str(gd[k]))
            for k in _bool_keys:
                v = gv.get(k)
                if v and k in gd:
                    v.set(bool(gd[k]))

    def _save_config(self):
        path = filedialog.asksaveasfilename(
            defaultextension='.json',
            filetypes=[("JSON 配置文件", "*.json"), ("所有文件", "*.*")],
            title="保存配置")
        if path:
            raw = self._collect_gui_raw()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(raw, f, indent=4, ensure_ascii=False)
            self._log(f"配置已保存至：{path}\n")

    def _load_config(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON 配置文件", "*.json"), ("所有文件", "*.*")],
            title="加载配置")
        if path:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                self._apply_raw_config(raw)
                self._log(f"配置已加载：{path}\n")
            except Exception as e:
                messagebox.showerror("加载失败", str(e))

    def _auto_save_config(self):
        try:
            raw = self._collect_gui_raw()
            with open(_AUTO_SAVE_FILE, 'w', encoding='utf-8') as f:
                json.dump(raw, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def _load_auto_save(self):
        if _AUTO_SAVE_FILE.exists():
            try:
                with open(_AUTO_SAVE_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                self._apply_raw_config(raw)
            except Exception:
                pass
        # 启动后若 gap_csv 路径已恢复且文件存在，自动解析其 # 头填入组焦距/口径
        try:
            gp = self._var_gap_csv.get().strip()
            if gp and Path(gp).exists():
                header = self._parse_gauss_csv_header(gp)
                self._apply_gauss_header_to_groups(header)
        except Exception:
            pass  # 启动期容错：解析失败不阻断 GUI 启动

    # ──────────────────────────────────────────────────────────────────
    #  日志操作
    # ──────────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        self.log_text.insert(tk.END, msg)
        self.log_text.see(tk.END)

    def _clear_log(self):
        self.log_text.delete('1.0', tk.END)

    def _copy_log(self):
        text = self.log_text.get('1.0', tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ──────────────────────────────────────────────────────────────────
    #  状态栏计时器
    # ──────────────────────────────────────────────────────────────────
    def _start_timer(self):
        """运行开始时调用：启动计时器。"""
        import time
        self._timer_start = time.time()
        self._status_dot.configure(text_color="#55cc88")
        self._status_text.configure(text="运行中", text_color="#55cc88")
        self._tick_timer()

    def _tick_timer(self):
        """每秒更新一次计时显示。"""
        if self._timer_start is None:
            return
        import time
        elapsed = int(time.time() - self._timer_start)
        m, s = divmod(elapsed, 60)
        self._timer_label.configure(text=f"耗时 {m:02d}:{s:02d}")
        self._timer_after_id = self.root.after(1000, self._tick_timer)

    def _stop_timer(self, success: bool = True):
        """运行结束时调用：停止计时器，更新状态。"""
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        if success:
            self._status_dot.configure(text_color="#55cc88")
            self._status_text.configure(text="完成", text_color="#55cc88")
        else:
            self._status_dot.configure(text_color="#ff5555")
            self._status_text.configure(text="出错", text_color="#ff5555")

    def _reset_status(self):
        """恢复空闲状态。"""
        self._timer_start = None
        self._status_dot.configure(text_color="gray50")
        self._status_text.configure(text="就绪", text_color="gray")
        self._timer_label.configure(text="")

    # ──────────────────────────────────────────────────────────────────
    #  运行控制
    # ──────────────────────────────────────────────────────────────────
    def _on_import_zemax(self):
        if self._running:
            return
        _cfg_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_run_config.json')
        if not os.path.exists(_cfg_json):
            messagebox.showwarning("无法导入", "未找到 last_run_config.json，请先运行自动流程。")
            return
        self._log("\n[导入 Zemax] 请确认 Zemax 已打开 Programming → Interactive Extension\n")
        self._running = True
        self.btn_run.configure(state='disabled')
        self.btn_zemax.configure(state='disabled')
        self._import_thread = threading.Thread(
            target=self._zemax_import_entry,
            args=(self._log_queue,),
            daemon=True,
        )
        self._import_thread.start()
        self.root.after(100, self._poll_import_queue)

    def _run(self):
        if self._running:
            return

        try:
            params = self._collect_all_params()
        except (ValueError, TypeError) as e:
            messagebox.showerror(
                "参数错误",
                f"有数值字段为空或填写了非数字，请检查焦距、口径、F/#、波长、"
                f"权重等数值字段后重试。\n\n详情：{e}")
            return
        except Exception as e:
            messagebox.showerror("参数解析错误", str(e))
            return

        errors = self._validate_params(params)
        if errors:
            messagebox.showerror("参数错误", "\n".join(errors))
            return

        self._auto_save_config()

        self.btn_run.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self.progress.start()
        self._log(f"\n{'═'*60}\n▶ 开始运行，模式：{params['run_mode']}\n{'═'*60}\n")
        self._running = True
        self._start_timer()

        self._log_queue = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_entry,
            args=(params, self._log_queue),
            daemon=True,
        )
        self._worker_thread.start()
        self.root.after(100, self._poll_log_queue)

    def _stop(self):
        """强制终止 worker 线程（设标志位，线程本身需在下次 print 时感知）。"""
        self._running = False
        self._log("\n⏹ 已发出停止信号，等待当前任务完成...\n")

    # 日志最大行数；超出后自动清除最早的一半，防止 Text 控件越来越卡
    MAX_LOG_LINES = 5000

    def _trim_log(self):
        """若日志超过 MAX_LOG_LINES 行，删除最早的一半。"""
        line_count = int(self.log_text.index('end-1c').split('.')[0])
        if line_count > self.MAX_LOG_LINES:
            self.log_text.delete('1.0', f'{line_count // 2}.0')

    def _poll_log_queue(self):
        """每 200ms 批量取出队列中所有消息，拼接后一次性 insert，减少控件刷新次数。"""
        messages = []
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if msg == '__DONE__':
                    if messages:
                        self.log_text.insert(tk.END, ''.join(messages))
                        self._trim_log()
                        self.log_text.see(tk.END)
                    self._on_complete(success=True)
                    return
                elif msg == '__ERROR__':
                    err_msg = self._log_queue.get_nowait()
                    if messages:
                        self.log_text.insert(tk.END, ''.join(messages))
                        self._trim_log()
                        self.log_text.see(tk.END)
                    self._on_complete(success=False, err_msg=err_msg)
                    return
                messages.append(msg)
        except queue.Empty:
            pass

        if messages:
            self.log_text.insert(tk.END, ''.join(messages))
            self._trim_log()
            self.log_text.see(tk.END)  # 每批只调用一次，避免每条消息都触发布局计算

        if self._worker_thread and not self._worker_thread.is_alive():
            self._on_complete(success=True)
            return

        if self._running:
            self.root.after(200, self._poll_log_queue)  # 100ms → 200ms，降低轮询频率

    def _on_complete(self, success: bool, err_msg: str = ''):
        self._running = False
        self._stop_timer(success=success)
        self.progress.stop()
        self.btn_run.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        # 自动流程跑完后，若 last_run_config.json 存在则启用"导入 Zemax"按钮
        _cfg_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_run_config.json')
        if os.path.exists(_cfg_json):
            self.btn_zemax.configure(state='normal')
        if success:
            self._log(f"\n{'═'*60}\n✅ 运行完成\n{'═'*60}\n")
        else:
            self._log(f"\n{'═'*60}\n❌ 运行出错：\n{err_msg}\n{'═'*60}\n")

    # ──────────────────────────────────────────────────────────────────
    #  Worker 线程入口
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _worker_entry(params: dict, log_queue: queue.Queue):
        """在独立线程中执行 run_action_a_pipeline，stdout 重定向到队列。

        终端只显示精简流（过滤候选详细块），完整输出始终落盘到
        ``<项目根>/logs/auto_run_YYYYMMDD_HHMMSS.log``。
        """
        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr
        # 时间戳化日志文件，保存在脚本同级 logs/ 目录
        _action_a_dir = Path(__file__).parent
        _ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        _log_path = str(_action_a_dir / 'logs' / f'auto_run_{_ts}.log')
        _writer = _QueueWriter(log_queue, log_path=_log_path)
        sys.stdout = _writer
        sys.stderr = _writer

        try:
            print(f"[精简模式] 完整输出 → {_log_path}\n")
            # 延迟导入，避免 GUI 启动时加载耗时模块
            if str(_action_a_dir) not in sys.path:
                sys.path.insert(0, str(_action_a_dir))
            from main import run_action_a_pipeline
            run_action_a_pipeline(params)
            print(f"\n[精简模式] 完整输出已写入 → {_log_path}")
            log_queue.put('__DONE__')
        except Exception:
            error_msg = traceback.format_exc()
            # 通过已重定向的 stdout 写入队列，确保堆栈在日志中实时可见
            print(f"\n{'='*60}\n❌ 运行出错：\n{error_msg}\n{'='*60}")
            log_queue.put('__ERROR__')
            log_queue.put(error_msg)
        finally:
            try:
                _writer.close_log()
            except Exception:
                pass
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr

    @staticmethod
    def _zemax_import_entry(log_queue: queue.Queue):
        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr
        _writer = _QueueWriter(log_queue)
        sys.stdout = _writer
        sys.stderr = _writer
        try:
            import test_bridge
            test_bridge.run_test()
            log_queue.put("\n[导入 Zemax] 执行完毕，请查看上方 PASS/FAIL 结果。\n")
        except Exception:
            import traceback
            log_queue.put("\n[导入 Zemax] 失败：\n" + traceback.format_exc() + "\n")
        finally:
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr

    def _poll_import_queue(self):
        messages = []
        try:
            while True:
                msg = self._log_queue.get_nowait()
                messages.append(msg)
        except queue.Empty:
            pass

        if messages:
            self.log_text.insert(tk.END, ''.join(messages))
            self._trim_log()
            self.log_text.see(tk.END)

        if self._import_thread and not self._import_thread.is_alive():
            self._running = False
            self.btn_run.configure(state='normal')
            _cfg_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_run_config.json')
            if os.path.exists(_cfg_json):
                self.btn_zemax.configure(state='normal')
            return

        if self._running:
            self.root.after(200, self._poll_import_queue)


# ══════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()    # Windows 打包兼容

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    app = ActionGUI(root)
    root.mainloop()