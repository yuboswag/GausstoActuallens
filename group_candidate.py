"""
group_candidate.py
阶段一：多候选方案管理与多样性筛选。

核心思路
--------
变焦镜头的系统像差是各组元像差的叠加（带符号），最优系统来自各组元
像差相互补偿的最优组合，而非各组元单独最优的简单叠加。

本模块的职责：
  1. 为每个组元保留 top-N 个候选方案（而非只保留第一名）；
  2. 多样性筛选：确保这 N 个候选的像差特征（赛德尔特征向量）尽量分散，
     避免 N 个候选都聚集在"某一类"解附近，从而给系统级联合优化提供
     更大的组合搜索空间；
  3. 封装为 GroupCandidate 数据结构，携带 system_optimizer.py 所需的全部信息。

重要说明：nominal_seidel（名义赛德尔系数）
------------------------------------------
每个候选的 nominal_seidel 是在以下**名义光线条件**下计算的：
  边缘光高    h = D_mm / 2
  主光线角    ūbar = _NOMINAL_UBAR_RAD = 0.1 rad
  光阑位置    面 0（名义，孤立组元分析）
  入射角      u0 = 0（平行入射）
这些条件是人为设定的参考，目的是给每个候选贴一个"像差特征指纹"，
用于多样性分类——它不代表该组元在系统实际变焦位置的赛德尔贡献。
系统级实际贡献由 system_optimizer.py 中全系统追迹给出。
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from structure import compute_initial_structure
from validation import build_seq_with_dispersion
from seidel_gemini import analyze_one_position

# ════════════════════════════════════════════════════════════════════
#  §0  名义光线条件常量（仅用于多样性分类，不代表系统实际值）
# ════════════════════════════════════════════════════════════════════
_NOMINAL_UBAR_RAD = 0.1   # 名义主光线角（rad），固定值，仅用于特征指纹计算
_NOMINAL_STOP_IDX = 0     # 名义光阑位于面 0（孤立组元分析的简化约定）

# 用于多样性采样的赛德尔分量键列表（五种几何像差，不含色差项）
_DIVERSITY_KEYS = ['SI', 'SII', 'SIII', 'SIV', 'SV']


# ════════════════════════════════════════════════════════════════════
#  §1  数据结构
# ════════════════════════════════════════════════════════════════════
@dataclass
class GroupCandidate:
    """
    单个组元的一个候选方案，携带系统级优化所需的全部信息。

    字段说明
    --------
    group_index      : 组元编号（0-based），对应 ALL_GROUPS 的索引
    glass_combo      : 玻璃牌号列表（按光路顺序）
    nd_values        : {牌号: n_ref}，工作参考波长折射率
    vgen_list        : 广义阿贝数列表，与 glass_combo 顺序一致
    focal_lengths_mm : 各片焦距 (mm)
    cemented_pairs   : 胶合对列表，如 [(1,2), ...]（0-based 索引）
    spacings_mm      : 片间空气间距列表 (mm)，胶合面处为 0
    d_mm             : 通光口径 (mm)
    struct_result    : compute_initial_structure 的完整返回值
                       （含 surfaces、thickness 等，供建面序列使用）
    nominal_seidel   : 名义条件下的赛德尔系数字典
                       键：'SI','SII','SIII','SIV','SV'
                       ⚠ 仅用于多样性分类，不代表系统中实际赛德尔贡献
    merit_value      : 搜索阶段 opt_score（越小越好），仅供参考和排序
    n_surfaces       : 该组元面序列的面数
                       = 2 × N片 − N胶合对
                       用于系统拼接时快速确定各组面索引范围，避免重建 seq
    """
    group_index     : int
    glass_combo     : List[str]
    nd_values       : Dict[str, float]
    vgen_list       : List[float]
    focal_lengths_mm: List[float]
    cemented_pairs  : List[Tuple[int, int]]
    spacings_mm     : List[float]
    d_mm            : float
    struct_result   : Dict[str, Any]
    nominal_seidel  : Dict[str, float]    # ⚠ 名义条件，非系统实际值
    merit_value     : float
    n_surfaces      : int                  # 面序列长度，用于系统拼接索引定位


# ════════════════════════════════════════════════════════════════════
#  §2  面数计算辅助
# ════════════════════════════════════════════════════════════════════
def _count_surfaces(
    n_lenses    : int,
    cemented_pairs: List[Tuple[int, int]],
) -> int:
    """
    计算面序列中的面数，公式推导自 _build_lens_sequence 的构建逻辑：
      - 独立单片：贡献前后 2 面
      - 胶合对 (i,j)：贡献 3 面（前面 + 胶合面 + 后面），
        比两片独立（4 面）少 1 面
    故：面数 = 2 × N片 − N胶合对
    """
    return 2 * n_lenses - len(cemented_pairs)


# ════════════════════════════════════════════════════════════════════
#  §3  候选面序列构建（供 system_optimizer.py 调用）
# ════════════════════════════════════════════════════════════════════
def build_candidate_seq(candidate: 'GroupCandidate') -> List[Dict]:
    """
    从 GroupCandidate 构建已注入色散的面序列。

    返回值可直接用于 trace_paraxial / analyze_one_position。
    内部对 struct_result['surfaces'] 和 struct_result['thickness']
    做深拷贝，修改返回 seq（如更改 t_after）不影响 candidate 本身。

    供 system_optimizer.py 调用，避免重复手动构建。
    """
    return build_seq_with_dispersion(
        glass_names    = candidate.glass_combo,
        nd_values      = candidate.nd_values,
        cemented_pairs = candidate.cemented_pairs,
        surfaces       = candidate.struct_result['surfaces'],
        thickness      = candidate.struct_result['thickness'],
        spacings_mm    = candidate.spacings_mm,
        vgen_list      = candidate.vgen_list,
    )


# ════════════════════════════════════════════════════════════════════
#  §4  名义赛德尔计算（仅用于多样性分类）
# ════════════════════════════════════════════════════════════════════
def _compute_nominal_seidel(
    glass_combo   : List[str],
    nd_values     : Dict[str, float],
    vgen_list     : List[float],
    cemented_pairs: List[Tuple[int, int]],
    spacings_mm   : List[float],
    d_mm          : float,
    struct_result : Dict[str, Any],
) -> Dict[str, float]:
    """
    在孤立组元、名义光线条件下计算赛德尔系数，仅作为多样性分类依据。

    名义光线条件（⚠ 不代表系统实际值，仅用于"像差特征指纹"比较）：
      边缘光高    h = D_mm / 2
      主光线角    ū = _NOMINAL_UBAR_RAD = 0.1 rad（固定名义值）
      光阑位置    面 0（名义约定）
      入射角      u0 = 0（平行入射）

    返回只含几何像差的字典：{'SI','SII','SIII','SIV','SV'}
    色差项 CI/CII 不参与多样性分类（搜索阶段已由消色差约束筛选）。

    计算失败时返回全零字典，不影响流程（仅降低该候选在多样性采样中的代表性）。
    """
    seq = build_seq_with_dispersion(
        glass_names    = glass_combo,
        nd_values      = nd_values,
        cemented_pairs = cemented_pairs,
        surfaces       = struct_result['surfaces'],
        thickness      = struct_result['thickness'],
        spacings_mm    = spacings_mm,
        vgen_list      = vgen_list,
    )

    try:
        result = analyze_one_position(
            seq,
            D_mm         = d_mm,
            half_fov_rad = _NOMINAL_UBAR_RAD,   # 名义主光线角，非实际值
            stop_idx     = _NOMINAL_STOP_IDX,    # 名义光阑在面 0
        )
        totals = result['totals']
        # 只返回几何像差项，键名与 seidel_gemini.ABERR_KEYS 子集一致
        return {k: totals[k] for k in _DIVERSITY_KEYS}
    except Exception as e:
        print(f"    ⚠ nominal_seidel 计算异常（{e}），以零向量代替，不影响主流程。")
        return {k: 0.0 for k in _DIVERSITY_KEYS}


# ════════════════════════════════════════════════════════════════════
#  §5  最大-最小距离多样性采样
# ════════════════════════════════════════════════════════════════════
def _maxmin_diversity_sample(
    candidates: List['GroupCandidate'],
    k         : int,
) -> List['GroupCandidate']:
    """
    最大-最小距离采样（Max-Min Diversity Sampling）：
    从候选列表中选 k 个像差特征最多样的代表性方案。

    算法步骤
    --------
    1. 将每个候选表示为 5D 名义赛德尔向量 [SI, SII, SIII, SIV, SV]
    2. 按各维度标准差归一化（消除量级差异导致的主导效应）；
       若某维度标准差 < 1e-10（近似恒定），则该维度保持原值（归一化因子为 1）
    3. 第一个选点：merit_value 最低的候选（确保搜索阶段最优方案必定入选）
    4. 后续每步：在未选候选中，找"到已选集合中最近距离最大"的点加入，
       即每次选与已有代表集合差异最大的候选

    若 len(candidates) <= k，直接返回全部，不做采样。

    返回列表保持原始 GroupCandidate 对象引用（不做拷贝）。
    """
    if len(candidates) <= k:
        return list(candidates)

    n = len(candidates)

    # 构建 (N × 5) 赛德尔向量矩阵
    vecs = np.array(
        [[c.nominal_seidel[key] for key in _DIVERSITY_KEYS] for c in candidates],
        dtype=float,
    )

    # 按列标准差归一化；标准差过小的维度不做归一化（保持原值）
    col_std = vecs.std(axis=0)
    col_std[col_std < 1e-10] = 1.0
    vecs_norm = vecs / col_std

    selected_idxs: List[int] = []
    remaining_idxs: List[int] = list(range(n))

    # 第一个选点：merit_value 最低的候选（搜索阶段排名第一，保证必定入选）
    first = min(remaining_idxs, key=lambda i: candidates[i].merit_value)
    selected_idxs.append(first)
    remaining_idxs.remove(first)

    # 迭代选点：每步选与已选集合"最近距离最大"的候选
    for _ in range(k - 1):
        if not remaining_idxs:
            break

        best_cand_idx = -1
        best_min_dist = -1.0

        for idx in remaining_idxs:
            # 该候选到已选集合中每个成员的欧氏距离
            dists = [
                float(np.linalg.norm(vecs_norm[idx] - vecs_norm[s]))
                for s in selected_idxs
            ]
            # 到已选集合的最近距离（即该候选与已有代表的"最小差异"）
            min_dist = min(dists)
            # 保留最近距离最大的候选（与已有代表集合差异最大）
            if min_dist > best_min_dist:
                best_min_dist = min_dist
                best_cand_idx = idx

        selected_idxs.append(best_cand_idx)
        remaining_idxs.remove(best_cand_idx)

    return [candidates[i] for i in selected_idxs]


# ════════════════════════════════════════════════════════════════════
#  §6  候选列表摘要打印
# ════════════════════════════════════════════════════════════════════
def _print_candidate_summary(
    candidates: List['GroupCandidate'],
    group_name: str,
) -> None:
    """打印候选列表摘要，含赛德尔特征向量和 merit，便于用户核查多样性效果。"""
    print(f"\n  [{group_name}] 最终候选列表（按 merit 升序）：")
    header = (f"  {'排名':>3}  {'玻璃组合':<40}  "
              f"{'merit':>8}  "
              + "  ".join(f"{k:>7}" for k in _DIVERSITY_KEYS))
    print(header)
    print(f"  {'-'*110}")
    for i, c in enumerate(candidates, 1):
        combo_str = ' / '.join(c.glass_combo)
        seidel_str = "  ".join(
            f"{c.nominal_seidel[k]:>+7.4f}" for k in _DIVERSITY_KEYS
        )
        print(f"  {i:>3}.  {combo_str:<40}  "
              f"{c.merit_value:>8.5f}  {seidel_str}")
    print(f"  注：以上赛德尔系数为名义条件（h=D/2, ūbar=0.1rad, 光阑在面0），"
          f"仅用于多样性分类。")


# ════════════════════════════════════════════════════════════════════
#  §7  主接口：构建多样化候选列表
# ════════════════════════════════════════════════════════════════════
def select_diverse_candidates(
    search_results    : list,       # action_a() 的完整输出（未二次截断）
    group_index       : int,        # 组元编号（0-based）
    group_name        : str,        # 组元名称（如 "G1"），仅用于打印
    cemented_pairs    : list,       # ALL_CEMENTED_PAIRS[gi]
    spacings_mm       : list,       # ALL_SPACINGS_MM[gi]
    d_mm              : float,      # ALL_D_MM[gi]
    min_r_mm          : float,      # S_MIN_R_MM[gi]
    t_edge_min        : float,      # S_T_EDGE_MIN[gi]
    t_center_min      : float,      # S_T_CENTER_MIN[gi]
    t_cemented_min    : float,      # S_T_CEMENTED_MIN[gi]
    pbar_overrides    : dict,       # 变焦组 p̄ 覆盖字典；定焦组传 {}
    top_n             : int = 10,   # 最终保留的候选数上限
) -> List['GroupCandidate']:
    """
    从 action_a() 搜索结果中构建多样化 GroupCandidate 列表。

    调用说明
    --------
    为获得足够的多样性候选池，建议调用 action_a() 时将 top_n 设为
    较大值（如 SYSTEM_CAND_N × 3），此函数再从中做二次多样性筛选。
    若 action_a 的候选数 ≤ 3 × top_n，则全部保留（不做采样）。

    流程
    ----
    1. 对每个搜索结果调用 compute_initial_structure 计算结构参数
       打印进度：正在计算组元 G? 候选 i/n...
    2. 计算名义赛德尔系数（孤立组元、名义光线条件）
    3. 若候选数 ≤ 3×top_n：全部保留
       否则：执行最大-最小距离采样，选 top_n 个最多样方案
    4. 按 merit_value 升序排列后返回

    参数
    ----
    search_results  : action_a() 返回的候选 dict 列表，每项含
                      'names'/'ns'/'phis'/'Vgens'/'opt_score' 等字段
    pbar_overrides  : 同一组元的所有候选共享相同的 p̄（p̄ 由变焦 CSV 决定，
                      与玻璃选择无关），定焦组传入空 dict {}

    返回
    ----
    list[GroupCandidate]，最多 top_n 个，按 merit_value 升序排列
    """
    if not search_results:
        print(f"  [多样性筛选] {group_name}：搜索结果为空，返回空列表。")
        return []

    n_total = len(search_results)
    print(f"\n  [多样性筛选] {group_name}：共 {n_total} 个搜索候选，"
          f"目标保留 {top_n} 个多样化方案。")
    print(f"  正在逐候选计算初始结构和名义赛德尔系数...")

    # ── Step 1：逐候选计算结构 + 名义赛德尔 ─────────────────────────
    built_candidates: List[GroupCandidate] = []

    for i, res in enumerate(search_results):
        print(f"    正在计算 {group_name} 候选 {i+1}/{n_total}：{' / '.join(res['names'])}",
              flush=True)

        glass_names      = res['names']
        nd_values        = {name: n for name, n in zip(res['names'], res['ns'])}
        focal_lengths_mm = [1.0 / p for p in res['phis']]
        vgen_list        = res['Vgens']
        merit_value      = res['opt_score']

        # 计算初始结构（与 auto 模式第一名的计算方式完全一致）
        try:
            struct_result = compute_initial_structure(
                glass_names      = glass_names,
                nd_values        = nd_values,
                focal_lengths_mm = focal_lengths_mm,
                cemented_pairs   = cemented_pairs,
                spacings_mm      = spacings_mm,
                D_mm             = d_mm,
                min_R_mm         = min_r_mm,
                t_edge_min       = t_edge_min,
                t_center_min     = t_center_min,
                t_cemented_min   = t_cemented_min,
                h1               = d_mm / 2.0,
                u0               = 0.0,
                pbar_overrides   = pbar_overrides,
            )
        except Exception as e:
            print(f"    ⚠ {group_name} 候选 {i+1} 结构计算失败（{e}），跳过。")
            continue

        # 计算名义赛德尔系数（孤立组元，固定名义光线条件）
        nominal_seidel = _compute_nominal_seidel(
            glass_combo    = glass_names,
            nd_values      = nd_values,
            vgen_list      = vgen_list,
            cemented_pairs = cemented_pairs,
            spacings_mm    = spacings_mm,
            d_mm           = d_mm,
            struct_result  = struct_result,
        )

        # 计算面数（用于系统拼接时确定面索引范围）
        n_surfaces = _count_surfaces(len(glass_names), cemented_pairs)

        candidate = GroupCandidate(
            group_index      = group_index,
            glass_combo      = glass_names,
            nd_values        = nd_values,
            vgen_list        = vgen_list,
            focal_lengths_mm = focal_lengths_mm,
            cemented_pairs   = cemented_pairs,
            spacings_mm      = spacings_mm,
            d_mm             = d_mm,
            struct_result    = struct_result,
            nominal_seidel   = nominal_seidel,
            merit_value      = merit_value,
            n_surfaces       = n_surfaces,
        )
        built_candidates.append(candidate)

    if not built_candidates:
        print(f"  [多样性筛选] {group_name}：所有候选的结构计算均失败，返回空列表。")
        return []

    n_built    = len(built_candidates)
    threshold  = 3 * top_n

    # ── Step 2：多样性筛选 ────────────────────────────────────────
    if n_built <= threshold:
        print(f"  [多样性筛选] {group_name}：候选数 {n_built} ≤ 阈值 {threshold}，"
              f"全部保留，跳过采样。")
        selected = list(built_candidates)
    else:
        print(f"  [多样性筛选] {group_name}：候选数 {n_built} > 阈值 {threshold}，"
              f"执行最大-最小距离采样，选取 {top_n} 个...")
        selected = _maxmin_diversity_sample(built_candidates, top_n)

    # 按 merit_value 升序排列（最优方案排在第一位）
    selected.sort(key=lambda c: c.merit_value)

    print(f"  [多样性筛选] {group_name}：最终保留 {len(selected)} 个候选方案。")
    _print_candidate_summary(selected, group_name)

    return selected
