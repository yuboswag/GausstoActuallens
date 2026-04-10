"""
config.py
Action_a 配置文件：默认参数与解析辅助函数。
"""

import re
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════
#  G1 组外表面排除玻璃列表（化学稳定性/耐候性不足，不适合做最外层镜片）
# ══════════════════════════════════════════════════════════════════════
EXCLUDED_FOR_OUTER = {
   'H-QK3L','H-FK55','H-FK61','H-FK61B','H-FK71','H-FK95N',
    'H-K9LGT', 'H-K9L*', 'H-K9LA', 'H-K10',
    'H-ZPK1A', 'H-ZPK2A', 'H-ZPK3', 'H-BaK6',
    'H-ZK3',   'H-ZK3A',  'H-ZK6',  'H-ZK7A', 'H-ZK9B', 'H-ZK10',
    'H-ZK10L', 'H-ZK11',  'H-ZK14', 'H-ZK20', 'H-ZK21',
    'H-ZK50',  'H-ZK50GT','H-LaK2A','H-LaK4L','H-LaK5A',
    'H-LaK6A', 'H-LaK7A', 'H-LaK10','H-LaK11','H-LaK12',
    'H-LaK50A','H-LaK51A','H-LaK72','ZF7',     'ZF7L',
    'H-LaF2',  'H-ZLaF52A','H-ZLaF53B','H-ZLaF53BGT',
}

# ══════════════════════════════════════════════════════════════════════
#  默认配置（对应 main.py 默认值）
# ══════════════════════════════════════════════════════════════════════
_DEFAULT_GROUPS = [
    {
        'name':            'G1',
        'zoom_csv_group':  '',
        'f_group':         '56.959',
        'D':               '35',
        'structure':       'pos,neg,pos,pos',
        'glass_roles':     '',
        'apo':             False,
        'cemented_pairs':  '(1,2)',
        'spacings_mm':     '1.0,0.0,1.0',
        'min_f_mm':        '40',
        'max_f_mm':        '',
        'allow_duplicate': True,
        'min_r_mm':        '50.0',
        't_edge_min':      '1.5',
        't_center_min':    '2.0',
        't_cemented_min':  '4.0',
        'glass_names':     'H-LaF50B,ZF6,H-FK95N,H-LaK7A',
        'focal_lengths_mm':'34.04,-24.82,20.05,-20.00',
        'vgen_list':       '34.507,20.567,45.013,30.000',
        'nd_vals':         '',
    },
    {
        'name':            'G2',
        'zoom_csv_group':  'G2',
        'f_group':         '-12.151',
        'D':               '15',
        'structure':       'neg,neg,pos,neg',
        'glass_roles':     '',
        'apo':             False,
        'cemented_pairs':  '(2,3)',
        'spacings_mm':     '1.5,1.5,0.0',
        'min_f_mm':        '15',
        'max_f_mm':        '',
        'allow_duplicate': True,
        'min_r_mm':        '25.0',
        't_edge_min':      '1.0',
        't_center_min':    '1.5',
        't_cemented_min':  '3.0',
        'glass_names':     'H-ZLaF50E,H-ZLaF50E,H-ZF88,H-QK3L',
        'focal_lengths_mm':'-30.00,-30.00,25.00,-40.00',
        'vgen_list':       '45.013,45.013,20.000,55.000',
        'nd_vals':         '',
    },
    {
        'name':            'G3',
        'zoom_csv_group':  'G3',
        'f_group':         '24.409',
        'D':               '12',
        'structure':       'pos,neg,pos',
        'glass_roles':     '',
        'apo':             False,
        'cemented_pairs':  '(1,2)',
        'spacings_mm':     '1.0,0.0',
        'min_f_mm':        '15',
        'max_f_mm':        '',
        'allow_duplicate': True,
        'min_r_mm':        '18.0',
        't_edge_min':      '1.0',
        't_center_min':    '1.5',
        't_cemented_min':  '3.0',
        'glass_names':     'H-ZK9B,ZF6,H-FK61B',
        'focal_lengths_mm':'30.00,-20.00,25.00',
        'vgen_list':       '40.000,20.567,50.000',
        'nd_vals':         '',
    },
    {
        'name':            'G4',
        'zoom_csv_group':  '',
        'f_group':         '72.545',
        'D':               '10',
        'structure':       'pos,neg,pos,neg',
        'glass_roles':     '',
        'apo':             False,
        'cemented_pairs':  '(0,1)',
        'spacings_mm':     '0.0,4.0,4.0',
        'min_f_mm':        '15',
        'max_f_mm':        '',
        'allow_duplicate': True,
        'min_r_mm':        '16.0',
        't_edge_min':      '1.0',
        't_center_min':    '1.5',
        't_cemented_min':  '3.0',
        'glass_names':     'H-LaK7A,H-ZF4A,H-FK95N,H-F4',
        'focal_lengths_mm':'34.04,-24.82,20.05,-20.00',
        'vgen_list':       '34.507,20.567,45.013,30.000',
        'nd_vals':         '',
    },
]

_AUTO_SAVE_FILE = Path(__file__).parent / 'action_a_last_config.json'


# ══════════════════════════════════════════════════════════════════════
#  参数解析辅助函数
# ══════════════════════════════════════════════════════════════════════
def _parse_structure(s: str) -> list:
    """'pos,neg,pos,pos' 或 '+,-,+,+' → ['pos','neg','pos','pos']"""
    parts = [p.strip() for p in s.split(',') if p.strip()]
    result = []
    for p in parts:
        if p in ('+', 'pos'):
            result.append('pos')
        elif p in ('-', 'neg'):
            result.append('neg')
        else:
            result.append(p)
    return result


def _parse_list_str(s: str) -> list:
    """逗号分隔字符串 → 去空白的字符串列表"""
    return [p.strip() for p in s.split(',') if p.strip()]


def _parse_floats(s: str) -> list:
    """'1.0,2.5,-3.0' → [1.0, 2.5, -3.0]"""
    return [float(p.strip()) for p in s.split(',') if p.strip()]


def _parse_cemented_pairs(s: str) -> list:
    """'(1,2),(2,3)' → [(1,2),(2,3)]；空字符串 → []"""
    s = s.strip()
    if not s:
        return []
    pairs = re.findall(r'\((\d+)\s*,\s*(\d+)\)', s)
    return [(int(a), int(b)) for a, b in pairs]


def _parse_melt_filter(s: str) -> list:
    """'MA,常熔,2' → ['MA','常熔','2']"""
    return [p.strip() for p in s.split(',') if p.strip()]