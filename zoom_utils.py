"""
zoom_utils.py
变焦行程数据工具：从 CSV 读取各变焦位置的边缘光线数据，
计算 h⁴ 加权平均共轭因子 p̄，用于变焦组初始结构的形状因子优化。
"""

import numpy as np
from pathlib import Path

# ============================================================
# 第九部分：初始结构计算（共轭因子 p、形状因子 q、曲率半径 R、中心厚度 t）
# ============================================================
#
# 功能：给定穷举搜索确定的玻璃材料和焦距分配，用初级像差理论计算
#       各片的共轭因子、形状因子、曲率半径和中心厚度，
#       作为 Zemax 优化的初始结构。
#
# 设计原则：
#   • 单片：q_opt = -2(n²-1)p/(n+2)，Seidel 球差极小化解析解
#   • 胶合双片：在"所有面 |R| ≥ min_R_mm"的可行域内最小化 |SI|
#     → 放弃 SI=0 硬约束，残余球差交由 Zemax 处理
#   • 厚度：正透镜由矢高+最小边缘厚推算，负透镜取最小中心厚
#
# 变焦组（G2、G3）扩展原理：
#   变焦过程中各组元的共轭因子 p 随位置连续变化，单一位置的 p
#   不能代表整个变焦行程。正确做法是对 K 个代表性变焦位置求
#   h⁴ 加权平均 p̄，再代入 q_opt 公式，使得全行程综合球差最小。
#   权重 h⁴ 来自 S_I ∝ h⁴φ³，孔径最大的位置对球差贡献最大，
#   应在形状选取中获得更高的"发言权"。
#   G4（中继组）共轭关系固定，p 恒定，与 G1 同属单点计算。
# ============================================================


def compute_pbar_from_zoom_data(zoom_positions, verbose=True):
    """
    从多个变焦位置的近轴边缘光线追迹数据，计算 h⁴ 加权平均共轭因子 p̄。

    这是变焦组（G2、G3 等）初始结构设计的关键前置步骤。
    变焦过程中，p_k = (u_out_k + u_in_k) / (u_out_k - u_in_k) 随位置连续
    变化。单一变焦位置计算出的 p 只代表该焦距档，而透镜形状在整个行程中
    固定不变，因此需要以 h⁴ 为权重对所有变焦位置的 p_k 做加权平均。

    球差 S_I ∝ h⁴φ³，通光孔径越大的变焦位置球差贡献越大，应赋予更高权重。
    对全行程求导并令 dΣS_I/dq = 0，解析解为：
        q_opt = -2(n²-1)·p̄ / (n+2)，其中 p̄ = Σ(h_k⁴·p_k) / Σh_k⁴

    参数
    ----
    zoom_positions : list[dict]，每个 dict 对应一个变焦位置，包含：
        'name'  : str，位置名称（仅用于打印，可选）
        'h'     : float，该位置该组元的边缘光线高度（mm）
        'u_in'  : float，折射前边缘光线角度（rad）
        'u_out' : float，折射后边缘光线角度（rad）
    verbose : bool，是否打印中间过程

    返回
    ----
    p_bar : float，h⁴ 加权平均共轭因子
    p_values : list[float]，各位置的 p_k（用于诊断）
    weights  : list[float]，各位置的 h⁴ 权重（已归一化，用于诊断）
    """
    p_values  = []
    h4_weights = []

    for pos in zoom_positions:
        ui = pos['u_in']
        uo = pos['u_out']
        h  = pos['h']
        denom = uo - ui
        if abs(denom) < 1e-12:
            # u_in ≈ u_out 意味着该组元光焦度极弱，p 趋于无穷大（不参与加权）
            p_k = None
        else:
            p_k = (uo + ui) / denom
        p_values.append(p_k)
        h4_weights.append(h ** 4)

    # 过滤掉 p 为 None 或 h=0 的位置
    valid = [(p, w) for p, w in zip(p_values, h4_weights)
             if p is not None and w > 1e-12]

    if not valid:
        raise ValueError("所有变焦位置的 p 均无效，请检查光线追迹数据。")

    sum_w  = sum(w for _, w in valid)
    p_bar  = sum(p * w for p, w in valid) / sum_w

    # 归一化权重（用于打印）
    norm_w = [w / sum_w for w in h4_weights]

    if verbose:
        print(f"\n  {'位置':>10}  {'h(mm)':>8}  {'u_in':>10}  {'u_out':>10}"
              f"  {'p_k':>10}  {'h⁴权重':>8}  {'归一化':>8}")
        print("  " + "-" * 72)
        for i, pos in enumerate(zoom_positions):
            name = pos.get('name', f'位置{i+1}')
            p_str = f"{p_values[i]:>+10.5f}" if p_values[i] is not None else f"{'(无效)':>10}"
            h4_str = f"{h4_weights[i]:>8.3f}"
            nw_str = f"{norm_w[i]:>8.4f}"
            print(f"  {name:>10}  {pos['h']:>8.4f}  {pos['u_in']:>10.6f}"
                  f"  {pos['u_out']:>10.6f}  {p_str}  {h4_str}  {nw_str}")
        print(f"\n  → p̄（h⁴ 加权平均）= {p_bar:+.6f}")
        # 提示变焦范围内的 p 变化幅度
        valid_p = [p for p in p_values if p is not None]
        if len(valid_p) > 1:
            p_range = max(valid_p) - min(valid_p)
            print(f"  → p 变化幅度 Δp = {p_range:.4f}"
                  f"（广角到长焦：{valid_p[0]:+.4f} → {valid_p[-1]:+.4f}）")

    return p_bar, p_values, norm_w

def load_zoom_ray_csv(csv_path, lens_col_map,
                      pos_col='位置',
                      encoding='utf-8-sig'):
    """
    从高斯求解工具导出的 CSV 文件中读取各变焦位置的边缘光线追迹数据，
    自动构建 compute_pbar_from_zoom_data 所需的输入格式。

    CSV 格式要求（由配套 simulator.py 导出）：
        必须包含列：位置、h_Gx (mm)、u_in_Gx (rad)、u_out_Gx (rad)
        其中 x 为组元编号（G1~G4），每行对应一个变焦采样位置。

    参数
    ----
    csv_path     : str 或 Path，CSV 文件路径。
    lens_col_map : dict，{片索引(0-based): 组元名称字符串}
        指定哪些片需要读取以及对应的 CSV 列前缀。
        例如：{1: 'G2', 2: 'G3'} 表示读取片1(G2)和片2(G3)的数据，
        程序会自动拼接列名 'h_G2 (mm)'、'u_in_G2 (rad)'、'u_out_G2 (rad)'。
    pos_col      : str，位置名称列的列名，默认 '位置'。
    encoding     : str，CSV 编码，默认 'utf-8-sig'（Excel 导出兼容）。

    返回
    ----
    zoom_ray_data : dict，{片索引: [{'name':..., 'h':..., 'u_in':..., 'u_out':...}, ...]}
        格式与 S_ZOOM_RAY_DATA 完全相同，可直接替换手填数据。

    异常
    ----
    FileNotFoundError : CSV 文件不存在。
    KeyError          : CSV 中缺少所需列，错误信息列出缺失的列名。
    ValueError        : CSV 行数为 0 或数值解析失败。
    """
    import csv as _csv
    from pathlib import Path as _Path

    path = _Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到变焦光线数据文件：{csv_path}")

    with open(path, newline='', encoding=encoding) as f:
        # 跳过 # 开头的元数据行，兼容旧格式
        lines = [line for line in f if not line.strip().startswith('#')]
    import io as _io
    reader = _csv.DictReader(_io.StringIO(''.join(lines)))
    rows = list(reader)

    if not rows:
        raise ValueError(f"CSV 文件为空：{csv_path}")

    # 去除列名首尾空白（Excel 有时会在列名前后加空格）
    cleaned_rows = [{k.strip(): v.strip() for k, v in row.items()} for row in rows]

    zoom_ray_data = {}

    for lens_idx, gname in lens_col_map.items():
        h_col    = f'h_{gname} (mm)'
        uin_col  = f'u_in_{gname} (rad)'
        uout_col = f'u_out_{gname} (rad)'

        # 检查所需列是否存在
        missing_cols = [c for c in [pos_col, h_col, uin_col, uout_col]
                        if c not in cleaned_rows[0]]
        if missing_cols:
            available = list(cleaned_rows[0].keys())
            raise KeyError(
                f"CSV 中缺少以下列（片{lens_idx+1}/{gname}）：{missing_cols}\n"
                f"  文件实际列名：{available}"
            )

        positions = []
        for row in cleaned_rows:
            try:
                positions.append({
                    'name':  row[pos_col],
                    'h':     float(row[h_col]),
                    'u_in':  float(row[uin_col]),
                    'u_out': float(row[uout_col]),
                })
            except ValueError as e:
                raise ValueError(
                    f"CSV 数值解析失败（片{lens_idx+1}/{gname}，行：{row}）：{e}"
                )

        zoom_ray_data[lens_idx] = positions

    return zoom_ray_data


def parse_csv_metadata(csv_path, encoding='utf-8-sig'):
    """
    解析 CSV 文件头部的元数据行（# 开头）。

    返回 dict，可能包含：
        'stop_group': int (1-based，与 Gaussianoptics GUI 一致)
        'stop_shift': float
        'f_number_wide': float
        'f_number_tele': float
        'sensor_size': float

    如果没有元数据行，返回空 dict。
    向后兼容：无 # 行的旧 CSV 文件直接返回空 dict。
    """
    from pathlib import Path as _Path
    metadata = {}
    try:
        with open(_Path(csv_path), 'r', encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if not line.startswith('#'):
                    break  # 遇到非注释行则停止
                content = line[1:].strip()
                if '=' in content:
                    key, value = content.split('=', 1)
                    key   = key.strip().lower()
                    value = value.strip()
                    try:
                        if key == 'stop_group':
                            metadata['stop_group'] = int(value)
                        elif key == 'stop_shift':
                            metadata['stop_shift'] = float(value)
                        elif key in ('f_number_wide', 'f_number_tele', 'sensor_size'):
                            metadata[key] = float(value)
                    except ValueError:
                        pass  # 解析失败则跳过
    except (FileNotFoundError, OSError):
        pass
    return metadata


def load_zoom_configs_for_zemax(
    csv_path,
    bfd_mm,
    fnum_wide,
    fnum_tele=None,
    pos_col='位置',
    efl_col='焦距 EFL (mm)',
    d1_col='d1 (G1-G2间距) (mm)',
    d2_col='d2 (G2-G3间距) (mm)',
    d3_col='d3 (G3-G4间距) (mm)',
    encoding='utf-8-sig',
):
    """
    从 Gaussianoptics 导出的 CSV 构建 write_zoom_system 所需的 ZOOM_CONFIGS。

    BFD 修正属于 ZOS-API 闭环反馈部分，由调用方迭代确定，此处直接使用传入的 bfd_mm。

    参数
    ----
    csv_path  : str | Path，Gaussianoptics 导出的 CSV 文件路径
    bfd_mm    : float，后焦距（mm），直接传给 write_zoom_system 的 bfd_mm 参数
    fnum_wide : float，广角端 F/#
    fnum_tele : float | None，长焦端 F/#；None 表示全程固定 F/#
    pos_col   : str，位置名称列的列名，默认 '位置'

    返回
    ----
    zoom_configs : list[tuple]，格式与 write_zoom_system 要求一致：
                   [(name, efl, d1, d2, d3, epd), ...]
    bfd_physical : float，直接返回传入的 bfd_mm（未修正）
    """
    import csv as _csv
    import io as _io
    from pathlib import Path as _Path
    path = _Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到变焦数据文件：{csv_path}")
    with open(path, newline='', encoding=encoding) as f:
        lines = [line for line in f if not line.strip().startswith('#')]
    reader = _csv.DictReader(_io.StringIO(''.join(lines)))
    rows = [{k.strip(): v.strip() for k, v in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"CSV 文件为空：{csv_path}")
    required = [pos_col, efl_col, d1_col, d2_col, d3_col]
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise KeyError(f"CSV 缺少以下列：{missing}\n实际列名：{list(rows[0].keys())}")
    zoom_configs = []
    n = len(rows)
    for i, row in enumerate(rows):
        name = row[pos_col]
        efl  = float(row[efl_col])
        d1   = float(row[d1_col])
        d2   = float(row[d2_col])
        d3   = float(row[d3_col])
        if fnum_tele is None or fnum_tele == fnum_wide:
            fnum = fnum_wide
        else:
            t = i / max(n - 1, 1)
            fnum = fnum_wide + (fnum_tele - fnum_wide) * t
        epd = round(efl / fnum, 3)
        zoom_configs.append((name, efl, d1, d2, d3, epd))
    print(f"[load_zoom_configs_for_zemax] 读取 {len(zoom_configs)} 个变焦位置，bfd_mm={bfd_mm}")
    for cfg in zoom_configs:
        print(f"  {cfg[0]:<22} EFL={cfg[1]:.3f}  d1={cfg[2]:.3f}"
              f"  d2={cfg[3]:.3f}  d3={cfg[4]:.3f}  EPD={cfg[5]:.3f}")
    return zoom_configs


def check_mechanical_gaps_feasible(
    d1_thin_arr, d2_thin_arr, d3_thin_arr,
    delta_Hp_G1, delta_H_G2,
    delta_Hp_G2, delta_H_G3,
    delta_Hp_G3, delta_H_G4,
    min_gap_mm=2.0
):
    """
    检查给定主平面修正量下，所有变焦位置的机械气隙是否均 >= min_gap_mm。

    参数
    ----
    d1_thin_arr, d2_thin_arr, d3_thin_arr : array_like
        各变焦位置的薄透镜间距（主面间距），单位 mm
    delta_Hp_G1, delta_H_G2 : float
        G1 后主面偏移、G2 前主面偏移
    delta_Hp_G2, delta_H_G3 : float
        G2 后主面偏移、G3 前主面偏移
    delta_Hp_G3, delta_H_G4 : float
        G3 后主面偏移、G4 前主面偏移
    min_gap_mm : float
        最小机械气隙要求，默认 2.0 mm

    返回
    ----
    (feasible, min_d1, min_d2, min_d3)
        feasible : bool，所有位置气隙均 >= min_gap_mm 时 True
        min_d1/min_d2/min_d3 : 各气隙在所有位置中的最小值
    """
    d1_mech = d1_thin_arr + delta_Hp_G1 - delta_H_G2
    d2_mech = d2_thin_arr + delta_Hp_G2 - delta_H_G3
    d3_mech = d3_thin_arr + delta_Hp_G3 - delta_H_G4

    min_d1 = float(np.min(d1_mech))
    min_d2 = float(np.min(d2_mech))
    min_d3 = float(np.min(d3_mech))

    feasible = (min_d1 >= min_gap_mm and
                min_d2 >= min_gap_mm and
                min_d3 >= min_gap_mm)

    return feasible, min_d1, min_d2, min_d3


def correct_zoom_spacings(zoom_configs, group_principal_planes):
    """
    将高斯光学（薄透镜/主面间距）的变焦间距修正为物理面间距。

    高斯求解器输出的 d1, d2, d3 是主面间距（H'_前组 到 H_后组），
    而 Zemax LDE 需要的是物理面间距（前组最后面 到 后组第一面）。

    修正公式：
        d_physical = d_gaussian + delta_Hp_prev - delta_H_next

    其中：
    - delta_Hp_prev: 前一组后主面 H' 到其最后面的距离（典型负值，H'在组内部）
    - delta_H_next:  后一组前主面 H 到其第一面的距离（典型正值，H在组内部）
    - 两项都使从 d_gaussian 中"扣除"主面在组内部的偏移量

    参数
    ----
    zoom_configs : list[tuple]
        原始的变焦配置列表，每个元素为 (name, efl, d1, d2, d3, epd)
        其中 d1/d2/d3 是高斯主面间距
    group_principal_planes : list[tuple]
        4个组的主面数据，每个元素为 (delta_H, delta_Hp)
        索引 0=G1, 1=G2, 2=G3, 3=G4
        来自 compute_initial_structure 返回的 delta_H, delta_Hp

    返回
    ----
    corrected_configs : list[tuple]
        修正后的配置列表，格式同 zoom_configs，但 d1/d2/d3 已修正为物理间距
    """
    if len(group_principal_planes) != 4:
        raise ValueError(f"需要4个组的主面数据，实际收到 {len(group_principal_planes)} 个")

    # d1 = G1后 → G2前 的间距
    # d2 = G2后 → G3前 的间距
    # d3 = G3后 → G4前 的间距
    # 修正量：delta_Hp of 前组（从最后面量，负值=组内）
    #         delta_H  of 后组（从第一面量，正值=组内）

    corrections = []
    gap_labels = ['d1(G1→G2)', 'd2(G2→G3)', 'd3(G3→G4)']
    for gap_idx in range(3):
        prev_group = gap_idx      # 0=G1, 1=G2, 2=G3
        next_group = gap_idx + 1  # 1=G2, 2=G3, 3=G4
        dHp_prev = group_principal_planes[prev_group][1]  # delta_Hp
        dH_next  = group_principal_planes[next_group][0]  # delta_H
        correction = dHp_prev - dH_next  # 通常为负值（物理间距 < 高斯间距）
        corrections.append(correction)

    # 打印修正量
    print(f"\n[主面间距修正]")
    print(f"  {'间距':>12}  {'修正量(mm)':>10}  {'delta_Hp前组':>12}  {'delta_H后组':>12}")
    print("  " + "-" * 52)
    for i in range(3):
        dHp = group_principal_planes[i][1]
        dH  = group_principal_planes[i+1][0]
        print(f"  {gap_labels[i]:>12}  {corrections[i]:>+10.4f}  {dHp:>+12.4f}  {dH:>+12.4f}")

    # 应用修正
    corrected_configs = []
    for name, efl, d1, d2, d3, epd in zoom_configs:
        d1_corr = d1 + corrections[0]
        d2_corr = d2 + corrections[1]
        d3_corr = d3 + corrections[2]
        corrected_configs.append((name, efl, d1_corr, d2_corr, d3_corr, epd))

    # 打印对比表
    print(f"\n  {'位置':>12}  {'d1原':>8} {'d1修':>8}  {'d2原':>8} {'d2修':>8}  {'d3原':>8} {'d3修':>8}")
    print("  " + "-" * 68)
    for orig, corr in zip(zoom_configs, corrected_configs):
        print(f"  {orig[0]:>12}  {orig[2]:>8.3f} {corr[2]:>8.3f}"
              f"  {orig[3]:>8.3f} {corr[3]:>8.3f}"
              f"  {orig[4]:>8.3f} {corr[4]:>8.3f}")

    # 检查是否有修正后间距为负（物理不可能）
    for corr in corrected_configs:
        for di, label in zip([corr[2], corr[3], corr[4]], ['d1', 'd2', 'd3']):
            if di < 0.3:  # 最小空气间距 0.3mm
                print(f"  ⚠ 警告：{corr[0]} 的 {label} = {di:.3f} mm < 0.3mm，物理间距过小！")

    return corrected_configs
