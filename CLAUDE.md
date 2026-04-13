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
