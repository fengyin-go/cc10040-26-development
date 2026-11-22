"""字段血缘领域服务：追踪字段的来源、转换逻辑和下游影响。

跨模块数据流: metric_definition -> field_lineage（追踪指标字段来源）
             caliber_version -> field_lineage（口径变更影响分析）
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class FieldLineageService(BaseModuleService):
    MODULE = "field_lineage"
    ENTITY = "字段血缘"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["目标字段", "来源字段列表", "转换逻辑", "中间表", "血缘深度"]
    REQUIRED_FIELDS = ["目标字段", "来源字段列表", "转换逻辑"]
    STATUS_ORDER = ["已建立", "待验证", "已断裂"]
    ACTION_RULES = {"刷新血缘": "已建立", "标记断裂": "已断裂"}
    NEGATIVE_ACTIONS = []

    # 补登来源时可带上适用区间；不进列表字段，避免改变既有登记表单契约。
    OPTIONAL_RANGE_FIELDS = ["适用版本", "生效开始", "生效结束"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记血缘条目，并保留可选的适用版本与生效区间。

        跨模块数据流: field_lineage -> recalc_engine（重算依赖核验按区间取来源）
        """
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        for field in self.OPTIONAL_RANGE_FIELDS:
            value = str(values.get(field) or "").strip()
            if value:
                entry[field] = value
        return entry, missing

    def trace_field(self, field_name: str) -> dict[str, Any]:
        """追溯字段的完整血缘链路。

        跨模块数据流: field_lineage -> metric_definition（关联指标信息）
        """
        lineage_data = store.get_field_lineage(field_name)
        if lineage_data is None:
            return {"field": field_name, "lineage": [], "depth": 0}

        # 关联指标信息
        metric_relations = []
        for m in store.rows("metric_definition"):
            if field_name in str(m.get("计算口径", "")) or field_name in str(m.get("指标名称", "")):
                metric_relations.append({
                    "metric_name": m.get("指标名称"),
                    "metric_id": m.get("id"),
                    "status": m.get("status"),
                })

        lineage_data["related_metrics"] = metric_relations
        lineage_data["related_metric_count"] = len(metric_relations)
        return lineage_data

    def get_downstream_impact(self, field_name: str) -> dict[str, Any]:
        """分析字段变更对下游的影响。

        跨模块数据流: field_lineage -> query_builder, data_service, report_template
        """
        lineage_data = store.get_field_lineage(field_name)

        # 查找下游依赖
        affected_queries = []
        affected_services = []
        affected_reports = []

        for tmpl in store.rows("query_builder"):
            if field_name in str(tmpl.get("关联指标", "")) or field_name in str(tmpl.get("维度组合", "")):
                affected_queries.append(tmpl.get("模板名称"))

        for api in store.rows("data_service"):
            if field_name in str(api.get("关联指标或模型", "")):
                affected_services.append(api.get("接口名称"))

        for report in store.rows("report_template"):
            if field_name in str(report.get("包含指标集", "")):
                affected_reports.append(report.get("模板名称"))

        return {
            "field": field_name,
            "lineage_depth": lineage_data.get("depth", 0) if lineage_data else 0,
            "downstream_queries": affected_queries,
            "affected_queries_count": len(affected_queries),
            "downstream_services": affected_services,
            "affected_services_count": len(affected_services),
            "downstream_reports": affected_reports,
            "affected_reports_count": len(affected_reports),
            "total_downstream_impact": len(affected_queries) + len(affected_services) + len(affected_reports),
        }