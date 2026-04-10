"""
main.py
主程序入口：用户配置区 + 运行调度。

运行方式（必须在终端，多进程不能用单元格模式）：
    python main.py

模式切换：修改下方 RUN_MODE 变量：
    "search"    → 第一步：穷举搜索玻璃组合和光焦度分配
                   对 ALL_GROUPS 中每个组元依次搜索
    "structure" → 第二步：计算曲率半径、厚度并验证
                   对 ALL_GROUPS 中每个组元依次计算
    "auto"      → 第一步搜索完成后，自动用第一名衔接第二步
                   全部组元完成后自动执行系统级赛德尔分析
    "seidel"    → 第三步：全系统级赛德尔像差诊断
                   复用第二步所有配置 + S12 系统级专属参数

修复记录
--------
[BugFix-A] S_FOCAL_LENGTHS_MM 原来只有 3 项，但 S_GLASS_NAMES 有 4 块玻璃，
           H-F4 的焦距 -20.00 mm 缺失，已补充为 4 项。
[BugFix-B] S_SPACINGS_MM 原来为 [0, 4, 4]：
           spacing[0]=0 意味着片1-片2之间无间距（但它们并非胶合对），
           spacing[1]=4 意味着胶合面（片2-片3）有 4mm 间距（物理上不可能）。
           已修正为 [1, 0, 1]：胶合面（索引1）为 0，两端空气间隔各 1mm。
[BugFix-C] S_VGEN_LIST 原来只有 3 项，H-F4 的 V_gen=30.000 缺失，已补充。
[整合]      将 run_step3.py 的赛德尔分析逻辑整合为 "seidel" 模式，
           新增 S11 赛德尔专属配置区（4 个参数），消除两文件参数重复和不一致风险。
[多组元]    GROUP_PARAMS → ALL_GROUPS 列表；第二步配置 → ALL_* 列表；
           search/structure/auto 模式均支持批量多组元计算；
           seidel 模式改为系统级全链路分析，新增 S12 配置区。
"""

from __future__ import annotations

import contextlib
import copy
import math
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

from glass_db import load_glass_db, split_glass_db, _fill_nd_from_db
from search import action_a, print_results
from seidel_gemini import (
    analyze_all_zoom_positions,
    find_stop_index, load_zoom_positions_from_csv,
    print_all, print_seq_index,
)
from structure import compute_initial_structure
from validation import build_seq_with_dispersion, validate_initial_structure
from zoom_utils import compute_pbar_from_zoom_data, load_zoom_ray_csv, parse_csv_metadata, correct_zoom_spacings
from group_candidate import select_diverse_candidates
from system_optimizer import (find_best_combinations, generate_diagnosis_report,
                              refine_combination)
from config import EXCLUDED_FOR_OUTER
from runner import (_run_seidel_analysis, _run_system_seidel_analysis,
                    _build_system_seq, _stop_idx_from_group_seqs)


# ════════════════════════════════════════════════════════════════════
#  可调用的主流程函数（GUI 和命令行均调用此函数）
# ════════════════════════════════════════════════════════════════════
def run_action_a_pipeline(params: dict):
    """
    Action_a 主流程函数，GUI 和命令行均可调用。

    params 字典结构
    ---------------
    run_mode        : 'search' | 'structure' | 'auto' | 'seidel'
    glass_xlsx      : str，玻璃库 xlsx 路径
    melt_filter     : list[str]，熔炼频率过滤
    lam_short_nm    : float，短波波长 (nm)
    lam_ref_nm      : float，参考波长 (nm)
    lam_long_nm     : float，长波波长 (nm)
    top_n           : int，search/structure 模式保留候选数
    system_search_n : int，auto 模式 action_a 输出上限
    system_cand_n   : int，auto 模式多样性采样后每组保留数
    n_workers       : int，并行进程数
    phi_scan_steps  : int，光焦度扫描步数（默认 20）
    optical_percentile : int，光学百分位过滤（默认 30）
    tol_disp        : float，色差容差（默认 1e-4）
    w_apo           : float，APO 惩罚权重（默认 2000.0）
    tol_phi         : float，焦距约束容差（默认 1e-5）
    s_zoom_csv      : str | None，变焦行程 CSV 路径
    groups          : list[dict]，各组元配置（见下方字段说明）
    system          : dict，系统级分析参数

    groups[i] 字段
    --------------
    name            : str，组元名称（如 'G1'）
    zoom_csv_group  : str | None，CSV 列名前缀（变焦组填 'G2' 等，定焦组填 None/''）
    f_group         : float，组元等效焦距 (mm)
    D               : float，通光口径 (mm)
    structure       : list[str]，镜片正负列表，如 ['pos','neg','pos','pos']
    glass_roles     : list[str] | None，玻璃选择池限制，如 ['pos','neg','any','pos']
    apo             : bool，是否要求二级光谱校正
    cemented_pairs  : list[tuple] | None，胶合对（0-based），如 [(1,2)]
    min_f_mm        : float | None，最小片焦距约束
    max_f_mm        : float | None，最大片焦距约束
    allow_duplicate : bool，是否允许重复玻璃（默认 True）
    spacings_mm     : list[float]，片间距列表（胶合面处填 0）
    min_r_mm        : float，最小曲率半径约束
    t_edge_min      : float，正透镜最小边缘厚度
    t_center_min    : float，负透镜最小中心厚度
    t_cemented_min  : float，胶合组最小总厚度
    glass_names     : list[str]，第二步玻璃牌号（structure/seidel 模式）
    nd_vals         : dict，第二步折射率字典（空则从玻璃库自动读取）
    focal_lengths_mm: list[float]，第二步各片焦距 (mm)
    vgen_list       : list[float]，第二步广义阿贝数列表
    target_f_mm     : float，第二步目标组元焦距（通常等于 f_group）

    system 字段
    -----------
    gap_csv         : str | None，含组间间距列的 CSV 文件路径
    gap_columns     : list[str]，CSV 中组间间距列名（N-1 个）
    stop_group_idx  : int，光阑所在组元 0-based 索引（0=G1, 1=G2, ...）
    stop_offset     : int，组内面偏移（0=第一面，-1=最后一面）
    fnum_wide       : float，广角端 F/#
    fnum_tele       : float | None，长焦端 F/#；None 表示固定 F 数
    sensor_diag_mm  : float，传感器对角线 (mm)
    weights         : dict | None，像差权重字典 {'SI':5, 'SII':5, ...}
    """
    # ── 解包全局参数 ─────────────────────────────────────────────────
    RUN_MODE           = params['run_mode']
    GLASS_XLSX         = Path(params['glass_xlsx'])
    MELT_FILTER        = params.get('melt_filter', ['MA'])
    LAM_SHORT_NM       = float(params.get('lam_short_nm', 450))
    LAM_REF_NM         = float(params.get('lam_ref_nm', 550))
    LAM_LONG_NM        = float(params.get('lam_long_nm', 850))
    TOP_N              = int(params.get('top_n', 10))
    SYSTEM_SEARCH_N    = int(params.get('system_search_n', 30))
    SYSTEM_CAND_N      = int(params.get('system_cand_n', 10))
    N_WORKERS          = int(params.get('n_workers', 4))
    PHI_SCAN_STEPS     = int(params.get('phi_scan_steps', 20))
    OPTICAL_PERCENTILE = int(params.get('optical_percentile', 30))
    TOL_DISP           = float(params.get('tol_disp', 1e-4))
    W_APO              = float(params.get('w_apo', 2000.0))
    TOL_PHI            = float(params.get('tol_phi', 1e-5))

    _s_zoom_csv = params.get('s_zoom_csv') or None
    S_ZOOM_CSV  = Path(_s_zoom_csv) if _s_zoom_csv else None

    groups_cfg = params.get('groups', [])
    N_GROUPS   = len(groups_cfg)

    # ── 将 groups 列表还原为 ALL_GROUPS 风格的 dict 列表 ────────────
    ALL_GROUPS = []
    for g in groups_cfg:
        ALL_GROUPS.append({
            'name':            g['name'],
            'zoom_csv_group':  g.get('zoom_csv_group') or None,
            'f_group':         float(g['f_group']),
            'D':               float(g.get('D', g.get('d_mm', 10.0))),
            'structure':       g['structure'],
            'glass_roles':     g.get('glass_roles') or None,
            'apo':             bool(g.get('apo', False)),
            'cemented':        g.get('cemented_pairs') or None,
            'min_f_mm':        float(g['min_f_mm']) if g.get('min_f_mm') not in (None, '') else None,
            'max_f_mm':        float(g['max_f_mm']) if g.get('max_f_mm') not in (None, '') else None,
            'allow_duplicate': bool(g.get('allow_duplicate', True)),
        })

    # ── 第二步 ALL_* 列表 ────────────────────────────────────────────
    ALL_GLASS_NAMES     = [g.get('glass_names', [])      for g in groups_cfg]
    ALL_ND              = [dict(g.get('nd_vals', {}))     for g in groups_cfg]
    ALL_FOCAL_LENGTHS_MM= [g.get('focal_lengths_mm', []) for g in groups_cfg]
    ALL_CEMENTED_PAIRS  = [g.get('cemented_pairs') or []  for g in groups_cfg]
    ALL_SPACINGS_MM     = [g.get('spacings_mm', [])       for g in groups_cfg]
    ALL_D_MM            = [float(g.get('D', g.get('d_mm', 10.0))) for g in groups_cfg]
    ALL_VGEN_LIST       = [g.get('vgen_list', [])         for g in groups_cfg]
    ALL_TARGET_F_MM     = [float(g.get('target_f_mm', g['f_group'])) for g in groups_cfg]

    S_MIN_R_MM      = [float(g.get('min_r_mm', 20.0))     for g in groups_cfg]
    S_T_EDGE_MIN    = [float(g.get('t_edge_min', 1.0))    for g in groups_cfg]
    S_T_CENTER_MIN  = [float(g.get('t_center_min', 1.5))  for g in groups_cfg]
    S_T_CEMENTED_MIN= [float(g.get('t_cemented_min', 3.0))for g in groups_cfg]

    S_EFL_TOL       = 0.02
    S_SAG_R_WARN    = 0.20
    S_SAG_R_LIMIT   = 0.30
    S_PETZVAL_LIMIT = 0.05

    # ── 系统级参数 ───────────────────────────────────────────────────
    sys_cfg = params.get('system', {})
    _gap_csv = sys_cfg.get('gap_csv') or None
    S_SYSTEM_GAP_CSV        = Path(_gap_csv) if _gap_csv else None
    S_SYSTEM_GAP_COLUMNS    = sys_cfg.get('gap_columns', [])
    _raw_sgi = sys_cfg.get('stop_group_idx')
    if _raw_sgi is None or str(_raw_sgi).lower() in ('auto', 'none', ''):
        S_SYSTEM_STOP_GROUP_IDX = None   # 稍后从 CSV 元数据解析
    else:
        S_SYSTEM_STOP_GROUP_IDX = int(_raw_sgi)
    # ── auto 模式光阑位置：从 CSV 元数据自动读取 ────────────────────
    if S_SYSTEM_STOP_GROUP_IDX is None:
        if S_SYSTEM_GAP_CSV is not None and Path(S_SYSTEM_GAP_CSV).exists():
            _csv_meta = parse_csv_metadata(S_SYSTEM_GAP_CSV)
            if 'stop_group' in _csv_meta:
                S_SYSTEM_STOP_GROUP_IDX = _csv_meta['stop_group'] - 1  # 1-based → 0-based
                print(f"  ℹ 从 CSV 元数据自动读取光阑位置：G{_csv_meta['stop_group']}"
                      f"（索引 {S_SYSTEM_STOP_GROUP_IDX}）")
            else:
                S_SYSTEM_STOP_GROUP_IDX = 2   # 默认 G3
                print(f"  ℹ CSV 无光阑元数据，光阑位置使用默认值：G3（索引 2）")
        else:
            S_SYSTEM_STOP_GROUP_IDX = 2       # 无 CSV，默认 G3
    S_SYSTEM_STOP_OFFSET    = int(sys_cfg.get('stop_offset', 0))
    S_SYSTEM_FNUM_WIDE      = float(sys_cfg.get('fnum_wide', 4.0))
    _fnum_tele = sys_cfg.get('fnum_tele')
    S_SYSTEM_FNUM_TELE      = float(_fnum_tele) if _fnum_tele not in (None, '', 'None') else None
    S_SYSTEM_SENSOR_DIAG_MM = float(sys_cfg.get('sensor_diag_mm', 7.6))
    SYSTEM_ABERR_WEIGHTS    = sys_cfg.get('weights') or None

    # ══════════════════════════════════════════════════════════════════
    #  search 模式
    # ══════════════════════════════════════════════════════════════════
    if RUN_MODE == "search":
        glass_db = load_glass_db(
            GLASS_XLSX,
            melt_filter  = MELT_FILTER,
            lam_short_nm = LAM_SHORT_NM,
            lam_ref_nm   = LAM_REF_NM,
            lam_long_nm  = LAM_LONG_NM,
        )

        pos_pool_all, neg_pool_all = split_glass_db(glass_db)
        pos_pool_outer = [(name, g) for name, g in pos_pool_all
                          if name not in EXCLUDED_FOR_OUTER]
        neg_pool_outer = [(name, g) for name, g in neg_pool_all
                          if name not in EXCLUDED_FOR_OUTER]

        for gi, gp in enumerate(ALL_GROUPS):
            print(f"\n{'#'*82}")
            print(f"#  [ 组元 {gi+1}/{N_GROUPS}：{gp['name']} ]  "
                  f"f={gp['f_group']} mm  D={gp['D']} mm  "
                  f"结构={gp['structure']}")
            print(f"{'#'*82}")

            POOL_OVERRIDES = {0: pos_pool_outer} if gp['name'] == 'G1' else {}

            results = action_a(
                f_group               = gp["f_group"],
                D                     = gp["D"],
                structure             = gp["structure"],
                apo                   = gp["apo"],
                glass_db              = glass_db,
                glass_roles           = gp.get("glass_roles", None),
                cemented_pairs        = gp.get("cemented", None),
                phi_scan_steps        = PHI_SCAN_STEPS,
                min_f_mm              = gp.get("min_f_mm", None),
                max_f_mm              = gp.get("max_f_mm", None),
                allow_duplicate_glass = gp.get("allow_duplicate", False),
                adaptive_grouping     = False,
                pool_overrides        = POOL_OVERRIDES,
                optical_percentile    = OPTICAL_PERCENTILE,
                top_n                 = TOP_N,
                n_workers             = N_WORKERS,
                tol_disp              = TOL_DISP,
                w_apo                 = W_APO,
                tol_phi               = TOL_PHI,
            )

            print_results(results, gp["f_group"],
                          gp["structure"], gp["apo"],
                          glass_db,
                          tol_disp = TOL_DISP,
                          tol_phi  = TOL_PHI,
                          )

            if gi < N_GROUPS - 1:
                print(f"\n{'─'*82}")

        print(f"\n{'='*82}")
        print(f"  ✅ 共完成 {N_GROUPS} 个组元的搜索。")
        print(f"  提示：记录各组第一名的玻璃牌号和焦距，")
        print(f"  填入本文件【第二步配置区】的 ALL_* 列表，")
        print(f"  然后将 RUN_MODE 改为 \"structure\" 再次运行。")
        print(f"{'='*82}")

    # ══════════════════════════════════════════════════════════════════
    #  structure 模式
    # ══════════════════════════════════════════════════════════════════
    elif RUN_MODE == "structure":
        print(f"\n{'!'*82}")
        print(f"  ⚠  ALL_ND 应填写 n_ref（工作参考波长 {LAM_REF_NM:.0f}nm 的折射率），")
        print(f"     而非目录 d 线（587.56nm）的 nd。")
        print(f"     宽谱系统（{LAM_SHORT_NM:.0f}/{LAM_REF_NM:.0f}/{LAM_LONG_NM:.0f}nm）中两者偏差约 0.005~0.015，")
        print(f"     V4 Petzval 结果若与第一步搜索排名中的 P_ptz 不一致，请检查此项。")
        print(f"     推荐做法：从第一步输出表格的 \"n(λref)\" 列直接复制，无需重查手册。")
        print(f"{'!'*82}\n")

        _lists_to_check = {
            'groups[i].glass_names':      ALL_GLASS_NAMES,
            'groups[i].focal_lengths_mm': ALL_FOCAL_LENGTHS_MM,
            'groups[i].vgen_list':        ALL_VGEN_LIST,
            'groups[i].cemented_pairs':   ALL_CEMENTED_PAIRS,
            'groups[i].spacings_mm':      ALL_SPACINGS_MM,
            'groups[i].nd_vals':          ALL_ND,
            'groups':                     groups_cfg,
        }
        _errors = []
        for _name, _lst in _lists_to_check.items():
            if len(_lst) != N_GROUPS:
                _errors.append(f"  {_name} 有 {len(_lst)} 项，期望 {N_GROUPS} 项")
        if _errors:
            print(f"\n❌ 配置区长度不匹配，请检查以下列表：")
            for _e in _errors:
                print(_e)
            sys.exit(1)

        _glass_db_cache = None

        for gi, gp in enumerate(ALL_GROUPS):
            gnames    = ALL_GLASS_NAMES[gi]
            nd_vals   = dict(ALL_ND[gi])
            f_list    = ALL_FOCAL_LENGTHS_MM[gi]
            cem_pairs = ALL_CEMENTED_PAIRS[gi]
            spacings  = ALL_SPACINGS_MM[gi]
            d_mm      = ALL_D_MM[gi]
            vgen      = ALL_VGEN_LIST[gi]
            target_f  = ALL_TARGET_F_MM[gi]

            print(f"\n{'#'*82}")
            print(f"#  [ 组元 {gi+1}/{N_GROUPS}：{gp['name']} ]  初始结构计算")
            print(f"#  玻璃：{' / '.join(gnames)}")
            print(f"#  焦距：{f_list} mm")
            print(f"{'#'*82}")

            missing = [g for g in gnames if g not in nd_vals]
            if missing:
                if _glass_db_cache is None:
                    _glass_db_cache = load_glass_db(
                        GLASS_XLSX,
                        melt_filter  = MELT_FILTER,
                        lam_short_nm = LAM_SHORT_NM,
                        lam_ref_nm   = LAM_REF_NM,
                        lam_long_nm  = LAM_LONG_NM,
                        verbose      = False,
                    )
                print(f"  ND 字典缺少以下玻璃的折射率，尝试从玻璃库读取：{missing}")
                _fill_nd_from_db(missing, nd_vals, _glass_db_cache)

            pbar_overrides = {}
            _csv_group = gp.get("zoom_csv_group")
            if S_ZOOM_CSV is not None and _csv_group is not None:
                csv_path = S_ZOOM_CSV if S_ZOOM_CSV.is_absolute() \
                           else Path(__file__).parent / S_ZOOM_CSV
                _lens_map = {i: _csv_group for i in range(len(gp["structure"]))}
                print(f"\n  变焦组共轭因子加权平均计算  CSV：{csv_path}  组名：{_csv_group}")
                zoom_ray_data = load_zoom_ray_csv(csv_path, _lens_map)
                for lens_idx, positions in zoom_ray_data.items():
                    gname_str = gnames[lens_idx] if lens_idx < len(gnames) else f'片{lens_idx+1}'
                    print(f"\n  片{lens_idx+1}（{gname_str}）的变焦行程数据：")
                    p_bar, _, _ = compute_pbar_from_zoom_data(positions, verbose=True)
                    pbar_overrides[lens_idx] = p_bar
                print(f"\n  汇总：pbar_overrides = {{"
                      + ", ".join(f"{k}: {v:+.6f}" for k, v in pbar_overrides.items())
                      + "}")
            else:
                reason = "s_zoom_csv 为 None" if S_ZOOM_CSV is None else "zoom_csv_group 为 None"
                print(f"\n  提示：{reason}，所有片位均使用单点追迹 p 值。")

            struct_result = compute_initial_structure(
                glass_names      = gnames,
                nd_values        = nd_vals,
                focal_lengths_mm = f_list,
                cemented_pairs   = cem_pairs,
                spacings_mm      = spacings,
                D_mm             = d_mm,
                min_R_mm         = S_MIN_R_MM[gi],
                t_edge_min       = S_T_EDGE_MIN[gi],
                t_center_min     = S_T_CENTER_MIN[gi],
                t_cemented_min   = S_T_CEMENTED_MIN[gi],
                h1               = d_mm / 2.0,
                u0               = 0.0,
                pbar_overrides   = pbar_overrides,
            )

            validate_initial_structure(
                result           = struct_result,
                glass_names      = gnames,
                nd_values        = nd_vals,
                focal_lengths_mm = f_list,
                cemented_pairs   = cem_pairs,
                spacings_mm      = spacings,
                D_mm             = d_mm,
                target_f_mm      = target_f,
                Vgen_list        = vgen,
                efl_tol          = S_EFL_TOL,
                sag_r_warn       = S_SAG_R_WARN,
                sag_r_limit      = S_SAG_R_LIMIT,
                petzval_limit    = S_PETZVAL_LIMIT,
            )

            if gi < N_GROUPS - 1:
                print(f"\n{'─'*82}")

        print(f"\n  提示：第二步完成后，可将 RUN_MODE 改为 \"seidel\" 运行第三步。")
        print(f"  首次运行 seidel 模式前，请先对照上方面序列索引填好 S12 配置区。")

    # ══════════════════════════════════════════════════════════════════
    #  auto 模式
    # ══════════════════════════════════════════════════════════════════
    elif RUN_MODE == "auto":
        glass_db = load_glass_db(
            GLASS_XLSX,
            melt_filter  = MELT_FILTER,
            lam_short_nm = LAM_SHORT_NM,
            lam_ref_nm   = LAM_REF_NM,
            lam_long_nm  = LAM_LONG_NM,
        )

        pos_pool_all, neg_pool_all = split_glass_db(glass_db)
        pos_pool_outer = [(name, g) for name, g in pos_pool_all
                          if name not in EXCLUDED_FOR_OUTER]
        neg_pool_outer = [(name, g) for name, g in neg_pool_all
                          if name not in EXCLUDED_FOR_OUTER]

        auto_struct_results  = []
        auto_all_glass_names = []
        auto_all_nd_values   = []
        auto_all_vgen_lists  = []
        auto_group_candidates = []

        # ── 串行搜索各组元（action_a 内部 ProcessPoolExecutor 自己利用多核）──
        print(f"\n  串行搜索 {N_GROUPS} 个组元（每组内部用 {N_WORKERS} 进程并行）...")
        _worker_results = []
        for gi, gp in enumerate(ALL_GROUPS):
            _cem_pairs  = ALL_CEMENTED_PAIRS[gi]
            _spacings   = ALL_SPACINGS_MM[gi]
            _d_mm       = ALL_D_MM[gi]
            _min_r_mm   = S_MIN_R_MM[gi]
            _t_edge     = S_T_EDGE_MIN[gi]
            _t_center   = S_T_CENTER_MIN[gi]
            _t_cemented = S_T_CEMENTED_MIN[gi]
            _zoom_csv_str = str(S_ZOOM_CSV) if S_ZOOM_CSV is not None else None

            try:
                POOL_OVERRIDES = {0: pos_pool_outer} if gp['name'] == 'G1' else {}

                results = action_a(
                    f_group               = gp["f_group"],
                    D                     = gp["D"],
                    structure             = gp["structure"],
                    apo                   = gp["apo"],
                    glass_db              = glass_db,
                    glass_roles           = gp.get("glass_roles", None),
                    cemented_pairs        = gp.get("cemented", None),
                    phi_scan_steps        = PHI_SCAN_STEPS,
                    min_f_mm              = gp.get("min_f_mm", None),
                    max_f_mm              = gp.get("max_f_mm", None),
                    allow_duplicate_glass = gp.get("allow_duplicate", False),
                    adaptive_grouping     = False,
                    pool_overrides        = POOL_OVERRIDES,
                    optical_percentile    = OPTICAL_PERCENTILE,
                    top_n                 = SYSTEM_SEARCH_N,
                    n_workers             = N_WORKERS,   # 由 action_a 内部 ProcessPoolExecutor 并行
                    tol_disp              = TOL_DISP,
                    w_apo                 = W_APO,
                    tol_phi               = TOL_PHI,
                )

                if not results:
                    _worker_results.append({
                        'gi': gi, 'candidates': [], 'struct_result': None,
                        'glass_names': [], 'nd_values': {}, 'vgen_list': [],
                        'focal_lengths': [], 'pbar_overrides': {}, 'results': [],
                        'success': True, 'error': None,
                    })
                    continue

                best               = results[0]
                _auto_glass_names  = best['names']
                _auto_nd_values    = {name: n for name, n
                                      in zip(best['names'], best['ns'])}
                _auto_focal_lengths = [1.0 / p for p in best['phis']]
                _auto_vgen_list    = best['Vgens']

                # 计算 pbar_overrides（变焦组）
                _pbar_overrides = {}
                _csv_group = gp.get("zoom_csv_group")
                if _zoom_csv_str and _csv_group:
                    _csv_path = Path(_zoom_csv_str)
                    _lens_map = {i: _csv_group for i in range(len(gp["structure"]))}
                    try:
                        zoom_ray_data = load_zoom_ray_csv(_csv_path, _lens_map)
                        for lens_idx, positions in zoom_ray_data.items():
                            p_bar, _, _ = compute_pbar_from_zoom_data(
                                positions, verbose=False)
                            _pbar_overrides[lens_idx] = p_bar
                    except Exception as _e:
                        print(f"  ⚠ [{gp['name']}] 变焦 CSV 读取失败：{_e}")

                _struct_result = compute_initial_structure(
                    glass_names      = _auto_glass_names,
                    nd_values        = _auto_nd_values,
                    focal_lengths_mm = _auto_focal_lengths,
                    cemented_pairs   = _cem_pairs,
                    spacings_mm      = _spacings,
                    D_mm             = _d_mm,
                    min_R_mm         = _min_r_mm,
                    t_edge_min       = _t_edge,
                    t_center_min     = _t_center,
                    t_cemented_min   = _t_cemented,
                    h1               = _d_mm / 2.0,
                    u0               = 0.0,
                    pbar_overrides   = _pbar_overrides,
                )

                _group_cands = select_diverse_candidates(
                    search_results  = results,
                    group_index     = gi,
                    group_name      = gp['name'],
                    cemented_pairs  = _cem_pairs,
                    spacings_mm     = _spacings,
                    d_mm            = _d_mm,
                    min_r_mm        = _min_r_mm,
                    t_edge_min      = _t_edge,
                    t_center_min    = _t_center,
                    t_cemented_min  = _t_cemented,
                    pbar_overrides  = _pbar_overrides,
                    top_n           = SYSTEM_CAND_N,
                )

                _worker_results.append({
                    'gi':            gi,
                    'candidates':    _group_cands,
                    'struct_result': _struct_result,
                    'glass_names':   _auto_glass_names,
                    'nd_values':     _auto_nd_values,
                    'vgen_list':     _auto_vgen_list,
                    'focal_lengths': _auto_focal_lengths,
                    'pbar_overrides':_pbar_overrides,
                    'results':       results,
                    'success':       True,
                    'error':         None,
                })

            except Exception:
                import traceback as _tb
                _worker_results.append({
                    'gi': gi, 'candidates': [], 'struct_result': None,
                    'glass_names': [], 'nd_values': {}, 'vgen_list': [],
                    'focal_lengths': [], 'pbar_overrides': {}, 'results': [],
                    'success': False, 'error': _tb.format_exc(),
                })

        # ── 处理各组元结果 ────────────────────────────────────────────
        for _wr in _worker_results:
            gi = _wr['gi']
            gp = ALL_GROUPS[gi]

            print(f"\n{'#'*82}")
            print(f"#  [ 组元 {gi+1}/{N_GROUPS}：{gp['name']} ]  "
                  f"AUTO（搜索 → 结构一键直通）")
            print(f"{'#'*82}")

            if not _wr['success']:
                print(f"\n  ❌ {gp['name']} 搜索或结构计算出错：\n{_wr['error']}")
                auto_struct_results.append(None)
                auto_all_glass_names.append([])
                auto_all_nd_values.append({})
                auto_all_vgen_lists.append([])
                auto_group_candidates.append([])
                continue

            _results_gi = _wr['results']
            if _results_gi:
                print_results(_results_gi, gp["f_group"],
                              gp["structure"], gp["apo"],
                              glass_db,
                              tol_disp = TOL_DISP,
                              tol_phi  = TOL_PHI)

            if _wr['struct_result'] is None:
                print(f"\n  [AUTO] {gp['name']} 搜索无结果，跳过结构计算。")
                auto_struct_results.append(None)
                auto_all_glass_names.append([])
                auto_all_nd_values.append({})
                auto_all_vgen_lists.append([])
                auto_group_candidates.append([])
                continue

            auto_glass_names   = _wr['glass_names']
            auto_nd_values     = _wr['nd_values']
            auto_focal_lengths = _wr['focal_lengths']
            auto_vgen_list     = _wr['vgen_list']
            pbar_overrides     = _wr['pbar_overrides']

            print(f"\n{'='*82}")
            print(f"  [AUTO] {gp['name']} 自动衔接第二步：使用第一名结果")
            print(f"  玻璃：{' / '.join(auto_glass_names)}")
            print(f"  焦距：{[round(f, 3) for f in auto_focal_lengths]} mm")
            print(f"  V_gen：{[round(v, 3) if v else None for v in auto_vgen_list]}")
            print(f"{'='*82}")

            if pbar_overrides:
                print(f"\n  pbar_overrides = {{"
                      + ", ".join(f"{k}: {v:+.6f}" for k, v in pbar_overrides.items())
                      + "}")
            else:
                reason = ("s_zoom_csv 为 None" if S_ZOOM_CSV is None
                          else "zoom_csv_group 为 None")
                print(f"\n  提示：{reason}，所有片位均使用单点追迹 p 值。")

            validate_initial_structure(
                result           = _wr['struct_result'],
                glass_names      = auto_glass_names,
                nd_values        = auto_nd_values,
                focal_lengths_mm = auto_focal_lengths,
                cemented_pairs   = ALL_CEMENTED_PAIRS[gi],
                spacings_mm      = ALL_SPACINGS_MM[gi],
                D_mm             = ALL_D_MM[gi],
                target_f_mm      = gp["f_group"],
                Vgen_list        = auto_vgen_list,
                efl_tol          = S_EFL_TOL,
                sag_r_warn       = S_SAG_R_WARN,
                sag_r_limit      = S_SAG_R_LIMIT,
                petzval_limit    = S_PETZVAL_LIMIT,
            )

            auto_struct_results.append(_wr['struct_result'])
            auto_all_glass_names.append(auto_glass_names)
            auto_all_nd_values.append(auto_nd_values)
            auto_all_vgen_lists.append(auto_vgen_list)
            auto_group_candidates.append(_wr['candidates'])

            if gi < N_GROUPS - 1:
                print(f"\n{'─'*82}")

        valid_count = sum(1 for r in auto_struct_results if r is not None)
        if valid_count == N_GROUPS:
            # ── 主面间距修正：提取各组主面位置 ─────────────────────────
            group_principal_planes = []
            for _gi_pp in range(N_GROUPS):
                _sr = auto_struct_results[_gi_pp]
                _dH  = _sr.get('delta_H', 0.0)
                _dHp = _sr.get('delta_Hp', 0.0)
                group_principal_planes.append((_dH, _dHp))
                print(f"  {ALL_GROUPS[_gi_pp]['name']}: delta_H={_dH:+.4f} mm, delta_Hp={_dHp:+.4f} mm")

            _sys_opt_done = False

            if S_SYSTEM_GAP_CSV is None:
                print(f"\n  ⚠ 未配置组间间距 CSV（S_SYSTEM_GAP_CSV=None），"
                      f"跳过系统级联合优化。")
            elif not all(auto_group_candidates):
                print(f"\n  ⚠ 部分组元无候选方案，跳过系统级联合优化。")
            else:
                _sys_csv_path = S_SYSTEM_GAP_CSV if S_SYSTEM_GAP_CSV.is_absolute() \
                                else Path(__file__).parent / S_SYSTEM_GAP_CSV
                if not _sys_csv_path.exists():
                    print(f"\n  ⚠ 组间间距 CSV 不存在（{_sys_csv_path}），"
                          f"跳过系统级联合优化。")
                else:
                    try:
                        _sys_zoom_pos = load_zoom_positions_from_csv(
                            _sys_csv_path,
                            gap_col_names = S_SYSTEM_GAP_COLUMNS,
                        )
                        _sys_efls = [zp['efl'] for zp in _sys_zoom_pos]

                        if any(e is None for e in _sys_efls):
                            print(f"\n  ⚠ CSV 中存在 EFL=None，无法计算入瞳直径，"
                                  f"跳过系统级联合优化。")
                        else:
                            _fnum_t   = (S_SYSTEM_FNUM_TELE
                                         if S_SYSTEM_FNUM_TELE is not None
                                         else S_SYSTEM_FNUM_WIDE)
                            _efl_min  = min(_sys_efls)
                            _efl_max  = max(_sys_efls)
                            _efl_span = _efl_max - _efl_min if _efl_max != _efl_min else 1.0
                            _sys_d_mm = []
                            for _efl in _sys_efls:
                                _t    = (_efl - _efl_min) / _efl_span
                                _fnum = S_SYSTEM_FNUM_WIDE + (_fnum_t - S_SYSTEM_FNUM_WIDE) * _t
                                _sys_d_mm.append(_efl / _fnum)
                            _sys_fov = math.atan(S_SYSTEM_SENSOR_DIAG_MM / 2.0 / _efl_min)

                            print(f"\n{'#'*82}")
                            print(f"#  [ 系统级联合优化 ]  {N_GROUPS} 个组元候选就绪，"
                                  f"变焦位置数={len(_sys_zoom_pos)}")
                            print(f"{'#'*82}")

                            _ref_n_surfs  = [cands[0].n_surfaces
                                             for cands in auto_group_candidates]
                            _sys_stop_idx = _stop_idx_from_group_seqs(
                                [[None] * n for n in _ref_n_surfs],
                                S_SYSTEM_STOP_GROUP_IDX,
                                S_SYSTEM_STOP_OFFSET,
                            )
                            print(f"  光阑面绝对索引（组元索引+偏移计算）: {_sys_stop_idx}")

                            _best_combos = find_best_combinations(
                                all_group_candidates = auto_group_candidates,
                                zoom_positions       = _sys_zoom_pos,
                                stop_idx             = _sys_stop_idx,
                                d_mm_list            = _sys_d_mm,
                                half_fov_rad         = _sys_fov,
                                top_k                = 5,
                                weights              = SYSTEM_ABERR_WEIGHTS,
                            )

                            if _best_combos:
                                _best_combo    = _best_combos[0]['combo']
                                _initial_merit = _best_combos[0]['merit']

                                print(f"\n{'#'*82}")
                                print(f"#  [ 反馈迭代优化 ]  对最优组合做形状因子微调")
                                print(f"{'#'*82}")

                                _refined_combo, _refined_merit, _refine_report = refine_combination(
                                    best_combo         = _best_combo,
                                    zoom_positions     = _sys_zoom_pos,
                                    stop_group_idx     = S_SYSTEM_STOP_GROUP_IDX,
                                    stop_offset        = S_SYSTEM_STOP_OFFSET,
                                    d_mm_list          = _sys_d_mm,
                                    half_fov_rad       = _sys_fov,
                                    all_cemented_pairs = ALL_CEMENTED_PAIRS,
                                    all_spacings_mm    = ALL_SPACINGS_MM,
                                    weights            = SYSTEM_ABERR_WEIGHTS,
                                    max_iter           = 300,
                                )

                                print(f"  优化前 merit = {_initial_merit:.4f}")
                                print(f"  优化后 merit = {_refined_merit:.4f}")
                                print(f"  改善 = {_refine_report['improvement_pct']:.1f}%")

                                if _refined_merit < _initial_merit:
                                    _best_combo = _refined_combo
                                    print(f"  ✅ 采用优化后的结构")
                                else:
                                    print(f"  ⚠ 优化未改善，保持原结构")

                                generate_diagnosis_report(
                                    best_combo     = _best_combo,
                                    zoom_positions = _sys_zoom_pos,
                                    stop_idx       = _sys_stop_idx,
                                    d_mm_list      = _sys_d_mm,
                                    half_fov_rad   = _sys_fov,
                                )

                                _best_sys = _best_combo
                                auto_struct_results  = [c.struct_result  for c in _best_sys]
                                auto_all_glass_names = [c.glass_combo    for c in _best_sys]
                                auto_all_nd_values   = [c.nd_values      for c in _best_sys]
                                auto_all_vgen_lists  = [c.vgen_list      for c in _best_sys]
                                _sys_opt_done = True

                    except Exception as _e:
                        print(f"\n  ⚠ 系统级联合优化异常（{_e}），"
                              f"继续使用各组第一名进行后续赛德尔分析。")

            if _sys_opt_done:
                print(f"\n{'#'*82}")
                print(f"#  [ 系统级赛德尔分析 ]  使用联合优化最优组合")
                print(f"{'#'*82}")
            else:
                print(f"\n{'#'*82}")
                print(f"#  [ 系统级赛德尔分析 ]  全 {N_GROUPS} 个组元结构计算完成（各组第一名）")
                print(f"{'#'*82}")

            # ── 主面间距修正并保存 ────────────────────────────────────
            if S_SYSTEM_GAP_CSV is not None:
                from zoom_utils import load_zoom_configs_for_zemax
                _sys_csv_path_pp = (S_SYSTEM_GAP_CSV if S_SYSTEM_GAP_CSV.is_absolute()
                                    else Path(__file__).parent / S_SYSTEM_GAP_CSV)
                try:
                    _raw_zoom_cfgs = load_zoom_configs_for_zemax(
                        csv_path  = _sys_csv_path_pp,
                        bfd_mm    = 8.0,  # 默认 BFD，后续 Zemax 闭环中迭代修正
                        fnum_wide = S_SYSTEM_FNUM_WIDE,
                        fnum_tele = S_SYSTEM_FNUM_TELE,
                    )
                    _corrected_zoom_cfgs = correct_zoom_spacings(
                        _raw_zoom_cfgs, group_principal_planes)

                    # 保存到 JSON，供 Zemax 写入脚本直接读取
                    _corr_data = {
                        'group_principal_planes': [
                            {'group': ALL_GROUPS[i]['name'],
                             'delta_H': group_principal_planes[i][0],
                             'delta_Hp': group_principal_planes[i][1]}
                            for i in range(N_GROUPS)
                        ],
                        'corrected_zoom_configs': [
                            {'name': c[0], 'efl': c[1],
                             'd1': c[2], 'd2': c[3], 'd3': c[4], 'epd': c[5]}
                            for c in _corrected_zoom_cfgs
                        ],
                        'raw_zoom_configs': [
                            {'name': c[0], 'efl': c[1],
                             'd1': c[2], 'd2': c[3], 'd3': c[4], 'epd': c[5]}
                            for c in _raw_zoom_cfgs
                        ],
                    }
                    _corr_path = Path(__file__).parent / 'last_run_config.json'
                    # 合并到已有的 last_run_config.json
                    _existing = {}
                    if _corr_path.exists():
                        import json as _json
                        with open(_corr_path, 'r', encoding='utf-8') as _f:
                            _existing = _json.load(_f)

                    # 构建各组面处方（供 Zemax 写入脚本使用）
                    all_surface_prescriptions = []
                    for _gi_sp in range(N_GROUPS):
                        _sr_sp = auto_struct_results[_gi_sp]
                        _gnames_sp = auto_all_glass_names[_gi_sp]
                        _nd_sp = auto_all_nd_values[_gi_sp]
                        _cem_sp = ALL_CEMENTED_PAIRS[_gi_sp]
                        _spacings_sp = ALL_SPACINGS_MM[_gi_sp]
                        _thick_sp = _sr_sp['thickness']
                        _surfs_sp = _sr_sp['surfaces']

                        # 按 compute_initial_structure 输出格式，
                        # 逐面构建 (面序号, 描述, R, nd, 厚度, 玻璃名) 记录
                        group_surfaces = []
                        surf_list_sp = list(enumerate(_surfs_sp))
                        for si, (desc, R_val, lens_idx, is_cem) in surf_list_sp:
                            # 确定本面之后的玻璃和厚度
                            if is_cem:
                                # 胶合面后是后片玻璃
                                pair = next((p for p in _cem_sp if p[0] == lens_idx), None)
                                if pair:
                                    next_li = pair[1]
                                    glass = _gnames_sp[next_li]
                                    nd = _nd_sp[glass]
                                    t = _thick_sp[next_li][0]
                                else:
                                    glass = None
                                    nd = 1.0
                                    t = 0.0
                            elif si + 1 < len(_surfs_sp):
                                _, _, next_li, next_is_cem = _surfs_sp[si + 1]
                                if next_li == lens_idx or next_is_cem:
                                    # 前表面，下一个是胶合面或同片 → 本片玻璃
                                    glass = _gnames_sp[lens_idx]
                                    nd = _nd_sp[glass]
                                    t = _thick_sp[lens_idx][0]
                                else:
                                    # 后表面 → 空气间隔
                                    glass = None
                                    nd = 1.0
                                    t = _spacings_sp[lens_idx] if lens_idx < len(_spacings_sp) else 0.0
                            else:
                                # 组内最后一面
                                glass = None
                                nd = 1.0
                                t = 0.0

                            group_surfaces.append({
                                'desc': desc,
                                'R': round(R_val, 3),
                                'nd': round(nd, 4),
                                't': round(t, 4),
                                'glass': glass,
                            })

                        all_surface_prescriptions.append({
                            'group': ALL_GROUPS[_gi_sp]['name'],
                            'surfaces': group_surfaces,
                        })

                    _corr_data['surface_prescriptions'] = all_surface_prescriptions
                    _existing['principal_plane_correction'] = _corr_data
                    import json as _json
                    with open(_corr_path, 'w', encoding='utf-8') as _f:
                        _json.dump(_existing, _f, indent=2, ensure_ascii=False)
                    print(f"\n  ✅ 主面修正数据已保存到 {_corr_path}")
                except Exception as _e:
                    print(f"\n  ⚠ 主面间距修正失败：{_e}，后续 Zemax 写入将使用未修正间距。")

            _run_system_seidel_analysis(
                all_struct_results = auto_struct_results,
                all_glass_names    = auto_all_glass_names,
                all_nd_values      = auto_all_nd_values,
                all_cemented_pairs = ALL_CEMENTED_PAIRS,
                all_spacings_mm    = ALL_SPACINGS_MM,
                all_vgen_list      = auto_all_vgen_lists,
                system_gap_csv     = S_SYSTEM_GAP_CSV,
                system_gap_columns = S_SYSTEM_GAP_COLUMNS,
                stop_group_idx     = S_SYSTEM_STOP_GROUP_IDX,
                stop_offset        = S_SYSTEM_STOP_OFFSET,
                sensor_diag_mm     = S_SYSTEM_SENSOR_DIAG_MM,
                fnum_wide          = S_SYSTEM_FNUM_WIDE,
                fnum_tele          = S_SYSTEM_FNUM_TELE,
            )
        else:
            print(f"\n  ⚠ 仅 {valid_count}/{N_GROUPS} 个组元完成结构计算，"
                  f"跳过系统级赛德尔分析。")

    # ══════════════════════════════════════════════════════════════════
    #  seidel 模式
    # ══════════════════════════════════════════════════════════════════
    elif RUN_MODE == "seidel":
        _lists_to_check = {
            'groups[i].glass_names':      ALL_GLASS_NAMES,
            'groups[i].focal_lengths_mm': ALL_FOCAL_LENGTHS_MM,
            'groups[i].vgen_list':        ALL_VGEN_LIST,
            'groups[i].cemented_pairs':   ALL_CEMENTED_PAIRS,
            'groups[i].spacings_mm':      ALL_SPACINGS_MM,
            'groups[i].nd_vals':          ALL_ND,
            'groups':                     groups_cfg,
        }
        _errors = []
        for _name, _lst in _lists_to_check.items():
            if len(_lst) != N_GROUPS:
                _errors.append(f"  {_name} 有 {len(_lst)} 项，期望 {N_GROUPS} 项")
        if _errors:
            print(f"\n❌ 配置区长度不匹配，请检查以下列表：")
            for _e in _errors:
                print(_e)
            sys.exit(1)

        print(f"\n{'='*82}")
        print(f"  动作A  模式：系统级赛德尔像差诊断  共 {N_GROUPS} 个组元")
        print(f"  F/#：{S_SYSTEM_FNUM_WIDE}~{S_SYSTEM_FNUM_TELE}   传感器对角线：{S_SYSTEM_SENSOR_DIAG_MM} mm")
        print(f"{'='*82}")

        _glass_db_cache = None
        seidel_struct_results  = []
        seidel_all_nd_values   = []

        for gi, gp in enumerate(ALL_GROUPS):
            gnames    = ALL_GLASS_NAMES[gi]
            nd_vals   = dict(ALL_ND[gi])
            f_list    = ALL_FOCAL_LENGTHS_MM[gi]
            cem_pairs = ALL_CEMENTED_PAIRS[gi]
            spacings  = ALL_SPACINGS_MM[gi]
            d_mm      = ALL_D_MM[gi]

            missing = [g for g in gnames if g not in nd_vals]
            if missing:
                if _glass_db_cache is None:
                    _glass_db_cache = load_glass_db(
                        GLASS_XLSX,
                        melt_filter  = MELT_FILTER,
                        lam_short_nm = LAM_SHORT_NM,
                        lam_ref_nm   = LAM_REF_NM,
                        lam_long_nm  = LAM_LONG_NM,
                        verbose      = False,
                    )
                print(f"  {gp['name']} ND 字典缺少以下玻璃，尝试从玻璃库读取：{missing}")
                _fill_nd_from_db(missing, nd_vals, _glass_db_cache)

            if len(gnames) != len(ALL_VGEN_LIST[gi]):
                raise ValueError(
                    f"{gp['name']} glass_names（{len(gnames)} 项）与 "
                    f"vgen_list（{len(ALL_VGEN_LIST[gi])} 项）长度不匹配。"
                )
            if len(gnames) != len(f_list):
                raise ValueError(
                    f"{gp['name']} glass_names（{len(gnames)} 项）与 "
                    f"focal_lengths_mm（{len(f_list)} 项）长度不匹配。"
                )

            # seidel 模式同样需要计算 pbar_overrides，与 structure/auto 模式保持一致
            _pbar_overrides_seidel = {}
            _csv_group_seidel = gp.get("zoom_csv_group")
            if S_ZOOM_CSV is not None and _csv_group_seidel is not None:
                _csv_path_seidel = (S_ZOOM_CSV if S_ZOOM_CSV.is_absolute()
                                    else Path(__file__).parent / S_ZOOM_CSV)
                _lens_map_seidel = {i: _csv_group_seidel
                                    for i in range(len(gp["structure"]))}
                try:
                    zoom_ray_data_seidel = load_zoom_ray_csv(
                        _csv_path_seidel, _lens_map_seidel)
                    for lens_idx, positions in zoom_ray_data_seidel.items():
                        p_bar, _, _ = compute_pbar_from_zoom_data(
                            positions, verbose=False)
                        _pbar_overrides_seidel[lens_idx] = p_bar
                    print(f"  {gp['name']} pbar_overrides = {{"
                          + ", ".join(f"{k}: {v:+.6f}"
                                      for k, v in _pbar_overrides_seidel.items())
                          + "}")
                except Exception as _e:
                    print(f"  ⚠ [{gp['name']}] 变焦 CSV 读取失败：{_e}，"
                          f"使用单点 p 值。")

            with open(os.devnull, 'w', encoding='utf-8') as devnull, \
                    contextlib.redirect_stdout(devnull):
                struct_result = compute_initial_structure(
                    glass_names      = gnames,
                    nd_values        = nd_vals,
                    focal_lengths_mm = f_list,
                    cemented_pairs   = cem_pairs,
                    spacings_mm      = spacings,
                    D_mm             = d_mm,
                    min_R_mm         = S_MIN_R_MM[gi],
                    t_edge_min       = S_T_EDGE_MIN[gi],
                    t_center_min     = S_T_CENTER_MIN[gi],
                    t_cemented_min   = S_T_CEMENTED_MIN[gi],
                    pbar_overrides   = _pbar_overrides_seidel,
                )

            seidel_struct_results.append(struct_result)
            seidel_all_nd_values.append(nd_vals)

        _run_system_seidel_analysis(
            all_struct_results = seidel_struct_results,
            all_glass_names    = ALL_GLASS_NAMES,
            all_nd_values      = seidel_all_nd_values,
            all_cemented_pairs = ALL_CEMENTED_PAIRS,
            all_spacings_mm    = ALL_SPACINGS_MM,
            all_vgen_list      = ALL_VGEN_LIST,
            system_gap_csv     = S_SYSTEM_GAP_CSV,
            system_gap_columns = S_SYSTEM_GAP_COLUMNS,
            stop_group_idx     = S_SYSTEM_STOP_GROUP_IDX,
            stop_offset        = S_SYSTEM_STOP_OFFSET,
            sensor_diag_mm     = S_SYSTEM_SENSOR_DIAG_MM,
            fnum_wide          = S_SYSTEM_FNUM_WIDE,
            fnum_tele          = S_SYSTEM_FNUM_TELE,
        )

    else:
        print(f"未知的 run_mode='{RUN_MODE}'，"
              f"请设置为 \"search\"、\"structure\"、\"auto\" 或 \"seidel\"。")


# ════════════════════════════════════════════════════════════════════
#  主程序入口
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 多进程在 Windows 上必须在 if __name__ == "__main__" 保护下启动。

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  ★  运行模式选择  ★                                         ║
    # ║                                                              ║
    # ║  "search"    → 第一步：对 ALL_GROUPS 每个组元穷举搜索        ║
    # ║                完成后记录各组第一名，填入第二步配置区。       ║
    # ║                                                              ║
    # ║  "structure" → 第二步：对每个组元计算曲率半径、厚度并验证    ║
    # ║                入口处校验所有 ALL_* 列表长度与 ALL_GROUPS    ║
    # ║                一致，不一致则打印明确错误信息并退出。        ║
    # ║                                                              ║
    # ║  "auto"      → 搜索 + 结构一键直通，全部组元完成后          ║
    # ║                自动执行系统级赛德尔分析（需填好 S12）。      ║
    # ║                                                              ║
    # ║  "seidel"    → 第三步：全系统级赛德尔像差诊断。              ║
    # ║                复用第二步所有配置 + S12 系统级 4 个参数。    ║
    # ║                首次使用：先设为 "structure" 运行一次，       ║
    # ║                对照 print_seq_index 输出填好 S12，           ║
    # ║                再切换到 "seidel"。                          ║
    # ╚══════════════════════════════════════════════════════════════╝
    RUN_MODE = "auto"   # ← 改这一行切换步骤


    # ╔══════════════════════════════════════════════════════════════╗
    # ║              第一步：玻璃穷举搜索配置                         ║
    # ╚══════════════════════════════════════════════════════════════╝

    # ── 1. 玻璃库路径 ────────────────────────────────────────────
    GLASS_XLSX = Path(__file__).parent / "CDGM202509_with_Schott.xlsx"

    # ── 2. 玻璃库过滤 ────────────────────────────────────────────
    # MA/A/B 是常规批量生产牌号，C/D 为特殊定制。
    MELT_FILTER = ['MA']

    # ── 3. 工作波段（单位：nm）───────────────────────────────────
    # ★ 4片APO与波段的物理约束：
    #   450-550-850nm 波段：APO系数比值~50倍，4片精确APO无解 → 用 apo=False
    #   486-588-656nm 波段：F-d-C可见光，4片精确APO可行 → 用 apo=True
    LAM_SHORT_NM = 450
    LAM_REF_NM   = 550
    LAM_LONG_NM  = 850

    # ── 4. 组元参数列表（向后兼容：填单字典也能运行）──────────────
    #
    #  ALL_GROUPS 中每个字典的字段与原 GROUP_PARAMS 完全一致。
    #  search/structure/auto 模式按索引顺序依次处理每个组元。
    #
    ALL_GROUPS = [
        {
            "name":            "G1",
            "zoom_csv_group":  None,       # 定焦组填 None，跳过 p̄ 计算
            "f_group":         56.959,
            "D":               35,
            "structure":       ['pos', 'neg', 'pos', 'pos'],
            "apo":             False,
            "cemented":        [(1, 2)],   # ZF6 与 H-FK95N 胶合
            "min_f_mm":        40,
            "max_f_mm":        None,
            "allow_duplicate": True,
        },
        {
            "name":            "G2",
            "zoom_csv_group":  "G2",       # 变焦组，填 CSV 列名前缀
            "f_group":         -12.151,
            "D":               15,
            "structure":       ['neg', 'neg', 'pos', 'neg'],
            "apo":             False,
            "cemented":        [(2, 3)],   
            "min_f_mm":        15,
            "max_f_mm":        None,
            "allow_duplicate": True,
        },
        {
            "name":            "G3",
            "zoom_csv_group":  "G3",
            "f_group":         24.409,
            "D":               12,
            "structure":       ['pos', 'neg', 'pos'],
            "apo":             False,
            "cemented":        [(1, 2)],   
            "min_f_mm":        15,
            "max_f_mm":        None,
            "allow_duplicate": True,
        },
        {
            "name":            "G4",
            "zoom_csv_group":  None,
            "f_group":         72.545,
            "D":               10,
            "structure":       ['pos', 'neg', 'pos', 'neg'],
            "apo":             False,
            "cemented":        [(0, 1)],   
            "min_f_mm":        15,
            "max_f_mm":        None,
            "allow_duplicate": True,
        },
    ]

    # ── 5. 穷举精度 ───────────────────────────────────────────────
    PHI_SCAN_STEPS = 20

    # ── 6. 筛选严格程度 ───────────────────────────────────────────
    OPTICAL_PERCENTILE = 30
    TOP_N              = 10   # search/structure 模式下每组保留的候选数

    # ── 6b. 系统级联合优化候选数（仅 auto 模式生效）──────────────
    #
    # 三个参数的关系：
    #   SYSTEM_SEARCH_N ：action_a() 在 auto 模式下的搜索输出上限。
    #                     建议设为 SYSTEM_CAND_N × 3，给多样性采样足够的候选池。
    #                     search / structure 模式下仍使用 TOP_N，不受此参数影响。
    #   SYSTEM_CAND_N   ：多样性采样后每组最终保留的候选数。
    #                     组合空间 = SYSTEM_CAND_N^4（4组时），
    #                     ≤ 10000 时穷举，超出时自动分步剪枝。
    #   TOP_N           ：search / structure 模式（非系统优化场景）的候选数，
    #                     两种模式互不干扰，各自独立配置。
    SYSTEM_SEARCH_N = 30   # auto 模式 action_a 输出上限（建议 ≥ SYSTEM_CAND_N × 3）
    SYSTEM_CAND_N   = 10   # auto 模式多样性采样后每组保留的候选数

    # ── 7. 并行进程数 ─────────────────────────────────────────────
    N_WORKERS = 4

    # ── 8. 外表面玻璃限制（仅对 G1 生效，引用模块顶部常量）──────────
    # EXCLUDED_FOR_OUTER 已在文件顶部定义为模块级常量，此处直接引用。

    # ── 9. 软硬混合约束参数 ───────────────────────────────────────
    # apo=True  时：TOL_DISP 收紧到 1e-3~1e-4，W_APO 保持 500
    # apo=False 时：TOL_DISP 可放宽至 1e-3，W_APO 调大到 2000
    TOL_DISP = 1e-4
    W_APO    = 2000.0
    TOL_PHI  = 1e-5


    # ╔══════════════════════════════════════════════════════════════╗
    # ║         第二步：初始结构计算配置                              ║
    # ║  在第一步运行完成、确定各组玻璃和焦距后填入以下参数           ║
    # ║  ALL_* 列表索引与 ALL_GROUPS 严格一一对应                    ║
    # ╚══════════════════════════════════════════════════════════════╝

    # ── S1. 各组玻璃工作参考波长折射率 n_ref（单位：mm⁻¹ 无量纲）──
    # [Fix-Bug5] 此处应填写工作参考波长（LAM_REF_NM，当前 550nm）下的折射率 n_ref，
    # 而非目录 d 线（587.56nm）的 nd。两者在宽谱系统（450/550/850nm）中偏差约
    # 0.005~0.015，若填目录 nd，V4 Petzval 场曲值将与第一步搜索排名中的 P_ptz 不一致。
    #
    # 填写来源（任选其一）：
    #   a. 直接从第一步输出表格的 "n(λref)" 列复制（推荐，无需重新查手册）
    #   b. 从 CDGM 手册查取 n_ref 波长对应的折射率值
    #   c. 留空或缺失 → 程序自动从玻璃库读取 n_ref（见 _fill_nd_from_db）
    # ⚠ 若你之前填的是手册 nd（587.56nm），请核对后替换为 n_ref（550nm）。
    ALL_ND = [
        # G1 — 留空，程序从玻璃库自动读取 n_ref(LAM_REF_NM=550nm)
        {},
        # G2
        {},
        # G3
        {},
        # G4
        {},
    ]

    # ── S2. 各组玻璃牌号（按光路顺序）──────────────────────────
    ALL_GLASS_NAMES = [
        # G1
        ['H-LaF50B', 'ZF6', 'H-FK95N', 'H-LaK7A'],
        # G2
        ['H-ZLaF50E', 'H-ZLaF50E', 'H-ZF88', 'H-QK3L'],
        # G3
        ['H-ZK9B', 'ZF6', 'H-FK61B'],
        # G4
        ['H-LaK7A', 'H-ZF4A', 'H-FK95N', 'H-F4'],
    ]

    # ── S3. 各组各片焦距（mm）──────────────────────────────────
    # 从第一步"第一名详情"表格的 f(mm) 列复制
    # [BugFix-A] 确保每组列表项数与 ALL_GLASS_NAMES 对应组一致
    ALL_FOCAL_LENGTHS_MM = [
        # G1（4片）—— 示例值，请替换为第一步搜索结果
        [34.04, -24.82, 20.05, -20.00],
        # G2（4片）—— 示例值，请替换为第一步搜索结果
        [-30.00, -30.00, 25.00, -40.00],
        # G3（3片）—— 示例值，请替换为第一步搜索结果
        [30.00, -20.00, 25.00],
        # G4（4片）—— 示例值，请替换为第一步搜索结果
        [34.04, -24.82, 20.05, -20.00],
    ]

    # ── S4. 各组胶合对（0-based 索引）────────────────────────────
    # [BugFix-B] 胶合面处 ALL_SPACINGS_MM 中对应项必须为 0
    ALL_CEMENTED_PAIRS = [
        [(1, 2)],   
        [(2, 3)],   
        [(1, 2)],   
        [(0, 1)],   
    ]

    # ── S5. 各组相邻片间距（mm），胶合面处必须填 0 ──────────────
    # [BugFix-B] 胶合面（索引由 ALL_CEMENTED_PAIRS 决定）填 0
    ALL_SPACINGS_MM = [
        [1.0, 0.0, 1.0],   
        [1.5, 1.5, 0.0],   # G2: 胶合面索引2为0
        [1.0, 0.0],        # G3: 胶合面索引1为0
        [0.0, 4.0, 4.0],   # G4: 胶合面索引0为0
    ]

    # ── S6. 各组通光口径（mm）──────────────────────────────────
    ALL_D_MM = [35.0, 15.0, 12.0, 10.0]   # G1/G2/G3/G4

    # ── S7. 最小曲率半径约束（mm，各组元独立）──────────────────
    # 各组元口径不同，建议各自从 0.8×D 开始设定；
    # 若报"可行域为空"则适当减小对应组元的值或换焦距更长的玻璃
    S_MIN_R_MM = [
        50.0,   # G1
        25.0,   # G2
        18.0,   # G3
        16.0,   # G4
    ]

    # ── S8. 厚度约束（mm，各组元独立）──────────────────────────
    # 各组元口径不同，厚度下限也应各自设定
    S_T_EDGE_MIN = [
        1.5,   # G1  正透镜最小边缘厚度
        1.0,   # G2
        1.0,   # G3
        1.0,   # G4
    ]
    S_T_CENTER_MIN = [
        2.0,   # G1  负透镜最小中心厚度
        1.5,   # G2
        1.5,   # G3
        1.5,   # G4
    ]
    S_T_CEMENTED_MIN = [
        4.0,   # G1  胶合组最小总厚度
        3.0,   # G2
        3.0,   # G3
        3.0,   # G4
    ]

    # ── S9. 变焦行程光线追迹数据（变焦组专用）────────────────────
    #
    # 将高斯求解工具导出的 CSV 文件路径填入 S_ZOOM_CSV。
    # 程序自动从各组的 zoom_csv_group 推断 CSV 列名。
    # 若设为 None，则所有片位均使用单点追迹 p 值。
    # ─────────────────────────────────────────────────────────────
    S_ZOOM_CSV = None   # ← 不需要则保持 None；如 Path(__file__).parent / "zoom.csv"
    # ─────────────────────────────────────────────────────────────

    # ── S10. 各组验证参数 ────────────────────────────────────────
    #
    # ALL_TARGET_F_MM：各组目标等效焦距（从 ALL_GROUPS f_group 复制）
    # ALL_VGEN_LIST  ：各组广义阿贝数列表，顺序须与 ALL_GLASS_NAMES 严格一致
    #                 来源：第一步 action_a 输出表格中的 V_gen 列
    #                 [BugFix-C] 确保每组项数与玻璃片数一致
    # ─────────────────────────────────────────────────────────────
    ALL_TARGET_F_MM = [56.959, -12.151, 24.409, 72.545]

    ALL_VGEN_LIST = [
        # G1（4片）—— 来自第一步 V_gen 列，示例值请替换
        [34.507, 20.567, 45.013, 30.000],
        # G2（4片）—— 示例值，请替换
        [45.013, 45.013, 20.000, 55.000],
        # G3（3片）—— 示例值，请替换
        [40.000, 20.567, 50.000],
        # G4（4片）—— 示例值，请替换
        [34.507, 20.567, 45.013, 30.000],
    ]

    S_EFL_TOL        = 0.02
    S_SAG_R_WARN     = 0.20
    S_SAG_R_LIMIT    = 0.30
    S_PETZVAL_LIMIT  = 0.05
    # ─────────────────────────────────────────────────────────────


    # ╔══════════════════════════════════════════════════════════════╗
    # ║  S12. 系统级赛德尔分析专属配置                               ║
    # ║  RUN_MODE = "seidel" 或 "auto" 结尾时生效                   ║
    # ║                                                              ║
    # ║  首次使用说明：                                              ║
    # ║    1. 先将 RUN_MODE 设为 "structure" 运行一次；              ║
    # ║    2. 对照 print_seq_index 输出，确认光阑所在组元索引和       ║
    # ║       组内偏移，填入 S_SYSTEM_STOP_GROUP_IDX / OFFSET；     ║
    # ║    3. 打开 S_SYSTEM_GAP_CSV，复制组间间距列的完整列名，       ║
    # ║       填入 S_SYSTEM_GAP_COLUMNS（顺序：G1→G2, G2→G3, ...）；║
    # ║    4. 将 RUN_MODE 改为 "seidel" 运行。                      ║
    # ╚══════════════════════════════════════════════════════════════╝

    # 🔧 系统参数1：含组间间距列的 CSV 文件（通常与高斯求解工具导出的文件相同）
    S_SYSTEM_GAP_CSV = Path(__file__).parent / "111.csv"   

    # 🔧 系统参数2：CSV 中组间间距列的完整列名（共 N_GROUPS-1 个，顺序不能错）
    S_SYSTEM_GAP_COLUMNS = [
        'd1 (G1-G2间距) (mm)',
        'd2 (G2-G3间距) (mm)',
        'd3 (G3-G4间距) (mm)',
    ]

    # 🔧 系统参数3：光阑面位置——按组元索引指定（与玻璃名称无关，不受胶合形式影响）
    #   S_SYSTEM_STOP_GROUP_IDX : 光阑所在组元的 0-based 索引（0=G1, 1=G2, 2=G3, 3=G4）
    #   S_SYSTEM_STOP_OFFSET    : 在该组面序列中的偏移（0=该组第一面，-1=该组最后一面）
    #   典型配置：
    #     光阑在 G3 第一面  → GROUP_IDX=2, OFFSET=0
    #     光阑在 G2 最后一面 → GROUP_IDX=1, OFFSET=-1
    S_SYSTEM_STOP_GROUP_IDX = 2   # 光阑在 G3（0-based 索引 2）
    S_SYSTEM_STOP_OFFSET    = 0   # G3 第一面

    # 🔧 系统参数4：F 数设定（两种方式二选一，按实际情况填写）
    #
    # 方式A（固定 F 数）：全变焦位置使用同一 F/#，将 S_SYSTEM_FNUM_TELE 设为 None。
    #   D(pos) = EFL(pos) / S_SYSTEM_FNUM_WIDE
    #
    # 方式B（变 F 数）：广角/长焦 F/# 不同，程序按 EFL 线性插值后计算各位置 D。
    #   D(pos) = EFL(pos) / F#_interp(pos)
    #   前提：CSV 中各行 EFL 列必须有效（非 None）。
    #
    S_SYSTEM_FNUM_WIDE = 4.0    # 广角端 F/#（两种方式均需填写）
    S_SYSTEM_FNUM_TELE = 5.6    # 长焦端 F/#；若固定 F 数则设为 None

    # 🔧 系统参数5：传感器对角线（mm）
    #   程序自动计算广角端半视场角：arctan(对角线/2 / EFL_wide)
    #   例如：传感器对角 7.6mm，f_wide=12mm → arctan(3.8/12) ≈ 17.6°
    S_SYSTEM_SENSOR_DIAG_MM = 7.6

    # 🔧 系统参数6：系统优化像差权重
    #   用于 find_best_combinations 和 refine_combination 的目标函数。
    #   权重越大，优化器越优先压缩该项像差。
    #   不同系统可按需调整：
    #     • 成像镜头：SI, SII, SIII 高权重，SV 低权重（畸变可数字校正）
    #     • 投影/量测镜头：SV 也需要高权重（畸变不可接受）
    #     • 色差敏感系统：CI, CII 提高权重
    #   设为 None 时使用等权（所有项权重 1.0），向后兼容。
    SYSTEM_ABERR_WEIGHTS = {
        'SI':   5.0,    # 球差
        'SII':  5.0,    # 慧差
        'SIII': 3.0,    # 像散
        'SIV':  1.0,    # 场曲
        'SV':   0.1,    # 畸变
        'CI':   2.0,    # 轴向色差
        'CII':  2.0,    # 垂轴色差
    }


    # ════════════════════════════════════════════════════════════════════
    # ★★★  以下无需修改  ★★★
    #   构建 params 字典后调用 run_action_a_pipeline，保持命令行兼容。
    # ════════════════════════════════════════════════════════════════════

    # 向后兼容：单字典形式自动包装为列表
    if isinstance(ALL_GROUPS, dict):
        ALL_GROUPS = [ALL_GROUPS]

    params = {
        'run_mode':          RUN_MODE,
        'glass_xlsx':        str(GLASS_XLSX),
        'melt_filter':       MELT_FILTER,
        'lam_short_nm':      LAM_SHORT_NM,
        'lam_ref_nm':        LAM_REF_NM,
        'lam_long_nm':       LAM_LONG_NM,
        'top_n':             TOP_N,
        'system_search_n':   SYSTEM_SEARCH_N,
        'system_cand_n':     SYSTEM_CAND_N,
        'n_workers':         N_WORKERS,
        'phi_scan_steps':    PHI_SCAN_STEPS,
        'optical_percentile':OPTICAL_PERCENTILE,
        'tol_disp':          TOL_DISP,
        'w_apo':             W_APO,
        'tol_phi':           TOL_PHI,
        's_zoom_csv':        str(S_ZOOM_CSV) if S_ZOOM_CSV is not None else None,
        'groups': [
            {
                'name':            gp["name"],
                'zoom_csv_group':  gp.get("zoom_csv_group"),
                'f_group':         gp["f_group"],
                'D':               gp["D"],
                'structure':       gp["structure"],
                'glass_roles':     gp.get("glass_roles"),
                'apo':             gp.get("apo", False),
                'cemented_pairs':  gp.get("cemented"),
                'min_f_mm':        gp.get("min_f_mm"),
                'max_f_mm':        gp.get("max_f_mm"),
                'allow_duplicate': gp.get("allow_duplicate", True),
                'spacings_mm':     ALL_SPACINGS_MM[gi],
                'd_mm':            ALL_D_MM[gi],
                'min_r_mm':        S_MIN_R_MM[gi],
                't_edge_min':      S_T_EDGE_MIN[gi],
                't_center_min':    S_T_CENTER_MIN[gi],
                't_cemented_min':  S_T_CEMENTED_MIN[gi],
                'glass_names':     ALL_GLASS_NAMES[gi],
                'nd_vals':         ALL_ND[gi],
                'focal_lengths_mm':ALL_FOCAL_LENGTHS_MM[gi],
                'vgen_list':       ALL_VGEN_LIST[gi],
                'target_f_mm':     ALL_TARGET_F_MM[gi],
            }
            for gi, gp in enumerate(ALL_GROUPS)
        ],
        'system': {
            'gap_csv':        str(S_SYSTEM_GAP_CSV) if S_SYSTEM_GAP_CSV is not None else None,
            'gap_columns':    S_SYSTEM_GAP_COLUMNS,
            'stop_group_idx': S_SYSTEM_STOP_GROUP_IDX,
            'stop_offset':    S_SYSTEM_STOP_OFFSET,
            'fnum_wide':      S_SYSTEM_FNUM_WIDE,
            'fnum_tele':      S_SYSTEM_FNUM_TELE,
            'sensor_diag_mm': S_SYSTEM_SENSOR_DIAG_MM,
            'weights':        SYSTEM_ABERR_WEIGHTS,
        },
    }

    run_action_a_pipeline(params)
