"""
action_gui.py
Action_a 图形用户界面：参数配置、运行控制、实时日志显示。

运行方式：
    python action_gui.py
"""

import io
import json
import os
import queue
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
#  stdout 重定向：将 print 输出写入 queue.Queue
# ══════════════════════════════════════════════════════════════════════
class _QueueWriter(io.TextIOBase):
    """将写入重定向到 queue.Queue，供 GUI 轮询显示。"""

    def __init__(self, q: queue.Queue):
        self._q = q

    def write(self, msg: str) -> int:
        if msg:
            self._q.put(msg)
        return len(msg) if msg else 0

    def flush(self):
        pass


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
        fixed_bottom.columnconfigure((0, 1, 2, 3), weight=1)

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

        self.progress = ctk.CTkProgressBar(fixed_bottom, mode='indeterminate')
        self.progress.grid(row=1, column=0, columnspan=4, sticky='ew', pady=(4, 0))

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

        # ── 模式选择（2×2 卡片）──────────────────────────────────────
        self._var_mode = tk.StringVar(value='auto')
        self._mode_cards = {}

        mode_frame = ctk.CTkFrame(p)
        mode_frame.grid(row=row, column=0, columnspan=2, sticky='ew', pady=4, padx=4)
        row += 1

        _MODES = [
            ('search',    '① search',    '穷举搜索'),
            ('structure', '② structure', '计算结构'),
            ('auto',      '③ auto',      '搜索→结构一键通'),
            ('seidel',    '④ seidel',    '系统像差诊断'),
        ]

        mode_inner = ctk.CTkFrame(mode_frame, fg_color="transparent")
        mode_inner.pack(fill='x', padx=8, pady=8)
        mode_inner.columnconfigure((0, 1), weight=1)

        for i, (val, title, desc) in enumerate(_MODES):
            r, c = divmod(i, 2)
            card = ctk.CTkFrame(mode_inner, corner_radius=8, border_width=1,
                                 border_color="gray40", cursor="hand2")
            card.grid(row=r, column=c, padx=4, pady=4, sticky='ew')

            lbl_title = ctk.CTkLabel(card, text=title, font=("", 12, "bold"),
                                      anchor='w')
            lbl_title.pack(padx=12, pady=(8, 0), anchor='w')
            lbl_desc = ctk.CTkLabel(card, text=desc, font=("", 11),
                                     text_color="gray", anchor='w')
            lbl_desc.pack(padx=12, pady=(0, 8), anchor='w')

            self._mode_cards[val] = {
                'frame': card, 'title': lbl_title, 'desc': lbl_desc,
            }

            for widget in (card, lbl_title, lbl_desc):
                widget.bind("<Button-1>", lambda e, v=val: self._select_mode(v))

        self._refresh_mode_highlight()

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
        self._var_stop_gi   = tk.StringVar(value='2')
        self._var_stop_off  = tk.StringVar(value='0')
        self._var_fnum_w    = tk.StringVar(value='4.0')
        self._var_fnum_t    = tk.StringVar(value='5.6')
        self._var_sensor    = tk.StringVar(value='7.6')
        self._var_sys_srch  = tk.StringVar(value='30')
        self._var_sys_cand  = tk.StringVar(value='10')

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
        _w_toggle.grid(row=9, column=0, columnspan=2, sticky='w', pady=(6, 0))

        self._weights_frame = ctk.CTkFrame(sys_frame, fg_color='transparent')
        self._weights_frame.grid(row=10, column=0, columnspan=2, sticky='ew')
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

        # ═══ 可折叠：第二步参数 ════════════════════════════════════
        step2_section = CollapsibleSection(
            tab, title="第二步参数（结构/赛德尔模式）", default_open=False)
        step2_section.grid(row=row, column=0, sticky='ew', padx=4, pady=4)
        row += 1
        s2 = step2_section.content

        for lbl, key, dv, hint in [
            ("玻璃牌号",     'glass_names',      gdef['glass_names'],      '逗号分隔'),
            ("各片焦距(mm)", 'focal_lengths_mm', gdef['focal_lengths_mm'], '逗号分隔'),
            ("广义阿贝数",   'vgen_list',        gdef['vgen_list'],        '逗号分隔'),
            ("折射率字典",   'nd_vals',          gdef['nd_vals'],          '空=自动，或 玻璃:n,...'),
        ]:
            fi, _ = _labeled_entry(s2, lbl, key, dv, hint=hint)
            fi.pack(fill='x', pady=2)

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

    def _select_mode(self, mode: str):
        """卡片点击回调：更新模式变量并刷新高亮。"""
        self._var_mode.set(mode)
        self._refresh_mode_highlight()

    def _refresh_mode_highlight(self):
        """根据 _var_mode 当前值刷新卡片高亮状态。"""
        current = self._var_mode.get()
        for val, widgets in self._mode_cards.items():
            if val == current:
                widgets['frame'].configure(border_color="#7c83ff", border_width=2)
                widgets['title'].configure(text_color="#7c83ff")
                widgets['desc'].configure(text_color="#9999cc")
            else:
                widgets['frame'].configure(border_color="gray40", border_width=1)
                widgets['title'].configure(text_color=("gray10", "gray90"))
                widgets['desc'].configure(text_color="gray")

    # ──────────────────────────────────────────────────────────────────
    #  浏览文件
    # ──────────────────────────────────────────────────────────────────
    def _browse_xlsx(self):
        path = filedialog.askopenfilename(
            title="选择玻璃库 xlsx 文件",
            filetypes=[("Excel 文件", "*.xlsx;*.xls"), ("所有文件", "*.*")])
        if path:
            self._var_xlsx.set(path)

    def _browse_gap_csv(self):
        path = filedialog.askopenfilename(
            title="选择组间间距 CSV 文件",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if path:
            self._var_gap_csv.set(path)

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

            groups.append({
                'name':            _sv('name', f'G{gi+1}'),
                'zoom_csv_group':  _sv('zoom_csv_group', '') or None,
                'f_group':         float(_sv('f_group', '0')),
                'D':               float(_sv('D', '10')),
                'structure':       _parse_structure(struct_raw),
                'glass_roles':     _parse_list_str(roles_raw) if roles_raw else None,
                'apo':             _bv('apo', False),
                'cemented_pairs':  _parse_cemented_pairs(cem_raw),
                'spacings_mm':     _parse_floats(sp_raw) if sp_raw else [],
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
            'run_mode':          self._var_mode.get(),
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
            'run_mode':   self._var_mode.get(),
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
        _bool_keys = ['apo', 'allow_duplicate']
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

        self._var_mode.set(raw.get('run_mode', 'auto'))
        self._refresh_mode_highlight()
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
        _bool_keys = ['apo', 'allow_duplicate']
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
    def _run(self):
        if self._running:
            return

        try:
            params = self._collect_all_params()
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
        if success:
            self._log(f"\n{'═'*60}\n✅ 运行完成\n{'═'*60}\n")
        else:
            self._log(f"\n{'═'*60}\n❌ 运行出错：\n{err_msg}\n{'═'*60}\n")

    # ──────────────────────────────────────────────────────────────────
    #  Worker 线程入口
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _worker_entry(params: dict, log_queue: queue.Queue):
        """在独立线程中执行 run_action_a_pipeline，stdout 重定向到队列。"""
        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr
        _writer = _QueueWriter(log_queue)
        sys.stdout = _writer
        sys.stderr = _writer

        try:
            # 延迟导入，避免 GUI 启动时加载耗时模块
            action_a_dir = Path(__file__).parent
            if str(action_a_dir) not in sys.path:
                sys.path.insert(0, str(action_a_dir))
            from main import run_action_a_pipeline
            run_action_a_pipeline(params)
            log_queue.put('__DONE__')
        except Exception:
            error_msg = traceback.format_exc()
            # 通过已重定向的 stdout 写入队列，确保堆栈在日志中实时可见
            print(f"\n{'='*60}\n❌ 运行出错：\n{error_msg}\n{'='*60}")
            log_queue.put('__ERROR__')
            log_queue.put(error_msg)
        finally:
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr


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