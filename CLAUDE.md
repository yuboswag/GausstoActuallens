# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when operating in this directory.

**Always use Chinese for replies and code comments; keep variable names in English.**


Configuration in `utils/config.py`. Key run modes:
- `OPTIMIZE_ALL` — full pipeline (collect from Zemax + design)
- `OPTIMIZE_COLLECT` — collect only
- `VALIDATE` — validate an existing cam curve against Zemax

**Key files**:
- `core/` — Zemax connector, data collector, optimizer
- `gui/` — MainWindow tkinter interface
- `utils/` — config, file I/O, logging
- `plotting/` — visualization (cam curves, validation plots)

---

## Gaussianoptics

**Purpose**: Gaussian (paraxial) optics calculator for zoom lens layout — tracks EFL, principal planes, TTL, BFD across zoom positions. Used to generate input data (the `111.csv`) for Action_a and cam_optimizer.

```
python main.py          # GUI entry (tkinter-based)
```

Configuration in `config.py` (`ZoomConfig` dataclass). Core modules:
- `simulator.py` — paraxial ray tracing, zoom position calculation
- `optimizer.py` — optical parameter optimization
- `gui.py` — tkinter interface for configuration

---

## Common Zemax Setup

- **Version**: Ansys Zemax OpticStudio 2024 R1.00
- **Path**: `D:\Ansys Zemax OpticStudio 2024 R1.00`
- **Connection**: pythonnet + `ZOSAPI_NetHelper.dll`, **no COM/win32com**
- **Mode**: Interactive Extension (open Zemax → Programming → Interactive Extension)
- **Key pitfall**: `clr.AddReference()` runs once per module, use module-level guards

---

## Python Environment

All projects use standard Python 3 with numpy. No virtualenv management here — the user manages dependencies externally.

---

## ZOS-API Known Limitations (坑位列表)

- **EFL 直读**：ZOS-API 2024 R1 extension 模式下无 `Analyses.New_Cardinals()` 可靠接口。
  - `MFE.EFFL` 操作数在多配置系统下不生效（所有配置返回同一值）。
  - `SystemData.Aperture` 无 `EntrancePupilDiameter` 属性（extension 模式下属性不可访问）。
  - 当前方案：直接使用高斯解 EFL 作为参考值传入 `read_zoom_efl(reference_efls=[...])`。
  - 精确验证需手动在 Zemax 运行 `Analysis > Cardinal Points` 并查看报告。
- **MCE 变量设置**：`GetOperandCell().CreateSolveType(Variable)` 在 extension 模式下可能失败，需捕获异常并继续。
- **光线瞄准**：`SystemData.RayAiming.RayAiming = Paraxial` 必须在 LDE 面写入后、MCE 写入前设置。
- **分析文件输出**：`GetTextFile()` 在 extension 模式下不稳定，不应依赖。

---

## ZOS-API Cardinal Points Analysis（EFL 读取）

**正确调用路径**（ZOS-API 2024 R1 验证）：

```python
analysis_id = self._ZOSAPI.Analysis.AnalysisIDM.CardinalPoints
analysis = self._system.Analyses.New_Analysis(analysis_id)
analysis.ApplyAndWaitForCompletion()
results = analysis.GetResults()
results.GetTextFile(tmp_path)
```

**错误的调用**（会抛 AttributeError）：
- ❌ `self._system.Analyses.New_CardinalPoints()` — 此方法在 2024 R1 中不存在
- ❌ `analysis.ApplyAndClose()` — 不等完成就关闭，`GetTextFile` 无内容

**输出报告格式**：Windows 中文环境下输出为 **UTF-16-LE** 编码。
解析 EFL 时查找包含 `焦长` 或 `Effective Focal Length` 的行，
取冒号后最后一个数值并取绝对值（前组焦长为负）。

**文件生成的竞态问题**：
`GetTextFile(path)` 偶尔在 Windows 下异步写入失败，应使用
`tempfile.NamedTemporaryFile(delete=False)` 生成路径，并加入
最多 5 次的重试循环（间隔指数退避：100/200/400/800/1600ms）。

---

## MCE 多组态 EFL 读取

**已知坑**（extension 模式下）：
- `mfe.CalculateMeritFunction()` 会把 `MCE.CurrentConfiguration` 重置到 1，
  导致逐配置插 EFFL 操作数读 EFL 时返回的值全是 Config 1 的。
- 因此 EFL 读取**只能**用 Cardinal Points Analysis 路径，不能用 MFE EFFL 操作数路径。

**正确做法**（每个配置独立循环）：
```python
for cfg in range(1, n_configs + 1):
    mce.SetCurrentConfiguration(cfg)
    analysis = system.Analyses.New_Analysis(AnalysisIDM.CardinalPoints)
    analysis.ApplyAndWaitForCompletion()
    # 导出 → 解析 → Close
```

**验证**：
- `SetCurrentConfiguration(cfg)` 确实生效，可通过读取 `lde.GetSurfaceAt(7/14/19).Thickness`
  确认 5 个组态下 d1/d2/d3 值不同。
- 但 EFL 读取不能用 MFE EFFL 操作数路径（多配置会重置到 Config 1）。

---

## 正补偿型四组元变焦：d2 与 EFL 反相关

**光学事实**（本项目的系统类型）：
- **d2 减小 → EFL 增大**（Tele 端 d2 最小，EFL 最长）
- **d2 增大 → EFL 减小**（Wide 端 d2 最大，EFL 最短）

**迭代修正公式（正确方向）**：
```python
# errors[i] = (actual - target) / target
# 实际 < 目标 → errors < 0 → 需 EFL 增大 → d2 应减小 → correction < 0
correction = errors[i] * damping * d2[i]   # 注意：不带负号！
```

**历史教训**：如果写成 `-errors[i] * ...`（带负号），Config 4/5 会在几轮内
d2 爆炸到 400~1700 mm，最终把 .zmx 文件写坏。如果看到 d2 单调增大且 EFL 单调
减小，立刻停止迭代并检查符号。

---

## 已知设计限制（初始结构）

当前 `last_run_config.json` 提供的初始结构：
- Config 1 (Wide):    11.9 mm 目标 → Zemax 读出 11.9 mm（误差 ~0%）
- Config 2 (Med-W):   33.2 mm 目标 → Zemax 读出 33.1 mm（误差 ~0.4%）
- Config 3 (Medium):  62.3 mm 目标 → Zemax 读出 60.2 mm（误差 ~3%）
- Config 4 (Med-T):   98.3 mm 目标 → Zemax 读出 85.3 mm（误差 ~13%）
- Config 5 (Tele):   141.0 mm 目标 → Zemax 读出 97.4 mm（误差 ~31%，**d2 已触底 2.0mm**）

Config 4/5 无法通过单独调 d2 收敛到目标，因为镜片组 power 分配
（G1~62, G2~-14, G3~25, G4~66 mm）对长焦端 power 不足。
**解决方案**：后续在 Zemax 中手动优化（变量：曲率 + 间距），或回到
Gaussianoptics 重新分配 G1/G4 焦距。

---

## 可删除文件清单（临时/一次性脚本）

以下文件已完成使命，可以直接删除（或移至 `archive/` 后删除）：

**Python 测试/调试脚本**：
- `test_bfd_correction.py`
- `test_principal_planes.py`
- `test_run_optimization.py`
- `test_read_performance.py`
- `test_optimization_setup.py`
- `test_write_zoom.py`
- `test_load_zoom_configs.py`
- `debug_surfaces.py`

**Zemax 项目文件（.zmx）**：
- `test_optimization_setup.zmx`
- `test_singlet.zmx`
- `test_zoom_system.zmx`
- `test_zoom_performance.zmx`

**说明**：
- 以上文件为开发过程中的一次性诊断/测试脚本，功能已由 `test_zoom_lde.py` 和 `diag_minimal.py` 覆盖。
- 保留的**生产级别**诊断脚本：`diag_minimal.py`、`diagnose_efl_switching.py`（在 `archive/` 中）。
- `archive/diagnose_epd.py`、`archive/diagnose_cardinals.py`、`archive/diagnose_effl.py` 也保留，供后续参考。
