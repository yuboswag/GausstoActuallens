# CLAUDE.md

本文件为 Claude Code 在本目录工作时的约定。
**当前任务状态见 `PLAN.md`，开始工作前请先读一次。**

**硬性规则**：回复与代码注释一律中文；变量名保持英文。

---

## 项目定位

**Action_a**：四组元正补偿型变焦镜头初始结构自动化设计工具。
主流程：高斯解求 power 分配 → 玻璃搜索 → Seidel 像差 SLSQP 联合优化
→ 写入 Zemax 验证。

- 输入：`111.csv`（姊妹项目 Gaussianoptics 生成的变焦位置参数）
- 输出：`test_zoom_lde.zmx`（Zemax 多组态镜头文件）

---

## 环境约定

- **OS**：Windows 10 原生。**不要**给 WSL2 / Linux 方案。
- **Python**：系统 Python 3 + numpy，无 venv，依赖用户自管。
- **Zemax**：Ansys Zemax OpticStudio 2024 R1.00
  - 安装路径：`D:\Ansys Zemax OpticStudio 2024 R1.00`
  - 连接：pythonnet + `ZOSAPI_NetHelper.dll`，**禁用 COM / win32com**
  - 运行模式：**Interactive Extension**（Zemax → Programming → Interactive Extension）
  - `clr.AddReference()` 每模块仅能执行一次，需模块级 guard

---

## 模块导航（平铺结构）

| 类别 | 文件 | 主角 |
|------|------|------|
| 入口 / 调度 | `main.py`, `runner.py`, `action_gui.py` | `runner.py` 是主流程 |
| 配置 / 结构 | `config.py`, `structure.py` | `config.py` 是全局参数入口 |
| 光学核心 | `dispersion.py`, `glass_db.py`, `seidel_gemini.py`, `solver.py`, `zoom_utils.py` | `solver.py` 求 power 分配 |
| 搜索 / 优化 | `group_candidate.py`, `search.py`, `scoring.py`, `system_optimizer.py`, `sensitivity_scan.py` | `system_optimizer.py` 跑 SLSQP 联合优化 |
| Zemax 桥接 | `zemax_bridge.py`, `test_bridge.py`, `run_test_zoom_lde.py` | `zemax_bridge.py` 是桥接主体 |
| 验证 / 诊断 | `validation.py`, `diag_minimal.py`, `analyze_theoretical_efl.py`, `invert_d2.py`, `invert_all_gaps.py` | 按需读，无固定主角 |

需要函数级细节时直接读对应文件，**不要**在本文档里展开实现说明。

---

## 不要修改的目录

- `archive/` — 历史版本与弃用脚本归档，保留供参考
- `__pycache__/` — Python 缓存
- `.git/`、`.claude/` — 工具自身目录

---

## 关键技术约定

### Surface 索引 +1 偏移

代码内部索引 `i` 对应 Zemax LDE 里的 `i+1` 号面。全项目一致使用此约定，
读写 LDE / MCE 时留意换算。

### ZOS-API 新系统初始化

`New(False)` 创建的新系统默认带一个中间面，**插入镜片面前必须先删除**，
否则会多出空面导致索引错位。

### 光线瞄准写入顺序

`SystemData.RayAiming.RayAiming = Paraxial` 必须在 **LDE 面写入之后、
MCE 写入之前** 设置，否则不生效。

### EFL 读取：只用 Cardinal Points Analysis

**禁止**用 MFE `EFFL` 操作数读多组态 EFL —— `mfe.CalculateMeritFunction()`
会把 `MCE.CurrentConfiguration` 重置到 1，逐配置读出的全是 Config 1 的值。

**唯一可靠路径**：

```python
aid = self._ZOSAPI.Analysis.AnalysisIDM.CardinalPoints
analysis = self._system.Analyses.New_Analysis(aid)
analysis.ApplyAndWaitForCompletion()      # 不要用 ApplyAndClose()
analysis.GetResults().GetTextFile(tmp_path)
```

- 报告文件编码：**UTF-16-LE**（Windows 中文环境）
- 解析：查找含 `焦长` 或 `Effective Focal Length` 的行，取冒号后最后一个数值的绝对值（前组焦长为负）
- `GetTextFile` 偶尔异步写入失败，需重试（指数退避）
- **不存在**的方法：`New_CardinalPoints()`、`Analyses.New_Cardinals()` — 会抛 AttributeError

### 正补偿四组元：d2 与 EFL 反相关（符号方向）

- d2 减小 → EFL 增大（Tele 端 d2 最小）
- d2 增大 → EFL 减小（Wide 端 d2 最大）

d2 迭代修正（`errors[i] = (actual - target) / target`）：

```python
correction = errors[i] * damping * d2[i]   # 不带负号
```

若写成带负号，d2 会在数轮内爆炸到几百毫米并把 .zmx 写坏。
**自检信号**：看到 d2 单调增大且 EFL 单调减小，立即停止并查符号。

### Gaussianoptics → Zemax 间距转换（2026-04 更正）

**Gaussianoptics 直接输出物理顶点间距 d1/d2/d3**，可直接写入 Zemax LDE 厚度列。
不需要主面修正。

经实验证实（Zemax Cardinal Points 读 EFL）：
- 短焦/中短焦：raw d 写入后偏差 < 1%（11.89 vs 11.90, 33.0 vs 33.22）
- 中焦：偏差 5%
- 中长焦：偏差 18%
- 长焦：偏差 37%

长焦端偏差是薄组近似在 G2/G3 贴近时的固有局限（高阶耦合无法被薄组模型预测），
不是 pipeline bug，由 Zemax DLS 优化（EFFL 目标 + 间距变量）解决。

主面位置 δH/δHp 仍由 structure.py 计算，公式 (D-1)/C 和 (1-A)/C，
用于赛德尔分析、BFD 精修和诊断，**不再用于修正间距**。

历史教训：
- structure.py 原公式符号反了（(1-D)/C, (A-1)/C），已修复
- test_bridge.py 原本自己重算主面，绕过 structure.py 修复，已改为读 JSON
- zoom_utils.correct_zoom_spacings 已改为 NOOP（实验对比 raw vs 修正后，raw 全面更优）

### 其他 ZOS-API extension 模式坑

- `GetOperandCell().CreateSolveType(Variable)` 可能失败，需 try/except 继续
- `SystemData.Aperture` 无 `EntrancePupilDiameter` 属性，需绕行
- `GetTextFile` 整体不稳定，任何导出都不能假设一次成功

---

## 任务状态与临时记录

当前结构收敛情况、待删脚本清单、历史修复记录等统一见 `PLAN.md`。
本文件只保留长期生效的约定。
