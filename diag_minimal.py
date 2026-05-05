"""
diag_minimal.py — 最小诊断脚本

只做两件事：
  1. 探测 ZOS-API 中 Cardinal Points Analysis 的正确 API 名字（不连 Zemax 后备，连了更好）
  2. 读 last_run_config.json，看 d2 的 1716 mm 是从哪来的（不用 Zemax）
"""

import os
import json
import traceback

LOG = r'D:\myprojects\gauss_to_lens\_diag_minimal.log'

def log(msg=''):
    print(msg, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

# 清空旧日志
try: os.remove(LOG)
except FileNotFoundError: pass


# ============================================================
# PART 1：探测 Cardinal Points Analysis 的真实 API 名
# ============================================================
log("=" * 60)
log("  PART 1：探测 Cardinal Points Analysis API")
log("=" * 60)

try:
    from zemax_bridge import ZemaxBridge
    bridge = ZemaxBridge()
    bridge.connect(mode='extension')

    # 1a. I_Analyses 上所有 New_ 开头的方法
    analyses = bridge._system.Analyses
    log("\n--- I_Analyses 上所有 New_* 方法 ---")
    for attr in sorted(dir(analyses)):
        if attr.startswith('New_'):
            log(f"    {attr}")

    # 1b. AnalysisIDM 枚举里所有含 Card / Focal / First / Order 的成员
    log("\n--- AnalysisIDM 枚举中相关成员 ---")
    IDM = bridge._ZOSAPI.Analysis.AnalysisIDM
    keywords = ['card', 'focal', 'first', 'order', 'gauss', 'pupil']
    for name in sorted(dir(IDM)):
        if any(k in name.lower() for k in keywords):
            try:
                val = getattr(IDM, name)
                log(f"    AnalysisIDM.{name}  =  {val}")
            except Exception:
                pass

    # 1c. 尝试 New_Analysis 调用看能不能跑起来
    log("\n--- 尝试 New_Analysis(AnalysisIDM.CardinalPoints) ---")
    try:
        analysis_id = IDM.CardinalPoints
        log(f"    AnalysisIDM.CardinalPoints 存在，值 = {analysis_id}")
        analysis = analyses.New_Analysis(analysis_id)
        log(f"    New_Analysis 调用成功，对象类型 = {type(analysis).__name__}")
        analysis.ApplyAndWaitForCompletion()
        log("    ApplyAndWaitForCompletion 调用成功")
        tmp = r'D:\myprojects\gauss_to_lens\_diag_minimal_card.txt'
        analysis.GetResults().GetTextFile(tmp)
        if os.path.exists(tmp):
            size = os.path.getsize(tmp)
            log(f"    报告文件生成，大小 {size} 字节")
            with open(tmp, 'r', encoding='utf-16-le', errors='replace') as f:
                content = f.read()
            log("    ── 报告前 600 字符 ──")
            log(content[:600])
            log("    ──────────────────────")
        else:
            log("    ❌ 报告文件未生成")
        analysis.Close()
    except AttributeError as e:
        log(f"    ❌ AnalysisIDM.CardinalPoints 不存在: {e}")
    except Exception as e:
        log(f"    ❌ 分析运行失败: {type(e).__name__}: {e}")
        log(traceback.format_exc())

    bridge.disconnect()
    log("\n[Zemax 已断开]")

except Exception as e:
    log(f"\n[PART 1 异常] {type(e).__name__}: {e}")
    log(traceback.format_exc())


# ============================================================
# PART 2：读 last_run_config.json，追踪 d2 的 1700 mm 来源
# ============================================================
log("\n" + "=" * 60)
log("  PART 2：d2 异常值来源排查（纯静态分析）")
log("=" * 60)

json_path = r'D:\myprojects\gauss_to_lens\last_run_config.json'
try:
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    log(f"\n[读取] {json_path}")
    log(f"[顶层 key] {list(data.keys())}")

    pp = data.get('principal_plane_correction', {})
    log(f"[principal_plane_correction 的 key] {list(pp.keys())}")

    raw = pp.get('raw_zoom_configs', [])
    log(f"\n--- raw_zoom_configs（{len(raw)} 项）---")
    for c in raw:
        log(f"  {c.get('name','?'):6s}  efl={c.get('efl',0):7.2f}  "
            f"d1={c.get('d1',0):10.3f}  d2={c.get('d2',0):10.3f}  "
            f"d3={c.get('d3',0):10.3f}  epd={c.get('epd',0):7.3f}")

    corrected = pp.get('corrected_zoom_configs', [])
    if corrected:
        log(f"\n--- corrected_zoom_configs（{len(corrected)} 项）---")
        for c in corrected:
            if isinstance(c, dict):
                log(f"  {c.get('name','?'):6s}  efl={c.get('efl',0):7.2f}  "
                    f"d1={c.get('d1',0):10.3f}  d2={c.get('d2',0):10.3f}  "
                    f"d3={c.get('d3',0):10.3f}")
            else:
                log(f"  {c[0]:6s}  efl={c[1]:7.2f}  "
                    f"d1={c[2]:10.3f}  d2={c[3]:10.3f}  d3={c[4]:10.3f}")
    else:
        log("\n--- corrected_zoom_configs 不存在于 JSON 中 ---")

    # 主面数据
    gpp = pp.get('group_principal_planes', None)
    log(f"\n--- group_principal_planes ---")
    if gpp:
        for i, g in enumerate(gpp):
            log(f"  G{i+1}: delta_H={g.get('delta_H','?')}, delta_Hp={g.get('delta_Hp','?')}")
    else:
        log("  （无此字段）")

    # 跑 correct_zoom_spacings 看实时输出
    log(f"\n--- 实时运行 zoom_utils.correct_zoom_spacings ---")
    try:
        from zoom_utils import correct_zoom_spacings
        raw_tuples = [(c['name'], c['efl'], c['d1'], c['d2'], c['d3'], c['epd']) for c in raw]
        if gpp:
            group_pp = [(g['delta_H'], g['delta_Hp']) for g in gpp]
        else:
            group_pp = [(0.0, 0.0)] * 4
        log(f"  使用 group_pp = {group_pp}")
        out = correct_zoom_spacings(raw_tuples, group_pp)
        for c in out:
            log(f"  {c[0]:6s}  efl={c[1]:7.2f}  "
                f"d1={c[2]:10.3f}  d2={c[3]:10.3f}  d3={c[4]:10.3f}")
    except Exception as e:
        log(f"  ❌ {type(e).__name__}: {e}")
        log(traceback.format_exc())

except FileNotFoundError:
    log(f"[跳过] {json_path} 不存在")
except Exception as e:
    log(f"[PART 2 异常] {type(e).__name__}: {e}")
    log(traceback.format_exc())

log("\n" + "=" * 60)
log("  诊断完成")
log("=" * 60)
