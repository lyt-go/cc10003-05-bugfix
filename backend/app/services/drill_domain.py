"""聚合查询下钻的共享领域规则。

查询模板（queryboard）与下钻路径（datadrill）必须围绕同一个模板版本共同变化，
凡是两边都要用到的口径判断——维度/度量字段解析、路径准入校验、失效来源描述——
都收敛到这里，避免两个服务各写一份规则导致版本不一致。
"""
from __future__ import annotations

from typing import Any

# 字段在页面上可能用逗号、顿号、分号或空白分隔，统一兼容。
_FIELD_SPLITTERS = [",", "，", "、", ";", "；"]
# 每层的度量之间用竖线分隔，层内多度量再走字段分隔符。
_LAYER_SEP = "|"

TEMPLATE_DRAFT = "草稿"
TEMPLATE_VALIDATING = "模板验证中"
TEMPLATE_ENABLED = "已启用"
TEMPLATE_DEPRECATED = "已废弃"
TEMPLATE_STATUS_ORDER = [
    TEMPLATE_DRAFT,
    TEMPLATE_VALIDATING,
    TEMPLATE_ENABLED,
    TEMPLATE_DEPRECATED,
]

PATH_DRAFT = "草稿"
PATH_REVIEWING = "路径评审中"
PATH_ENABLED = "已启用"
PATH_CLOSED = "已关闭"
PATH_STATUS_ORDER = [PATH_DRAFT, PATH_REVIEWING, PATH_ENABLED, PATH_CLOSED]


def split_fields(raw: Any) -> list[str]:
    """把「渠道,区域、门店」这类字符串拆成去空白后的字段列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw)
    for sep in _FIELD_SPLITTERS[1:]:
        text = text.replace(sep, _FIELD_SPLITTERS[0])
    return [part.strip() for part in text.split(_FIELD_SPLITTERS[0]) if part.strip()]


def split_layers(raw: Any) -> list[list[str]]:
    """解析路径的逐层字段（每层维度或每层度量）。

    两种书写形式都支持：
    - 层间用 ``|`` 分隔，层内多字段用逗号，如 ``销售额,件数|销售额,件数``；
    - 单层单字段时直接逗号平铺，如 ``品类,子品类,商品``，每个字段自成一层。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [split_fields(item) for item in raw]
    text = str(raw)
    if _LAYER_SEP in text:
        return [split_fields(part) for part in text.split(_LAYER_SEP) if part.strip()]
    # 没有层间分隔符时，每个字段视为一层，保证「维度层数」与层级声明对得上。
    return [[field] for field in split_fields(text)]


def join_fields(fields: list[str]) -> str:
    return ",".join(fields)


def template_snapshot(template: dict[str, Any]) -> dict[str, Any]:
    """固化模板版本快照，写进路径与审计，保证后续可还原当时版本。"""
    return {
        "模板ID": int(template.get("id", 0)),
        "模板编号": template.get("模板编号"),
        "模板版本": int(template.get("模板版本", 1)),
        "状态": template.get("status"),
        "维度字段": list(split_fields(template.get("维度字段"))),
        "度量字段": list(split_fields(template.get("度量字段"))),
        "指标名称": template.get("指标名称"),
        "数据源": template.get("数据源"),
        "缓存秒数": template.get("缓存秒数"),
    }


def validate_path_admission(
    path: dict[str, Any],
    template: dict[str, Any] | None,
) -> list[str]:
    """路径启用/换版前的统一准入校验，返回全部拒绝原因（空列表代表通过）。

    规则对应需求：
    1. 模板必须存在且处于「已启用」，验证中/草稿/已废弃一律不允许启用路径；
    2. 路径每一层维度都必须能在模板维度字段中找到来源；
    3. 路径各层的度量口径必须一致，且与模板度量字段完全相同；
    4. 起始维度必须与第一层维度一致，层级数与每层度量数要对得上。
    """
    errors: list[str] = []
    path_code = path.get("路径编号", f"路径{path.get('id')}")

    if template is None:
        errors.append(
            f"{path_code} 关联的查询模板（ID={path.get('关联模板ID')}）不存在或已归档，无法启用"
        )
        return errors

    tpl_status = template.get("status")
    tpl_code = template.get("模板编号")
    tpl_version = int(template.get("模板版本", 1))
    if tpl_status != TEMPLATE_ENABLED:
        errors.append(
            f"{path_code} 关联模板 {tpl_code}(v{tpl_version}) 当前为「{tpl_status}」，"
            "只有已启用模板才能启用下钻路径"
        )

    template_dims = split_fields(template.get("维度字段"))
    template_measures = split_fields(template.get("度量字段"))
    layer_dims = split_layers(path.get("每层维度名"))
    layer_measures = split_layers(path.get("每层度量"))
    start_dim = str(path.get("起始维度") or "").strip()

    if not layer_dims:
        errors.append(f"{path_code} 未配置每层维度名，无法形成下钻层级")
    if not layer_measures:
        errors.append(f"{path_code} 未配置每层度量，无法确认度量口径")

    # 规则 2：逐层维度都要在模板维度字段中有来源。
    missing_dims: list[str] = []
    for index, dims in enumerate(layer_dims, start=1):
        for dim in dims:
            if dim not in template_dims:
                missing_dims.append(f"第{index}层维度「{dim}」")
    if missing_dims:
        errors.append(
            f"{path_code} 维度字段缺少来源：{'、'.join(missing_dims)} 不在模板 "
            f"{tpl_code}(v{tpl_version}) 的维度字段（{join_fields(template_dims) or '空'}）中"
        )

    # 起始维度必须落在第一层，避免点进去的第一层与声明的起始维度不一致。
    if layer_dims and start_dim and start_dim != layer_dims[0][0]:
        errors.append(
            f"{path_code} 起始维度「{start_dim}」与第一层维度「{layer_dims[0][0]}」不一致"
        )

    # 规则 3：层内度量必须与模板度量口径一致，且逐层保持同一口径。
    baseline: list[str] | None = None
    for index, measures in enumerate(layer_measures, start=1):
        if baseline is None:
            baseline = measures
        elif measures != baseline:
            errors.append(
                f"{path_code} 第{index}层度量（{join_fields(measures)}）与首层口径"
                f"（{join_fields(baseline)}）不一致，下钻各层度量口径必须相同"
            )
    if baseline is not None and baseline != template_measures:
        errors.append(
            f"{path_code} 每层度量（{join_fields(baseline)}）与模板 {tpl_code}(v{tpl_version}) "
            f"度量字段（{join_fields(template_measures)}）口径不一致"
        )

    # 层级数要对齐，防止中间层级丢失（有维度没度量或反之）。
    if layer_dims and layer_measures and len(layer_dims) != len(layer_measures):
        errors.append(
            f"{path_code} 维度层数（{len(layer_dims)}）与度量层数（{len(layer_measures)}）"
            "不一致，存在缺失的中间层级"
        )

    return errors


def deprecation_reason(template: dict[str, Any]) -> str:
    """模板废弃后，路径详情上展示的失效来源说明。"""
    return (
        f"模板{template.get('模板编号')}(v{int(template.get('模板版本', 1))})"
        f"已于{template.get('废弃时间') or '废弃时间未记录'}废弃，"
        "新的聚合请求已阻止，历史下钻层级与查询缓存保留"
    )
