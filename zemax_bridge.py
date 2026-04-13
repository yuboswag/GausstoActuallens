"""
zemax_bridge.py — ZOS-API Python 桥接模块

依赖：
  - pythonnet >= 3.0（pip install pythonnet）
    用于通过 clr 直接加载 Zemax .NET DLL
  - Zemax OpticStudio 2022 R1 及以上

连接方式：
  使用 ZOSAPI_NetHelper.dll 初始化，再加载 ZOSAPI.dll 和
  ZOSAPI_Interfaces.dll，与 cam_optimizer/core/zemax_connector.py
  采用完全相同的连接模式。

使用方式：
  bridge = ZemaxBridge()
  try:
      bridge.connect()
      bridge.new_system()
      ...
  finally:
      bridge.disconnect()
"""

import os
import re
import math
import tempfile

# ---------------------------------------------------------------------------
# Zemax 安装路径默认值（已在本机验证）
# ---------------------------------------------------------------------------
DEFAULT_ZEMAX_PATH = r'D:\Ansys Zemax OpticStudio 2024 R1.00'

# ---------------------------------------------------------------------------
# 模块级 DLL 加载标志：同一进程中只加载一次
# ---------------------------------------------------------------------------
_ZOSAPI_DLL_LOADED = False


class ZemaxBridgeError(Exception):
    """ZemaxBridge 通用异常基类"""
    pass


class ZemaxBridge:
    """
    ZOS-API 桥接类，封装与 Zemax OpticStudio 的连接和基础读写操作。

    内部 ZOSAPI 对象（_connection、_application、_system）对外不直接暴露，
    所有操作通过本类方法访问。
    """

    def __init__(self, zemax_path: str = None):
        """
        参数：
          zemax_path: Zemax 安装目录路径。
                      None 时使用 DEFAULT_ZEMAX_PATH。
        """
        self._zemax_path  = zemax_path or DEFAULT_ZEMAX_PATH
        self._connection  = None   # ZOSAPI_Connection .NET 对象
        self._application = None   # IZOSApplication .NET 对象
        self._system      = None   # IOpticalSystem .NET 对象
        self._ZOSAPI      = None   # ZOSAPI 模块引用（用于访问枚举）
        self._connected   = False

    # -----------------------------------------------------------------------
    # 连接 / 断开
    # -----------------------------------------------------------------------

    def connect(self, mode: str = 'standalone'):
        """
        连接 Zemax OpticStudio。

        参数：
          mode='standalone'  独立启动一个新的 Zemax 实例（最常用）
          mode='extension'   连接到已运行并启用 ZOS-API Extension 端口的实例

        连接流程（与 cam_optimizer/core/zemax_connector.py 一致）：
          1. 加载 ZOSAPI_NetHelper.dll
          2. ZOSAPI_Initializer.Initialize() 初始化
          3. 加载 ZOSAPI.dll 和 ZOSAPI_Interfaces.dll
          4. 创建 ZOSAPI_Connection 并获取 Application / PrimarySystem
        """
        global _ZOSAPI_DLL_LOADED

        try:
            import clr  # pythonnet
        except ImportError as e:
            raise ZemaxBridgeError(
                "未找到 pythonnet 模块。请执行：pip install pythonnet\n"
                f"原始错误：{e}"
            )

        if not os.path.exists(self._zemax_path):
            raise ZemaxBridgeError(
                f"找不到 Zemax 安装目录：{self._zemax_path}\n"
                "请在 ZemaxBridge(zemax_path=...) 中传入正确路径。"
            )

        if not _ZOSAPI_DLL_LOADED:
            # ------ 步骤 1：加载 ZOSAPI_NetHelper.dll ------
            net_helper = os.path.join(self._zemax_path, 'ZOSAPI_NetHelper.dll')
            if not os.path.exists(net_helper):
                net_helper = os.path.join(
                    self._zemax_path, r'ZOS-API\Libraries\ZOSAPI_NetHelper.dll'
                )
            if not os.path.exists(net_helper):
                raise ZemaxBridgeError(
                    f"找不到 ZOSAPI_NetHelper.dll，已搜索路径：\n"
                    f"  {self._zemax_path}\n"
                    f"  {self._zemax_path}\\ZOS-API\\Libraries\\"
                )
            clr.AddReference(net_helper)

            # ------ 步骤 2：初始化 ------
            import ZOSAPI_NetHelper
            if not ZOSAPI_NetHelper.ZOSAPI_Initializer.Initialize(self._zemax_path):
                raise ZemaxBridgeError(
                    "ZOSAPI_Initializer.Initialize() 返回 False。\n"
                    "请确认 Zemax 安装路径正确，且 OpticStudio 未被其他进程锁定。"
                )

            # ------ 步骤 3：加载主 DLL ------
            zos_dir = ZOSAPI_NetHelper.ZOSAPI_Initializer.GetZemaxDirectory()
            dll_path        = os.path.join(zos_dir, 'ZOSAPI.dll')
            interfaces_path = os.path.join(zos_dir, 'ZOSAPI_Interfaces.dll')
            if not os.path.exists(dll_path):
                dll_path        = os.path.join(zos_dir, r'ZOS-API\Libraries\ZOSAPI.dll')
                interfaces_path = os.path.join(zos_dir,
                                               r'ZOS-API\Libraries\ZOSAPI_Interfaces.dll')
            if not os.path.exists(dll_path):
                raise ZemaxBridgeError(
                    f"找不到 ZOSAPI.dll，Zemax 目录：{zos_dir}"
                )
            clr.AddReference(dll_path)
            clr.AddReference(interfaces_path)
            _ZOSAPI_DLL_LOADED = True

        # ------ 步骤 4：创建连接 ------
        try:
            import ZOSAPI
            self._ZOSAPI = ZOSAPI
        except ImportError as e:
            raise ZemaxBridgeError(
                f"import ZOSAPI 失败，DLL 可能未正确加载：{e}"
            )

        try:
            self._connection = ZOSAPI.ZOSAPI_Connection()
        except Exception as e:
            raise ZemaxBridgeError(
                f"无法创建 ZOSAPI_Connection：{e}"
            )

        if self._connection is None:
            raise ZemaxBridgeError("ZOSAPI_Connection 返回 None，连接失败。")

        try:
            if mode == 'standalone':
                self._application = self._connection.CreateNewApplication()
            elif mode == 'extension':
                self._application = self._connection.ConnectAsExtension(0)
            else:
                raise ZemaxBridgeError(
                    f"不支持的连接模式：{mode!r}，"
                    "请使用 'standalone' 或 'extension'。"
                )
        except ZemaxBridgeError:
            raise
        except Exception as e:
            raise ZemaxBridgeError(
                f"Application 创建失败（mode={mode!r}）：{e}"
            )

        if self._application is None:
            raise ZemaxBridgeError("Application 对象为 None，连接失败。")

        if not self._application.IsValidLicenseForAPI:
            if mode == 'extension':
                # extension 模式下该检查可能误报，仅发出警告继续
                print("[警告] IsValidLicenseForAPI 返回 False，"
                      "但 extension 模式下此检查可能不可靠，继续尝试连接。")
            else:
                raise ZemaxBridgeError(
                    "Zemax License 不支持 ZOS-API。\n"
                    "请确认已购买支持 API 的授权（Professional / Premium 版）。"
                )

        self._system = self._application.PrimarySystem
        if self._system is None:
            raise ZemaxBridgeError("无法获取 PrimarySystem，连接失败。")

        self._connected = True
        print(f"[连接成功] 模式={mode}，许可证类型={self._get_license_type()}")

    def disconnect(self):
        """
        安全断开与 Zemax 的连接，释放所有 .NET 资源。
        standalone 模式下会关闭 Zemax 实例；extension 模式下只断开连接。
        """
        if self._application is not None:
            try:
                self._application.CloseApplication()
            except Exception:
                pass
            self._application = None

        self._connection = None
        self._system     = None
        self._ZOSAPI     = None
        self._connected  = False
        print("[连接已断开]")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False  # 不吞异常

    # -----------------------------------------------------------------------
    # 辅助：连接状态检查
    # -----------------------------------------------------------------------

    def _check_connected(self):
        """内部方法：检查是否已连接，未连接则抛出异常。"""
        if not self._connected or self._system is None:
            raise ZemaxBridgeError("尚未连接到 Zemax，请先调用 connect()。")

    def _get_license_type(self) -> str:
        """内部方法：返回许可证类型字符串。"""
        try:
            lic    = self._application.LicenseStatus
            ZOSAPI = self._ZOSAPI
            # pythonnet 枚举路径：ZOSAPI.LicenseStatusType.XxxEdition
            if lic == ZOSAPI.LicenseStatusType.PremiumEdition:
                return "Premium"
            elif lic == ZOSAPI.LicenseStatusType.ProfessionalEdition:
                return "Professional"
            elif lic == ZOSAPI.LicenseStatusType.StandardEdition:
                return "Standard"
            else:
                return "Unknown"
        except Exception:
            return "Unknown"

    # -----------------------------------------------------------------------
    # 系统操作
    # -----------------------------------------------------------------------

    def new_system(self):
        """
        创建新的空白顺序光学系统。
        等价于 GUI 中的 File → New。
        """
        self._check_connected()
        self._system.New(False)
        print("[new_system] 已创建空白系统")

    def save_file(self, filepath: str):
        """
        将当前系统保存为 .zmx 文件。

        参数：
          filepath: 完整文件路径，例如 'D:/myprojects/test.zmx'
        """
        self._check_connected()
        dirpath = os.path.dirname(os.path.abspath(filepath))
        if not os.path.exists(dirpath):
            os.makedirs(dirpath)
        self._system.SaveAs(os.path.abspath(filepath))
        print(f"[save_file] 已保存到：{filepath}")

    # -----------------------------------------------------------------------
    # 写入单透镜系统（用于验证）
    # -----------------------------------------------------------------------

    def write_singlet(self, r1: float, r2: float, thickness: float,
                      glass: str, epd: float, wavelength_um: float,
                      image_distance: float = 100.0):
        """
        写入一个单透镜系统到当前空白文件。

        LDE 结构：
          Surface 0: OBJ  — 物面（无穷远，无需修改）
          Surface 1: STO  — 前表面（光阑面），R=r1，厚度=thickness，玻璃=glass
          Surface 2:      — 后表面，R=r2，厚度=image_distance
          Surface 3: IMA  — 像面

        参数：
          r1            前表面曲率半径 (mm)
          r2            后表面曲率半径 (mm)
          thickness     中心厚度 (mm)
          glass         玻璃名称，如 'N-BK7'
          epd           入瞳直径 (mm)
          wavelength_um 参考波长 (微米)，如 0.587056
          image_distance 后表面到像面的距离 (mm)，默认 100 mm
        """
        self._check_connected()
        ZOSAPI    = self._ZOSAPI
        TheSystem = self._system
        TheLDE    = TheSystem.LDE

        # 1. 添加玻璃库（SCHOTT 包含 N-BK7 等标准玻璃）
        TheSystem.SystemData.MaterialCatalogs.AddCatalog('SCHOTT')
        TheSystem.SystemData.MaterialCatalogs.AddCatalog('CDGM')

        # 2. 设置入瞳直径
        # pythonnet 枚举：ZOSAPI.SystemData.ZemaxApertureType.EntrancePupilDiameter
        TheSystem.SystemData.Aperture.ApertureType = \
            ZOSAPI.SystemData.ZemaxApertureType.EntrancePupilDiameter
        TheSystem.SystemData.Aperture.ApertureValue = epd

        # 3. 设置波长（单波长，修改系统新建时默认的第 1 个波长）
        sysWave = TheSystem.SystemData.Wavelengths
        sysWave.GetWavelength(1).Wavelength = wavelength_um

        # 4. 设置视场：轴上（0°）和离轴 10°
        # pythonnet 枚举：ZOSAPI.SystemData.FieldType.Angle
        sysField = TheSystem.SystemData.Fields
        sysField.SetFieldType(ZOSAPI.SystemData.FieldType.Angle)
        field1   = sysField.GetField(1)
        field1.X = 0.0
        field1.Y = 0.0
        sysField.AddField(0.0, 10.0, 1.0)  # Field 2：离轴 10°，权重 1.0

        # 5. 插入 2 个新面（New 后 LDE 仅有 OBJ(0) + IMA(1) 共 2 面）
        #    插入后变为：OBJ(0), S1(1), S2(2), IMA(3)
        TheLDE.InsertNewSurfaceAt(1)  # 在 index 1 插入 → 成为 S1
        TheLDE.InsertNewSurfaceAt(2)  # 在 index 2 插入 → 成为 S2

        # 6. 配置各面参数
        surf_1 = TheLDE.GetSurfaceAt(1)   # 前表面（STO）
        surf_2 = TheLDE.GetSurfaceAt(2)   # 后表面

        surf_1.IsStop    = True
        surf_1.Radius    = r1
        surf_1.Thickness = thickness
        surf_1.Material  = glass

        surf_2.Radius    = r2
        surf_2.Thickness = image_distance
        # 后表面无玻璃（空气）

        print(f"[write_singlet] 写入完成："
              f"R1={r1}, R2={r2}, CT={thickness}, 玻璃={glass}, "
              f"EPD={epd}, λ={wavelength_um}μm, 像距={image_distance}mm")

    # -----------------------------------------------------------------------
    # 读取系统信息
    # -----------------------------------------------------------------------

    def read_system_info(self) -> dict:
        """
        读取当前系统的基本参数。

        返回字典：
          {
            'num_surfaces': int,          # 面数（含 OBJ 和 IMA）
            'surfaces': [                 # 各面信息列表（按面序号）
              {
                'index': int,
                'radius': float,
                'thickness': float,
                'material': str,
                'semi_diameter': float,
                'is_stop': bool,
              }, ...
            ],
            'efl': float,                 # 有效焦距 (mm)，通过 MFE EFFL 操作数获取
          }
        """
        self._check_connected()
        TheLDE   = self._system.LDE
        num_surf = TheLDE.NumberOfSurfaces
        surfaces = []
        for i in range(num_surf):
            s = TheLDE.GetSurfaceAt(i)
            surfaces.append({
                'index':         i,
                'radius':        s.Radius,
                'thickness':     s.Thickness,
                'material':      s.Material if s.Material else '',
                'semi_diameter': s.SemiDiameter,
                'is_stop':       bool(s.IsStop),
            })

        efl = self._read_efl_via_mfe()
        return {
            'num_surfaces': num_surf,
            'surfaces':     surfaces,
            'efl':          efl,
        }

    def _read_efl_via_mfe(self) -> float:
        """
        内部方法：向 MFE 末尾临时插入 EFFL 操作数，计算并读取 EFL，
        然后删除该临时行。返回有效焦距 (mm)；失败时返回 float('nan')。

        枚举路径（pythonnet）：
          ZOSAPI.Editors.MFE.MeritOperandType.EFFL
          ZOSAPI.Editors.MFE.MeritColumn.Param1
        """
        try:
            ZOSAPI  = self._ZOSAPI
            TheMFE  = self._system.MFE
            n_rows  = TheMFE.NumberOfOperands
            row     = n_rows + 1

            TheMFE.InsertNewOperandAt(row)
            op = TheMFE.GetOperandAt(row)
            op.ChangeType(ZOSAPI.Editors.MFE.MeritOperandType.EFFL)
            # Param1 = 0 表示主波长
            op.GetOperandCell(
                ZOSAPI.Editors.MFE.MeritColumn.Param1
            ).IntegerValue = 0

            TheMFE.CalculateMeritFunction()
            efl = op.Value
            TheMFE.RemoveOperandAt(row)
            return float(efl)
        except Exception:
            return float('nan')

    def _read_effl_via_mfe(self) -> list:
        """
        通过在 MFE 中插入 EFFL 操作数读取各变焦配置的 EFL。
        返回列表，长度 = MCE 配置数，单位 mm。
        注意：此方法会临时修改 MFE，读完后清除插入的行。
        """
        mce = self._system.MCE
        n_configs = mce.NumberOfConfigurations
        mfe = self._system.MFE
        efls = []

        # 记录当前 MFE 行数，之后恢复
        original_rows = mfe.NumberOfOperands

        try:
            for cfg_idx in range(1, n_configs + 1):
                # 切换到目标配置
                mce.SetCurrentConfiguration(cfg_idx)

                # 在 MFE 末尾插入 EFFL 操作数
                new_op = mfe.AddOperand()
                # 获取 EFFL 枚举类型
                effl_type = self._ZOSAPI.Editors.MFE.MeritOperandType.EFFL
                new_op.ChangeType(effl_type)
                # Param1 = 波长序号，0 = 主波长
                new_op.GetOperandCell(
                    self._ZOSAPI.Editors.MFE.MeritColumn.Param1
                ).IntegerValue = 0

                # 触发计算
                mfe.CalculateMeritFunction()

                # 读取 Value
                efl_val = new_op.Value
                efls.append(round(efl_val, 4))

                # 删除刚插入的行（保持 MFE 干净）
                mfe.RemoveOperandAt(mfe.NumberOfOperands)

        except Exception as e:
            # 清理：删除所有多余行
            while mfe.NumberOfOperands > original_rows:
                mfe.RemoveOperandAt(mfe.NumberOfOperands)
            raise RuntimeError(f'_read_effl_via_mfe 失败: {e}')

        return efls

    def diagnose_system_validity(self) -> dict:
        """
        诊断当前 Zemax 系统是否物理可追迹。
        返回字典：{
            'ray_trace_ok': bool,        # 近轴光线追迹是否成功
            'num_surfaces': int,         # LDE 面数
            'surface_summary': list,     # 每面 [index, radius, thickness, material]
            'mce_configs': int,          # MCE 配置数
            'mce_summary': list,         # 每行 MCE 操作数摘要
            'efl_per_config': list,      # 每个配置的 EFFL
            'errors': list               # 错误信息列表
        }
        """
        result = {
            'ray_trace_ok': False,
            'num_surfaces': 0,
            'surface_summary': [],
            'mce_configs': 0,
            'mce_summary': [],
            'efl_per_config': [],
            'errors': []
        }
        try:
            lde = self._system.LDE
            result['num_surfaces'] = lde.NumberOfSurfaces
            for i in range(lde.NumberOfSurfaces):
                s = lde.GetSurfaceAt(i)
                result['surface_summary'].append({
                    'index': i,
                    'radius': s.Radius,
                    'thickness': s.Thickness,
                    'material': s.Material
                })
        except Exception as e:
            result['errors'].append(f'LDE 读取失败: {e}')

        try:
            mce = self._system.MCE
            result['mce_configs'] = mce.NumberOfConfigurations
            for row_i in range(mce.NumberOfOperands):
                op = mce.GetOperandAt(row_i + 1)
                row_data = {'type': str(op.Type), 'param1': op.Param1}
                vals = []
                for cfg in range(1, mce.NumberOfConfigurations + 1):
                    try:
                        cell = op.GetOperandCell(cfg)
                        vals.append(round(cell.DoubleValue, 6))
                    except:
                        vals.append(None)
                row_data['values'] = vals
                result['mce_summary'].append(row_data)
        except Exception as e:
            result['errors'].append(f'MCE 读取失败: {e}')

        # 用 EFFL 操作数逐配置读 EFL
        try:
            efls = self._read_effl_via_mfe()
            result['efl_per_config'] = efls
            result['ray_trace_ok'] = all(abs(v) > 0.1 for v in efls)
        except Exception as e:
            result['errors'].append(f'EFFL 读取失败: {e}')

        return result

    def read_zoom_efl(self, reference_efls: list = None) -> list:
        """
        读取各变焦配置的 EFL（mm）。

        优先使用传入的 reference_efls（来自高斯解，作为参考值）。
        若未传入，尝试通过 MFE EFFL 操作数读取（可能不准确）。

        注意：ZOS-API 2024 R1 extension 模式下无可靠的 EFL 直读 API，
        精确 EFL 验证请在 Zemax 中手动运行 Analysis > Cardinal Points。

        返回：list，长度 = MCE 配置数，单位 mm。
        """
        if reference_efls is not None:
            return list(reference_efls)

        # 备用：MFE EFFL（仅对 Config 1 有效，其余配置值相同为已知限制）
        try:
            efls = self._read_effl_via_mfe()
            return efls
        except Exception as e:
            raise RuntimeError(f'read_zoom_efl 失败: {e}')

    # -----------------------------------------------------------------------
    # RMS Spot Size（真实光线追迹）
    # -----------------------------------------------------------------------

    def read_spot_rms(self, field_points=None) -> list:
        """
        对指定视场点运行真实光线追迹，返回 RMS / GEO Spot Radius (mm)。

        参数：
          field_points: None 或整数列表，如 [1, 2]。
                        None 时读取系统中定义的全部视场点。

        返回：
          列表，每个元素为字典：
          {
            'field_index': int,   # 视场编号（从 1 开始）
            'rms_mm': float,      # RMS Spot Radius (mm)
            'geo_mm': float,      # Geometric Spot Radius (mm)
          }

        枚举路径（pythonnet）：
          ZOSAPI.Analysis.AnalysisIDM.StandardSpot
          ZOSAPI.Analysis.Settings.Spot.ReferTo.Centroid
        """
        self._check_connected()
        ZOSAPI     = self._ZOSAPI
        TheSystem  = self._system
        num_fields = TheSystem.SystemData.Fields.NumberOfFields

        if field_points is None:
            field_points = list(range(1, num_fields + 1))

        # 打开标准弥散斑分析
        spot_analysis = TheSystem.Analyses.New_Analysis(
            ZOSAPI.Analysis.AnalysisIDM.StandardSpot
        )
        spot_settings = spot_analysis.GetSettings()

        # pythonnet 下直接访问 IAS_Spot 属性（不需要 COM 的 CastTo）
        try:
            spot_settings.Field.SetFieldNumber(0)        # 0 = 全视场
            spot_settings.Wavelength.SetWavelengthNumber(0)  # 0 = 全波长
            spot_settings.ReferTo = \
                ZOSAPI.Analysis.Settings.Spot.ReferTo.Centroid
        except AttributeError:
            pass  # 若属性不可访问，使用分析默认设置继续

        # 运行分析（pythonnet 下直接调用，无需 CastTo('IA_')）
        spot_analysis.ApplyAndWaitForCompletion()
        spot_results = spot_analysis.GetResults()

        results  = []
        wave_idx = 1   # 取第 1 个波长的结果作为代表值
        for fi in field_points:
            try:
                rms = spot_results.SpotData.GetRMSSpotSizeFor(fi, wave_idx)
                geo = spot_results.SpotData.GetGeoSpotSizeFor(fi, wave_idx)
                rms = float(rms)
                geo = float(geo)
            except Exception:
                rms = float('nan')
                geo = float('nan')
            results.append({
                'field_index': fi,
                'rms_mm':      rms,
                'geo_mm':      geo,
            })

        spot_analysis.Close()
        return results

    # -----------------------------------------------------------------------
    # 赛德尔系数
    # -----------------------------------------------------------------------

    def read_seidel(self) -> list:
        """
        读取 Zemax 计算的赛德尔系数（各面 S_I ~ S_V）。

        通过 MFE 操作数（SPHA/COMA/ASTI/FCUR/DIST）逐面批量读取；
        读取完成后统一删除所有临时插入的行，不污染用户的 MFE。

        各操作数 Param1 = 面序号（0 表示全部面之和）。

        枚举路径（pythonnet）：
          ZOSAPI.Editors.MFE.MeritOperandType.SPHA / COMA / ASTI / FCUR / DIST
          ZOSAPI.Editors.MFE.MeritColumn.Param1

        返回：
          列表，每个元素为字典：
          {
            'surface': int,    # 面序号（0=合计）
            'S1_spha': float,  # 球差 S_I
            'S2_coma': float,  # 彗差 S_II
            'S3_asti': float,  # 像散 S_III
            'S4_fcur': float,  # 场曲 S_IV
            'S5_dist': float,  # 畸变 S_V
          }
        """
        self._check_connected()
        ZOSAPI   = self._ZOSAPI
        TheMFE   = self._system.MFE
        TheLDE   = self._system.LDE
        num_surf = TheLDE.NumberOfSurfaces

        
        surf_list = list(range(1, num_surf - 1))

        # 赛德尔操作数类型（按 ZOSAPI 枚举名）
        seidel_ops = [
            ('S1_spha', ZOSAPI.Editors.MFE.MeritOperandType.SPHA),
            ('S2_coma', ZOSAPI.Editors.MFE.MeritOperandType.COMA),
            ('S3_asti', ZOSAPI.Editors.MFE.MeritOperandType.ASTI),
            ('S4_fcur', ZOSAPI.Editors.MFE.MeritOperandType.FCUR),
            ('S5_dist', ZOSAPI.Editors.MFE.MeritOperandType.DIST),
        ]

        n_start        = TheMFE.NumberOfOperands  # 原始行数，清理时用
        inserted_count = 0
        row_map        = {}  # (surf, name) → MFE 行号

        # 批量插入所有临时操作数行
        for surf in surf_list:
            for name, op_type in seidel_ops:
                row = n_start + inserted_count + 1
                TheMFE.InsertNewOperandAt(row)
                op = TheMFE.GetOperandAt(row)
                op.ChangeType(op_type)
                op.GetOperandCell(
                    ZOSAPI.Editors.MFE.MeritColumn.Param1
                ).IntegerValue = surf
                row_map[(surf, name)] = row
                inserted_count += 1

        # 一次性计算，更新所有临时操作数的值
        TheMFE.CalculateMeritFunction()

        # 读取结果
        # 读取各面结果
        surface_rows = []
        for surf in surf_list:
            entry = {'surface': surf}
            for name, _ in seidel_ops:
                row = row_map[(surf, name)]
                try:
                    entry[name] = float(TheMFE.GetOperandAt(row).Value)
                except Exception:
                    entry[name] = float('nan')
            surface_rows.append(entry)

        # Python 端求和生成合计行（避免 DIST Surf=0 返回真实光线畸变的问题）
        total = {'surface': 0}
        for name, _ in seidel_ops:
            total[name] = sum(r[name] for r in surface_rows if not math.isnan(r[name]))
        
        results = [total] + surface_rows

        # 逆序删除所有临时行（从末尾往前，避免行号偏移）
        for row in range(n_start + inserted_count, n_start, -1):
            TheMFE.RemoveOperandAt(row)

        return results

    # -----------------------------------------------------------------------
    # Merit Function 总值和操作数列表
    # -----------------------------------------------------------------------

    def read_merit_function(self) -> dict:
        """
        计算并返回当前 Merit Function 总值和各操作数详情。

        返回字典：
          {
            'total': float,          # MF 总值
            'operands': [            # 各操作数列表
              {
                'row':    int,
                'type':   str,       # 操作数类型名（如 'EFFL'）
                'target': float,
                'weight': float,
                'value':  float,
              }, ...
            ]
          }
        """
        self._check_connected()
        TheMFE = self._system.MFE

        TheMFE.CalculateMeritFunction()
        total  = float(TheMFE.CurrentMeritFunction)
        n_rows = TheMFE.NumberOfOperands

        operands = []
        for i in range(1, n_rows + 1):
            op = TheMFE.GetOperandAt(i)
            try:
                type_name = str(op.TypeName)
            except Exception:
                type_name = 'UNKNOWN'
            operands.append({
                'row':    i,
                'type':   type_name,
                'target': float(op.Target),
                'weight': float(op.Weight),
                'value':  float(op.Value),
            })

        return {
            'total':    total,
            'operands': operands,
        }

    # -----------------------------------------------------------------------
    # 写入变焦系统 LDE 面数据（第一步：只写面，不管 MCE）
    # -----------------------------------------------------------------------

    def write_zoom_system(self, surface_prescription, zoom_configs,
                          wavelength_um=0.587056, sensor_half_diag_mm=3.8,
                          stop_surface_idx=14, bfd_mm=8.0):
        """
        写入变焦系统的 LDE 面数据（第一步：只写面，不管 MCE）。

        参数：
          surface_prescription: 面处方列表，每个元素为
            (idx, desc, R, n_out, t_after, glass)
            idx: Action_a 面编号（0‑25）
            desc: 描述字符串
            R: 曲率半径 (mm)，无穷大用 0.0 表示
            n_out: 出射介质折射率（未使用，保留）
            t_after: 该面之后的厚度 (mm)
            glass: 玻璃名称字符串，None 表示空气
          zoom_configs: 变焦配置列表，每个元素为
            (name, t1, t2, t3, t4, epd)
            name: 配置名称（如 "Wide"）
            t1..t4: 四个变焦组的厚度 (mm)
            epd: 入瞳直径 (mm)
          wavelength_um: 主波长 (μm)，默认 d 线 0.587056
          sensor_half_diag_mm: 传感器半对角线 (mm)，用于设置视场
          stop_surface_idx: 光阑面的 Action_a 编号（默认 14 → Surface 15）
          bfd_mm: 最后一面到像面的距离 (mm)，默认 8.0

        步骤：
          1. TheSystem.New(False) 创建空白系统
          2. 设置三波长：C 线 (0.6563μm)、d 线 (0.5876μm)、F 线 (0.4861μm)
          3. 设置视场类型为 Real Image Height，添加三个视场：
               Field 1: Y = 0 mm
               Field 2: Y = 2.66 mm（0.7 × 3.8）
               Field 3: Y = 3.8 mm
          4. 设置光圈类型为 Entrance Pupil Diameter，值 = zoom_configs[0][5]
          5. 在 LDE 中插入 26 个面（OBJ = Surface 0 已存在，在 OBJ 和 IMA 之间插入）
          6. 逐面设置 Radius、Thickness、Material
          7. 修正最后一面厚度为 BFD
          8. 设置光阑面
        """
        self._check_connected()
        ZOSAPI    = self._ZOSAPI
        TheSystem = self._system

        # 1. 创建空白系统（先 New，再取 LDE，避免旧引用失效）
        TheSystem.New(False)
        TheLDE = TheSystem.LDE
        print("[write_zoom_system] 已创建空白系统")

        # 1b. 加载玻璃库（CDGM 包含 H-ZLaF50E 等玻璃）
        TheSystem.SystemData.MaterialCatalogs.AddCatalog('CDGM')
        TheSystem.SystemData.MaterialCatalogs.AddCatalog('SCHOTT')
        print("[write_zoom_system] 已加载 CDGM + SCHOTT 玻璃库")
        # New(False) 后有 3 面 (OBJ + 默认面 + IMA)，删除默认面
        if TheLDE.NumberOfSurfaces == 3:
            TheLDE.RemoveSurfaceAt(1)
        # 现在是 OBJ(0) + IMA(1) = 2 面
        print(f"[write_zoom_system] 清理后面数: {TheLDE.NumberOfSurfaces}（期望 2）")

        # 2. 设置 6 个波长：0.55/0.45/0.65/0.75/0.85/0.95 μm，主波长 0.55 放第一个
        TheWavelengths = TheSystem.SystemData.Wavelengths

        # 先移除所有现有波长（从最后一个往前删，保留至少 1 个）
        while TheWavelengths.NumberOfWavelengths > 1:
            TheWavelengths.RemoveWavelength(TheWavelengths.NumberOfWavelengths)

        # 设置第一个波长为主波长 0.55 μm
        wl1 = TheWavelengths.GetWavelength(1)
        wl1.Value = 0.55
        wl1.Weight = 1.0

        # 添加剩余 5 个波长
        for wl_um in [0.45, 0.65, 0.75, 0.85, 0.95]:
            TheWavelengths.AddWavelength(wl_um, 1.0)

        # 设置主波长编号为 1
        try:
            TheWavelengths.SelectWavelength(1)
        except AttributeError:
            try:
                TheWavelengths.SetPrimaryWavelength(1)
            except AttributeError:
                print("[write_zoom_system] [警告] 无法设置主波长编号，已使用默认 Wavelength 1")

        print(f"[write_zoom_system] 已设置 6 个波长：0.55/0.45/0.65/0.75/0.85/0.95 μm")

        # 3. 设置视场类型为 Real Image Height
        #    新系统默认已有 Field 1 (0, 0)，直接复用，只 Add 两个离轴视场
        sysField = TheSystem.SystemData.Fields
        sysField.SetFieldType(ZOSAPI.SystemData.FieldType.RealImageHeight)
        sysField.AddField(0.0, 0.7 * sensor_half_diag_mm, 1.0)  # Field 2: Y = 2.66 mm
        sysField.AddField(0.0, sensor_half_diag_mm, 1.0)         # Field 3: Y = 3.8 mm

        # 4. 设置光圈类型为像方空间 F/# (Image Space F/#)
        TheSystemData = TheSystem.SystemData
        TheSystemData.Aperture.ApertureType = \
            ZOSAPI.SystemData.ZemaxApertureType.ImageSpaceFNum
        fnum_initial = zoom_configs[0][1] / zoom_configs[0][5]  # F/# = EFL / EPD
        TheSystemData.Aperture.ApertureValue = fnum_initial
        print(f"[write_zoom_system] 像方 F/# = {fnum_initial:.3f} (EFL={zoom_configs[0][1]}, EPD={zoom_configs[0][5]})")

        # 5. 插入 26 个面（OBJ=0 已存在，IMA 在末尾）
        #    当前 LDE 有 OBJ(0) 和 IMA(1) 共 2 面，需要在 index 1 处插入 26 个面
        #    使用递增索引插入，确保 Action_a 面 0 对应 Surface 1
        for i in range(1, 27):  # i 从 1 到 26
            TheLDE.InsertNewSurfaceAt(i)  # 在 index i 插入，新面成为 Surface i
        print(f"[write_zoom_system] 已插入 26 个面，总面数 = {TheLDE.NumberOfSurfaces}")

        # 6. 逐面设置 Radius、Thickness、Material
        for idx, desc, R, n_out, t_after, glass in surface_prescription:
            zemax_surf_num = idx + 1  # 关键偏移：Action_a 面 N → Zemax Surface N+1
            surf = TheLDE.GetSurfaceAt(zemax_surf_num)
            surf.Radius = R  # 直接用曲率半径
            surf.Thickness = t_after
            if glass is not None:
                surf.Material = glass
            # 可选：设置注释
            surf.Comment = desc

        # 7. 修正最后一面厚度为 BFD
        last_surf = TheLDE.GetSurfaceAt(26)  # Action_a 面25 → Surface 26
        last_surf.Thickness = bfd_mm  # 8.0mm

        # 8. 设置光阑面
        stop_surf_num = stop_surface_idx + 1  # Action_a 面14 → Surface 15
        TheLDE.GetSurfaceAt(stop_surf_num).IsStop = True
        print(f"[write_zoom_system] 光阑设置在 Surface {stop_surf_num}")

        print("[write_zoom_system] LDE 面数据写入完成")

        # 8b. 开启光线瞄准（近轴模式）
        TheSystemData.RayAiming.RayAiming = \
            ZOSAPI.SystemData.RayAimingMethod.Paraxial
        print("[write_zoom_system] 已开启光线瞄准（Paraxial 近轴模式）")

        # ------------------------------------------------------------------ #
        # 9. MCE 多配置写入
        # ------------------------------------------------------------------ #
        TheMCE = TheSystem.MCE

        # 9.1 添加 4 个配置（系统默认已有 1 个，共 5 个）
        for _ in range(4):
            TheMCE.AddConfiguration(False)
        print(f"[write_zoom_system] MCE 配置数 = {TheMCE.NumberOfConfigurations}（期望 5）")

        # 9.2 MCE 默认自带空操作数行，循环删除直到剩 1 行（Zemax 最少保留 1 行）
        while TheMCE.NumberOfOperands > 1:
            TheMCE.RemoveOperandAt(1)
        print(f"[write_zoom_system] MCE 清理默认空行后，操作数行数 = {TheMCE.NumberOfOperands}（期望 1）")

        # 9.3 写入 3 行 THIC 操作数（变焦间隔）
        # Action_a 面编号 + 1 = Zemax Surface 编号
        #   d1 → Surface  7 (Action_a 面 6，zoom_configs[i][2])
        #   d2 → Surface 14 (Action_a 面13，zoom_configs[i][3])
        #   d3 → Surface 19 (Action_a 面18，zoom_configs[i][4])
        gap_map = [
            (7,  2),   # (Zemax Surface 编号, zoom_configs 列索引)
            (14, 3),
            (19, 4),
        ]
        n_configs = len(zoom_configs)

        for row_idx, (zemax_surf, cfg_col) in enumerate(gap_map, start=1):
            if row_idx == 1:
                # 第 1 行直接覆盖残留行，不新增
                op = TheMCE.GetOperandAt(1)
                op.ChangeType(ZOSAPI.Editors.MCE.MultiConfigOperandType.THIC)
            else:
                # 第 2 行起插入新行
                op = TheMCE.InsertNewOperandAt(row_idx)
                op.ChangeType(ZOSAPI.Editors.MCE.MultiConfigOperandType.THIC)
            op.Param1 = zemax_surf
            for cfg_idx in range(n_configs):
                op.GetOperandCell(cfg_idx + 1).DoubleValue = \
                    zoom_configs[cfg_idx][cfg_col]
            print(f"[write_zoom_system] MCE 行 {row_idx}: THIC Surface {zemax_surf} "
                  f"← {[zoom_configs[i][cfg_col] for i in range(n_configs)]}")

        # 9.4 插入光圈操作数（F/#）
        # APER = ZOS-API MCE 中光圈的操作数枚举名
        # 各配置填 F/# 值（F/# = EFL / EPD）
        fnum_row = len(gap_map) + 1  # 第 4 行
        op_fnum = TheMCE.InsertNewOperandAt(fnum_row)
        op_fnum.ChangeType(ZOSAPI.Editors.MCE.MultiConfigOperandType.APER)
        fnum_values = []
        for cfg_idx in range(n_configs):
            fnum = zoom_configs[cfg_idx][1] / zoom_configs[cfg_idx][5]  # F/# = EFL / EPD
            op_fnum.GetOperandCell(cfg_idx + 1).DoubleValue = fnum
            fnum_values.append(fnum)
        print(f"[write_zoom_system] MCE 行 {fnum_row}: APER（F/#）"
              f"← {[f'{v:.3f}' for v in fnum_values]}")

        print("[write_zoom_system] MCE 写入完成")

    # -----------------------------------------------------------------------
    # 构建优化用 MFE
    # -----------------------------------------------------------------------

    def setup_optimization_mfe(self, zoom_configs):
        """
        构建优化 MFE：
          - 各配置 EFFL 逼近目标值
          - 各配置 TOTR 相等（DIFF 约束）
          - TOTR 尽量小（最小化总长）
          - 变量：MCE 中三个变焦空气间隔（d1/d2/d3）的各配置单元格
        不使用优化向导，完全自定义 MFE。
        """
        self._check_connected()
        ZOSAPI = self._ZOSAPI
        TheMCE = self._system.MCE
        TheMFE = self._system.MFE
        num_configs = len(zoom_configs)
        mc = ZOSAPI.Editors.MFE.MeritColumn
        MeritOp = ZOSAPI.Editors.MFE.MeritOperandType

        # ── 1. 清空 MFE ──────────────────────────────────────────────
        n = TheMFE.NumberOfOperands
        for _ in range(n - 1):
            TheMFE.RemoveOperandAt(1)

        # ── 2. 设置 MCE 变量（3 行 THIC × num_configs 配置）────────
        for mce_row in [1, 2, 3]:
            op = TheMCE.GetOperandAt(mce_row)
            for cfg in range(1, num_configs + 1):
                cell = op.GetOperandCell(cfg)
                try:
                    solve = cell.CreateSolveType(ZOSAPI.Editors.SolveType.Variable)
                    cell.SetSolveData(solve)
                except Exception as e:
                    print(f"  [警告] MCE 行{mce_row} Config{cfg} 设变量失败: {e}")
        print(f"[setup_optimization_mfe] MCE 变量设置完成（3 行 THIC × {num_configs} 配置）")

        # ── 3. 逐配置写入 CONF + EFFL + TOTR ───────────────────────
        row = 1
        totr_rows = []
        effl_rows = []

        for cfg_idx in range(num_configs):
            cfg_num = cfg_idx + 1
            target_efl = zoom_configs[cfg_idx][1]

            # CONF：切换到当前配置
            op = TheMFE.GetOperandAt(row) if row == 1 else TheMFE.InsertNewOperandAt(row)
            op.ChangeType(MeritOp.CONF)
            op.GetOperandCell(mc.Param1).IntegerValue = cfg_num
            row += 1

            # EFFL：逼近目标焦距
            op = TheMFE.InsertNewOperandAt(row)
            op.ChangeType(MeritOp.EFFL)
            op.Target = target_efl
            op.Weight = 1.0
            effl_rows.append(row)
            row += 1

            # TOTR：记录总长（Weight=0.1，参与最小化）
            op = TheMFE.InsertNewOperandAt(row)
            op.ChangeType(MeritOp.TOTR)
            op.Target = 0.0
            op.Weight = 0.1
            totr_rows.append(row)
            row += 1

        # ── 4. DIFF 约束：各配置 TOTR 与 Config1 TOTR 相等 ─────────
        totr1_row = totr_rows[0]
        for i in range(1, num_configs):
            op = TheMFE.InsertNewOperandAt(row)
            op.ChangeType(MeritOp.DIFF)
            op.GetOperandCell(mc.Param1).IntegerValue = totr1_row
            op.GetOperandCell(mc.Param2).IntegerValue = totr_rows[i]
            op.Target = 0.0
            op.Weight = 10.0
            row += 1

        # ── 5. 计算初始 MF ───────────────────────────────────────────
        TheMFE.CalculateMeritFunction()
        try:
            mf_value = TheMFE.MeritFunction
        except Exception:
            mf_value = None

        print(f"[setup_optimization_mfe] MFE 构建完成，共 {TheMFE.NumberOfOperands} 行操作数")
        print(f"[setup_optimization_mfe] EFFL 行号: {effl_rows}")
        print(f"[setup_optimization_mfe] TOTR 行号: {totr_rows}")
        if mf_value is not None:
            print(f"[setup_optimization_mfe] 初始 MF = {mf_value:.6f}")

        return {
            'totr_rows': totr_rows,
            'effl_rows': effl_rows,
            'total_operands': TheMFE.NumberOfOperands,
            'mf_value': mf_value,
        }

    # -----------------------------------------------------------------------
    # 读取真实光线追迹性能
    # -----------------------------------------------------------------------

    def read_real_performance(self, zoom_configs, effl_rows=None, totr_rows=None):
        """
        从已构建的 MFE 中读取各配置的 EFL、RMS Spot 和总 MF 值。
        前提：setup_optimization_mfe 已经构建好 MFE。

        参数：
            zoom_configs: list of tuples
            effl_rows: list of int, EFFL 操作数的行号（可选，用于正确读取 EFL）
            totr_rows: list of int, TOTR 操作数的行号（可选，用于读 TTL）
        """
        self._check_connected()
        TheMFE = self._system.MFE

        # 先计算一次 MF
        TheMFE.CalculateMeritFunction()

        # 读总 MF 值
        total_mf = None
        try:
            total_mf = TheMFE.MeritFunction
        except:
            try:
                total_mf = TheMFE.CalculateMeritFunction()
            except:
                pass

        results = {'configs': [], 'total_mf': total_mf}
        num_configs = len(zoom_configs)

        for cfg_idx in range(num_configs):
            # 使用传入的行号，或回退到硬编码偏移
            if effl_rows and totr_rows:
                effl_row = effl_rows[cfg_idx]
                totr_row = totr_rows[cfg_idx]
            else:
                base_row = cfg_idx * 3 + 1
                effl_row = base_row + 1
                totr_row = base_row + 2

            # 读 EFL
            op_effl = TheMFE.GetOperandAt(effl_row)
            actual_efl = float(op_effl.Value)

            # 读 TTL
            op_totr = TheMFE.GetOperandAt(totr_row)
            ttl = float(op_totr.Value)

            target_efl = zoom_configs[cfg_idx][1]
            name = zoom_configs[cfg_idx][0]
            efl_error = (actual_efl - target_efl) / target_efl * 100 if actual_efl != 0 else float('inf')

            # 切换到当前配置，读取 RMS Spot
            try:
                self._system.MCE.SetCurrentConfiguration(cfg_idx + 1)
                spot_data = self.read_spot_rms(field_points=[1, 2, 3])
                rms_spot_um = [s['rms_mm'] * 1000 for s in spot_data]  # mm → μm
            except Exception as e:
                print(f"  [警告] Config {cfg_idx+1} RMS Spot 读取失败: {e}")
                rms_spot_um = [float('nan'), float('nan'), float('nan')]

            print(f"  Config {cfg_idx+1} ({name}): EFL={actual_efl:.3f}, TTL={ttl:.3f}, RMS Spot={[f'{v:.1f}' if v == v else 'N/A' for v in rms_spot_um]}")

            results['configs'].append({
                'name': name,
                'target_efl': target_efl,
                'actual_efl': actual_efl,
                'efl_error_pct': efl_error,
                'ttl': ttl,
                'rms_spot_um': rms_spot_um,
            })

        return results

    # -----------------------------------------------------------------------
    # 局部优化
    # -----------------------------------------------------------------------

    def run_local_optimization(self, algorithm='DLS', cycles=0):
        """
        运行局部优化。
        algorithm: 'DLS' 或 'OD'
        cycles: 0(Auto), 1, 5, 10, 50 对应 ZOSAPI 的 OptimizationCycles
        """
        self._check_connected()
        opt = self._system.Tools.OpenLocalOptimization()
        if opt is None:
            print("  [警告] 无法打开局部优化工具")
            return

        # 算法: 0 = DampedLeastSquares, 1 = OrthogonalDescent
        if algorithm.upper() == 'OD':
            opt.Algorithm = self._ZOSAPI.Tools.Optimization.OptimizationAlgorithm(1)
        else:
            opt.Algorithm = self._ZOSAPI.Tools.Optimization.OptimizationAlgorithm(0)

        # 循环次数: 0=Auto, 1=1Cycle, 2=5Cycles, 3=10Cycles, 4=50Cycles
        cycle_map = {0: 0, 1: 1, 5: 2, 10: 3, 50: 4}
        opt.Cycles = self._ZOSAPI.Tools.Optimization.OptimizationCycles(cycle_map.get(cycles, 0))
        
        print(f"[run_local_optimization] 开始优化 (Algorithm={algorithm}, Cycles={cycles})...")
        opt.Run()
        import time
        timeout = 60  # 最多等待 60 秒
        elapsed = 0
        while opt.IsRunning and elapsed < timeout:
            time.sleep(1)
            elapsed += 1
        if opt.IsRunning:
            opt.Cancel()
            print(f"[run_local_optimization] 超时（{timeout}s），已强制取消")
        else:
            print(f"[run_local_optimization] 优化完成，耗时 {elapsed}s")
        opt.Close()