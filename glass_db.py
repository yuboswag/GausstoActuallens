"""
glass_db.py
玻璃数据库读取与分池：从 CDGM xlsx 加载折射率、色散参数，按 V_gen 中位数分池。
"""

import numpy as np
import pandas as pd
from pathlib import Path
from dispersion import sellmeier_n, schott_n, compute_generalized_params, fit_PV_line

# ============================================================
# 第二部分：玻璃数据库读取
# ============================================================

def load_glass_db(xlsx_path, melt_filter=None,
                  lam_short_nm=486.13,
                  lam_ref_nm=587.56,
                  lam_long_nm=656.27,
                  verbose=True):
    """
    从 CDGM xlsx 读取玻璃数据库，计算广义色散参数。

    支持格式：
        格式A：官方原始宽表（≥100列）
        格式B：精简版（<13列，无Sellmeier）
        格式C：精简版+Sellmeier（13列）← 推荐

    返回 glass_db 字典，每项包含：
        nd, vd, dPgF          可见光标准参数（兼容性保留）
        n_ref                 参考波长折射率 [FIX-2]
        V_gen, P_gen, dP_gen  广义色散参数
        rel_cost, melt        成本和生产频率
        has_sellmeier         是否有 Sellmeier 系数
    """
    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到玻璃库文件：{xlsx_path}")

    xl = pd.ExcelFile(path)
    sheet_name = next(
        (s for s in xl.sheet_names
         if any(k in s for k in ['glass', 'Glass', '玻璃', 'CDGM', 'cdgm'])),
        xl.sheet_names[0]
    )

    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    n_cols = raw.shape[1]

    if n_cols >= 100:
        # 格式A：官方宽表（列索引依赖特定版本，建议按列名定位）
        data = raw.iloc[1:, [0, 13, 23, 61, 229, 230]].copy()
        data.columns = ['name', 'nd', 'vd', 'dPgF', 'melt_freq', 'price_raw']
        price_divisor = 10.0
        sellmeier_data = raw.iloc[1:, [27, 28, 29, 30, 31, 32]].copy()
        sellmeier_data.columns = ['A0', 'A1', 'A2', 'A3', 'A4', 'A5']
        sellmeier_data.index = data.index
        has_sellmeier_col = True
    elif n_cols >= 13:
        # 格式C：精简版+Sellmeier
        data = raw.iloc[3:, [0, 1, 2, 3, 4, 6]].copy()
        data.columns = ['name', 'nd', 'vd', 'dPgF', 'melt_freq', 'price_raw']
        price_divisor = 1.0
        sellmeier_data = raw.iloc[3:, [7, 8, 9, 10, 11, 12]].copy()
        sellmeier_data.columns = ['A0', 'A1', 'A2', 'A3', 'A4', 'A5']
        sellmeier_data.index = data.index
        has_sellmeier_col = True
    else:
        # 格式B：精简版无Sellmeier
        data = raw.iloc[3:, [0, 1, 2, 3, 4, 6]].copy()
        data.columns = ['name', 'nd', 'vd', 'dPgF', 'melt_freq', 'price_raw']
        price_divisor = 1.0
        sellmeier_data = None
        has_sellmeier_col = False

    # 清洗
    data = data[data['name'].apply(lambda x: isinstance(x, str))].copy()
    data = data[~data['name'].str.strip().isin(['over!', 'Over!', 'OVER!', ''])].copy()
    for col in ['nd', 'vd', 'dPgF', 'price_raw']:
        data[col] = pd.to_numeric(data[col], errors='coerce')
    data = data.dropna(subset=['nd', 'vd', 'dPgF'])
    if melt_filter is not None:
        data = data[data['melt_freq'].isin(melt_filter)]

    db = {}
    n_with_sellmeier = 0

    for idx, row in data.iterrows():
        name = row['name'].strip()
        price = row['price_raw']
        rel_cost = float(price) / price_divisor if pd.notna(price) else 1.0

        entry = {
            'nd':       float(row['nd']),
            'vd':       float(row['vd']),
            'dPgF':     float(row['dPgF']),
            'rel_cost': rel_cost,
            'melt':     str(row['melt_freq']).strip(),
            'n_ref':    float(row['nd']),   # [FIX-2] 默认用 nd，有精确系数时覆盖
            'V_gen':    None,
            'P_gen':    None,
            'dP_gen':   None,
            'has_sellmeier': False,
        }

        if has_sellmeier_col and idx in sellmeier_data.index:
            srow = sellmeier_data.loc[idx]
            try:
                coeffs = tuple(float(srow[c]) for c in ['A0', 'A1', 'A2', 'A3', 'A4', 'A5'])
                if not any(np.isnan(coeffs)):
                    # ── 自动检测公式类型 ──────────────────────────────────
                    # Excel 中混存了两种格式：
                    #   • 多数玻璃：Sellmeier 系数（K1,L1,K2,L2,K3,L3），
                    #     列名虽标为 A0~A5，但数值本质是 Sellmeier。
                    #   • 少数玻璃（如 ZF6/ZF7 等）：真正的 Schott A0~A5。
                    # 判断依据：在 d 线（587.56nm）用两种公式各算一次，
                    # 与材料库 nd 对比，偏差 < 0.01 的为正确格式。
                    n_d_sell = sellmeier_n(0.58756, *coeffs)
                    n_d_scho = schott_n(0.58756, *coeffs)
                    nd_cat   = entry['nd']

                    sell_ok = (not np.isnan(n_d_sell)) and abs(n_d_sell - nd_cat) < 0.01
                    scho_ok = (not np.isnan(n_d_scho)) and abs(n_d_scho - nd_cat) < 0.01

                    if sell_ok:
                        # 多数情况：Sellmeier 格式
                        n_r, V_g, P_g = compute_generalized_params(
                            coeffs, lam_short_nm, lam_ref_nm, lam_long_nm
                        )
                        formula = 'sellmeier'
                    elif scho_ok:
                        # 少数情况：真正的 Schott A0~A5
                        A0, A1, A2, A3, A4, A5 = coeffs
                        n_s = schott_n(lam_short_nm / 1000.0, A0, A1, A2, A3, A4, A5)
                        n_r = schott_n(lam_ref_nm  / 1000.0, A0, A1, A2, A3, A4, A5)
                        n_l = schott_n(lam_long_nm / 1000.0, A0, A1, A2, A3, A4, A5)
                        if not any(np.isnan([n_s, n_r, n_l])) and abs(n_s - n_l) > 1e-10:
                            V_g = (n_r - 1.0) / (n_s - n_l)
                            P_g = (n_s - n_r) / (n_s - n_l)
                        else:
                            n_r = V_g = P_g = None
                        formula = 'schott'
                    else:
                        n_r = V_g = P_g = None
                        formula = None

                    if n_r is not None:
                        entry.update({
                            'V_gen': V_g, 'P_gen': P_g,
                            'has_sellmeier': True, 'schott': coeffs,
                            'n_ref': n_r,
                            'dispersion_formula': formula,
                        })
                        n_with_sellmeier += 1
                        if scho_ok and not sell_ok and verbose:
                            print(f"  ⓘ [{name}] 识别为 Schott A0~A5，已用 Schott 公式计算。")
                    elif verbose:
                        print(f"  ⚠ [{name}] 系数与 nd 不符（sell={n_d_sell:.4f}，"
                              f"schott={n_d_scho:.4f}，nd={nd_cat:.5f}），回退标准值。")
            except (ValueError, TypeError):
                pass

        # 无精确色散系数时回退到可见光标准值（F-d-C 波段精确，其他波段近似）
        if entry['V_gen'] is None:
            entry['V_gen'] = float(row['vd'])
            entry['P_gen'] = float(row['dPgF']) + (0.6438 - 0.001682 * float(row['vd']))
            entry['dP_gen'] = float(row['dPgF'])

        db[name] = entry

    # 拟合 P-V 直线，计算广义异常色散量
    a_fit, b_fit = fit_PV_line(db)
    for name, g in db.items():
        if g['V_gen'] is not None and abs(g['V_gen']) > 1e-6:
            g['dP_gen'] = g['P_gen'] - (a_fit + b_fit / g['V_gen'])
        else:
            g['dP_gen'] = 0.0

    db['__fit__'] = {
        'a': a_fit, 'b': b_fit,
        'lam_short': lam_short_nm,
        'lam_ref':   lam_ref_nm,
        'lam_long':  lam_long_nm,
    }

    if verbose:
        crown = sum(1 for k, g in db.items() if k != '__fit__' and g['vd'] > 50)
        flint = sum(1 for k, g in db.items() if k != '__fit__' and g['vd'] < 50)
        n_schott = sum(1 for k, g in db.items()
                       if k != '__fit__' and g.get('dispersion_formula') == 'schott')
        print(f"玻璃库：{len(db)-1} 种  （冕牌 νd>50: {crown}，燧石 νd<50: {flint}）")
        print(f"  工作波段：{lam_short_nm:.2f}nm / {lam_ref_nm:.2f}nm / {lam_long_nm:.2f}nm")
        print(f"  精确色散系数：{n_with_sellmeier} 种"
              f"（Sellmeier: {n_with_sellmeier - n_schott}，Schott: {n_schott}）")
        print(f"  P-V拟合系数：a={a_fit:.6f}  b={b_fit:.6f}")
        if melt_filter:
            print(f"  生产频率过滤：{melt_filter}")

    return db


def split_glass_db(glass_db):
    """
    按 V_gen 中位数将玻璃分为正片候选池（低色散）和负片候选池（高色散）。
    中位数自动确定，比固定阈值更鲁棒。
    """
    vgens = [g['V_gen'] for k, g in glass_db.items()
             if k != '__fit__' and g['V_gen'] is not None]

    if len(vgens) == 0:
        pos_pool = [(k, g) for k, g in glass_db.items()
                    if k != '__fit__' and g['vd'] > 50]
        neg_pool = [(k, g) for k, g in glass_db.items()
                    if k != '__fit__' and g['vd'] < 50]
        return pos_pool, neg_pool

    median_v = np.median(vgens)
    pos_pool = [(k, g) for k, g in glass_db.items()
                if k != '__fit__' and g['V_gen'] is not None
                and g['V_gen'] > median_v]
    neg_pool = [(k, g) for k, g in glass_db.items()
                if k != '__fit__' and g['V_gen'] is not None
                and g['V_gen'] <= median_v]
    return pos_pool, neg_pool


# ════════════════════════════════════════════════════════════════════
#  内部辅助：从玻璃库补全缺失 nd 值
# ════════════════════════════════════════════════════════════════════
def _fill_nd_from_db(
    glass_names : list,
    nd_dict     : dict,
    glass_db    : dict | None,
) -> dict:
    """
    将 glass_names 中不在 nd_dict 内的玻璃从 glass_db 中补入。
    原地修改 nd_dict 并返回。

    [Fix-Bug5] 优先使用 n_ref（工作参考波长折射率），与搜索评分基准一致；
    若 glass_db 中无 n_ref 字段则回退到 nd，并打印警告提示用户核查。
    """
    for g in glass_names:
        if g not in nd_dict:
            if glass_db is not None and g in glass_db:
                entry   = glass_db[g]
                n_ref_v = entry.get('n_ref')
                n_val   = n_ref_v if n_ref_v is not None else entry.get('nd')
                if n_val is None:
                    raise KeyError(
                        f"玻璃 {g} 的 n_ref/nd 字段均为空，请手动填写到对应的 ALL_ND 子字典。"
                    )
                src = 'n_ref' if n_ref_v is not None else 'nd（⚠ 无 n_ref，建议核查）'
                nd_dict[g] = float(n_val)
                print(f"    {g}: n(λref)={nd_dict[g]:.5f}（从玻璃库读取，来源={src}）")
            else:
                raise KeyError(
                    f"玻璃 {g} 在 ALL_ND 和玻璃库中均未找到，"
                    f"请手动填写 n_ref 值到对应的 ALL_ND 子字典。"
                )
    return nd_dict
