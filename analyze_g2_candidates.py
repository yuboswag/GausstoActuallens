# -*- coding: utf-8 -*-
"""
G2 候选分析脚本 (v2 - ABCD 算法跟 structure.py 自洽)

目的: 从 5c_debug.log 的 [5c.5-after] 行解析每个候选的 R 值,
     用 ABCD 矩阵算厚透镜 EFL + 主面位移 ΔH/ΔH',
     按组分类输出表格, 验证'换更小 k 候选能否解决 Tele d2_physical 装配问题'。

约定: 角度 ABCD + reduced distance (与 structure.py _efl_with_scale 一致)
  R = [[1, 0], [-φ, 1]],  φ = (n_after - n_before) * c
  T = [[1, t/n], [0, 1]]
  f_thick = -1/C, ΔH = (D-1)/C, ΔH' = (1-A)/C

输入:
  - 5c_after_unique.txt
  - last_run_config.json

输出: 控制台表格
"""
from __future__ import annotations
import re
import json
import ast
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np

ROOT = Path(r"D:\myprojects\gauss_to_lens")
LOG_FILE = ROOT / "5c_after_unique.txt"
JSON_FILE = ROOT / "last_run_config.json"


# ── 1. 解析 5c.5-after 行 ───────────────────────────────────────
PAT = re.compile(
    r"\[5c\.5-after\]\s+k_applied=(?P<k>[\d.]+)\s+"
    r"cem_R=(?P<cem>\{[^}]*\})\s+"
    r"fin_R=(?P<fin>\{[^}]*\})"
)


def _strip_np(s: str) -> str:
    return re.sub(r"np\.float64\(([^)]+)\)", r"\1", s)


def parse_log() -> List[dict]:
    rows = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            m = PAT.search(line)
            if not m:
                continue
            k = float(m.group("k"))
            try:
                cem = ast.literal_eval(_strip_np(m.group("cem")))
                fin = ast.literal_eval(_strip_np(m.group("fin")))
            except Exception as e:
                print(f"  跳过解析失败行 {ln}: {e}")
                continue
            rows.append({"k": k, "cem": cem, "fin": fin, "line": ln})
    return rows


# ── 2. 按 cem/fin 索引模式判 group ──────────────────────────────
def classify(row: dict) -> str:
    """
    G1: cem=(1,2), fin idx={0,3}
    G2: cem=(2,3), fin idx={0,1}
    G3: cem=(1,2), fin idx={0}
    G4: cem=(0,1), fin idx={2,3}
    """
    cem_keys = list(row["cem"].keys())
    fin_keys = sorted(row["fin"].keys())
    if not cem_keys:
        return "?"
    cem = cem_keys[0]
    if cem == (1, 2) and fin_keys == [0, 3]:
        return "G1"
    if cem == (2, 3) and fin_keys == [0, 1]:
        return "G2"
    if cem == (1, 2) and fin_keys == [0]:
        return "G3"
    if cem == (0, 1) and fin_keys == [2, 3]:
        return "G4"
    return f"?cem={cem},fin={fin_keys}"


# ── 3. 从 JSON 读 prescription ──────────────────────────────────
def load_json() -> dict:
    with open(JSON_FILE, encoding="utf-8") as f:
        return json.load(f)


def extract_group_struct(json_data: dict, group: str) -> dict:
    pp = json_data["principal_plane_correction"]
    surfaces = next(g["surfaces"] for g in pp["surface_prescriptions"] if g["group"] == group)
    pp_entry = next(g for g in pp["group_principal_planes"] if g["group"] == group)
    return {
        "surfaces": surfaces,
        "current_dH": pp_entry["delta_H"],
        "current_dHp": pp_entry["delta_Hp"],
    }


# ── 4. ABCD trace (structure.py 同款) ───────────────────────────
def trace_group(R_seq: List[float], n_seq: List[float], t_seq: List[float]) -> Tuple[float, float, float]:
    """
    完全照搬 structure.py _efl_with_scale.
    R_seq: K 个折射面 R
    n_seq: 长度 K+1, n_seq[0]=1.0 (前空气), n_seq[i+1] = 第 i 个表面之后的介质 nd
    t_seq: 长度 K, t_seq[i] = 第 i 个表面后的介质中物理厚度. 最后一个 = 0
    """
    K = len(R_seq)
    assert len(n_seq) == K + 1, f"n_seq 长度 {len(n_seq)} 应为 {K+1}"
    assert len(t_seq) == K, f"t_seq 长度 {len(t_seq)} 应为 {K}"

    M = np.eye(2)
    n_prev = n_seq[0]
    for idx in range(K):
        c = 1.0 / R_seq[idx] if abs(R_seq[idx]) > 1e-12 else 0.0
        n_after = n_seq[idx + 1]
        phi = (n_after - n_prev) * c
        R_mat = np.array([[1.0, 0.0], [-phi, 1.0]])
        M = R_mat @ M
        if idx < K - 1:
            T_mat = np.array([[1.0, t_seq[idx] / n_after], [0.0, 1.0]])
            M = T_mat @ M
        n_prev = n_after

    A, _B, C, D = M[0, 0], M[0, 1], M[1, 0], M[1, 1]
    if abs(C) < 1e-15:
        return float("inf"), 0.0, 0.0
    return -1.0 / C, (D - 1.0) / C, (1.0 - A) / C


# ── 5. 各组结构 builder ─────────────────────────────────────────
def build_g1_trace(cem_R, fin_R, struct):
    """G1: [片1][空气][片2 胶合 片3][空气][片4]"""
    s = struct["surfaces"]
    n1, t1   = s[0]["nd"], s[0]["t"]
    air1     = s[1]["t"]
    n2, t2   = s[2]["nd"], s[2]["t"]
    n3, t3   = s[3]["nd"], s[3]["t"]
    air2     = s[4]["t"]
    n4, t4   = s[5]["nd"], s[5]["t"]
    R0a, R0b = fin_R[0]
    Rca, Rcm, Rcb = cem_R
    R3a, R3b = fin_R[3]
    R_seq = [R0a, R0b, Rca, Rcm, Rcb, R3a, R3b]
    n_seq = [1.0, n1, 1.0, n2, n3, 1.0, n4, 1.0]
    t_seq = [t1, air1, t2, t3, air2, t4, 0.0]
    return R_seq, n_seq, t_seq


def build_g2_trace(cem_R, fin_R, struct):
    """G2: [片1][空气][片2][空气][片3 胶合 片4]"""
    s = struct["surfaces"]
    n1, t1   = s[0]["nd"], s[0]["t"]
    air1     = s[1]["t"]
    n2, t2   = s[2]["nd"], s[2]["t"]
    air2     = s[3]["t"]
    n3, t3   = s[4]["nd"], s[4]["t"]
    n4, t4   = s[5]["nd"], s[5]["t"]
    R0a, R0b = fin_R[0]
    R1a, R1b = fin_R[1]
    Rca, Rcm, Rcb = cem_R
    R_seq = [R0a, R0b, R1a, R1b, Rca, Rcm, Rcb]
    n_seq = [1.0, n1, 1.0, n2, 1.0, n3, n4, 1.0]
    t_seq = [t1, air1, t2, air2, t3, t4, 0.0]
    return R_seq, n_seq, t_seq


def build_g3_trace(cem_R, fin_R, struct):
    """G3: [片1][空气][片2 胶合 片3]"""
    s = struct["surfaces"]
    n1, t1 = s[0]["nd"], s[0]["t"]
    air1   = s[1]["t"]
    n2, t2 = s[2]["nd"], s[2]["t"]
    n3, t3 = s[3]["nd"], s[3]["t"]
    R0a, R0b = fin_R[0]
    Rca, Rcm, Rcb = cem_R
    R_seq = [R0a, R0b, Rca, Rcm, Rcb]
    n_seq = [1.0, n1, 1.0, n2, n3, 1.0]
    t_seq = [t1, air1, t2, t3, 0.0]
    return R_seq, n_seq, t_seq


def build_g4_trace(cem_R, fin_R, struct):
    """G4: [片1 胶合 片2][空气][片3][空气][片4]"""
    s = struct["surfaces"]
    n1, t1 = s[0]["nd"], s[0]["t"]
    n2, t2 = s[1]["nd"], s[1]["t"]
    air1   = s[2]["t"]
    n3, t3 = s[3]["nd"], s[3]["t"]
    air2   = s[4]["t"]
    n4, t4 = s[5]["nd"], s[5]["t"]
    Rca, Rcm, Rcb = cem_R
    R2a, R2b = fin_R[2]
    R3a, R3b = fin_R[3]
    R_seq = [Rca, Rcm, Rcb, R2a, R2b, R3a, R3b]
    n_seq = [1.0, n1, n2, 1.0, n3, 1.0, n4, 1.0]
    t_seq = [t1, t2, air1, t3, air2, t4, 0.0]
    return R_seq, n_seq, t_seq


BUILDERS = {"G1": build_g1_trace, "G2": build_g2_trace,
            "G3": build_g3_trace, "G4": build_g4_trace}


# ── 6. JSON 当前 R 还原 (用于 sanity check) ──────────────────────
def build_from_json_current(group: str, struct: dict):
    s = struct["surfaces"]
    if group == "G2":
        fin_R = {0: (s[0]["R"], s[1]["R"]), 1: (s[2]["R"], s[3]["R"])}
        cem_R = (s[4]["R"], s[5]["R"], s[6]["R"])
    elif group == "G1":
        fin_R = {0: (s[0]["R"], s[1]["R"]), 3: (s[5]["R"], s[6]["R"])}
        cem_R = (s[2]["R"], s[3]["R"], s[4]["R"])
    elif group == "G3":
        fin_R = {0: (s[0]["R"], s[1]["R"])}
        cem_R = (s[2]["R"], s[3]["R"], s[4]["R"])
    elif group == "G4":
        fin_R = {2: (s[3]["R"], s[4]["R"]), 3: (s[5]["R"], s[6]["R"])}
        cem_R = (s[0]["R"], s[1]["R"], s[2]["R"])
    else:
        raise ValueError(group)
    return cem_R, fin_R


# ── 7. 主流程 ───────────────────────────────────────────────────
def main():
    print("=" * 92)
    print("G2 候选分析 v2: ABCD 算法跟 structure.py 自洽")
    print("=" * 92)

    rows = parse_log()
    print(f"\n解析 {len(rows)} 条 [5c.5-after] 记录")

    by_group: Dict[str, List[dict]] = {"G1": [], "G2": [], "G3": [], "G4": [], "?": []}
    for r in rows:
        g = classify(r)
        if g.startswith("?"):
            by_group["?"].append(r)
        else:
            by_group[g].append(r)
    for g in ("G1", "G2", "G3", "G4"):
        print(f"  {g}: {len(by_group[g])} 候选")
    if by_group["?"]:
        print(f"  ?: {len(by_group['?'])} 条")

    json_data = load_json()
    zoom_cfgs = json_data["principal_plane_correction"]["raw_zoom_configs"]
    pp_list = json_data["principal_plane_correction"]["group_principal_planes"]
    json_dH  = {g["group"]: g["delta_H"]  for g in pp_list}
    json_dHp = {g["group"]: g["delta_Hp"] for g in pp_list}

    print(f"\n5 个 zoom 配置 (CSV 空气间距):")
    for cfg in zoom_cfgs:
        print(f"  {cfg['name']:>22}: d1={cfg['d1']:>7.3f}  d2={cfg['d2']:>7.3f}  d3={cfg['d3']:>7.3f}")

    group_results: Dict[str, List[dict]] = {}
    for group in ("G1", "G2", "G3", "G4"):
        struct = extract_group_struct(json_data, group)
        builder = BUILDERS[group]

        # sanity check: JSON 当前 R 用同款 trace
        cur_cem, cur_fin = build_from_json_current(group, struct)
        R0, n0, t0 = builder(cur_cem, cur_fin, struct)
        f0, dH0, dHp0 = trace_group(R0, n0, t0)

        print(f"\n{'=' * 92}")
        print(f"=== {group} 候选分析 ===")
        print(f"{'=' * 92}")
        print(f"[sanity] JSON 当前 R 用同款 trace:")
        print(f"  f_thick = {f0:+.4f}    ΔH = {dH0:+.4f}    ΔH' = {dHp0:+.4f}")
        print(f"  JSON 备份:                ΔH = {json_dH[group]:+.4f}    ΔH' = {json_dHp[group]:+.4f}")
        ok = abs(dH0 - json_dH[group]) < 0.05 and abs(dHp0 - json_dHp[group]) < 0.05
        print(f"  匹配: {'✓' if ok else '✗ 模型可能有误'}")

        # 候选去重
        candidates = by_group[group]
        seen: Dict[float, dict] = {}
        for r in candidates:
            kk = round(r["k"], 4)
            if kk not in seen:
                seen[kk] = r
        uniq = sorted(seen.values(), key=lambda r: r["k"])

        print(f"\n候选表 ({len(uniq)} 条独立候选):")
        print(f"  {'k':>7} | {'f_thick':>9} | {'ΔH':>8} | {'ΔH′':>8} | mark")
        print(f"  {'-' * 7}-+-{'-' * 9}-+-{'-' * 8}-+-{'-' * 8}-+-{'-' * 11}")
        results = []
        for r in uniq:
            cem_R = r["cem"][list(r["cem"].keys())[0]]
            fin_R = r["fin"]
            try:
                R_seq, n_seq, t_seq = builder(cem_R, fin_R, struct)
                f_thick, dH, dHp = trace_group(R_seq, n_seq, t_seq)
            except Exception as e:
                print(f"  k={r['k']:.4f}  追迹失败: {e}")
                continue
            is_current = abs(dHp - json_dHp[group]) < 0.05 and abs(dH - json_dH[group]) < 0.05
            mark = "← CURRENT" if is_current else ""
            print(f"  {r['k']:>7.4f} | {f_thick:>+9.3f} | {dH:>+8.3f} | {dHp:>+8.3f} | {mark}")
            results.append({"k": r["k"], "f_thick": f_thick, "dH": dH, "dHp": dHp, "is_current": is_current})
        group_results[group] = results

    # ── 关键: G2 候选物理可装配性分析 ────────────────────────
    print(f"\n{'=' * 92}")
    print("=== 物理可装配性分析: 假设 G1/G3/G4 用当前 JSON 选定, 只换 G2 候选 ===")
    print(f"{'=' * 92}")

    g2_results = group_results["G2"]
    dHp_g1 = json_dHp["G1"]
    dH_g3  = json_dH["G3"]
    dHp_g3 = json_dHp["G3"]
    dH_g4  = json_dH["G4"]

    print(f"\n固定主面值 (G1/G3/G4 当前):")
    print(f"  G1 ΔH'={dHp_g1:+.3f}  G3 ΔH={dH_g3:+.3f}  G3 ΔH'={dHp_g3:+.3f}  G4 ΔH={dH_g4:+.3f}")
    print(f"\n公式: d_phys = d_csv + ΔH'_left - ΔH_right")
    print(f"      d1 ~ G1-G2,   d2 ~ G2-G3,   d3 ~ G3-G4")
    print(f"      ! 标记表示 d_phys < 2mm (装配下限)\n")

    THRESH = 2.0
    print(f"  按 G2 候选列出每个 zoom 配置下的 (d1, d2, d3)_物理:")
    for res in g2_results:
        dH_g2  = res["dH"]
        dHp_g2 = res["dHp"]
        flag = "← CURRENT" if res["is_current"] else ""
        print(f"\n  G2: k={res['k']:.4f}  ΔH={dH_g2:+.3f}  ΔH'={dHp_g2:+.3f}  {flag}")
        print(f"    {'config':>22} | {'d1_csv':>7} {'d1_phy':>7} | {'d2_csv':>7} {'d2_phy':>7} | {'d3_csv':>7} {'d3_phy':>7} | ok?")
        all_ok = True
        for cfg in zoom_cfgs:
            d1p = cfg["d1"] + dHp_g1 - dH_g2
            d2p = cfg["d2"] + dHp_g2 - dH_g3
            d3p = cfg["d3"] + dHp_g3 - dH_g4
            ok_str = ""
            for d in (d1p, d2p, d3p):
                if d < THRESH:
                    ok_str += "!"
                    all_ok = False
            if not ok_str:
                ok_str = "✓"
            print(f"    {cfg['name']:>22} | {cfg['d1']:>7.2f} {d1p:>+7.2f} | "
                  f"{cfg['d2']:>7.2f} {d2p:>+7.2f} | {cfg['d3']:>7.2f} {d3p:>+7.2f} | {ok_str}")

    print(f"\n{'=' * 92}")
    print("结论: 关键看 Tele 行 d2_phy. 哪个候选能让 Tele d2_phy ≥ 2mm?")
    print(f"{'=' * 92}")


if __name__ == "__main__":
    main()