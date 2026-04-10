"""
seidel_gemini.py  ──  第三步：逐面赛德尔系数与色差系数计算 (专业版：含孔径光阑动态支持)
================================================================
理论依据：Welford《Aberrations of Optical Systems》第7章
          Warren Smith《Modern Optical Engineering》第3章

坐标约定
  • 光轴沿 +z 方向
  • 曲率半径 R > 0：球心在顶点右侧；R < 0：球心在左侧；±inf 为平面
  • 角度 u 以近轴小角近似，逆时针为正
  • 拉格朗日不变量 H = n(u·ȳ − ū·y) 在全系统为常数

赛德尔系数定义（符号与 Zemax Standard Seidel 一致）
  S_I   球差     S_II  彗差     S_III 像散
  S_IV  场曲     S_V   畸变
  C_I   初级轴向色差             C_II  初级垂轴色差

修复记录
--------
[Fix-2] add_dispersion_to_seq 新增 key='name' 模式，支持以玻璃名称为键
        的 glass_dn_map，彻底消除两块 nd 相同玻璃（如 H-ZLaF50E / H-F4）
        互相覆盖 dn 值的 bug。
        • key='nd'（默认）：原有行为，向后完全兼容。
        • key='name'：从面描述字段（desc）中提取玻璃名，按名称精确匹配。
          desc 格式约定：'片N(玻璃名) 前/后表面'——与 _build_lens_sequence 一致。
          实现分两步：
            Step-1 预扫描 seq，对名称解析成功的面收集 nd→dn 反查表；
            Step-2 逐面注入，名称匹配优先，失败时用 Step-1 的反查表做 nd 近邻
                   fallback，容差 0.002，超出则视为空气（dn=0）并打印警告。
          这彻底修复了前一版本中 nd_to_dn_fallback 被声明却从未填充、
          导致所有 fallback 路径静默返回 0.0 的 bug。
[Fix-efl] load_zoom_positions_from_csv：CSV 缺少 EFL 列时返回 None
          而非 0.0，与 run_step3.py Fix-4 的降级策略对齐。
[Fix-print] print_aberration_map：efl 为 None 时显示 'N/A' 而非崩溃。
"""

import csv
import copy
import io
import math
import re
import numpy as np
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
#  §0  配置：像差容差
# ════════════════════════════════════════════════════════════════════
TOLERANCES = {
    'SI':   0.10,   # 球差
    'SII':  0.05,   # 彗差
    'SIII': 0.05,   # 像散
    'SIV':  0.20,   # 场曲
    'SV':   0.10,   # 畸变
    'CI':   0.020,  # 轴向色差
    'CII':  0.020,  # 垂轴色差
}

ABERR_NAMES = {
    'SI':   '球  差 S_I  ', 'SII':  '彗  差 S_II ', 'SIII': '像  散 S_III',
    'SIV':  '场  曲 S_IV ', 'SV':   '畸  变 S_V  ',
    'CI':   '轴向色差 C_I ', 'CII':  '垂轴色差 C_II',
}
ABERR_KEYS = ['SI', 'SII', 'SIII', 'SIV', 'SV', 'CI', 'CII']

# ════════════════════════════════════════════════════════════════════
#  §1  色散量辅助函数
# ════════════════════════════════════════════════════════════════════
def dn_from_vd(n_d, v_d):
    """由折射率 nd 和阿贝数 Vd 计算色散量 dn = (nd-1)/Vd。"""
    if abs(v_d) < 1e-6:
        return 0.0
    return (n_d - 1.0) / v_d


# ── 内部辅助：从面描述字段中提取玻璃名 ─────────────────────────────
# desc 格式：'片N(玻璃名) 前表面' 或 '片N(玻璃名) 后表面'
# 正则匹配括号内的内容，例如 '片1(H-ZK11) 前表面' → 'H-ZK11'
_GLASS_NAME_RE = re.compile(r'片\d+\(([^)]+)\)')

def _extract_glass_name_from_desc(desc: str) -> str | None:
    """从 _build_lens_sequence 生成的 desc 字段中提取玻璃名称。"""
    m = _GLASS_NAME_RE.search(desc)
    return m.group(1) if m else None


def add_dispersion_to_seq(seq, glass_dn_map, air_dn=0.0, key='nd'):
    """
    向面序列注入色散量 (dn_in, dn_out)。

    参数
    ----
    seq          : 面序列（list[dict]），由 _build_lens_sequence 构建。
    glass_dn_map : 玻璃 → dn 的映射字典。
        key='nd'  时：{round(nd, 4): dn_value, ...}（原有格式，向后兼容）
        key='name'时：{'玻璃名': dn_value, ...}（名称键，无碰撞风险）
    air_dn       : 空气的色散量，默认 0.0。
    key          : 匹配模式，'nd'（默认）或 'name'。

    key='name' 模式说明（两步实现）
    --------------------------------
    Step-1  预扫描 seq：
        对每个 desc 解析成功且玻璃名在 glass_dn_map 中存在的面，
        将其非空气侧折射率 n_val 与对应 dn 写入 nd_to_dn_fallback。
        作用：为 Step-2 中名称解析失败的面（虚拟面、非标准 desc 等）
        提供基于 nd 近邻匹配的 fallback，容差 0.002。

    Step-2  逐面注入 dn_in / dn_out：
        优先路径：n ≈ 1.0 → air_dn；desc 中提取到玻璃名且 map 中有记录 → 直接使用。
        降级路径：名称解析失败或 map 中无记录 → nd 近邻匹配（来自 Step-1）；
                  仍失败 → air_dn，并打印警告提示用户检查配置。
    """
    if key == 'name':
        # ── Step-1：预扫描，建立 nd→dn 反查表（用于 fallback） ────────
        # 遍历所有面，对能成功解析名称的面，收集其非空气折射率 → dn 的对应关系。
        # 这样即使 Step-2 中某面 desc 格式异常，也能通过 nd 近邻找到正确的 dn。
        nd_to_dn_fallback: dict[float, float] = {}
        for surf in seq:
            glass_name = _extract_glass_name_from_desc(surf.get('desc', ''))
            if glass_name and glass_name in glass_dn_map:
                dn_val = glass_dn_map[glass_name]
                for n_val in (float(surf.get('n_in', 1.0)),
                              float(surf.get('n_out', 1.0))):
                    if abs(n_val - 1.0) >= 1e-3:           # 只记录非空气折射率
                        nd_to_dn_fallback[round(n_val, 5)] = dn_val

        def _fallback_nd(n_val: float) -> float | None:
            """
            nd 近邻匹配。
            返回匹配到的 dn；若 fallback 表为空或超出容差则返回 None（调用方打印警告）。
            """
            if abs(n_val - 1.0) < 1e-3:
                return air_dn
            if not nd_to_dn_fallback:
                return None
            best = min(nd_to_dn_fallback, key=lambda k: abs(k - n_val))
            return nd_to_dn_fallback[best] if abs(best - n_val) < 0.002 else None

        # ── Step-2：逐面注入 ──────────────────────────────────────────
        for surf in seq:
            desc       = surf.get('desc', '')
            glass_name = _extract_glass_name_from_desc(desc)
            n_in       = float(surf.get('n_in',  1.0))
            n_out      = float(surf.get('n_out', 1.0))

            # ── dn_in ──────────────────────────────────────────────────
            if abs(n_in - 1.0) < 1e-3:
                dn_in = air_dn                                  # 空气入射
            elif glass_name and glass_name in glass_dn_map:
                dn_in = glass_dn_map[glass_name]                # 名称精确匹配
            else:
                dn_in = _fallback_nd(n_in)
                if dn_in is None:
                    print(
                        f"  ⚠ [Fix-2 fallback] 面 '{desc}' dn_in 无法通过名称或"
                        f" nd 近邻匹配（n_in={n_in:.5f}），已置为 {air_dn}。"
                        f" 请检查 glass_dn_map 或面描述格式。"
                    )
                    dn_in = air_dn

            # ── dn_out ─────────────────────────────────────────────────
            if abs(n_out - 1.0) < 1e-3:
                dn_out = air_dn                                 # 空气出射
            elif glass_name and glass_name in glass_dn_map:
                dn_out = glass_dn_map[glass_name]               # 名称精确匹配
            else:
                dn_out = _fallback_nd(n_out)
                if dn_out is None:
                    print(
                        f"  ⚠ [Fix-2 fallback] 面 '{desc}' dn_out 无法通过名称或"
                        f" nd 近邻匹配（n_out={n_out:.5f}），已置为 {air_dn}。"
                        f" 请检查 glass_dn_map 或面描述格式。"
                    )
                    dn_out = air_dn

            # [Fix-Bug8] 改为直接赋值，避免面对象被复用时第二次注入的值被 setdefault 静默忽略
            surf['dn_in']  = dn_in
            surf['dn_out'] = dn_out

    else:
        # ── 原有 nd-keyed 模式（key='nd'，向后完全兼容）────────────────
        def _lookup(n_val):
            if abs(n_val - 1.0) < 1e-3:
                return air_dn
            if n_val in glass_dn_map:
                return glass_dn_map[n_val]
            best_key = min(glass_dn_map.keys(), key=lambda k: abs(k - n_val))
            if abs(best_key - n_val) < 0.002:
                return glass_dn_map[best_key]
            return air_dn

        for surf in seq:
            surf['dn_in']  = _lookup(float(surf.get('n_in',  1.0)))
            surf['dn_out'] = _lookup(float(surf.get('n_out', 1.0)))

    return seq


# ════════════════════════════════════════════════════════════════════
#  §2  变焦位置气隙替换与光阑动态定位
# ════════════════════════════════════════════════════════════════════
def build_zoom_system(seq_template, gap_indices, gap_values_mm):
    seq = copy.deepcopy(seq_template)
    for idx, val in zip(gap_indices, gap_values_mm):
        seq[idx]['t_after'] = float(val)
    return seq


def load_zoom_positions_from_csv(csv_path, gap_col_names,
                                  pos_col='位置',
                                  efl_col='焦距 EFL (mm)',
                                  encoding='utf-8-sig'):
    """
    从 CSV 读取变焦位置数据。

    [Fix-efl] EFL 列不存在时返回 None 而非 0.0，与 run_step3.py Fix-4 对齐，
    避免下游函数把 0 当有效焦距误用。
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 CSV：{csv_path}")
    with open(path, newline='', encoding=encoding) as f:
        # 跳过 # 开头的元数据行，向后兼容旧 CSV 格式
        lines = [line for line in f if not line.strip().startswith('#')]
    rows = [{k.strip(): v.strip() for k, v in row.items()}
            for row in csv.DictReader(io.StringIO(''.join(lines)))]

    # [Fix-Bug3] 在读取数据前校验所有必需列是否存在，防止缺列时静默截断
    if rows:
        missing_cols = [c for c in gap_col_names if c not in rows[0]]
        if missing_cols:
            available = list(rows[0].keys())
            raise KeyError(
                f"CSV 中缺少以下组间间距列：{missing_cols}\n"
                f"  文件实际列名：{available}\n"
                f"  请检查 S_SYSTEM_GAP_COLUMNS 配置是否与 CSV 列名完全一致（含空格）。"
            )

    zoom_positions = []
    for row in rows:
        gap_vals = [float(row[c]) for c in gap_col_names]   # [Fix-Bug3] 去掉 if c in row
        # [Fix-efl] 缺少 EFL 列 → None，不用 0.0
        efl = float(row[efl_col]) if efl_col in row else None
        zoom_positions.append({
            'name':          row.get(pos_col, '未知'),
            'efl':           efl,
            'gap_values_mm': gap_vals,
        })
    return zoom_positions


def find_stop_index(seq, keyword):
    """
    动态寻找光阑所在的表面序号。
    遍历面序列 seq 的 'desc' 字段，如果包含 keyword 则返回该序号。
    """
    for i, surf in enumerate(seq):
        if keyword in surf.get('desc', ''):
            return i
    print(f"⚠ 未找到包含关键词 '{keyword}' 的面，默认将光阑设在第 0 面。")
    return 0


# ════════════════════════════════════════════════════════════════════
#  §3  近轴光线追迹 (包含光阑位置修正)
# ════════════════════════════════════════════════════════════════════
def trace_paraxial(seq, stop_idx=0, n0=1.0, dn0=0.0,
                   y0=1.0, u0=0.0,
                   ybar0=0.0, ubar0=1.0):
    """
    近轴光线追迹：同时追迹边缘光线（y, u）和主光线（ȳ, ū）。
    stop_idx : 孔径光阑所在的面序号（0-based）。自动利用线性组合原理修正主光线。
    """
    y, u       = float(y0),    float(u0)
    ybar, ubar = float(ybar0), float(ubar0)  # 起初是"试验主光线"
    n, dn      = float(n0),    float(dn0)

    rays = []

    # 1. 正常追迹所有面（此时主光线为试验主光线）
    for surf in seq:
        R     = surf['R']
        n_out = float(surf.get('n_out', 1.0))
        dn_in  = float(surf.get('dn_in',  dn))
        dn_out = float(surf.get('dn_out', 0.0))

        c = 0.0 if (math.isinf(R) or abs(R) < 1e-14) else 1.0 / R

        # 折射不变量
        A    = n * (u    + y    * c)
        Abar = n * (ubar + ybar * c)

        # 近轴折射：n'u' = nu − yc(n'−n)
        u_out    = (n * u    - y    * c * (n_out - n)) / n_out
        ubar_out = (n * ubar - ybar * c * (n_out - n)) / n_out

        rays.append({
            'y': y,       'u_in': u,       'u_out': u_out,
            'ybar': ybar, 'ubar_in': ubar, 'ubar_out': ubar_out,
            'n_in': n,    'n_out': n_out,
            'dn_in': dn_in, 'dn_out': dn_out,
            'A': A,       'Abar': Abar,
            'R': R,       'c': c,
            'desc': surf.get('desc', f'面{len(rays)+1}'),
        })

        t    = float(surf.get('t_after', 0.0))
        y    = y    + u_out    * t
        u    = u_out
        ybar = ybar + ubar_out * t
        ubar = ubar_out
        n    = n_out
        dn   = dn_out

    # 2. 光阑位置修正 (Stop Shift / Pupil Shift)
    if stop_idx is not None and 0 <= stop_idx < len(rays):
        y_stop          = rays[stop_idx]['y']
        ybar_trial_stop = rays[stop_idx]['ybar']

        # 防止光阑恰好在焦平面（通常不可能发生）
        if abs(y_stop) > 1e-12:
            K = -ybar_trial_stop / y_stop

            # 将 K 因子应用到所有面的主光线参数上
            for r in rays:
                r['ybar']     += K * r['y']
                r['ubar_in']  += K * r['u_in']
                r['ubar_out'] += K * r['u_out']
                r['Abar']     += K * r['A']

    # 3. 计算拉格朗日不变量 H
    r0 = rays[0]
    H = r0['n_in'] * (r0['u_in'] * r0['ybar'] - r0['ubar_in'] * r0['y'])

    return rays, H


# ════════════════════════════════════════════════════════════════════
#  §4  逐面赛德尔系数计算
# ════════════════════════════════════════════════════════════════════
def compute_seidel_per_surface(rays, H):
    contribs = []
    for r in rays:
        y, A, Abar = r['y'], r['A'], r['Abar']
        n, np_, c  = r['n_in'], r['n_out'], r['c']
        dn, dnp    = r['dn_in'], r['dn_out']

        du_n = (r['u_out'] / np_ - r['u_in'] / n) if abs(np_) > 1e-14 else 0.0
        d1_n = (1.0 / np_ - 1.0 / n)              if abs(n)   > 1e-14 else 0.0

        SI   = -A**2     * y * du_n
        SII  = -A * Abar * y * du_n
        SIII = -Abar**2  * y * du_n
        SIV  =  H**2 * c * d1_n
        SV   = -(Abar / A) * (SIII + SIV) if abs(A) > 1e-14 else 0.0

        chi  = (dn  / n   if abs(n)   > 1e-14 else 0.0) \
             - (dnp / np_ if abs(np_) > 1e-14 else 0.0)
        CI   = A    * y * chi
        CII  = Abar * y * chi

        contribs.append({
            'SI': SI, 'SII': SII, 'SIII': SIII, 'SIV': SIV, 'SV': SV,
            'CI': CI, 'CII': CII, 'desc': r['desc'],
        })
    return contribs


def sum_contributions(contribs):
    totals = {k: 0.0 for k in ABERR_KEYS}
    for c in contribs:
        for k in ABERR_KEYS:
            totals[k] += c[k]
    return totals


# ════════════════════════════════════════════════════════════════════
#  §5  单变焦位置完整分析
# ════════════════════════════════════════════════════════════════════
def analyze_one_position(seq, D_mm, half_fov_rad, stop_idx=0, n0=1.0, dn0=0.0):
    rays, H  = trace_paraxial(seq, stop_idx=stop_idx, n0=n0, dn0=dn0,
                              y0=D_mm/2.0, u0=0.0,
                              ybar0=0.0,   ubar0=half_fov_rad)
    contribs = compute_seidel_per_surface(rays, H)
    totals   = sum_contributions(contribs)
    return {'contribs': contribs, 'totals': totals, 'H': H}


# ════════════════════════════════════════════════════════════════════
#  §6  全变焦位置批量分析
# ════════════════════════════════════════════════════════════════════
def analyze_all_zoom_positions(seq_template, zoom_positions, gap_indices,
                               D_mm, half_fov_rad, stop_idx=0,
                               tolerances=None, n0=1.0, dn0=0.0):
    """
    D_mm 支持两种形式：
      - float  : 全变焦位置使用同一入瞳直径（固定 F 数或固定入瞳）
      - list   : 与 zoom_positions 等长，每个位置独立入瞳直径（变 F 数场景）
    """
    tol = tolerances if tolerances is not None else TOLERANCES

    # D_mm 为列表时校验长度，防止无提示的 IndexError
    if isinstance(D_mm, (list, tuple)) and len(D_mm) != len(zoom_positions):
        raise ValueError(
            f"D_mm 列表长度 ({len(D_mm)}) 与 zoom_positions 数量 "
            f"({len(zoom_positions)}) 不一致。"
        )

    results = []

    for i, zp in enumerate(zoom_positions):
        d_this = D_mm[i] if isinstance(D_mm, (list, tuple)) else D_mm
        seq = build_zoom_system(seq_template, gap_indices, zp['gap_values_mm'])
        res = analyze_one_position(seq, d_this, half_fov_rad,
                                   stop_idx=stop_idx, n0=n0, dn0=dn0)
        res['name'] = zp['name']
        res['efl']  = zp['efl']   # 可能为 None（Fix-efl）
        results.append(res)

    diagnosis = {'exceed': {}, 'sources': {}, 'max_pos': {}}
    for k in ABERR_KEYS:
        vals     = [r['totals'][k] for r in results]
        abs_vals = [abs(v) for v in vals]
        max_abs  = max(abs_vals)
        max_idx  = abs_vals.index(max_abs)

        diagnosis['max_pos'][k] = results[max_idx]['name']
        if max_abs > tol.get(k, 1e9):
            diagnosis['exceed'][k] = max_abs

        contribs    = results[max_idx]['contribs']
        face_vals   = [(i, c['desc'], c[k]) for i, c in enumerate(contribs)]
        face_sorted = sorted(face_vals, key=lambda x: abs(x[2]), reverse=True)
        total_abs   = sum(abs(x[2]) for x in face_vals) or 1.0
        diagnosis['sources'][k] = [
            (i, desc, val, abs(val) / total_abs * 100)
            for i, desc, val in face_sorted[:5]
        ]

    return results, diagnosis


# ════════════════════════════════════════════════════════════════════
#  §7  打印输出
# ════════════════════════════════════════════════════════════════════
_SEP  = '═' * 102
_SEP2 = '─' * 102


def _fmt_efl(efl) -> str:
    """
    [Fix-print] 格式化 EFL 字段，兼容 efl=None（来自 Fix-efl 降级路径）。
    None → '     N/A'，数值 → 右对齐 8 位 2 位小数。
    """
    if efl is None:
        return '     N/A'
    return f'{efl:>8.2f}'


def print_aberration_map(results, stop_idx, seq_template, tolerances=None):
    tol = tolerances if tolerances is not None else TOLERANCES
    stop_desc = seq_template[stop_idx]['desc'] \
        if 0 <= stop_idx < len(seq_template) else "未知"

    print(f'\n{_SEP}')
    print(f'  像差地图[单位：mm，近轴赛德尔波前系数]')
    print(f'  光阑位置：自动锁定在 面 {stop_idx} ({stop_desc})')
    print(_SEP)

    hdr = f"  {'位置':>8}  {'EFL(mm)':>8}"
    for k in ABERR_KEYS:
        hdr += f"  {k:>9}"
    print(hdr)
    print(_SEP2)

    for r in results:
        # [Fix-print] 使用 _fmt_efl 避免 efl=None 导致的 TypeError
        row = f"  {r['name']:>8}  {_fmt_efl(r['efl'])}"
        for k in ABERR_KEYS:
            v, t = r['totals'][k], tol.get(k, 1e9)
            tag = '❌' if abs(v) > t else ('⚠ ' if abs(v) > 0.8 * t else '  ')
            row += f"  {v:>+7.4f}{tag}"
        print(row)
    print(_SEP2)

    tol_row = f"  {'容  差':>8}  {'':>8}"
    for k in ABERR_KEYS:
        tol_row += f"  {'±'+f'{tol[k]:.4f}':>9}"
    print(tol_row)

    max_row = f"  {'最 大 值':>8}  {'':>8}"
    for k in ABERR_KEYS:
        vmax = max(abs(r['totals'][k]) for r in results)
        tag  = '❌' if vmax > tol.get(k, 1e9) else '  '
        max_row += f"  {vmax:>+7.4f}{tag}"
    print(max_row)
    print(_SEP)


def print_surface_contributions(results, diagnosis, top_n=5):
    print(f'\n{_SEP}')
    print(f'  各面贡献分布  （取各像差最大的变焦位置，逐面排序）')
    print(_SEP)
    for k in ABERR_KEYS:
        sources  = diagnosis['sources'][k]
        max_pos  = diagnosis['max_pos'][k]
        flag     = '❌ 超标' if k in diagnosis['exceed'] else '✓  正常'
        print(f'\n  {ABERR_NAMES[k]}[{flag}]  最大值出现在：{max_pos}')
        print(f'  {"排名":>4}  {"面序号":>4}  {"面描述":36}  {"贡献(mm)":>11}  {"占比%":>6}  {"累计%":>6}')
        print(f'  {"─"*80}')
        cumulative = 0.0
        for rank, (idx, desc, val, pct) in enumerate(sources[:top_n], 1):
            cumulative += pct
            bar = '█' * max(1, int(pct / 5 + 0.5))
            print(f'  {rank:>4}  {idx:>4}  {desc:36}  {val:>+11.6f}'
                  f'  {pct:>5.1f}%  {cumulative:>5.1f}%  {bar}')
    print(f'\n{_SEP}')


def print_summary(diagnosis, tolerances=None):
    tol = tolerances if tolerances is not None else TOLERANCES
    exceed = diagnosis['exceed']
    print(f'\n{_SEP}')
    print(f'  诊断汇总')
    print(_SEP)
    if not exceed:
        print('\n  ✅ 全部像差均在容差范围内。进入下一阶段优化。')
    else:
        print(f'\n  ❌ 以下 {len(exceed)} 项超标，建议先处理再进入第四步：\n')
        advice = {
            'SI':   '调整球面曲率 R，或将单片拆分为多片以分摊球差。',
            'SII':  '调整主光线高度比 ȳ/y，或移动光阑位置（利用光阑移动定理）。',
            'SIII': '加强场镜补偿，像散与场曲通常联动。',
            'SIV':  '满足 Petzval 和条件 Σ(φ_i/n_i) ≈ 0；正负透镜折射率匹配。',
            'SV':   '检查光阑前后的结构对称性，非对称结构需专门校正。',
            'CI':   '检查各组 V_gen 分配（正组低V/高色散，负组高V/低色散）。',
            'CII':  '调整主光线经过各面的高度分布，垂轴色差对光阑位置极敏感。',
        }
        for k, max_val in exceed.items():
            tval = tol.get(k, 1e9)
            print(f'  • {ABERR_NAMES[k]} (最大值 {max_val:.5f}，超标 {max_val/tval:.1f}×)'
                  f' -> 建议: {advice.get(k, "")}')
    print(_SEP)


def print_all(results, diagnosis, stop_idx, seq_template, tolerances=None, top_n=5):
    print_aberration_map(results, stop_idx, seq_template, tolerances)
    print_surface_contributions(results, diagnosis, top_n)
    print_summary(diagnosis, tolerances)


def print_seq_index(seq):
    print('\n  面序列索引（打印后确认 gap_indices 和 stop_idx）：')
    print(f'  {"序号":>4}  {"描述":40}  {"R(mm)":>10}  {"n_out":>7}  {"t_after(mm)":>12}')
    print('  ' + '─' * 78)
    for i, s in enumerate(seq):
        R = s.get('R', float('inf'))
        R_str = f'{R:>10.3f}' if not math.isinf(R) else f'{"∞":>10}'
        print(f'  {i:>4}  {s.get("desc",""):40}  {R_str}'
              f'  {s.get("n_out",1.0):>7.4f}  {s.get("t_after",0):>12.4f}')


# ════════════════════════════════════════════════════════════════════
#  §8  测试入口 / 独立运行演示
# ════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    # 独立测试用例：直接运行本脚本查看效果。
    # 注意：测试用的 desc 格式为 'Gx-片N 前/后表面'，不含玻璃名括号，
    # 因此 dn 值直接使用预置在 seq_template 中的 dn_in/dn_out，
    # 不经过 add_dispersion_to_seq。
    seq_template = [
        # G1
        {'R':  80.0, 'n_in': 1.0,    'n_out': 1.5168, 't_after': 4.0,  'dn_in': 0.0,     'dn_out': 0.00814, 'desc': 'G1-片1 前表面'},
        {'R': -60.0, 'n_in': 1.5168, 'n_out': 1.0,    't_after': 0.5,  'dn_in': 0.00814, 'dn_out': 0.0,     'desc': 'G1-片1 后表面'},
        {'R': -55.0, 'n_in': 1.0,    'n_out': 1.7280, 't_after': 2.5,  'dn_in': 0.0,     'dn_out': 0.01321, 'desc': 'G1-片2 前表面'},
        {'R': 120.0, 'n_in': 1.7280, 'n_out': 1.0,    't_after': 12.0, 'dn_in': 0.01321, 'dn_out': 0.0,     'desc': 'G1-片2 后表面（G1→G2）'},
        # G2
        {'R': -40.0, 'n_in': 1.0,    'n_out': 1.6700, 't_after': 2.0,  'dn_in': 0.0,     'dn_out': 0.01095, 'desc': 'G2-片1 前表面'},
        {'R':  35.0, 'n_in': 1.6700, 'n_out': 1.0,    't_after': 0.3,  'dn_in': 0.01095, 'dn_out': 0.0,     'desc': 'G2-片1 后表面'},
        {'R':  32.0, 'n_in': 1.0,    'n_out': 1.5168, 't_after': 3.0,  'dn_in': 0.0,     'dn_out': 0.00814, 'desc': 'G2-片2 前表面'},
        {'R': -80.0, 'n_in': 1.5168, 'n_out': 1.0,    't_after': 8.0,  'dn_in': 0.00814, 'dn_out': 0.0,     'desc': 'G2-片2 后表面（G2→G3）'},
        # G3
        {'R': -90.0, 'n_in': 1.0,    'n_out': 1.5168, 't_after': 3.5,  'dn_in': 0.0,     'dn_out': 0.00814, 'desc': 'G3-片1 前表面'},
        {'R':  45.0, 'n_in': 1.5168, 'n_out': 1.0,    't_after': 0.4,  'dn_in': 0.00814, 'dn_out': 0.0,     'desc': 'G3-片1 后表面'},
    ]

    print_seq_index(seq_template)

    gap_indices        = [3, 7]
    STOP_KEYWORD       = "G2-片1 前表面"
    stop_surface_index = find_stop_index(seq_template, STOP_KEYWORD)

    zoom_positions = [
        {'name': '广角端', 'efl': 20.0,  'gap_values_mm': [18.5,  4.2]},
        {'name': '中间位', 'efl': 50.0,  'gap_values_mm': [ 9.8,  9.8]},
        {'name': '长焦端', 'efl': 100.0, 'gap_values_mm': [ 3.4, 16.8]},
    ]

    results, diagnosis = analyze_all_zoom_positions(
        seq_template   = seq_template,
        zoom_positions = zoom_positions,
        gap_indices    = gap_indices,
        D_mm           = 25.0,
        half_fov_rad   = math.radians(7.0),
        stop_idx       = stop_surface_index,
    )

    print_all(results, diagnosis,
              stop_idx     = stop_surface_index,
              seq_template = seq_template,
              top_n        = 5)