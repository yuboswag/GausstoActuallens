"""
search.py
玻璃穷举搜索主引擎：多进程并行穷举 CDGM 玻璃库，输出最优玻璃组合与光焦度分配。
"""

import os
import numpy as np
from pathlib import Path
from itertools import product as iterproduct
from concurrent.futures import ProcessPoolExecutor
from collections import Counter

from glass_db import split_glass_db
from solver import build_and_solve, pick_best_free_indices
from scoring import (is_valid, verify_constraints, optical_score, weighted_cost,
                     _process_one_combo)   # [FIX] 补充导入多进程工作函数

# ============================================================
# 预处理：V_gen 可行域剪枝（通用于任意 N 片）
# ============================================================

def prune_pools_by_vgen(candidate_pools, structure, phi_lo, phi_hi,
                        safety=2.0, min_keep=15):
    """
    基于消色差约束 Σφᵢ/Vᵢ = 0 对各候选池做预剪枝，返回缩减后的池列表。
    对任意 N 片、任意 pos/neg structure 通用，不依赖胶合结构。

    原理
    ----
    对位置 i（role=pos），其对 Σφ/V 的最大贡献为 phi_hi / V_min_i。
    消色差要求此贡献能被其余位置（neg 方向）平衡，即：
        phi_hi / V_i  ≤  safety × Σ_{j≠i, role=neg} (phi_hi / V_min_j)

    整理得 V_i 下界：
        V_i  ≥  phi_hi / (safety × neg_cancel_capacity)

    neg 位置同理，用 pos 的总消色差能力推算 neg 的 V 下界。

    参数
    ----
    candidate_pools : list[list[(name, g_dict)]]，各片候选池（完整版，非 slim）
    structure       : list[str]，如 ['pos','neg','pos','neg','pos']
    phi_lo, phi_hi  : float，φ 绝对值下/上界（1/max_f, 1/min_f）
    safety          : float，宽松系数，默认 2.0（避免过度剪枝漏掉边缘解）
    min_keep        : int，每个池至少保留的候选数，防止剪枝过激

    返回
    ----
    list[list[(name, g_dict)]]，长度与输入相同，每个池已过滤
    """
    N = len(candidate_pools)

    # 各池的 V_gen 最小值（只取有效正值）
    pool_v_min = []
    for pool in candidate_pools:
        vs = [g['V_gen'] for _, g in pool
              if g.get('V_gen') and g['V_gen'] > 1.0]
        pool_v_min.append(min(vs) if vs else 20.0)

    # 计算 neg 方向总消色差能力（所有 neg 位置的最大 φ/V 贡献之和）
    neg_cancel = sum(
        phi_hi / pool_v_min[i]
        for i, role in enumerate(structure)
        if role == 'neg'
    )
    # 计算 pos 方向总消色差能力
    pos_cancel = sum(
        phi_hi / pool_v_min[i]
        for i, role in enumerate(structure)
        if role == 'pos'
    )

    pruned = []
    for i, (pool, role) in enumerate(zip(candidate_pools, structure)):
        if role == 'pos':
            # pos 片的 φ/V 贡献必须能被 neg 片平衡
            # 去掉本池自身贡献（避免循环依赖）
            others_neg = neg_cancel  # neg 侧总量（pos 不贡献 neg cancel）
            if others_neg < 1e-12:
                pruned.append(pool)
                continue
            v_min_allowed = phi_hi / (safety * others_neg)
        else:
            # neg 片同理
            others_pos = pos_cancel
            if others_pos < 1e-12:
                pruned.append(pool)
                continue
            v_min_allowed = phi_hi / (safety * others_pos)

        filtered = [
            (nm, g) for nm, g in pool
            if g.get('V_gen') and g['V_gen'] >= v_min_allowed
        ]

        # 保底：至少保留 min_keep 个，防止过度剪枝
        if len(filtered) < min_keep:
            filtered = pool  # 回退到完整池

        pruned.append(filtered)

    # 打印剪枝效果摘要
    for i, (orig, filt, role) in enumerate(
            zip(candidate_pools, pruned, structure)):
        removed = len(orig) - len(filt)
        if removed > 0:
            print(f"  [V_gen 剪枝] 片{i+1}({role})："
                  f"{len(orig)} → {len(filt)} 种（剪掉 {removed} 种）")

    return pruned


# ============================================================
# 第六部分：主穷举函数
# ============================================================

def action_a(f_group, D, structure, apo,
             glass_db,
             glass_roles=None,
             cemented_pairs=None,
             phi_scan_steps=20,
             min_f_mm=None,
             max_f_mm=None,
             allow_duplicate_glass=False,
             adaptive_grouping=False,
             pool_overrides=None,
             optical_percentile=30,
             top_n=10,
             n_workers=5,
             tol_disp=5e-3,
             w_apo=500.0,
             tol_phi=1e-5):
    """
    动作A v6.2：广义波段版玻璃穷举，支持多进程并行。

    新增参数（软硬混合约束体系）
    ----------------------------
    tol_phi  : 合焦距约束容差（硬约束）。约束①由线性方程组精确求解，
               本质上自动满足，此参数主要用于 print_results 的标记显示。
               默认 1e-5，一般无需修改。
    tol_disp : 消初级色差约束容差（半软约束）。将约束②的过滤门槛从
               原来的 1e-6 放宽到该值，大幅增加候选方案数量。
               默认 5e-3，建议范围 1e-4 ~ 1e-2。
    w_apo    : 二级光谱（APO）惩罚权重（软约束，评分项）。约束③不再
               硬过滤，err_apo × w_apo 计入 optical_score，残余越小排名
               越靠前，但轻微违反的方案不会被直接丢弃。
               默认 500.0（经验值，使其与SA项量级相当）。

    cemented_pairs 说明（[FIX-8]）：
        None  → 自动推断：相邻异性片视为胶合
        []    → 显式指定无胶合面（全分离）
        [(i,j), ...]  → 指定具体胶合对
    """
    fit       = glass_db.get('__fit__', {})
    b_fit     = fit.get('b', 0.001682)
    lam_short = fit.get('lam_short', 486.13)
    lam_ref   = fit.get('lam_ref',   587.56)
    lam_long  = fit.get('lam_long',  656.27)

    phi_total = 1.0 / f_group
    N         = len(structure)
    n_const   = 3 if apo else 2
    n_free    = N - n_const

    if n_free < 0:
        raise ValueError(f"片数({N})小于约束数({n_const})，请增加片数或关闭APO。")

    if min_f_mm is None:
        min_f_mm = D / 2.0
    if max_f_mm is None:
        max_f_mm = 10.0 * abs(f_group)  # [BUG-FIX] APO模式下正片可达极长焦距，5x不够

    # 候选池
    pos_pool, neg_pool = split_glass_db(glass_db)
    all_pool = [(k, g) for k, g in glass_db.items() if k != '__fit__']

    if glass_roles is None:
        glass_roles = structure if phi_total > 0 else [
            'flint' if r == 'pos' else 'crown' for r in structure
        ]

    candidate_pools = []
    for i, role in enumerate(glass_roles):
        if role in ('pos', 'crown'):
            candidate_pools.append(pos_pool)
        elif role in ('neg', 'flint'):
            candidate_pools.append(neg_pool)
        elif role == 'any':
            candidate_pools.append(all_pool)
        else:
            raise ValueError(f"未知 glass_roles[{i}]：{role}")

    # [FIX-8] actual_cemented 必须在 if apo: 排序块之前确定，
    # 因为胶合感知排序需要知道哪些片位形成胶合对。
    # None → 自动推断（相邻异性片视为胶合）；[] → 显式全分离
    if cemented_pairs is None:
        actual_cemented = [
            (i, i + 1) for i in range(N - 1)
            if structure[i] != structure[i + 1]
        ]
    else:
        actual_cemented = list(cemented_pairs)

    # [Fix-Bug6] pool_overrides 覆盖移至 APO 排序之前，使覆盖池也经过 APO 排序
    if pool_overrides:
        for idx, override_pool in pool_overrides.items():
            if not (0 <= idx < N):
                raise ValueError(f"pool_overrides 片下标 {idx} 超出范围")
            candidate_pools[idx] = override_pool

    # [NEW] V_gen 可行域预剪枝：在 APO 排序之前缩减各池大小
    phi_lo_prune = 1.0 / max_f_mm
    phi_hi_prune = 1.0 / min_f_mm
    candidate_pools = prune_pools_by_vgen(
        candidate_pools, structure,
        phi_lo_prune, phi_hi_prune,
        safety=2.0, min_keep=15,
    )

    if apo:
        # 胶合感知排序辅助函数：
        # 给定"主池"中的一种玻璃，在"对池"里找满足胶合工艺条件
        # （ΔV≥12、Δn≥0.08）的最优 APO 合作伙伴，返回对应的 APO 误差。
        # 这个误差作为主池排序分数，使胶合对整体 APO 兼容度最优的玻璃优先被搜索到，
        # 而不是按各自孤立的 |dP_gen| 排序（后者对胶合 APO 完全是错误的准则）。
        def _best_partner_err(ga, partner_pool, b_fit_val,
                              dV_min=12.0, dn_min=0.08):
            Va = ga['V_gen']
            if not Va or Va <= 0:
                return np.inf
            ca = ga['dP_gen'] / Va + b_fit_val / Va ** 2
            if abs(ca) < 1e-10:
                return np.inf
            best = np.inf
            for _, gb in partner_pool:
                Vb = gb['V_gen']
                if not Vb or Vb <= 0: continue
                if abs(Va - Vb) < dV_min: continue
                if abs(ga.get('n_ref', ga['nd']) -
                       gb.get('n_ref', gb['nd'])) < dn_min: continue
                cb = gb['dP_gen'] / Vb + b_fit_val / Vb ** 2
                if abs(cb) < 1e-10: continue
                err = abs(ca / cb - Vb / Va) / abs(Vb / Va)
                if err < best:
                    best = err
            return best

        # 确定哪些片位处于胶合对中，需要使用胶合感知排序
        cemented_positions = set()
        for ci, cj in actual_cemented:
            cemented_positions.add(ci)
            cemented_positions.add(cj)

        def _apo_sort(pool, pool_idx):
            valid_v = [x for x in pool
                       if x[1]['V_gen'] is not None and x[1]['V_gen'] > 0]
            invalid_v = [x for x in pool
                         if x[1]['V_gen'] is None or x[1]['V_gen'] <= 0]

            if pool_idx in cemented_positions:
                # 找与本片胶合的对片片位，用对池做联合 APO 排序
                # 确定"对池"：遍历胶合对，找与本片位配对的另一片位
                partner_idx = None
                for ci, cj in actual_cemented:
                    if ci == pool_idx:
                        partner_idx = cj
                        break
                    if cj == pool_idx:
                        partner_idx = ci
                        break
                if partner_idx is not None:
                    partner_pool = candidate_pools[partner_idx]
                    # 按与对池最优合作伙伴的 APO 误差升序排列（越小越好）
                    valid_v = sorted(
                        valid_v,
                        key=lambda x: _best_partner_err(x[1], partner_pool, b_fit))
                else:
                    # 无法确定对池时退化为普通排序
                    valid_v = sorted(valid_v,
                                     key=lambda x: abs(x[1]['dP_gen']), reverse=True)
            else:
                # 非胶合片位：仍用原有的 |dP_gen| 排序
                valid_v = sorted(valid_v,
                                 key=lambda x: abs(x[1]['dP_gen']), reverse=True)

            invalid_v = sorted(invalid_v,
                               key=lambda x: abs(x[1]['dP_gen']), reverse=True)
            return valid_v + invalid_v

        # 第一遍：非胶合片位直接排序；胶合片位先用未排序的对池做初步排序
        # 第二遍：用初步排序后的对池做精确胶合感知排序（两轮收敛效果足够）
        candidate_pools = [_apo_sort(pool, i)
                           for i, pool in enumerate(candidate_pools)]
        # 第二轮：胶合片位用第一轮结果重新排序，收敛到稳定顺序
        candidate_pools = [_apo_sort(
                               # 非胶合片位已排好，不必重算
                               [x for x in candidate_pools[i]],
                               i)
                           for i in range(len(candidate_pools))]

    # [FIX-4] scan_range 直接返回物理有效光焦度区间，带正确符号，
    # 不依赖 phi_total，消除负组时的符号翻转 Bug。
    def scan_range(role):
        phi_lo = 1.0 / max_f_mm
        phi_hi = 1.0 / min_f_mm
        if role == 'pos':
            return np.linspace(phi_lo, phi_hi, phi_scan_steps)
        else:
            return np.linspace(-phi_hi, -phi_lo, phi_scan_steps)

    # 扫描/求解分组（在主进程中计算一次，传给所有工作进程）
    # [BUG-FIX] 扫描变量选择策略：优先选"少数角色"的片位作为扫描变量。
    # 原逻辑固定取前 n_free 个片位，对 pos-neg-pos-pos 结构会选 pos（片0），
    # 但 APO 解中 pos 焦距可达数百毫米（超出 max_f=5×f），扫描范围永远到不了。
    # 改为：n_free=1 时，选结构中出现次数最少的角色所对应的片位，
    # 例如 pos-neg-pos-pos → 选唯一的 neg（片1），其焦距在合理范围内可被扫到。
    if n_free == 0:
        scan_indices = []
        free_indices = list(range(N))
    elif n_free == 1:
        # 统计各角色出现次数，选"最少数角色"的第一个片位作为扫描变量
        role_counts = Counter(structure)
        rarest_role = min(role_counts, key=role_counts.get)
        scan_idx = next(i for i, r in enumerate(structure) if r == rarest_role)
        scan_indices = [scan_idx]
        free_indices  = [i for i in range(N) if i != scan_idx]
    else:
        scan_indices = list(range(n_free))
        free_indices = list(range(n_free, N))

    scan_grids = [scan_range(structure[i]) for i in scan_indices]

    print(f"\n  组元参数：f={f_group:+.1f}mm  D={D}mm  "
          f"φ_total={phi_total:+.6f}  "
          f"{'APO（消二级光谱）' if apo else '普通消色差'}")
    print(f"  工作波段：{lam_short:.1f}nm / {lam_ref:.1f}nm / {lam_long:.1f}nm")
    print(f"  片数={N}  约束数={n_const}  自由度={n_free}  "
          f"扫描步数={phi_scan_steps}")
    print(f"  结构：{' - '.join(structure)}")
    print(f"  胶合面：{actual_cemented if actual_cemented else '无（全分离）'}")
    print(f"  重复玻璃：{'允许' if allow_duplicate_glass else '不允许'}")
    for i, (role, pool) in enumerate(zip(structure, candidate_pools)):
        mark = "  ← 已覆盖" if (pool_overrides and i in pool_overrides) else ""
        print(f"  片{i+1}（{role}）候选：{len(pool)} 种{mark}")

    total_combos = 1
    for pool in candidate_pools:
        total_combos *= len(pool)
    print(f"  开始穷举，共 {total_combos:,} 种玻璃组合...", flush=True)

    # [OPT-3] 轻量化候选池：每个 glass dict 只保留 _process_one_combo 实际用到的 6 个字段，
    # 减少子进程 pickle 流量约 3-4×（从 ~10 字段缩减到 6 字段）。
    def _slim_pool(pool):
        return [
            (name, {
                'n_ref':    g['n_ref'],
                'V_gen':    g['V_gen'],
                'dP_gen':   g['dP_gen'],
                'dPgF':     g.get('dPgF', 0.0),
                'rel_cost': g['rel_cost'],
                'vd':       g.get('vd') or g.get('V_gen') or 0.0,   # [Fix-Bug4] V_gen 为 None 时回退到 0.0
            })
            for name, g in pool
        ]
    slim_pools = [_slim_pool(p) for p in candidate_pools]

    # [OPT-1] 构建参数生成器，将每个组合及其所需参数打包成元组
    # 注意：使用生成器而非列表，避免把所有组合预先加载到内存
    def combo_args_gen():
        for combo in iterproduct(*[
            [(name, g) for name, g in pool]
            for pool in slim_pools          # [OPT-3] 改用轻量化池
        ]):
            yield (combo, structure, actual_cemented, allow_duplicate_glass,
                   phi_total, apo, b_fit,
                   scan_indices, free_indices, scan_grids,
                   min_f_mm, max_f_mm,
                   tol_disp, w_apo)

    results   = []
    n_combo   = 0
    MAX_RESULTS_BUFFER = 100_000  # [FIX-7] 内存保护上限

    # [OPT-1] 多进程并行穷举
    # chunksize=200 表示每次给一个工作进程分配 200 个组合，
    # 这个值在"进程通信开销"和"负载均衡"之间取得平衡：
    # 太小（如1）→ 进程间通信频繁，开销大；
    # 太大（如10000）→ 某个进程拿到难组合时其他进程空闲，负载不均。
    print(f"  使用 {n_workers} 个并行进程（共 {os.cpu_count()} 核）...",
          flush=True)

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        for batch in executor.map(
            _process_one_combo,
            combo_args_gen(),
            chunksize=2000   # [OPT-3] 200→2000，减少 IPC 调度次数约 10×
        ):
            n_combo += 1

            if n_combo % 500_000 == 0:
                pct = n_combo / total_combos * 100
                print(f"  进度：{pct:.1f}%  有效方案 {len(results)} 个",
                      flush=True)

            results.extend(batch)

            # [FIX-7] 超出缓冲上限时，保留光学评分最优的前50%
            if len(results) >= MAX_RESULTS_BUFFER:
                results.sort(key=lambda x: x["opt_score"])
                results = results[:MAX_RESULTS_BUFFER // 2]
                print(f"  [内存保护] 已裁剪至 {len(results)} 个方案",
                      flush=True)

    print(f"\n  搜索完成：共处理 {n_combo:,} 种组合，"
          f"找到有效方案 {len(results)} 个")

    if not results:
        print(f"\n  未找到满足条件的方案。")
        print(f"  建议调整顺序：")
        print(f"    1. 增大 PHI_SCAN_STEPS（当前 {phi_scan_steps}）→ 40 或 60")
        print(f"    2. 缩小 min_f_mm（当前 {min_f_mm:.1f}mm）→ {D/4:.1f}mm")
        print(f"    3. 将 OPTICAL_PERCENTILE 改为 100 排除过滤干扰")
        print(f"    4. 将 EXCLUDED_FOR_OUTER 改为 set() 取消外表面限制")
        return []

    results.sort(key=lambda x: x["opt_score"])
    cutoff = max(1, int(len(results) * optical_percentile / 100))
    passed = results[:cutoff]

    print(f"\n  两阶段筛选：")
    print(f"    第一阶段（光学前{optical_percentile}%）：{len(passed):,} 个方案通过")
    print(f"    光学评分范围：{results[0]['opt_score']:.5f}"
          f" ~ {results[cutoff-1]['opt_score']:.5f}")
    passed.sort(key=lambda x: x["cost_score"])
    print(f"    第二阶段（成本排序）：最低均价={passed[0]['cost_score']:.2f}x")

    return passed[:top_n]


# ============================================================
# 第七部分：结果展示
# ============================================================

def print_results(results, f_group, structure, apo, glass_db,
                  tol_disp=5e-3, tol_phi=1e-5):
    if not results:
        return

    fit       = glass_db.get('__fit__', {})
    b_fit     = fit.get('b', 0.001682)
    lam_short = fit.get('lam_short', 486.13)
    lam_ref   = fit.get('lam_ref',   587.56)
    lam_long  = fit.get('lam_long',  656.27)
    phi_total = 1.0 / f_group
    N         = len(structure)

    print(f"\n{'='*82}")
    print(f"  最优方案（光学达标，成本由低到高）  "
          f"f={f_group:+.2f}mm  {'APO' if apo else '普通消色差'}")
    print(f"  波段：{lam_short:.1f}nm（短）/ {lam_ref:.1f}nm（参考）/ {lam_long:.1f}nm（长）")
    print(f"{'='*82}")

    col_w  = 10
    header = f"  {'排名':>3}  "
    for i in range(N):
        header += f"{'片'+str(i+1)+'('+structure[i][:3]+')':>{col_w}}  "
    header += f"{'均价':>5}  {'光学':>8}  {'场曲':>8}"
    print(header)
    print(f"  {'-'*78}")

    for i, r in enumerate(results, 1):
        row = f"  {i:>3}.  "
        for name in r["names"]:
            row += f"{name:>{col_w}}  "
        row += (f"{r['cost_score']:>5.2f}  "
                f"{r['opt_score']:>8.5f}  "
                f"{r['P_ptz']:>+8.5f}")
        print(row)

    best = results[0]
    print(f"\n{'='*82}")
    print(f"  ★ 第一名详情（性能达标中成本最低）")
    print(f"{'='*82}")

    role_labels = {'pos': '正片', 'neg': '负片'}

    print(f"  ※ n(λref)=折射率@{lam_ref:.0f}nm，V_gen=广义阿贝数({lam_short:.0f}/{lam_ref:.0f}/{lam_long:.0f}nm)，"
          f"均非目录 F-d-C 标准值（nd/νd）")
    print(f"\n  {'位置':>4}  {'角色':>4}  {'牌号':>10}  "
          f"{'n(λref)':>8}  {'V_gen':>8}  {'δP_gen':>8}  "
          f"{'δPg,F':>7}  {'相对成本':>6}  {'φ(mm⁻¹)':>11}  {'f(mm)':>9}")
    print(f"  {'-'*96}")

    for i, (nm, n_ref, Vg, dPg, dPgF, cost, phi, role) in enumerate(zip(
            best["names"], best["ns"],
            best["Vgens"], best["dPgens"], best["dPgFs"],
            best["rel_costs"], best["phis"], structure)):

        star = "★★" if dPg > 0.040 else ("★" if dPg > 0.015 else "  ")
        print(f"  {'片'+str(i+1):>4}  {role_labels[role]:>4}  "
              f"{nm:>10}  {n_ref:>8.5f}  "
              f"{Vg:>8.3f}  {dPg:>+7.4f}{star}  "
              f"{dPgF:>+7.4f}  {cost:>5.1f}x  "
              f"{phi:>+11.6f}  {1/phi:>+9.2f}")

    phis   = best["phis"]
    Vgens  = best["Vgens"]
    dPgens = best["dPgens"]

    print(f"\n  约束验证（广义波段）：")
    print(f"    Σφ              = {sum(phis):+.8f}  "
          f"（目标 {phi_total:+.8f}）  "
          f"{'✓' if abs(sum(phis)-phi_total)<tol_phi else '✗'}")

    err_disp = abs(sum(p / v for p, v in zip(phis, Vgens)))
    print(f"    Σφ/V_gen        = {sum(p/v for p, v in zip(phis, Vgens)):+.3e}  "
          f"（消初级色差，应=0）  "
          f"{'✓' if err_disp<tol_disp else '✗'}")

    if apo:
        lhs = sum(p * (dP / v + b_fit / v ** 2)
                  for p, dP, v in zip(phis, dPgens, Vgens))
        err_apo_val = abs(lhs)
        # 二级光谱为软约束（不硬过滤），✓ 仅作参考
        print(f"    Σφ·(δP/V+b/V²)  = {lhs:+.3e}  （消二级光谱，应≈0）  "
              f"{'✓' if err_apo_val < tol_disp else '△（软约束，已计入评分惩罚）'}")

    print(f"\n  Petzval 场曲 P = {best['P_ptz']:+.5f}")
    print(f"  加权均价       = {best['cost_score']:.2f}x  （H-K9L = 1.0x）")
    print(f"  光学评分       = {best['opt_score']:.5f}  （越小越好）")