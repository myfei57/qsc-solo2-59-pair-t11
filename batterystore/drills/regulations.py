"""周期档位、现场动作与证据类型的内置口径。

四档周期分别对应不同监管要求，最严档生效原则：同一单位同时命中多档时取更
短周期。内置档位是出厂默认，台账建立后仍可按单位实际的地方要求建自定义条目。
"""

from __future__ import annotations

# 现场联动动作：全部成功，演练才可能合格；任一项失败即整体失败。
ACTION_ALARM = "alarm_linkage"          # 火灾自动报警信号触发
ACTION_BROADCAST = "evac_broadcast"     # 应急广播启动
ACTION_PCS_TRIP = "pcs_trip"            # 联动切断 PCS 并网
ACTION_VALVE = "suppression_valve"      # 灭火装置动作到位（可按单位配置裁剪）

DEFAULT_REQUIRED_ACTIONS = (
    ACTION_ALARM,
    ACTION_BROADCAST,
    ACTION_PCS_TRIP,
    ACTION_VALVE,
)

ACTION_LABELS = {
    ACTION_ALARM: "报警联动触发",
    ACTION_BROADCAST: "应急广播启动",
    ACTION_PCS_TRIP: "联动切断PCS",
    ACTION_VALVE: "灭火装置到位",
}

# 合格演练必须留存的证据类型。
EVIDENCE_PLAN = "drill_plan"            # 演练方案
EVIDENCE_ROSTER = "signin_roster"       # 签到名册
EVIDENCE_PHOTO = "site_photo"           # 现场影像

DEFAULT_REQUIRED_EVIDENCE = (
    EVIDENCE_PLAN,
    EVIDENCE_ROSTER,
    EVIDENCE_PHOTO,
)

EVIDENCE_LABELS = {
    EVIDENCE_PLAN: "演练方案",
    EVIDENCE_ROSTER: "签到名册",
    EVIDENCE_PHOTO: "现场影像",
}


# 内置周期档：(档级键, 名称, 月数, 依据, 备注)
DEFAULT_TIERS = (
    {
        "code": "key_unit_half_year",
        "name": "消防安全重点单位档",
        "cadence_months": 6,
        "basis": (
            "《机关、团体、企业、事业单位消防安全管理规定》（公安部令第61号）"
            "要求消防安全重点单位至少每半年进行一次灭火和应急疏散演练。"
        ),
        "note": "储能电站按其火灾危险性参照重点单位从严管理。",
    },
    {
        "code": "general_unit_year",
        "name": "一般单位年度档",
        "cadence_months": 12,
        "basis": (
            "《中华人民共和国消防法》要求机关、团体、企业、事业等单位至少每年"
            "组织一次有针对性的消防演练。"
        ),
        "note": "其他单位适用；地方有更严规定的从其规定。",
    },
    {
        "code": "dense_quarter",
        "name": "人员密集场所季度档",
        "cadence_months": 3,
        "basis": (
            "人员密集场所行业管理与地方消防规定普遍要求至少每季度组织一次"
            "应急疏散演练。"
        ),
        "note": "需以属地现行规章为准，可在建台账时改配。",
    },
    {
        "code": "school_month",
        "name": "校园月度疏散档",
        "cadence_months": 1,
        "basis": (
            "中小学幼儿园应急疏散演练相关管理要求：每月开展一次应急疏散演练，"
            "并每学期至少开展一次综合性消防演练。"
        ),
        "note": "校园/幼托场景适用。",
    },
)
