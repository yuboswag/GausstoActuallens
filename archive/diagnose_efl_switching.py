"""
diagnose_efl_switching.py — Zemax MCE 组态切换诊断脚本

本脚本不修改任何现有代码，只读取 Zemax 状态，用于查明：
为何 _read_efl_via_cardinal() 对所有 MCE 组态返回相同的 EFL 值。

环境要求：
  - Ansys Zemax OpticStudio 2024 R1.00 已启动
  - 已打开 Programming → Interactive Extension
  - test_zoom_lde_corrected.zmx 已加载（或由脚本加载）

使用：
  python diagnose_efl_switching.py

输出：
  - 控制台打印所有诊断结果
  - 同时写入 _diag_efl_switching.log
  - 保留多个诊断 txt 文件供人眼对比
"""

import os
import tempfile
from zemax_bridge import ZemaxBridge

# ---------------------------------------------------------------------------
# 日志：同时输出到控制台和文件
# ---------------------------------------------------------------------------
LOG_PATH = r'D:\myprojects\Action_a\_diag_efl_switching.log'

def log(msg: str = ''):
    """打印并写入日志文件"""
    print(msg, flush=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')


def banner(title: str):
    line = '=' * 60
    log(f"\n{line}")
    log(f"  {title}")
    log(f"{line}")


# ===========================================================================
# 主程序
# ===========================================================================
def main():
    banner("诊断开始：连接 Zemax")

    # 清理旧日志
    try:
        os.remove(LOG_PATH)
    except FileNotFoundError:
        pass
    log(f"[日志文件] {LOG_PATH}")
    log("[说明] 本脚本不修改任何现有代码，只读取 Zemax 状态")

    bridge = ZemaxBridge()
    try:
        # ---------------------------------------------------------------
        # 准备：extension 模式连接 + 加载文件
        # ---------------------------------------------------------------
        bridge.connect(mode='extension')
        log("[连接成功] extension 模式")

        # 加载 .zmx 文件
        zmx_path = r'D:\myprojects\Action_a\test_zoom_lde_corrected.zmx'
        log(f"[加载文件] {zmx_path}")
        bridge._system.LoadFile(zmx_path, False)

        # 确认 MCE 配置数
        mce = bridge._system.MCE
        n_configs = mce.NumberOfConfigurations
        log(f"[MCE 配置数] {n_configs}（期望 5）")

        if n_configs != 5:
            log(f"[警告] 实际配置数为 {n_configs}，脚本预期为 5，继续执行...")

        # 常用对象
        mfe = bridge._system.MFE
        lde = bridge._system.LDE

        # ---------------------------------------------------------------
        # 诊断 A：SetCurrentConfiguration 是否生效
        # ---------------------------------------------------------------
        banner("诊断 A：SetCurrentConfiguration 是否生效")

        # results_a: list of (cfg, current_cfg_after_set, t7, t14, t19)
        results_a = []
        for cfg in range(1, n_configs + 1):
            mce.SetCurrentConfiguration(cfg)
            actual_cfg = mce.CurrentConfiguration

            # 读取 MCE 控制的三个变焦间隔面（Zemax Surface 编号）
            # Action_a 面 6 → Zemax Surface 7    (d1)
            # Action_a 面 13 → Zemax Surface 14  (d2)
            # Action_a 面 18 → Zemax Surface 19  (d3)
            t7  = lde.GetSurfaceAt(7).Thickness
            t14 = lde.GetSurfaceAt(14).Thickness
            t19 = lde.GetSurfaceAt(19).Thickness

            results_a.append((cfg, actual_cfg, t7, t14, t19))
            log(f"  cfg={cfg} → CurrentConfiguration={actual_cfg}, "
                f"Thick[S7]={t7:.4f}, Thick[S14]={t14:.4f}, Thick[S19]={t19:.4f}")

        # 判断
        d1_vals = [r[2] for r in results_a]
        d2_vals = [r[3] for r in results_a]
        d3_vals = [r[4] for r in results_a]

        d1_unique = len(set(round(v, 6) for v in d1_vals))
        d2_unique = len(set(round(v, 6) for v in d2_vals))
        d3_unique = len(set(round(v, 6) for v in d3_vals))

        all_cfg_match = all(r[1] == r[0] for r in results_a)  # (cfg, actual_cfg, ...)

        log(f"\n[诊断 A 结论]")
        log(f"  CurrentConfiguration 是否按预期切换：{'✅ 是' if all_cfg_match else '❌ 否'}")
        log(f"  d1 (Surface 7) 在 5 个组态下的值：{[round(v, 4) for v in d1_vals]}")
        log(f"  d2 (Surface 14) 在 5 个组态下的值：{[round(v, 4) for v in d2_vals]}")
        log(f"  d3 (Surface 19) 在 5 个组态下的值：{[round(v, 4) for v in d3_vals]}")

        if d2_unique == 1:
            log(f"  ❌  d2 (Surface 14) 在所有组态下完全相同 → MCE 未控制到 LDE")
        else:
            log(f"  ✅  d2 (Surface 14) 随组态变化 → MCE 已生效到 LDE")

        if not all_cfg_match:
            log(f"  ⚠  SetCurrentConfiguration 调用后 CurrentConfiguration 未正确切换")

        # ---------------------------------------------------------------
        # 诊断 B：探测 Cardinal Points Analysis API + 读取 EFL
        # ---------------------------------------------------------------
        banner("诊断 B：Cardinal Points API 探测与 EFL 读取")

        # --- B.1 探测 I_Analyses 上的方法 ---
        log("\n  --- I_Analyses 上含 'card'/'focal'/'efl'/'new_' 的方法/属性 ---")
        analyses = bridge._system.Analyses
        found_methods = []
        for attr in sorted(dir(analyses)):
            low = attr.lower()
            if any(k in low for k in ['card', 'focal', 'efl', 'new_']):
                log(f"    {attr}")
                found_methods.append(attr)

        log("\n  --- 所有 New_ 开头方法 ---")
        new_methods = [a for a in sorted(dir(analyses)) if a.startswith('New_')]
        for m in new_methods:
            log(f"    {m}")

        # --- B.2 探测 AnalysisIDM 枚举 ---
        log("\n  --- AnalysisIDM 枚举中含 'card'/'focal' 的成员 ---")
        IDM = bridge._ZOSAPI.Analysis.AnalysisIDM
        idm_candidates = []
        for name in sorted(dir(IDM)):
            low = name.lower()
            if any(k in low for k in ['card', 'focal', 'first', 'order']):
                try:
                    val = getattr(IDM, name)
                    log(f"    AnalysisIDM.{name}  = {val}")
                    idm_candidates.append((name, val))
                except Exception:
                    pass

        # --- B.3 选择正确的 Analysis ID ---
        # 优先顺序: CardinalPoints -> CardinalPts -> Cardinal_Points -> ...
        CARDINAL_IDM_NAME = None
        for candidate in ['CardinalPoints', 'CardinalPts', 'Cardinal_Points',
                          'CardinalPoint', 'FirstOrder', 'First_Order']:
            if hasattr(IDM, candidate):
                CARDINAL_IDM_NAME = candidate
                log(f"\n  ✅ 使用 AnalysisIDM.{candidate} 作为 Cardinal Points 分析")
                break

        if CARDINAL_IDM_NAME is None:
            # 保底：用第一个含 card 的
            for name, val in idm_candidates:
                if 'card' in name.lower():
                    CARDINAL_IDM_NAME = name
                    log(f"\n  ✅ 使用 AnalysisIDM.{name} 作为 Cardinal Points 分析")
                    break

        # 初始化诊断 B 结果容器（无论是否找到 API 都要定义）
        diag_files = []
        efl_from_cardinal = []

        if CARDINAL_IDM_NAME is None:
            log(f"\n  ❌ 未找到 Cardinal Points 相关的 AnalysisIDM 枚举成员，诊断 B 跳过")
        else:
            analysis_id = getattr(IDM, CARDINAL_IDM_NAME)

            # --- B.4 执行诊断：逐配置运行 Cardinal Points Analysis ---
            for cfg in range(1, n_configs + 1):
                mce.SetCurrentConfiguration(cfg)
                log(f"  Config {cfg}: Set后 CurrentConfiguration={mce.CurrentConfiguration}")

                try:
                    # 通用接口调用
                    analysis = bridge._system.Analyses.New_Analysis(analysis_id)
                    analysis.ApplyAndWaitForCompletion()

                    # 导出报告
                    tmp_path = fr'D:\myprojects\Action_a\_diag_cardinal_cfg{cfg}.txt'
                    results = analysis.GetResults()
                    results.GetTextFile(tmp_path)
                    analysis.Close()

                    # 检查文件
                    exists = os.path.exists(tmp_path)
                    log(f"  Config {cfg}: 文件存在性 → {exists}")

                    if exists:
                        file_size = os.path.getsize(tmp_path)
                        with open(tmp_path, 'r', encoding='utf-16-le', errors='replace') as f:
                            content = f.read()
                        log(f"  Config {cfg}: 文件大小 = {file_size} 字节")
                        log(f"  ── 前 800 字符 ──")
                        log(f"  {content[:800]}")
                        log(f"  ──────────────────")

                        # 打印含关键词的行
                        log(f"  含 EFL 关键词的行：")
                        found = False
                        for line in content.splitlines():
                            if ('焦长' in line or 'Effective' in line or
                                'EFL' in line or 'Focal Length' in line or '焦距' in line):
                                log(f"    {line.strip()}")
                                found = True
                                if '焦长' in line:
                                    parts = line.split(':')
                                    if len(parts) >= 2:
                                        nums = parts[1].split()
                                        if nums:
                                            try:
                                                efl_val = abs(float(nums[-1]))
                                                efl_from_cardinal.append(round(efl_val, 4))
                                            except ValueError:
                                                pass
                        if not found:
                            log(f"    （未找到 EFL 关键词行）")

                        diag_files.append(tmp_path)
                    else:
                        log(f"  ❌ Config {cfg}: 文件未生成！")

                except Exception as e:
                    log(f"  ❌ Config {cfg} 分析失败: {e}")
                    import traceback
                    log(traceback.format_exc())

            log(f"\n[诊断 B 结论]")
            log(f"  成功生成 {len(diag_files)} 个诊断文件：")
            for f in diag_files:
                log(f"    - {os.path.basename(f)}")
            if efl_from_cardinal:
                log(f"  解析到的 EFL 值：{efl_from_cardinal}")
                unique_efl = set(efl_from_cardinal)
                if len(unique_efl) == 1:
                    log(f"  ❌  5 个配置的 Cardinal 报告 EFL 完全相同 → 问题可能出在：")
                    log(f"      1) SetCurrentConfiguration 未生效（见诊断 A）")
                    log(f"      2) Cardinal Points Analysis 本身不随配置更新")
                else:
                    log(f"  ✅  EFL 值有差异，Cardinal 方式正常")

        # ---------------------------------------------------------------
        # 诊断 C：MFE EFFL 操作数法 + CalculateMeritFunction 重置测试
        # ---------------------------------------------------------------
        banner("诊断 C：MFE EFFL 操作数法 + CalculateMeritFunction 重置测试")

        efl_from_mfe = []
        reset_log = []

        for cfg in range(1, n_configs + 1):
            # 切换配置
            mce.SetCurrentConfiguration(cfg)
            cfg_after_set = mce.CurrentConfiguration

            # 在 MFE 末尾插入 EFFL 操作数
            original_rows = mfe.NumberOfOperands
            mfe.InsertNewOperandAt(original_rows + 1)
            row_idx = original_rows + 1
            op = mfe.GetOperandAt(row_idx)
            effl_type = bridge._ZOSAPI.Editors.MFE.MeritOperandType.EFFL
            op.ChangeType(effl_type)
            op.GetOperandCell(bridge._ZOSAPI.Editors.MFE.MeritColumn.Param1).IntegerValue = 0

            # 计算前记录配置
            cfg_before_calc = mce.CurrentConfiguration

            # 计算
            mfe.CalculateMeritFunction()

            # 计算后立即读 CurrentConfiguration
            cfg_after_calc = mce.CurrentConfiguration

            # 重新按行号取操作数（因引用失效）
            op_fresh = mfe.GetOperandAt(row_idx)
            efl_val = float(op_fresh.Value)

            # 清理
            mfe.RemoveOperandAt(row_idx)

            efl_from_mfe.append(round(efl_val, 4))
            reset_log.append((cfg_before_calc, cfg_after_calc))

            flag = "🔁 重置" if cfg_after_calc != cfg_before_calc else "✅ 不变"
            log(f"  cfg={cfg}: Set后={cfg_after_set}, Calc前={cfg_before_calc}, "
                f"Calc后={cfg_after_calc} [{flag}], EFL={efl_val:.4f}")

        log(f"\n[诊断 C 结论]")
        log(f"  EFL 值：{efl_from_mfe}")
        unique_mfe_efl = set(efl_from_mfe)
        if len(unique_mfe_efl) == 1:
            log(f"  ❌  MFE 方式 5 个配置 EFL 完全相同（与 Cardinal 现象一致）")
        else:
            log(f"  ✅  MFE 方式 EFL 有差异")

        # 检查 CalculateMeritFunction 是否重置配置
        resets = [ (b, a) for b, a in reset_log if a != b ]
        if resets:
            log(f"  ⚠  CalculateMeritFunction 后 CurrentConfiguration 被重置：")
            for b, a in resets:
                log(f"       {b} → {a}")
            log(f"     extension 模式已知坑：MFE 计算会重置配置到 1，导致后续读取偏差")
        else:
            log(f"  ✅  CalculateMeritFunction 未改变 CurrentConfiguration")

        # ---------------------------------------------------------------
        # 诊断 D：刷新系统对象后的 Cardinal 结果
        # ---------------------------------------------------------------
        banner("诊断 D：刷新系统对象后 Cardinal 结果对照")

        efl_after_refresh = []
        diag_d_files = []

        for cfg in range(1, n_configs + 1):
            mce.SetCurrentConfiguration(cfg)
            log(f"  Config {cfg}: 切换后 CurrentConfiguration={mce.CurrentConfiguration}")

            # 尝试多种刷新手段
            refresh_tried = []
            try:
                if hasattr(bridge._system, 'UpdateStatus'):
                    bridge._system.UpdateStatus()
                    refresh_tried.append("UpdateStatus")
            except Exception as e:
                log(f"    UpdateStatus 不可用: {e}")

            try:
                if hasattr(bridge._system, 'MakeSystemActive'):
                    bridge._system.MakeSystemActive()
                    refresh_tried.append("MakeSystemActive")
            except Exception as e:
                log(f"    MakeSystemActive 不可用: {e}")

            try:
                _ = bridge._application.PrimarySystem
                refresh_tried.append("PrimarySystem")
            except Exception as e:
                log(f"    PrimarySystem 获取失败: {e}")

            log(f"    已尝试刷新: {refresh_tried}")

            # Cardinal Points Analysis（通用接口）
            if CARDINAL_IDM_NAME:
                try:
                    analysis = bridge._system.Analyses.New_Analysis(analysis_id)
                    analysis.ApplyAndWaitForCompletion()

                    tmp_path = fr'D:\myprojects\Action_a\_diag_D_cardinal_cfg{cfg}.txt'
                    analysis.GetResults().GetTextFile(tmp_path)
                    analysis.Close()

                    exists = os.path.exists(tmp_path)
                    log(f"  Config {cfg}: D诊断文件 → {exists}")

                    if exists:
                        file_size = os.path.getsize(tmp_path)
                        with open(tmp_path, 'r', encoding='utf-16-le', errors='replace') as f:
                            content = f.read()
                        log(f"    文件大小 = {file_size} 字节")

                        # 提取焦长行
                        efl = None
                        for line in content.splitlines():
                            if '焦长' in line:
                                log(f"    → {line.strip()}")
                                parts = line.split(':')
                                if len(parts) >= 2:
                                    nums = parts[1].split()
                                    if nums:
                                        try:
                                            efl = abs(float(nums[-1]))
                                        except ValueError:
                                            pass
                                break
                        efl_after_refresh.append(round(efl, 4) if efl else None)
                        diag_d_files.append(tmp_path)
                except Exception as e:
                    log(f"  ❌ Config {cfg} D诊断失败: {e}")
                    efl_after_refresh.append(None)
            else:
                log(f"  ⚠ 跳过：无有效 Cardinal Analysis ID")
                efl_after_refresh.append(None)

        log(f"\n[诊断 D 结论]")
        log(f"  刷新后 EFL 值：{efl_after_refresh}")
        valid_vals = [v for v in efl_after_refresh if v is not None]
        if len(valid_vals) <= 1:
            log(f"  ❌  有效数据不足，无法判断")
        elif len(set(valid_vals)) == 1:
            log(f"  ❌  刷新未改变结果，Cardinal 方式仍返回相同值")
        else:
            log(f"  ✅  刷新后 EFL 出现差异，原引用可能需要更新")

        # ---------------------------------------------------------------
        # 诊断总结
        # ---------------------------------------------------------------
        banner("诊断总结")

        log("\n  诊断 A（SetCurrentConfiguration 是否生效）：")
        for cfg, act_cfg, t7, t14, t19 in results_a:
            status = "✅" if act_cfg == cfg else "❌"
            log(f"    {status} Config {cfg}: CurrentConfiguration={act_cfg}, "
                f"S7={t7:.4f}, S14={t14:.4f}, S19={t19:.4f}")

        log("\n  诊断 B（Cardinal 文本报告 EFL 值）：")
        for cfg_idx, fpath in enumerate(diag_files, start=1):
            if os.path.exists(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-16-le') as f:
                        content = f.read()
                    efl_line = [l.strip() for l in content.splitlines() if '焦长' in l]
                    if efl_line:
                        log(f"    Config {cfg_idx}: {efl_line[0]}")
                    else:
                        log(f"    Config {cfg_idx}: （未找到焦长行，见文件 {os.path.basename(fpath)}）")
                except:
                    log(f"    Config {cfg_idx}: （读取失败）")

        log("\n  诊断 C（MFE EFFL 操作数法 + 重置测试）：")
        for cfg_idx, (efl_val, (before, after)) in enumerate(zip(efl_from_mfe, reset_log), start=1):
            reset_flag = "🔁 重置" if after != before else "✅ 不变"
            log(f"    Config {cfg_idx}: EFL={efl_val:.4f}, Calc前={before}, Calc后={after} [{reset_flag}]")

        log("\n  诊断 D（刷新系统对象后 Cardinal）：")
        for cfg_idx, efl_val in enumerate(efl_after_refresh, start=1):
            log(f"    Config {cfg_idx}: 刷新后 EFL = {efl_val if efl_val else 'N/A'}")

        # 综合判断
        log("\n  综合判断：")
        if not all_cfg_match:
            log("    🔴 根因疑似：SetCurrentConfiguration API 调用后 CurrentConfiguration 未实际切换")
            log("        建议：尝试 MCE 切换后增加 system.UpdateStatus() 或重载文件")
        elif d2_unique == 1:
            log("    🔴 根因疑似：MCE 与 LDE 关联断裂（d2 在所有组态下相同）")
            log("        可能原因：write_zoom_system 写入 MCE 时未正确建立 THIC-Surface 关联")
        elif 'unique_mfe_efl' in locals() and len(unique_mfe_efl) == 1:
            log("    🟡 切换本身有效，但 Cardinal 和 MFE 读取方式都返回相同 EFL")
            log("        可能性：Cardinal Points Analysis 在多配置模式下使用系统默认配置")
        else:
            log("    🟢 未发现明显异常，Cardinal 输出正常")

        banner("诊断 A-D 完成")

    except Exception as e:
        log(f"\n[异常] {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())
    finally:
        # ---------------------------------------------------------------
        # 清理：断开连接
        # ---------------------------------------------------------------
        try:
            bridge.disconnect()
            log("\n[连接已断开]")
        except:
            pass

    # ===================================================================
    # 诊断 E：排查 d2 异常值的来源（不用 Zemax，只读文件）
    # ===================================================================
    try:
        banner("诊断 E：d2 异常值来源排查（静态数据分析）")

        import json
        from zoom_utils import correct_zoom_spacings

        json_path = r"D:\myprojects\Action_a\last_run_config.json"
        log(f"[读取] {json_path}")
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        pp = data.get('principal_plane_correction', {})
        raw_configs = pp.get('raw_zoom_configs', [])
        corrected_configs = pp.get('corrected_zoom_configs', [])

        log("\n--- raw_zoom_configs（主面修正前）---")
        for c in raw_configs:
            log(f"  {c['name']:6s}  efl={c['efl']:7.2f}  "
                f"d1={c['d1']:10.3f}  d2={c['d2']:10.3f}  d3={c['d3']:10.3f}  epd={c['epd']:7.3f}")

        if corrected_configs:
            log("\n--- corrected_zoom_configs（主面修正后）---")
            for c in corrected_configs:
                if isinstance(c, dict):
                    log(f"  {c['name']:6s}  efl={c['efl']:7.2f}  "
                        f"d1={c['d1']:10.3f}  d2={c['d2']:10.3f}  d3={c['d3']:10.3f}")
                else:
                    # tuple 格式
                    log(f"  {c[0]:6s}  efl={c[1]:7.2f}  "
                        f"d1={c[2]:10.3f}  d2={c[3]:10.3f}  d3={c[4]:10.3f}")

        # 独立运行 correct_zoom_spacings 验证当前逻辑
        log("\n--- 运行 correct_zoom_spacings，验证修正逻辑 ---")
        try:
            raw_tuples = [(c['name'], c['efl'], c['d1'], c['d2'], c['d3'], c['epd'])
                          for c in raw_configs]

            group_pp_stored = pp.get('group_principal_planes', None)
            if group_pp_stored:
                group_pp = [(g['delta_H'], g['delta_Hp']) for g in group_pp_stored]
                log(f"  使用已保存的 group_principal_planes: {group_pp}")
            else:
                group_pp = [(0.0, 0.0)] * 4
                log(f"  使用默认 group_principal_planes: {group_pp}")

            corrected = correct_zoom_spacings(raw_tuples, group_pp)
            log("\n  correct_zoom_spacings 输出：")
            for c in corrected:
                log(f"    {c[0]:6s}  efl={c[1]:7.2f}  "
                    f"d1={c[2]:10.3f}  d2={c[3]:10.3f}  d3={c[4]:10.3f}")

            # 检查 d2 是否出现异常值
            d2_vals = [c[3] for c in corrected]
            max_d2 = max(d2_vals)
            min_d2 = min(d2_vals)
            log(f"\n  d2 范围：min={min_d2:.3f} mm, max={max_d2:.3f} mm")
            if max_d2 > 200:
                log(f"  ❌  d2 异常大（>200 mm），可能 delta_H 计算错误或主面数据有问题")
            else:
                log(f"  ✅  d2 值在合理范围（1-200 mm）")

        except Exception as e:
            log(f"  ❌ correct_zoom_spacings 运行失败: {e}")
            import traceback
            log(traceback.format_exc())

        # 检查主面数据
        log("\n--- group_principal_planes 数据检查 ---")
        if group_pp_stored:
            for i, g in enumerate(group_pp_stored):
                log(f"  G{i+1}: delta_H={g.get('delta_H', '?'):.4f}, "
                    f"delta_Hp={g.get('delta_Hp', '?'):.4f}")
        else:
            log("  ⚠  last_run_config.json 中无 group_principal_planes，使用默认 (0,0)")

        banner("诊断 E 完成")

    except FileNotFoundError:
        log(f"\n[诊断 E 跳过] {json_path} 文件不存在")
    except Exception as e:
        log(f"\n[诊断 E 异常] {type(e).__name__}: {e}")
        import traceback
        log(traceback.format_exc())

    # ===================================================================
    # 最终提示
    # ===================================================================
    log("\n提示：")
    log("1. 查看上方各诊断段落的结论")
    log("2. 对比 _diag_cardinal_cfg{1..5}.txt 5 个文件内容是否一致")
    log("3. 对比 _diag_D_cardinal_cfg{1..5}.txt 5 个文件内容是否一致")
    log("4. 根据诊断 E 检查 d2 值来源")


if __name__ == '__main__':
    main()
