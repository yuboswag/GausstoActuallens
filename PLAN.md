# PLAN.md

本文件记录 Action_a 的**当前任务状态**，与 `CLAUDE.md`（长期约定）分工。
完成的任务移到"已完成"或直接删除；本文件应短、应动、应新。

---

## 当前设计瓶颈

基于 `last_run_config.json` 初始结构的 Zemax 验证结果：

| 组态 | 目标 EFL (mm) | Zemax 实测 (mm) | 误差 | 备注 |
|------|---------------|------------------|------|------|
| Config 1 (Wide)   | 11.9  | 11.9 | ~0%   | |
| Config 2 (Med-W)  | 33.2  | 33.1 | ~0.4% | |
| Config 3 (Medium) | 62.3  | 60.2 | ~3%   | |
| Config 4 (Med-T)  | 98.3  | 85.3 | ~13%  | |
| Config 5 (Tele)   | 141.0 | 97.4 | ~31%  | **d2 已触底 2.0 mm** |

**问题**：Config 4/5 长焦端无法通过单独调整 d2 收敛。
**原因**：当前组 power 分配（G1~62, G2~-14, G3~25, G4~66 mm）对长焦端 power 不足。
**下一步方向**（二选一或组合）：
1. Zemax 中手动优化，变量 = 曲率 + 间距
2. 回到 Gaussianoptics 重新分配 G1 / G4 焦距

---

## 进行中 / 待办

<!-- 在这里写当前正在做的事。举例：
- [ ] MCE 写入连接验证（LDE 已 PASS）
- [ ] 完成 config.py 从 main.py 剥离的后续清理
-->

---

## 清理残留

- `archive/debug_surfaces.py` — 功能已被 `diag_minimal.py` 覆盖，确认后可删
- （其余历史 `test_*.py` / `test_*.zmx` 开发脚本已在之前清理中全部删除）

---

## 历史教训（不要重蹈）

- **d2 修正符号**：历史上写成 `-errors[i] * damping * d2[i]`（带负号），
  Config 4/5 在几轮内 d2 爆炸到 **400~1700 mm**，把 .zmx 写坏。
  正确符号规则见 `CLAUDE.md` → "d2 与 EFL 反相关"。

---

## 已完成

<!-- 阶段性里程碑归档，避免与进行中项混淆 -->
- LDE 26 面写入全部 PASS
- `config.py` 已从 `main.py` 剥离
- 早期 `test_*.py` / `test_*.zmx` 开发脚本批量清理
