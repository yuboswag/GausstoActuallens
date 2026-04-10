"""
runner.py
内部辅助函数模块：赛德尔分析、全系统面序列拼接及光阑定位。
从 main.py 抽取，供 main.py 和其他调用方使用。
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from validation import build_seq_with_dispersion
from seidel_gemini import (
    analyze_all_zoom_positions,
    find_stop_index,
    load_zoom_positions_from_csv,
    print_all,
    print_seq_index,
)


# ════════════════════════════════════════════════════════════════════
#  内部辅助：单组元赛德尔分析（供单组调试使用）
# ════════════════════════════════════════════════════════════════════
def _run_seidel_analysis(
    struct_result  : dict[str, Any],
    glass_names    : list[str],
    nd_values      : dict[str, float],
    cemented_pairs : list[tuple[int, int]],
    spacings_mm    : list[float],
    vgen_list      : list[float | None],
    d_mm           : float,
    zoom_csv       : str | Path | None,
    gap_indices    : list[int],
    stop_keyword   : str,
    csv_columns    : list[str],
    half_fov_deg   : float,
) -> None:
    """
    封装赛德尔像差分析的完整流程，供各运行模式调用。

    参数全部来自调用方已验证过的配置变量，不做重复校验。
    流程：构建面序列 → 注入色散 → 加载变焦数据 → 逐面分析 → 打印报告。
    """
    # ── 1. 构建面序列并注入色散（name-keyed，彻底消除 nd 碰撞）────────
    seq = build_seq_with_dispersion(
        glass_names    = glass_names,
        nd_values      = nd_values,
        cemented_pairs = cemented_pairs,
        surfaces       = struct_result['surfaces'],
        thickness      = struct_result['thickness'],
        spacings_mm    = spacings_mm,
        vgen_list      = vgen_list,
    )

    # ── 2. 打印面序列索引（确认 GAP_INDICES / STOP_KEYWORD）────────
    print_seq_index(seq)

    # ── 3. 定位光阑 ──────────────────────────────────────────────
    stop_idx = find_stop_index(seq, stop_keyword)

    # ── 4. 加载变焦位置数据 ──────────────────────────────────────
    zoom_positions = []

    if zoom_csv is not None and str(zoom_csv).lower() != 'none' and gap_indices:
        csv_path = Path(zoom_csv)
        if not csv_path.is_absolute():
            csv_path = Path(__file__).parent / csv_path
        try:
            zoom_positions = load_zoom_positions_from_csv(
                csv_path,
                gap_col_names=csv_columns,
            )
            print(f"  ✅ 成功从 CSV 加载了 {len(zoom_positions)} 个变焦位置数据。")
        except Exception as e:
            print(f"  ⚠ 读取变焦 CSV 失败：{e}")
            zoom_positions = []

    # 降级：CSV 未加载时用面序列当前间距做单点分析
    if not zoom_positions:
        if gap_indices:
            max_idx = max(gap_indices)
            if max_idx >= len(seq):
                raise IndexError(
                    f"S_GAP_INDICES 中的最大索引 {max_idx} 超出面序列长度 "
                    f"{len(seq)}（有效范围 0~{len(seq)-1}）。\n"
                    f"请对照上方 print_seq_index 输出核对面序号。"
                )
        print(
            f"\n  ⚠ 未加载到有效变焦 CSV 数据，将仅对当前间距做单点近似分析。\n"
            f"    → 这不能代表全变焦行程的像差评估。"
        )
        gap_values = [seq[i]['t_after'] for i in gap_indices] if gap_indices else []
        zoom_positions = [{
            'name':          '当前位置（降级单点）',
            'efl':           None,
            'gap_values_mm': gap_values,
        }]

    # ── 6. 赛德尔分析 ────────────────────────────────────────────
    results, diagnosis = analyze_all_zoom_positions(
        seq_template = seq,
        zoom_positions = zoom_positions,
        gap_indices  = gap_indices,
        D_mm         = d_mm,
        half_fov_rad = math.radians(half_fov_deg),
        stop_idx     = stop_idx,
    )

    # ── 7. 打印报告 ───────────────────────────────────────────────
    print_all(
        results      = results,
        diagnosis    = diagnosis,
        stop_idx     = stop_idx,
        seq_template = seq,
        top_n        = 5,
    )


# ════════════════════════════════════════════════════════════════════
#  内部辅助：全系统面序列拼接
# ════════════════════════════════════════════════════════════════════
def _build_system_seq(
    group_seqs             : list[list[dict[str, Any]]],
    inter_group_gap_values : list[float],
) -> list[dict[str, Any]]:
    """
    将各组元面序列按光路顺序拼接为全系统面序列。

    拼接规则：将每组最后一面的 t_after 设为该组与下一组之间的空气间距。
    组内面序列保持不变；最后一组末面的 t_after 设为 0。

    group_seqs              : list[list[dict]]，各组已注入色散的面序列（深拷贝在内部完成）
    inter_group_gap_values  : list[float]，共 N-1 个组间空气间距（mm），
                              顺序为 [d_G1G2, d_G2G3, ..., d_G(N-1)GN]
    """
    system_seq = []
    for i, seq in enumerate(group_seqs):
        system_seq.extend(copy.deepcopy(seq))
        if i < len(group_seqs) - 1:
            gap_val = (inter_group_gap_values[i]
                       if i < len(inter_group_gap_values) else 0.0)
            # 将本组最后一面的 t_after 设为组间间距
            system_seq[-1]['t_after'] = float(gap_val)
    # 最后一组最后一面 t_after = 0（像面之前的间距由 BFD 另行处理）
    if system_seq:
        system_seq[-1]['t_after'] = 0.0
    return system_seq


def _stop_idx_from_group_seqs(
    group_seqs     : list[list[dict[str, Any]]],
    stop_group_idx : int,
    stop_offset    : int = 0,
) -> int:
    """
    从各组面序列计算光阑面的绝对索引。

    group_seqs      : list[list[dict]]，各组已构建的面序列
    stop_group_idx  : int，光阑所在组元的 0-based 索引
    stop_offset     : int，在该组内的偏移（0=第一面，-1=最后一面，支持负数）
    """
    base   = sum(len(s) for s in group_seqs[:stop_group_idx])
    n_this = len(group_seqs[stop_group_idx])
    if stop_offset < 0:
        stop_offset = n_this + stop_offset
    return base + max(0, min(stop_offset, n_this - 1))


# ════════════════════════════════════════════════════════════════════
#  内部辅助：系统级赛德尔分析
# ════════════════════════════════════════════════════════════════════
def _run_system_seidel_analysis(
    all_struct_results : list[dict[str, Any]],
    all_glass_names    : list[list[str]],
    all_nd_values      : list[dict[str, float]],
    all_cemented_pairs : list[list[tuple[int, int]]],
    all_spacings_mm    : list[list[float]],
    all_vgen_list      : list[list[float | None]],
    system_gap_csv     : str | Path | None,
    system_gap_columns : list[str],
    stop_group_idx     : int,
    stop_offset        : int,
    sensor_diag_mm     : float,
    fnum_wide          : float,
    fnum_tele          : float | None = None,
) -> None:
    """
    拼接全系统面序列并执行系统级赛德尔像差分析。

    物理原理：四组元面序列按光路顺序拼接，组间空气间距随变焦位置变化，
    一次性追迹全系统近轴光线，得到系统级赛德尔系数（反映组间耦合效应）。

    all_struct_results : list，各组 compute_initial_structure 返回值
    all_glass_names    : list[list[str]]，各组玻璃牌号列表
    all_nd_values      : list[dict]，各组 {玻璃名: nd} 字典
    all_cemented_pairs : list[list[tuple]]，各组胶合对
    all_spacings_mm    : list[list[float]]，各组片间空气间距
    all_vgen_list      : list[list[float]]，各组广义阿贝数列表
    system_gap_csv     : Path | None，含组间间距列的 CSV 文件
    system_gap_columns : list[str]，CSV 中组间间距列名（N-1 个）
    stop_group_idx     : int，光阑所在组元的 0-based 索引（0=G1, 1=G2, 2=G3...）
    stop_offset        : int，在该组面序列内的偏移（0=第一面，-1=最后一面）
    sensor_diag_mm     : float，传感器对角线（mm），程序自动按广角端 EFL 计算半视场角
    fnum_wide          : float，广角端 F/#
    fnum_tele          : float | None，长焦端 F/#；None 表示固定 F 数（等于 fnum_wide）
    """
    n_groups = len(all_struct_results)

    # ── 1. 为每组构建面序列并注入色散 ────────────────────────────
    group_seqs = []
    for i in range(n_groups):
        seq = build_seq_with_dispersion(
            glass_names    = all_glass_names[i],
            nd_values      = all_nd_values[i],
            cemented_pairs = all_cemented_pairs[i],
            surfaces       = all_struct_results[i]['surfaces'],
            thickness      = all_struct_results[i]['thickness'],
            spacings_mm    = all_spacings_mm[i],
            vgen_list      = all_vgen_list[i],
        )
        group_seqs.append(seq)

    # ── 2. 加载组间间距数据 ───────────────────────────────────────
    if system_gap_csv is None:
        print(f"  ⚠ S_SYSTEM_GAP_CSV 未设置，将跳过系统级赛德尔分析。")
        return

    csv_path = Path(system_gap_csv)
    if not csv_path.is_absolute():
        csv_path = Path(__file__).parent / csv_path
    if not csv_path.exists():
        print(f"  ⚠ 系统间距 CSV 文件不存在：{csv_path}，将跳过系统级赛德尔分析。")
        return

    try:
        zoom_positions_gaps = load_zoom_positions_from_csv(
            csv_path,
            gap_col_names=system_gap_columns,
        )
        print(f"  ✅ 系统级分析：成功从 CSV 加载了 {len(zoom_positions_gaps)} 个变焦位置。")
    except Exception as e:
        print(f"  ⚠ 读取系统间距 CSV 失败：{e}，将跳过系统级赛德尔分析。")
        return

    if not zoom_positions_gaps:
        print(f"  ⚠ 未读取到有效变焦位置数据，跳过系统级赛德尔分析。")
        return

    # ── 3. 用第一个变焦位置的间距构建模板面序列并打印索引 ────────
    first_gap_vals = zoom_positions_gaps[0]['gap_values_mm']
    seq_template = _build_system_seq(group_seqs, first_gap_vals)
    print_seq_index(seq_template)

    # ── 4. 定位光阑（按组元索引+偏移计算绝对序号，不依赖玻璃名称字符串）───
    stop_idx = _stop_idx_from_group_seqs(group_seqs, stop_group_idx, stop_offset)

    # ── 5. 计算组间间距对应的 gap_indices ─────────────────────────
    # 每个组间间距存储在该组最后一面的 t_after，
    # gap_indices[i] = 系统面序列中第 i 组最后一面的绝对序号（0-based）
    gap_indices = []
    cum = 0
    for i in range(n_groups - 1):
        cum += len(group_seqs[i])
        gap_indices.append(cum - 1)

    # ── 6. 按 F# 计算各变焦位置入瞳直径 D = EFL / F# ────────────
    efls = [zp['efl'] for zp in zoom_positions_gaps]
    if any(e is None for e in efls):
        print("  ⚠ 系统级赛德尔分析要求 CSV 中所有变焦位置的 EFL 均有效，"
              "但存在 None 值，将跳过分析。")
        return
    fnum_t = fnum_tele if fnum_tele is not None else fnum_wide   # 固定 F# 时两端相同
    efl_min, efl_max = min(efls), max(efls)
    efl_span = efl_max - efl_min if efl_max != efl_min else 1.0
    d_mm_list = []
    for efl in efls:
        t = (efl - efl_min) / efl_span                           # 0=广角端, 1=长焦端
        fnum = fnum_wide + (fnum_t - fnum_wide) * t
        d_mm_list.append(efl / fnum)
    if fnum_tele is None:
        print(f"  固定 F# {fnum_wide}：各位置入瞳直径 = "
              + ", ".join(f"{d:.2f}" for d in d_mm_list) + " mm")
    else:
        print(f"  变 F# {fnum_wide}~{fnum_t}：各位置入瞳直径 = "
              + ", ".join(f"{d:.2f}" for d in d_mm_list) + " mm")
    d_mm_for_analysis = d_mm_list

    # ── 6b. 按传感器对角线计算广角端半视场角 ─────────────────────
    half_fov_rad = math.atan(sensor_diag_mm / 2.0 / efl_min)
    print(f"  传感器对角线 {sensor_diag_mm} mm × 广角端 EFL {efl_min:.2f} mm"
          f" → 半视场角 {math.degrees(half_fov_rad):.2f}°")

    # ── 7. 赛德尔分析 ────────────────────────────────────────────
    results, diagnosis = analyze_all_zoom_positions(
        seq_template   = seq_template,
        zoom_positions = zoom_positions_gaps,
        gap_indices    = gap_indices,
        D_mm           = d_mm_for_analysis,
        half_fov_rad   = half_fov_rad,
        stop_idx       = stop_idx,
    )

    # ── 8. 打印报告 ───────────────────────────────────────────────
    print_all(
        results      = results,
        diagnosis    = diagnosis,
        stop_idx     = stop_idx,
        seq_template = seq_template,
        top_n        = 5,
    )
