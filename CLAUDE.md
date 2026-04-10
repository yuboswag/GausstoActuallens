# CLAUDE.md

本文件为 Claude Code 提供项目上下文。**请始终用中文回复和代码注释，变量名保持英文。**

## 项目概述

Action_a 是**正组补偿变焦镜头初始结构自动生成工具**，覆盖从 CDGM 玻璃选型、光焦度分配、Seidel 像差理论计算，到写入 Zemax OpticStudio 的完整流程。

四组变焦配置：

| 组别 | 角色 | 焦距 | 镜片数 |
|---|---|---|---|
| G1 | 前固定组 | +56.96 mm | 4 片 |
| G2 | 变倍组（移动，负） | −12.15 mm | 4 片 |
| G3 | 补偿组（移动，正） | +24.41 mm | 3 片 |
| G4 | 中继组（固定） | +72.55 mm | 4 片 |

## 运行方式

```bash
python main.py        # CLI，在第 905 行附近修改 RUN_MODE："search"/"structure"/"auto"/"seidel"
python action_gui.py  # CustomTkinter GUI
```

关键数据文件：
- `CDGM202509_with_Schott.xlsx` — 玻璃目录
- `111.csv` — 各变焦位置组间空气间隔（光线高度数据）
- `action_a_last_config.json` — GUI 配置自动存档

## 各源文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | 流水线总调度，`run_action_a_pipeline()` 入口 |
| `action_gui.py` | CustomTkinter GUI，stdout 重定向到日志面板 |
| `config.py` | 默认参数、约束解析助手 |
| `glass_db.py` | CDGM 玻璃目录加载、色散参数计算 |
| `dispersion.py` | Sellmeier/Schott 方程、广义 Abbe 数 |
| `solver.py` | 线性约束求解器（Cramer 法则） |
| `scoring.py` | 过滤（`is_valid`）、评分（`optical_score`）、多进程 worker |
| `search.py` | 暴力玻璃枚举，`ProcessPoolExecutor` |
| `structure.py` | Seidel R/t/q 计算 + SLSQP 曲率优化 |
| `validation.py` | V1–V4 前 Zemax 验证，`build_seq_with_dispersion()` |
| `seidel_gemini.py` | 逐面 Seidel 系数分析（SI–SV、CI、CII） |
| `runner.py` | 系统序列组装、光阑面计算、系统级 Seidel |
| `group_candidate.py` | 5D Seidel 向量最大最小距离多样性采样 |
| `system_optimizer.py` | 系统级联合优化，Nelder-Mead 形状因子精修 |
| `zemax_bridge.py` | ZOS-API .NET 桥接（pythonnet）—— 核心接口文件 |
| `zoom_utils.py` | 变焦轨迹工具，CSV 光线数据解析 |

## Zemax 集成

### 环境
- **版本**：Ansys Zemax OpticStudio 2024 R1.00
- **安装路径**：`D:\Ansys Zemax OpticStudio 2024 R1.00`
- **连接方式**：pythonnet (`clr`) + `ZOSAPI_NetHelper.dll`，**禁止使用 COM / win32com**
- **连接模式**：日常用 extension 模式（手动打开 Zemax → Programming → Interactive Extension）

### 闭环反馈链路

```
Action_a (Seidel 初始解)
    ↓ zemax_bridge.py 写入 LDE + MCE
    ↓ Zemax 真实光线追迹 → 读取 RMS Spot / EFL / Seidel
    ↓ 与目标对比 → 计算残差
    ↓ 调整参数 → 更新系统 → 迭代
```

### 光学规格（当前测试用例）

```
广角焦距: 12.0 mm    长焦焦距: 142 mm
TTL: 105 mm          BFD: 8 mm
像面半对角线: 3.8 mm
广角 F/4.0           长焦 F/5.6（非恒定光圈）
```

26 个光学面（Action_a 序号 0~25），变焦间隔在面 6/13/18，光阑在面 14。
5 个变焦配置（Wide / MW / Med / MT / Tele），d1/d2/d3 由 MCE 控制。

### 已知坑——修改 zemax_bridge.py 前必读

**连接层**
- `clr.AddReference()` 只能执行一次，需要模块级 guard
- extension 模式下 `IsValidLicenseForAPI` 可能返回 False，是误报，不阻断

**LDE 写入**
- 面序号偏移 **+1**：Action_a 面 N → Zemax Surface (N+1)，因为 Surface 0 = OBJ
- `TheSystem.New(False)` 后有 3 面（OBJ + 默认空面 + IMA），需先 `RemoveSurfaceAt(1)` 删默认面
- `TheLDE = TheSystem.LDE` 必须在 `TheSystem.New(False)` **之后**获取，否则引用失效

**视场 / 波长**
- 新系统默认已有 Field 1（0,0），`AddField` 是追加而非替换
- 把 d 线放波长位置 1，避免 `PrimaryWavelength` 属性不可靠

**MFE 操作数**
- 枚举路径：`self._ZOSAPI.Editors.MFE.MeritOperandType.EFFL`（pythonnet 路径，非 COM constants）
- `DIST` 操作数 `Surf=0` 返回真实光线畸变，不是 Seidel 贡献之和，合计需 Python 端求和
- Seidel 枚举名：`SPHA`（非 SSPH）、`COMA`、`ASTI`、`FCUR`、`DIST`
- EFL 用 `EFFL`，RMS 弥散斑用 `RSCE`

**MCE 写入**
- THIC 面序号用 Zemax 面号（已含 +1 偏移）：Surface 7 / 14 / 19
- 配置编号从 1 开始：`TheMCE.SetCurrentConfiguration(1)`
- 操作数值读写：`op.GetOperandCell(config_index).DoubleValue`

**光圈**
- 系统孔径类型用 `Image Space F/#`，MCE 的 APER 操作数存 F/# 值（不是 EPD）
- 各配置 F/# = EFL / EPD：Wide 3.995 / MW 4.258 / Med 4.618 / MT 5.067 / Tele 5.601

## 主面间距修正——集成方案

### 调用流程

在 runner.py 或 action_gui.py 的 auto 模式中，当4个组的 compute_initial_structure 全部完成后：

1. 从各组结果中提取主面数据：
```python
   group_pp = [
       (g1_result['delta_H'], g1_result['delta_Hp']),
       (g2_result['delta_H'], g2_result['delta_Hp']),
       (g3_result['delta_H'], g3_result['delta_Hp']),
       (g4_result['delta_H'], g4_result['delta_Hp']),
   ]
```

2. 加载原始变焦配置后立即修正：
```python
   from zoom_utils import load_zoom_configs_for_zemax, correct_zoom_spacings
   
   raw_configs = load_zoom_configs_for_zemax(csv_path, bfd_mm, fnum_wide, fnum_tele)
   corrected_configs = correct_zoom_spacings(raw_configs, group_pp)
```

3. 将 `corrected_configs` 传给 `write_zoom_system`，替代原来的 `raw_configs`。

### 注意事项
- 修正量通常在 1~5mm 量级，不可忽略
- 如果某个间距修正后 < 0.3mm，说明高斯解本身不够合理，需回退调整组焦距
- BFD 也需要类似修正：`bfd_physical = bfd_gaussian + delta_Hp_G4`（G4 后主面偏移）