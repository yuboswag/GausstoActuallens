"""
dispersion.py
色散公式：Sellmeier 方程、Schott 色散公式及广义色散参数计算。
"""

import numpy as np

# ============================================================
# 第一部分：Sellmeier 方程 & 广义色散参数计算
# ============================================================

def sellmeier_n(lam_um, K1, L1, K2, L2, K3, L3):
    """
    用 Sellmeier 方程计算波长 lam_um（单位：微米）处的折射率。
    计算失败（极点附近或 n²≤0）时返回 np.nan。
    """
    lam2 = lam_um ** 2

    # [FIX-1] lam² 极度接近共振极点 Li 时分母趋近零，会产生 inf 而非 nan，
    # 绕过后续的 n2<=0 检查。提前拦截，直接返回 nan。
    for Li in (L1, L2, L3):
        if abs(lam2 - Li) < 1e-15:
            return np.nan

    n2 = 1.0 + (K1 * lam2 / (lam2 - L1)
              + K2 * lam2 / (lam2 - L2)
              + K3 * lam2 / (lam2 - L3))
    if n2 <= 0:
        return np.nan
    return np.sqrt(n2)


def schott_n(lam_um, A0, A1, A2, A3, A4, A5):
    """
    用 Schott 色散公式计算波长 lam_um（单位：微米）处的折射率。
    公式：n² = A0 + A1·λ² + A2·λ⁻² + A3·λ⁻⁴ + A4·λ⁻⁶ + A5·λ⁻⁸
    计算失败（n²≤0）时返回 np.nan。
    """
    l2 = lam_um ** 2
    n2 = (A0
          + A1 * l2
          + A2 / l2
          + A3 / l2 ** 2
          + A4 / l2 ** 3
          + A5 / l2 ** 4)
    if n2 <= 0:
        return np.nan
    return np.sqrt(n2)


def compute_generalized_params(sellmeier_coeffs, lam_short_nm, lam_ref_nm, lam_long_nm):
    """
    计算单种玻璃在指定波段的广义阿贝数和广义部分色散。
    返回 (n_ref, V_gen, P_gen)，失败时返回 (None, None, None)。
    """
    K1, L1, K2, L2, K3, L3 = sellmeier_coeffs
    n_s = sellmeier_n(lam_short_nm / 1000.0, K1, L1, K2, L2, K3, L3)
    n_r = sellmeier_n(lam_ref_nm  / 1000.0, K1, L1, K2, L2, K3, L3)
    n_l = sellmeier_n(lam_long_nm / 1000.0, K1, L1, K2, L2, K3, L3)

    if any(np.isnan(x) for x in [n_s, n_r, n_l]):
        return None, None, None

    denom = n_s - n_l
    if abs(denom) < 1e-10:
        return None, None, None

    return n_r, (n_r - 1.0) / denom, (n_s - n_r) / denom


def fit_PV_line(glass_db):
    """
    对玻璃库拟合 P_gen = a + b·(1/V_gen)，返回 (a, b)。
    数据不足时回退到可见光标准值 (0.6438, 0.001682)。
    """
    Vlist, Plist = [], []
    for name, g in glass_db.items():
        V = g.get('V_gen')
        P = g.get('P_gen')
        if V is None or P is None or abs(V) < 1e-6:
            continue
        Vlist.append(1.0 / V)
        Plist.append(P)

    if len(Vlist) < 3:
        return 0.6438, 0.001682

    X = np.column_stack([np.ones(len(Vlist)), Vlist])
    result = np.linalg.lstsq(X, np.array(Plist), rcond=None)
    return float(result[0][0]), float(result[0][1])


