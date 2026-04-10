"""
test_principal_planes.py
验证主面计算函数的正确性：用一个已知参数的单片透镜做校验。

已知：单片透镜 R1=50mm, R2=-50mm (对称双凸), 中心厚度 5mm, nd=1.5
理论值：
  phi_surf1 = (1.5-1)/50 = 0.01
  phi_surf2 = (1-1.5)/(-50) = 0.01
  系统焦距 ≈ 1/(phi1 + phi2 - t/n * phi1*phi2) = 1/(0.02 - 0.000333)
           ≈ 50.85 mm
  对称双凸透镜 delta_H = -delta_Hp（对称性）
"""

from structure import compute_principal_planes

# 构造单片透镜的 surfaces_data 格式
surfaces = [
    ('片1(TEST) 前表面', 50.0, 0, False),
    ('片1(TEST) 后表面', -50.0, 0, False),
]
thickness = {0: (5.0, '测试')}
nd_values = {'TEST': 1.5}
glass_names = ['TEST']
spacings_mm = []
cemented_pairs = []

print("=== 单片对称双凸透镜主面测试 ===")
dH, dHp = compute_principal_planes(
    surfaces, thickness, nd_values, glass_names,
    spacings_mm, cemented_pairs
)

# 理论校验
phi1 = (1.5 - 1.0) / 50.0
phi2 = (1.0 - 1.5) / (-50.0)
t = 5.0
n = 1.5
phi_sys = phi1 + phi2 - (t/n) * phi1 * phi2
efl_theory = 1.0 / phi_sys

print(f"\n理论 EFL = {efl_theory:.4f} mm")
print(f"对称性检验: delta_H + delta_Hp 应≈0: {dH + dHp:.6f}")

# delta_H 理论值
A = 1 - (t/n) * phi2
D = 1 - (t/n) * phi1
C_neg = phi_sys  # C = -phi_sys in our matrix, but M[1][0] = -phi_sys
# delta_H = (1-D)/C where C = M[1][0]
# M[1][0] = -(phi1 + phi2 - t/n*phi1*phi2) = -phi_sys
C_val = -phi_sys
dH_theory = (1 - D) / C_val
dHp_theory = (A - 1) / C_val

print(f"理论 delta_H  = {dH_theory:+.4f} mm, 计算值 = {dH:+.4f} mm, 差 = {abs(dH-dH_theory):.6f}")
print(f"理论 delta_Hp = {dHp_theory:+.4f} mm, 计算值 = {dHp:+.4f} mm, 差 = {abs(dHp-dHp_theory):.6f}")

tol = 1e-6
assert abs(dH - dH_theory) < tol, f"delta_H 误差过大: {abs(dH-dH_theory)}"
assert abs(dHp - dHp_theory) < tol, f"delta_Hp 误差过大: {abs(dHp-dHp_theory)}"
print("\nAll tests passed")
