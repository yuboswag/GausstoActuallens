"""
system_optimizer.py
阶段二：系统级联合优化（2.1 系统评价函数 / 2.2 组合搜索 / 2.4 诊断报告）。

核心原理
--------
变焦镜头的系统总像差由各组元赛德尔系数叠加而成（带正负号）：

    S_sys,k(z) = Σ_g  S_k,g(z)       ← 各组元贡献之和，允许正负抵消

系统级评价函数（先叠加再平方，不是各组元分别平方求和）：

    merit = Σ_z  Σ_k  w_k × [ Σ_g S_k,g(z) ]²

"先和后平方"才能体现组间像差补偿效应：
  若 G2 球差 = +0.20，G3 球差 = -0.20，系统球差 = 0 → merit 贡献为 0；
  若用"先平方后和"则 = 0.04 + 0.04 = 0.08，无法反映抵消。

各组元赛德尔系数随变焦位置（组间间距）变化：
  本模块对每个变焦位置重新设置组间间距并追迹全系统近轴光线，
  自动获得各组元在该变焦位置实际光线入射条件下的赛德尔贡献。

模块结构
--------
  §1  内部辅助函数（面序列拼接、索引计算、权重归一化）
  §2  system_merit_function         全系统评价函数
  §3  find_best_combinations        组合搜索（穷举 or 分步剪枝）
  §4  generate_diagnosis_report     诊断报告（表格 + 文字摘要）

重要约束
--------
本模块不修改 seidel_gemini.py 中的赛德尔公式和近轴光线追迹逻辑，
所有光学计算均委托给现有函数，仅做流程调度和数据传递。
"""

import copy
import dataclasses
import math
from itertools import product as iterproduct
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from seidel_gemini import (
    ABERR_KEYS,
    analyze_one_position,
)
from group_candidate import GroupCandidate, build_candidate_seq


# ════════════════════════════════════════════════════════════════════
#  §0  常量
# ════════════════════════════════════════════════════════════════════

# 几何像差键（SI~SV），色差键（CI/CII）单独列出，两者均参与评价
_GEO_KEYS   = ['SI', 'SII', 'SIII', 'SIV', 'SV']
_COLOR_KEYS = ['CI', 'CII']

# 分隔线（与 seidel_gemini.py 风格保持一致）
_SEP  = '═' * 110
_SEP2 = '─' * 110


# ════════════════════════════════════════════════════════════════════
#  §1  内部辅助
# ════════════════════════════════════════════════════════════════════

def _compute_gap_indices(candidates_combo: List[GroupCandidate]) -> List[int]:
    """
    计算系统面序列中各组间间距面的绝对索引。

    规则（与 _run_system_seidel_analysis 中 gap_indices 计算方式一致）：
      gap_indices[i] = 第 i 组最后一面在系统面序列中的绝对索引（0-based）。
      该面的 t_after 存储第 i 组与第 i+1 组之间的空气间距。

    共 N_GROUPS-1 个索引，对应 N_GROUPS-1 个组间间距。
    """
    gap_indices: List[int] = []
    cum = 0
    for cand in candidates_combo[:-1]:      # 不含最后一组
        cum += cand.n_surfaces
        gap_indices.append(cum - 1)         # 本组最后一面（0-based）
    return gap_indices


def _compute_group_ranges(
    candidates_combo: List[GroupCandidate],
) -> List[Tuple[int, int]]:
    """
    计算系统面序列中各组的面索引范围。

    返回 [(start_0, end_0), (start_1, end_1), ...] 列表，
    end 为不含端点（切片用法：contribs[start:end]）。
    """
    ranges: List[Tuple[int, int]] = []
    cum = 0
    for cand in candidates_combo:
        ranges.append((cum, cum + cand.n_surfaces))
        cum += cand.n_surfaces
    return ranges


def _stitch_group_seqs(
    group_seqs      : List[List[dict]],
    first_gap_values: List[float],
) -> List[dict]:
    """
    将各组面序列深拷贝后拼接为系统面序列，设置初始组间间距。

    first_gap_values : 第一个变焦位置的 N_GROUPS-1 个组间间距（mm），
                       用于初始化模板中的 t_after 值。
                       后续通过就地修改更新各变焦位置的实际间距。

    返回值：可安全就地修改 t_after 的系统面序列（各面均为独立深拷贝）。
    """
    system_seq: List[dict] = []
    for i, seq in enumerate(group_seqs):
        system_seq.extend(copy.deepcopy(seq))      # 深拷贝，保护原始 seq
        if i < len(group_seqs) - 1:
            gap_val = (first_gap_values[i]
                       if i < len(first_gap_values) else 0.0)
            system_seq[-1]['t_after'] = float(gap_val)
    if system_seq:
        system_seq[-1]['t_after'] = 0.0            # 最后一面无输出间距
    return system_seq


def _normalize_weights(weights: Optional[dict]) -> dict:
    """
    返回归一化权重字典，覆盖 ABERR_KEYS 中所有键。
    未指定的键默认为 1.0（等权）。
    """
    w = {k: 1.0 for k in ABERR_KEYS}
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in ABERR_KEYS})
    return w



def _d_at_position(d_mm_list: Union[float, List[float]], zi: int) -> float:
    """按变焦位置索引取入瞳直径，支持标量（等 D）和列表（变 D）两种形式。"""
    if isinstance(d_mm_list, (list, tuple)):
        return float(d_mm_list[zi])
    return float(d_mm_list)


# ════════════════════════════════════════════════════════════════════
#  §2  系统级目标函数
# ════════════════════════════════════════════════════════════════════

def system_merit_function(
    candidates_combo: List[GroupCandidate],
    zoom_positions  : List[dict],
    stop_idx        : int,
    d_mm_list       : Union[float, List[float]],
    half_fov_rad    : float,
    weights         : Optional[dict] = None,
    seq_cache       : Optional[Dict[int, List[dict]]] = None,
) -> float:
    """
    计算一组候选方案组合在所有变焦位置下的系统总赛德尔系数评价值。

    参数
    ----
    candidates_combo : 长度等于组元数的 GroupCandidate 列表，
                       各元素对应各组选中的候选方案
    zoom_positions   : 变焦位置列表，每项为 {'name', 'efl', 'gap_values_mm'}
                       gap_values_mm 包含 N_GROUPS-1 个组间间距（mm）
    stop_idx         : 光阑面在系统面序列中的绝对索引（由调用方通过组元索引+偏移计算后传入）
    d_mm_list        : 各变焦位置的入瞳直径（mm）；float 表示全程等 D
    half_fov_rad     : 半视场角（rad）
    weights          : {aberration_key: weight} 字典，None 表示等权（各项权重 1.0）
    seq_cache        : {id(cand): seq} 预建面序列缓存；None 则实时调用 build_candidate_seq。
                       由 find_best_combinations 在搜索前统一预建，以消除穷举中的重复构建。

    返回
    ----
    merit : float，越小越好

    核心公式
    --------
    merit = Σ_z  Σ_k  w_k × [ Σ_g S_k,g(z) ]²

    实现说明
    --------
    1. 预建各组面序列（build_candidate_seq 或从 seq_cache 取深拷贝），完成色散注入。
    2. 拼接成系统面序列模板（_stitch_group_seqs，深拷贝，只做一次）。
    3. 对每个变焦位置：
       a. 就地修改系统面序列中各组间间距面的 t_after（O(N_GROUPS) 操作）。
          ※ analyze_one_position 对输入 seq 只读，不修改，因此就地修改安全。
       b. 调用 analyze_one_position 追迹全系统近轴光线 → 赛德尔系数。
       c. totals 即系统总赛德尔（所有面贡献之和 = Σ_g S_k,g(z)），
          直接满足"先叠加再平方"的评价公式。
    4. 累积各变焦位置、各像差项的加权平方和。
    """
    w = _normalize_weights(weights)

    # ── Step 1：取各组面序列（有缓存则深拷贝取出，否则实时构建）
    if seq_cache is not None:
        group_seqs = [copy.deepcopy(seq_cache[id(cand)]) for cand in candidates_combo]
    else:
        group_seqs = [build_candidate_seq(cand) for cand in candidates_combo]

    # ── Step 2：计算组间间距面索引 + 拼接系统面序列模板
    gap_indices = _compute_gap_indices(candidates_combo)
    first_gaps  = (zoom_positions[0]['gap_values_mm']
                   if zoom_positions else [0.0] * len(gap_indices))
    system_seq  = _stitch_group_seqs(group_seqs, first_gaps)

    merit = 0.0

    # ── Step 3：逐变焦位置评估 ──────────────────────────────────────
    for zi, zp in enumerate(zoom_positions):
        gap_vals = zp['gap_values_mm']

        # 就地更新组间间距（analyze_one_position 只读 seq，不修改，可安全就地操作）
        for face_idx, gap_val in zip(gap_indices, gap_vals):
            system_seq[face_idx]['t_after'] = float(gap_val)

        d_this = _d_at_position(d_mm_list, zi)

        try:
            res = analyze_one_position(
                system_seq, d_this, half_fov_rad, stop_idx=stop_idx,
            )
        except Exception:
            # 追迹失败（如数值奇异），赋大惩罚值，使该组合在排名中靠后
            merit += 1e12
            continue

        # totals = Σ_g S_k,g(z)（各面贡献之和，自动包含所有组元贡献）
        for k in ABERR_KEYS:
            merit += w[k] * res['totals'][k] ** 2      # Σ_k w_k × (系统总S_k)²

    return merit


# ════════════════════════════════════════════════════════════════════
#  §3  组合搜索
# ════════════════════════════════════════════════════════════════════

def _partial_merit(
    partial_combo   : List[GroupCandidate],
    zoom_positions  : List[dict],
    d_mm_list       : Union[float, List[float]],
    half_fov_rad    : float,
    weights         : Optional[dict],
    stop_idx_full   : int,
) -> float:
    """
    计算前 K 组的部分系统评价值（用于分步剪枝阶段）。

    ⚠ 近似说明：
      - 仅追迹前 K 组构成的子系统，假设剩余组贡献为零（乐观估计）。
      - 光阑索引裁剪到子系统长度范围内（超出则退为面 0）。
      - 组间间距只取 zoom_positions[z]['gap_values_mm'][:K-1]。
    此函数仅用于组合候选池的粗筛，不作为最终排名依据。
    """
    k_groups    = len(partial_combo)
    n_gaps      = k_groups - 1
    group_seqs  = [build_candidate_seq(cand) for cand in partial_combo]
    gap_indices = _compute_gap_indices(partial_combo)

    first_gaps = (zoom_positions[0]['gap_values_mm'][:n_gaps]
                  if zoom_positions else [0.0] * n_gaps)
    system_seq  = _stitch_group_seqs(group_seqs, first_gaps)

    # 光阑索引裁剪：若全系统光阑在尚未加入的组中，退为面 0
    n_total_faces = sum(c.n_surfaces for c in partial_combo)
    stop_idx_use  = stop_idx_full if stop_idx_full < n_total_faces else 0

    w     = _normalize_weights(weights)
    merit = 0.0

    for zi, zp in enumerate(zoom_positions):
        gap_vals = zp['gap_values_mm'][:n_gaps]
        for face_idx, gap_val in zip(gap_indices, gap_vals):
            system_seq[face_idx]['t_after'] = float(gap_val)
        d_this = _d_at_position(d_mm_list, zi)
        try:
            res = analyze_one_position(
                system_seq, d_this, half_fov_rad, stop_idx=stop_idx_use,
            )
            for k in ABERR_KEYS:
                merit += w[k] * res['totals'][k] ** 2
        except Exception:
            merit += 1e12

    return merit


def find_best_combinations(
    all_group_candidates: List[List[GroupCandidate]],
    zoom_positions      : List[dict],
    stop_idx            : int,
    d_mm_list           : Union[float, List[float]],
    half_fov_rad        : float,
    top_k               : int = 5,
    weights             : Optional[dict] = None,
    prune_m             : Optional[int]  = None,
) -> List[dict]:
    """
    在各组元候选方案中搜索系统像差互补最优的组合。

    参数
    ----
    all_group_candidates : list[list[GroupCandidate]]，
                           all_group_candidates[gi] 为第 gi 组的候选列表
    zoom_positions       : 变焦位置列表（含 gap_values_mm），
                           由 load_zoom_positions_from_csv 加载
    stop_idx             : 光阑面在全系统面序列中的绝对索引（0-based），
                           由调用方通过组元索引+偏移计算并传入
    d_mm_list            : 各变焦位置入瞳直径（float 或 list）
    half_fov_rad         : 半视场角（rad）
    top_k                : 返回最优组合的数量
    weights              : 各像差项权重，None 表示等权
    prune_m              : 分步剪枝中间层保留数量，None = 自动（top_k × 10，最小 50）

    返回
    ----
    list[dict]，每项：{'rank': int, 'merit': float, 'combo': list[GroupCandidate]}
    按 merit 升序排列，最多 top_k 项。

    搜索策略
    --------
    - 总组合数 N1×N2×…×NG ≤ 10000：直接穷举
    - 超出时：分步剪枝
        a) 逐步添加组元，每步保留 prune_m 个最优中间组合
        b) 所有组元加入后再做精确排序，取 top_k
    """
    if not all_group_candidates or not zoom_positions:
        print("  [系统优化] 候选列表或变焦位置数据为空，跳过搜索。")
        return []

    n_groups = len(all_group_candidates)
    counts   = [len(c) for c in all_group_candidates]
    total_combos = 1
    for c in counts:
        total_combos *= c

    print(f"\n{_SEP}")
    print(f"  [系统级联合优化] 搜索最优组合")
    print(f"  组元数={n_groups}，各组候选数={counts}，总组合数={total_combos:,}")
    print(f"  变焦位置数={len(zoom_positions)}，目标返回 top-{top_k}")
    print(_SEP)

    print(f"  光阑面绝对索引（调用方传入）: {stop_idx}")

    # ── 预建所有候选的面序列缓存，避免穷举中重复构建 ──────────────
    _seq_cache: Dict[int, List[dict]] = {}
    for group_cands in all_group_candidates:
        for cand in group_cands:
            cand_id = id(cand)
            if cand_id not in _seq_cache:
                _seq_cache[cand_id] = build_candidate_seq(cand)

    # ════════════════════════════════════════════════════════════════
    #  分支一：穷举（总组合数 ≤ 10000）
    # ════════════════════════════════════════════════════════════════
    if total_combos <= 10_000:
        print(f"  → 采用穷举策略（{total_combos:,} 种组合）\n")
        all_results: List[Tuple[float, List[GroupCandidate]]] = []

        for i, combo_tuple in enumerate(iterproduct(*all_group_candidates)):
            combo = list(combo_tuple)
            merit = system_merit_function(
                combo, zoom_positions, stop_idx, d_mm_list, half_fov_rad, weights,
                seq_cache=_seq_cache,
            )
            all_results.append((merit, combo))

            # 进度打印（每 500 个或末尾）
            if (i + 1) % 500 == 0 or (i + 1) == total_combos:
                pct = (i + 1) / total_combos * 100
                best_so_far = min(all_results, key=lambda x: x[0])[0]
                print(f"  穷举进度：{i+1:>6}/{total_combos}（{pct:5.1f}%）"
                      f"  当前最优 merit={best_so_far:.6f}", flush=True)

        all_results.sort(key=lambda x: x[0])

    # ════════════════════════════════════════════════════════════════
    #  分支二：分步剪枝（总组合数 > 10000）
    # ════════════════════════════════════════════════════════════════
    else:
        if prune_m is None:
            prune_m = max(top_k * 10, 50)
        print(f"  → 采用分步剪枝策略（中间层保留 M={prune_m}）\n")

        # 从第一组开始，逐步加入后续组元
        # active_pool: list of (partial_merit, partial_combo_list)
        active_pool: List[Tuple[float, List[GroupCandidate]]] = [
            (0.0, [cand]) for cand in all_group_candidates[0]
        ]

        for gi in range(1, n_groups):
            new_pool: List[Tuple[float, List[GroupCandidate]]] = []
            n_new = len(active_pool) * len(all_group_candidates[gi])
            print(f"  剪枝步骤 {gi+1}/{n_groups}："
                  f"扩展 {len(active_pool)} × {len(all_group_candidates[gi])}"
                  f" = {n_new:,} 个中间组合...", flush=True)

            for step_i, (_, partial) in enumerate(active_pool):
                for cand in all_group_candidates[gi]:
                    new_combo   = partial + [cand]
                    # 前 gi+1 组的部分评价值（乐观下界估计）
                    part_merit  = _partial_merit(
                        new_combo, zoom_positions, d_mm_list, half_fov_rad,
                        weights, stop_idx,
                    )
                    new_pool.append((part_merit, new_combo))

                if (step_i + 1) % max(1, len(active_pool) // 5) == 0:
                    pct = (step_i + 1) / len(active_pool) * 100
                    print(f"    进度：{step_i+1}/{len(active_pool)}（{pct:.0f}%）",
                          flush=True)

            # 剪枝：保留前 prune_m 个
            new_pool.sort(key=lambda x: x[0])
            if gi < n_groups - 1:
                # 中间层：保留 prune_m 个（粗筛）
                active_pool = new_pool[:prune_m]
                print(f"    剪枝后保留：{len(active_pool)} 个中间组合")
            else:
                # 最后一层：用精确全系统评价函数重新评分（替换部分评价）
                print(f"  最终层精确评分（{len(new_pool[:prune_m])} 个候选）...",
                      flush=True)
                final_pool: List[Tuple[float, List[GroupCandidate]]] = []
                for fi, (_, combo) in enumerate(new_pool[:prune_m]):
                    exact_merit = system_merit_function(
                        combo, zoom_positions, stop_idx, d_mm_list, half_fov_rad,
                        weights, seq_cache=_seq_cache,
                    )
                    final_pool.append((exact_merit, combo))
                    if (fi + 1) % max(1, prune_m // 5) == 0:
                        print(f"    精确评分进度：{fi+1}/{prune_m}", flush=True)
                active_pool = final_pool

        all_results = sorted(active_pool, key=lambda x: x[0])

    # ── 格式化返回值 ────────────────────────────────────────────────
    top_results = all_results[:top_k]
    output = []
    for rank, (merit, combo) in enumerate(top_results, 1):
        output.append({'rank': rank, 'merit': merit, 'combo': combo})

    # ── 打印排名摘要 ─────────────────────────────────────────────────
    print(f"\n{_SEP}")
    print(f"  [系统级联合优化] 最优组合排名（前 {len(output)} 名）")
    print(_SEP)
    col_w = 14
    header = f"  {'排名':>3}  {'系统merit':>12}"
    for gi in range(n_groups):
        header += f"  {'G'+str(gi+1)+' 玻璃组合':^{col_w*2}}"
    print(header)
    print(f"  {'-'*110}")
    for r in output:
        row = f"  {r['rank']:>3}.  {r['merit']:>12.6f}"
        for cand in r['combo']:
            combo_str = '/'.join(cand.glass_combo)
            row += f"  {combo_str:<{col_w*2}}"
        print(row)
    print(_SEP)

    return output


# ════════════════════════════════════════════════════════════════════
#  §4  诊断报告
# ════════════════════════════════════════════════════════════════════

def generate_diagnosis_report(
    best_combo  : List[GroupCandidate],
    zoom_positions: List[dict],
    stop_idx    : int,
    d_mm_list   : Union[float, List[float]],
    half_fov_rad: float,
) -> None:
    """
    输出系统级联合优化诊断报告。

    报告内容
    --------
    1. 最优组合总览（各组选中的玻璃和 merit 值）
    2. 逐变焦位置：各组元对每项赛德尔系数的贡献（带正负号）+ 系统总值
    3. 像差补偿分析：哪些组元对之间存在显著正负抵消
    4. 瓶颈识别：哪个变焦位置系统像差最大，主要来自哪个组元

    参数
    ----
    best_combo     : find_best_combinations 返回的最优组合（combo 字段）
    zoom_positions : 变焦位置列表（含 gap_values_mm）
    stop_idx       : 光阑面在全系统面序列中的绝对索引（0-based）
    d_mm_list      : 各变焦位置入瞳直径
    half_fov_rad   : 半视场角（rad）
    """
    if not best_combo or not zoom_positions:
        print("  [诊断报告] 输入为空，跳过。")
        return

    n_groups    = len(best_combo)
    group_names = [f'G{c.group_index + 1}' for c in best_combo]

    # ── 计算组面范围（光阑索引由调用方传入）──────────────────────
    gap_indices = _compute_gap_indices(best_combo)
    grp_ranges  = _compute_group_ranges(best_combo)

    # ── 构建系统面序列模板（一次）──────────────────────────────────
    group_seqs  = [build_candidate_seq(cand) for cand in best_combo]
    first_gaps  = zoom_positions[0]['gap_values_mm']
    system_seq  = _stitch_group_seqs(group_seqs, first_gaps)

    # ── 逐变焦位置追迹，收集每组贡献 ─────────────────────────────
    # per_pos_data[zi] = {
    #     'name'      : str,
    #     'group_seidel': {gi: {k: float}},  # 各组各像差项贡献（带符号）
    #     'sys_total' : {k: float},           # 系统总赛德尔
    # }
    per_pos_data = []

    for zi, zp in enumerate(zoom_positions):
        gap_vals = zp['gap_values_mm']
        for face_idx, gap_val in zip(gap_indices, gap_vals):
            system_seq[face_idx]['t_after'] = float(gap_val)
        d_this = _d_at_position(d_mm_list, zi)

        try:
            res = analyze_one_position(
                system_seq, d_this, half_fov_rad, stop_idx=stop_idx,
            )
        except Exception as e:
            print(f"  ⚠ 位置 {zp['name']} 追迹失败：{e}")
            continue

        contribs = res['contribs']
        sys_tot  = res['totals']

        # 按组切分面贡献，各组赛德尔系数 = 本组所有面的贡献之和
        grp_seidel: Dict[int, Dict[str, float]] = {}
        for gi, (g_start, g_end) in enumerate(grp_ranges):
            grp_contribs = contribs[g_start:g_end]
            grp_seidel[gi] = {
                k: sum(c[k] for c in grp_contribs) for k in ABERR_KEYS
            }

        per_pos_data.append({
            'name'        : zp['name'],
            'group_seidel': grp_seidel,
            'sys_total'   : sys_tot,
        })

    if not per_pos_data:
        print("  [诊断报告] 无有效变焦位置数据。")
        return

    # ════════════════════════════════════════════════════════════════
    #  报告块 1：最优组合总览
    # ════════════════════════════════════════════════════════════════
    print(f"\n{_SEP}")
    print(f"  ★ 系统级联合优化诊断报告")
    print(_SEP)
    print(f"  最优组合详情：")
    for gi, cand in enumerate(best_combo):
        print(f"    {group_names[gi]}：{' / '.join(cand.glass_combo)}"
              f"  merit(单组)={cand.merit_value:.5f}")
    print()

    # ════════════════════════════════════════════════════════════════
    #  报告块 2：逐变焦位置各组贡献表
    # ════════════════════════════════════════════════════════════════
    print(f"  ── 各变焦位置像差贡献明细 ──")
    print(f"  （正值：该方向像差，负值：反向像差；互补时两值异号且量级相近）\n")

    # 为每个像差项单独打一张表（横向太宽时行列分开更清晰）
    for k in ABERR_KEYS:
        # 表头
        col_w = 10
        print(f"  [{k}]")
        hdr   = f"  {'位置':>10}"
        for gname in group_names:
            hdr += f"  {gname:>{col_w}}"
        hdr += f"  {'系统总':>{col_w}}"
        print(hdr)
        print(f"  {'-'*( 12 + (col_w + 2) * (n_groups + 1))}")

        for pd in per_pos_data:
            row = f"  {pd['name']:>10}"
            for gi in range(n_groups):
                val = pd['group_seidel'][gi][k]
                tag = ' ❌' if abs(pd['sys_total'][k]) > _TOLERANCES.get(k, 1.0) else ''
                row += f"  {val:>+{col_w}.5f}"
            row += f"  {pd['sys_total'][k]:>+{col_w}.5f}{tag}"
            print(row)
        print()

    # ════════════════════════════════════════════════════════════════
    #  报告块 3：像差补偿分析
    # ════════════════════════════════════════════════════════════════
    print(f"  ── 像差补偿分析 ──")
    print(f"  （补偿率 = |各组之和| / 各组绝对值之和，越低代表互补效果越强）\n")

    for k in _GEO_KEYS:          # 只分析几何像差（色差项通常设计阶段已处理）
        # 对全变焦行程做平均补偿率
        comp_ratios = []
        for pd in per_pos_data:
            total_abs = sum(abs(pd['group_seidel'][gi][k]) for gi in range(n_groups))
            sys_abs   = abs(pd['sys_total'][k])
            if total_abs > 1e-10:
                comp_ratios.append(sys_abs / total_abs)

        if not comp_ratios:
            continue
        avg_comp = sum(comp_ratios) / len(comp_ratios)
        star = '  ★★ 优秀补偿' if avg_comp < 0.15 else \
               ('  ★  良好补偿' if avg_comp < 0.40 else '')
        print(f"    {k}：平均补偿率 = {avg_comp:.2%}{star}")

        # 找出补偿效果最强的组元对
        if n_groups >= 2:
            best_pair    = None
            best_pair_cr = 1.0
            for gi in range(n_groups):
                for gj in range(gi + 1, n_groups):
                    pair_ratios = []
                    for pd in per_pos_data:
                        a = pd['group_seidel'][gi][k]
                        b = pd['group_seidel'][gj][k]
                        denom = abs(a) + abs(b)
                        if denom > 1e-10:
                            pair_ratios.append(abs(a + b) / denom)
                    if pair_ratios:
                        pr = sum(pair_ratios) / len(pair_ratios)
                        if pr < best_pair_cr:
                            best_pair_cr = pr
                            best_pair    = (gi, gj)
            if best_pair and best_pair_cr < 0.50:
                gi, gj = best_pair
                print(f"      主要补偿对：{group_names[gi]} ↔ {group_names[gj]}"
                      f"（对间补偿率={best_pair_cr:.2%}）")
    print()

    # ════════════════════════════════════════════════════════════════
    #  报告块 4：瓶颈识别
    # ════════════════════════════════════════════════════════════════
    print(f"  ── 系统像差瓶颈识别 ──\n")

    # 找各像差项下系统总值绝对值最大的变焦位置
    for k in ABERR_KEYS:
        tol     = _TOLERANCES.get(k, 1.0)
        worst_z = max(per_pos_data, key=lambda pd: abs(pd['sys_total'][k]))
        sys_val = worst_z['sys_total'][k]
        exceed  = '  ❌ 超出容差' if abs(sys_val) > tol else ''

        print(f"    {k}：最差位置 = {worst_z['name']}"
              f"  系统总值 = {sys_val:+.5f}"
              f"（容差 ±{tol:.3f}）{exceed}")

        # 该位置各组贡献占比（绝对值占比，带符号标注）
        grp_vals   = [worst_z['group_seidel'][gi][k] for gi in range(n_groups)]
        total_abs  = sum(abs(v) for v in grp_vals) or 1.0
        contribs_sorted = sorted(
            enumerate(grp_vals), key=lambda x: abs(x[1]), reverse=True,
        )
        detail_parts = []
        for gi, val in contribs_sorted:
            if abs(val) / total_abs > 0.05:     # 只显示占比 > 5% 的组元
                pct  = abs(val) / total_abs * 100
                detail_parts.append(f"{group_names[gi]}={val:+.4f}({pct:.0f}%)")
        if detail_parts:
            print(f"      主要贡献：{' | '.join(detail_parts)}")

    print(f"\n{_SEP}")
    print(f"  诊断报告结束。")
    print(_SEP)


# ════════════════════════════════════════════════════════════════════
#  §5  模块内用到的容差常量（复用 seidel_gemini 的定义）
# ════════════════════════════════════════════════════════════════════
# 避免硬编码，从 seidel_gemini 中复用（此处为本模块私用引用）
try:
    from seidel_gemini import TOLERANCES as _TOLERANCES
except ImportError:
    _TOLERANCES = {
        'SI': 0.10, 'SII': 0.05, 'SIII': 0.05,
        'SIV': 0.20, 'SV': 0.10,
        'CI': 0.020, 'CII': 0.020,
    }


# ════════════════════════════════════════════════════════════════════
#  §6  形状因子反馈迭代优化
# ════════════════════════════════════════════════════════════════════

# 优化常量
_Q_DELTA_MAX  = 2.0   # Δq 偏移量的最大绝对值（超出则施加大惩罚）
_MIN_R_REFINE = 5.0   # 曲率约束：任意面 |R| 不得小于此值（mm）


def _q_from_radii(R1: float, R2: float, phi: float, n: float) -> float:
    """
    由曲率半径 (R1, R2)、光焦度 φ、折射率 n 反算形状因子 q。

    推导（来自 structure.py radii_single）：
        c1 = φ*(1+q) / (2*(n-1))
        c2 = φ*(q-1) / (2*(n-1))
        c1 + c2 = φ*q/(n-1)  →  q = (c1+c2)*(n-1)/φ
    """
    if abs(phi) < 1e-12 or abs(n - 1) < 1e-12:
        return 0.0
    c1 = 1.0 / R1 if abs(R1) < 1e6 else 0.0
    c2 = 1.0 / R2 if abs(R2) < 1e6 else 0.0
    return (c1 + c2) * (n - 1) / phi


def _radii_from_q(q: float, phi: float, n: float) -> Tuple[float, float]:
    """
    由形状因子 q、光焦度 φ、折射率 n 计算曲率半径 (R1, R2)。

    公式（与 structure.py radii_single 一致；此处不做 c_max 截断，
    由调用方在目标函数中施加曲率约束惩罚）：
        c1 = φ*(1+q) / (2*(n-1))
        c2 = φ*(q-1) / (2*(n-1))
    """
    if abs(n - 1) < 1e-12:
        return float('inf'), float('inf')
    c1 = phi * (1.0 + q) / (2.0 * (n - 1.0))
    c2 = phi * (q - 1.0) / (2.0 * (n - 1.0))
    R1 = 1.0 / c1 if abs(c1) > 1e-12 else float('inf')
    R2 = 1.0 / c2 if abs(c2) > 1e-12 else float('inf')
    return R1, R2


def _recompute_cemented_curvatures(
    R_cement_new: float,
    phi_front   : float,
    n_front     : float,
    phi_rear    : float,
    n_rear      : float,
) -> Tuple[float, float, float]:
    """
    给定新的胶合面曲率，重新计算胶合对的外表面曲率（薄透镜近似）。

    薄透镜近似下：
        φ_front = (n_front - 1) * (1/R_front - 1/R_cement)
        → R_front = 1 / (φ_front/(n_front-1) + 1/R_cement)

        φ_rear = (n_rear - 1) * (1/R_cement - 1/R_rear)
        → R_rear = 1 / (1/R_cement - φ_rear/(n_rear-1))

    胶合对的光焦度由 R_front、R_rear 重算保证不变，
    R_cement 改变只影响内部曲率分配（形状因子），不改变组合光焦度。

    返回 (R_front_new, R_cement_new, R_rear_new)
    """
    c_cement = 1.0 / R_cement_new if abs(R_cement_new) > 1e-12 else 1e12

    c_front = phi_front / (n_front - 1.0) + c_cement
    R_front = 1.0 / c_front if abs(c_front) > 1e-12 else 1e12

    c_rear = c_cement - phi_rear / (n_rear - 1.0)
    R_rear = 1.0 / c_rear if abs(c_rear) > 1e-12 else 1e12

    return R_front, R_cement_new, R_rear


def refine_combination(
    best_combo         : List[GroupCandidate],
    zoom_positions     : List[dict],
    stop_group_idx     : int,
    stop_offset        : int,
    d_mm_list          : Union[float, List[float]],
    half_fov_rad       : float,
    all_cemented_pairs : List,   # 保留参数兼容调用接口，实际从 best_combo 读取
    all_spacings_mm    : List,   # 同上
    weights            : Optional[dict] = None,
    max_iter           : int = 200,
    verbose            : bool = True,
) -> Tuple[List[GroupCandidate], float, dict]:
    """
    对最优组合做形状因子（q）连续参数微调优化（反馈迭代阶段）。

    核心思路
    --------
    各组元的解析最优形状因子 q 只使该组自身球差最小化，但系统总像差最优
    需要各组互补抵消——"牺牲单组元最优，换取系统级更优"。
    本函数将所有非胶合单片的形状因子偏移量 Δq 作为连续变量，
    以 system_merit_function 为目标，用 Nelder-Mead 搜索更优的 q 组合。

    胶合对处理（简化方案）
    ---------------------
    胶合对的三面曲率由相互耦合的光焦度约束决定，自由度低；
    本阶段固定胶合对曲率不变，只优化非胶合单片，大幅降低实现复杂度。

    参数
    ----
    best_combo         : find_best_combinations 返回的最优组合（combo 字段）
    zoom_positions     : 变焦位置列表（含 gap_values_mm）
    stop_group_idx     : 光阑所在组元的 0-based 索引（0=G1, 1=G2, ...）
    stop_offset        : 在该组面序列内的偏移（0=第一面，-1=最后一面）
    d_mm_list          : 各变焦位置入瞳直径（float 或 list）
    half_fov_rad       : 半视场角（rad）
    all_cemented_pairs : 各组胶合对（接口兼容参数，实际从 best_combo 读取）
    all_spacings_mm    : 各组片间距（接口兼容参数，实际从 best_combo 读取）
    weights            : 像差权重，None 表示等权
    max_iter           : Nelder-Mead 最大迭代次数
    verbose            : 是否打印优化进度（每 50 次迭代打印一次）

    返回
    ----
    refined_combo  : list[GroupCandidate]，优化后的组合（曲率已更新）
    refined_merit  : float，优化后的 merit 值
    report         : dict，包含：
        'initial_merit'   : 优化前 merit
        'final_merit'     : 优化后 merit
        'improvement_pct' : 改善百分比
        'n_iterations'    : 实际迭代次数
        'q_changes'       : list[float]，各变量的最优 Δq 值
    """
    from scipy.optimize import minimize as _sp_minimize

    n_groups = len(best_combo)

    # ── 计算光阑面绝对索引（复用 _stop_idx_from_group_seqs 逻辑）─────
    n_surfs_per_grp = [cand.n_surfaces for cand in best_combo]
    base_stop       = sum(n_surfs_per_grp[:stop_group_idx])
    n_stop_grp      = n_surfs_per_grp[stop_group_idx]
    ofs             = (stop_offset if stop_offset >= 0
                       else n_stop_grp + stop_offset)
    stop_idx        = base_stop + max(0, min(ofs, n_stop_grp - 1))

    # ── 构建优化变量映射表（仅非胶合单片）────────────────────────────
    # var_info[k] = (gi, lens_idx, phi_i, n_i)：第 k 个 Δq 对应的组元/片信息
    var_info   : List[Tuple[int, int, float, float]] = []
    q_original : List[float] = []

    for gi, cand in enumerate(best_combo):
        cemented_set = set(idx for pair in cand.cemented_pairs for idx in pair)
        nd_list      = [cand.nd_values[g] for g in cand.glass_combo]
        phi_list     = [1.0 / f for f in cand.focal_lengths_mm]

        for i in range(len(cand.glass_combo)):
            if i in cemented_set:
                continue    # 胶合片曲率固定，跳过不优化

            # 从 struct_result['surfaces'] 提取当前 R1、R2：
            # 非胶合单片 i 在 surfaces 列表中对应两个 (is_cem=False, lens_idx==i) 的面，
            # 按构建顺序第一个为前表面，第二个为后表面
            surf_Rs = [s[1] for s in cand.struct_result['surfaces']
                       if s[2] == i and not s[3]]
            if len(surf_Rs) < 2:
                continue    # 结构异常，跳过

            R1_curr, R2_curr = surf_Rs[0], surf_Rs[1]
            q0 = _q_from_radii(R1_curr, R2_curr, phi_list[i], nd_list[i])
            var_info.append((gi, i, phi_list[i], nd_list[i]))
            q_original.append(q0)

    n_vars = len(var_info)

    # ── 胶合对变量：每个胶合对增加一个 ΔR_c（胶合面曲率偏移量，mm）────
    # cemented_var_info[k] = (gi, (j1, j2), phi_front, n_front, phi_rear, n_rear)
    cemented_var_info : List[Tuple[int, Tuple[int, int], float, float, float, float]] = []
    R_cem_original    : List[float] = []

    for gi, cand in enumerate(best_combo):
        nd_list  = [cand.nd_values[g] for g in cand.glass_combo]
        phi_list = [1.0 / f for f in cand.focal_lengths_mm]
        for pair in cand.cemented_pairs:
            j1, j2 = pair
            # 在 surfaces 中找胶合面（lens_idx == j1, is_cem == True）
            cem_Rs = [s[1] for s in cand.struct_result['surfaces']
                      if s[2] == j1 and s[3]]
            if not cem_Rs:
                continue  # 结构异常，跳过
            R_cem = cem_Rs[0]
            cemented_var_info.append((gi, (j1, j2),
                                      phi_list[j1], nd_list[j1],
                                      phi_list[j2], nd_list[j2]))
            R_cem_original.append(R_cem)

    n_cem_vars   = len(cemented_var_info)
    n_total_vars = n_vars + n_cem_vars

    if n_total_vars == 0:
        print("  [形状因子优化] 无可优化变量（所有片均为胶合片且无独立面），跳过。")
        init_m = system_merit_function(
            best_combo, zoom_positions, stop_idx, d_mm_list, half_fov_rad, weights)
        return list(best_combo), init_m, {
            'initial_merit': init_m, 'final_merit': init_m,
            'improvement_pct': 0.0, 'n_iterations': 0, 'q_changes': [],
        }

    q_orig_arr     = np.array(q_original,     dtype=float)
    R_cem_orig_arr = np.array(R_cem_original, dtype=float)
    x0             = np.zeros(n_total_vars)   # 初始 Δq=0, ΔR_c=0

    # ── 计算初始 merit ─────────────────────────────────────────────
    initial_merit = system_merit_function(
        best_combo, zoom_positions, stop_idx, d_mm_list, half_fov_rad, weights)

    if verbose:
        print(f"\n  [形状因子优化] 优化变量数 = {n_total_vars}"
              f"（单片 Δq×{n_vars} + 胶合面 ΔR_c×{n_cem_vars}），"
              f"初始 merit = {initial_merit:.6f}")
        for k, (gi, li, phi_i, n_i) in enumerate(var_info):
            print(f"    变量{k+1}（Δq）: G{gi+1} 片{li+1}  "
                  f"q₀ = {q_orig_arr[k]:+.4f}  "
                  f"φ = {phi_i:+.5f}  n = {n_i:.5f}")
        for k, (gi_c, (j1, j2), phi_f, n_f, phi_r, n_r) in enumerate(cemented_var_info):
            print(f"    变量{n_vars+k+1}（ΔR_c）: G{gi_c+1} 片{j1+1}/{j2+1}  "
                  f"R_cem₀ = {R_cem_orig_arr[k]:+.4f} mm")

    iter_count = [0]   # 列表包装，使闭包内可修改

    def _build_modified_combo(delta_x: np.ndarray) -> List[GroupCandidate]:
        """
        给定优化变量向量（前 n_vars 个为 Δq，后 n_cem_vars 个为 ΔR_c），
        返回修改了曲率半径的候选组合副本。

        - Δq：非胶合单片的形状因子偏移，同原逻辑
        - ΔR_c：胶合面曲率偏移，外前/后表面由光焦度约束重算
        厚度近似不变（小扰动下属二阶效应）。
        """
        delta_q_arr = delta_x[:n_vars]
        delta_rc    = delta_x[n_vars:]

        # ── 单片 Δq 映射 ──────────────────────────────────────────
        group_var_map: Dict[int, Dict[int, Tuple[float, float, float]]] = {
            gi: {} for gi in range(n_groups)
        }
        for k, (gi, li, phi_i, n_i) in enumerate(var_info):
            group_var_map[gi][li] = (float(q_orig_arr[k] + delta_q_arr[k]), phi_i, n_i)

        # ── 胶合对 ΔR_c 映射 ────────────────────────────────────────
        # cem_front_map[gi][j1] = R_front_new（j1 的外前表面）
        # cem_cem_map[gi][j1]   = R_cem_new（胶合面，存储在 j1 下）
        # cem_rear_map[gi][j2]  = R_rear_new（j2 的外后表面）
        cem_front_map : Dict[int, Dict[int, float]] = {gi: {} for gi in range(n_groups)}
        cem_cem_map   : Dict[int, Dict[int, float]] = {gi: {} for gi in range(n_groups)}
        cem_rear_map  : Dict[int, Dict[int, float]] = {gi: {} for gi in range(n_groups)}

        for k, (gi_c, (j1, j2), phi_f, n_f, phi_r, n_r) in enumerate(cemented_var_info):
            drc = float(delta_rc[k])
            R_cem_new = float(R_cem_orig_arr[k]) + drc
            R_front_new, _, R_rear_new = _recompute_cemented_curvatures(
                R_cem_new, phi_f, n_f, phi_r, n_r)
            cem_front_map[gi_c][j1] = R_front_new
            cem_cem_map[gi_c][j1]   = R_cem_new
            cem_rear_map[gi_c][j2]  = R_rear_new

        new_combo: List[GroupCandidate] = []
        for gi, cand in enumerate(best_combo):
            has_single  = bool(group_var_map[gi])
            has_cemented = bool(cem_front_map[gi]) or bool(cem_rear_map[gi])

            if not has_single and not has_cemented:
                new_combo.append(cand)  # 该组无任何优化变量
                continue

            # 重建 surfaces 列表
            new_surfaces = []
            visit_count: Dict[int, int] = {}
            for s in cand.struct_result['surfaces']:
                s_desc, s_R, s_li, s_is_cem = s

                if s_li in group_var_map[gi] and not s_is_cem:
                    # 非胶合单片：按 Δq 更新
                    cnt = visit_count.get(s_li, 0)
                    visit_count[s_li] = cnt + 1
                    q_new, phi_i, n_i = group_var_map[gi][s_li]
                    R1_new, R2_new = _radii_from_q(q_new, phi_i, n_i)
                    new_surfaces.append((s_desc, R1_new if cnt == 0 else R2_new,
                                         s_li, s_is_cem))
                elif not s_is_cem and s_li in cem_front_map[gi]:
                    # 胶合对第一片的外前表面
                    new_surfaces.append((s_desc, cem_front_map[gi][s_li],
                                         s_li, s_is_cem))
                elif s_is_cem and s_li in cem_cem_map[gi]:
                    # 胶合面（is_cem=True，lens_idx 为前片索引）
                    new_surfaces.append((s_desc, cem_cem_map[gi][s_li],
                                         s_li, s_is_cem))
                elif not s_is_cem and s_li in cem_rear_map[gi]:
                    # 胶合对第二片的外后表面
                    new_surfaces.append((s_desc, cem_rear_map[gi][s_li],
                                         s_li, s_is_cem))
                else:
                    new_surfaces.append(s)

            new_struct = dict(cand.struct_result)
            new_struct['surfaces'] = new_surfaces
            new_cand = dataclasses.replace(cand, struct_result=new_struct)
            new_combo.append(new_cand)

        return new_combo

    def _penalized_merit(delta_x: np.ndarray) -> float:
        """带约束惩罚的系统 merit 目标函数（同时包含 Δq 和 ΔR_c 变量）。"""
        iter_count[0] += 1

        delta_q_arr = delta_x[:n_vars]
        delta_rc    = delta_x[n_vars:]

        # 约束1：单片 Δq 偏移量不得超过 _Q_DELTA_MAX
        if n_vars > 0 and np.any(np.abs(delta_q_arr) > _Q_DELTA_MAX):
            return 1e12

        # 约束2：非胶合单片各面 |R| >= _MIN_R_REFINE
        for k, (gi, li, phi_i, n_i) in enumerate(var_info):
            q_new = float(q_orig_arr[k] + delta_q_arr[k])
            R1, R2 = _radii_from_q(q_new, phi_i, n_i)
            if abs(R1) < _MIN_R_REFINE or abs(R2) < _MIN_R_REFINE:
                return 1e12

        # 约束3：胶合对 ΔR_c 不超过原胶合面曲率的 50%，且所有面 |R| >= _MIN_R_REFINE
        for k, (gi_c, (j1, j2), phi_f, n_f, phi_r, n_r) in enumerate(cemented_var_info):
            drc = float(delta_rc[k])
            R_cem_orig = float(R_cem_orig_arr[k])
            if abs(drc) > abs(R_cem_orig) * 0.5:
                return 1e12
            R_cem_new = R_cem_orig + drc
            if abs(R_cem_new) < _MIN_R_REFINE:
                return 1e12
            R_front_new, _, R_rear_new = _recompute_cemented_curvatures(
                R_cem_new, phi_f, n_f, phi_r, n_r)
            if abs(R_front_new) < _MIN_R_REFINE or abs(R_rear_new) < _MIN_R_REFINE:
                return 1e12

        # 构建修改后的候选组合并计算系统 merit
        modified_combo = _build_modified_combo(delta_x)
        try:
            merit = system_merit_function(
                modified_combo, zoom_positions, stop_idx,
                d_mm_list, half_fov_rad, weights,
            )
        except Exception:
            return 1e12

        # 每 50 次迭代打印进度
        if verbose and iter_count[0] % 50 == 0:
            print(f"  [形状因子优化] 迭代 {iter_count[0]:>4}  "
                  f"merit = {merit:.6f}", flush=True)

        return merit

    # ── Nelder-Mead 无梯度优化 ────────────────────────────────────
    opt_result = _sp_minimize(
        _penalized_merit,
        x0,
        method  = 'Nelder-Mead',
        options = {
            'maxiter': max_iter,
            'xatol'  : 1e-4,
            'fatol'  : 1e-6,
            'disp'   : False,
        },
    )

    best_delta_q = opt_result.x
    final_merit  = float(opt_result.fun) if opt_result.fun < 1e11 else initial_merit

    # 若优化结果反而变差，退回初始解（Δq = 0）
    if final_merit >= initial_merit:
        best_delta_q = x0
        final_merit  = initial_merit

    refined_combo   = _build_modified_combo(best_delta_q)
    improvement_pct = (initial_merit - final_merit) / max(abs(initial_merit), 1e-30) * 100.0

    if verbose:
        print(f"\n  [形状因子优化] 完成，共迭代 {iter_count[0]} 次")
        print(f"  优化前 merit = {initial_merit:.6f}")
        print(f"  优化后 merit = {final_merit:.6f}")
        print(f"  改善幅度     = {improvement_pct:.2f}%")
        if n_vars > 0:
            print(f"  单片形状因子 Δq 最优值：")
            for k, (gi, li, phi_i, n_i) in enumerate(var_info):
                print(f"    G{gi+1} 片{li+1}：Δq = {best_delta_q[k]:+.4f}")
        if n_cem_vars > 0:
            print(f"  胶合面曲率 ΔR_c 最优值：")
            for k, (gi_c, (j1, j2), _, _, _, _) in enumerate(cemented_var_info):
                print(f"    G{gi_c+1} 片{j1+1}/{j2+1}：ΔR_c = {best_delta_q[n_vars+k]:+.4f} mm  "
                      f"(R_cem: {R_cem_orig_arr[k]:+.4f} → {R_cem_orig_arr[k]+best_delta_q[n_vars+k]:+.4f})")

    report = {
        'initial_merit'  : initial_merit,
        'final_merit'    : final_merit,
        'improvement_pct': improvement_pct,
        'n_iterations'   : iter_count[0],
        'q_changes'      : list(best_delta_q),
    }

    return refined_combo, final_merit, report
