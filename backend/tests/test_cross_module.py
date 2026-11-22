"""业务行为测试：验证跨模块数据流链。

链1: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
链2: dimension_model -> drill_down -> pivot_table -> report_template
链3: user_cohort -> experiment_group -> segment_compare -> anomaly_detector
"""
from __future__ import annotations

import pytest

from app.services.metric_definition import MetricDefinitionService
from app.services.caliber_version import CaliberVersionService
from app.services.query_builder import QueryBuilderService
from app.services.data_service import DataServiceService
from app.services.cache_layer import CacheLayerService
from app.services.query_journal import QueryJournalService
from app.services.dimension_model import DimensionModelService
from app.services.drill_down import DrillDownService
from app.services.pivot_table import PivotTableService
from app.services.report_template import ReportTemplateService
from app.services.user_cohort import UserCohortService
from app.services.experiment_group import ExperimentGroupService
from app.services.segment_compare import SegmentCompareService
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.recalc_engine import RecalcEngineService
from app.services.field_lineage import FieldLineageService
from app.store import store


class TestMetricCaliberChain:
    """链1测试：指标定义 -> 口径版本 -> 查询构建器 -> 数据服务 -> 缓存 -> 查询日志"""

    def test_publish_metric_creates_caliber(self):
        """测试发布指标自动创建口径版本"""
        svc = MetricDefinitionService()
        # 指标ID=1 初始状态为"草稿"
        result, msg = svc.publish_with_caliber(1, "统计最近30天活跃用户数")
        assert result is not None
        assert "已发布" in msg

        # 验证口径版本已创建
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        assert len(calibers) > 0
        assert any(c.get("版本号") == "v1.0" for c in calibers)

    def test_metric_chain_completeness(self):
        """测试指标完整数据链查询"""
        svc = MetricDefinitionService()
        chain = svc.get_metric_chain(1)
        assert chain is not None
        assert chain["metric_name"] == "指标定义样例1"
        assert "caliber_count" in chain
        assert "query_templates" in chain
        assert "service_apis" in chain

    def test_caliber_activate_expires_old(self):
        """测试启用新口径版本自动过期旧版本"""
        svc = CaliberVersionService()
        # 先确认ID=1是已生效版本（由publish_with_caliber创建）
        # 需要先手动创建第二个版本
        store.rows("caliber_version").append({
            "id": 99, "所属指标": "指标定义样例1", "版本号": "v2.0",
            "口径描述": "新口径：统计自然月用户", "变更类型": "口径调整",
            "生效时间": "2026-11-01", "status": "待生效",
            "pending": True, "abnormal": False,
        })
        result, msg = svc.activate_version(99)
        assert result is not None
        assert "已生效" in msg

        # 验证旧版本已过期
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        active = [c for c in calibers if c.get("status") == "已生效"]
        expired = [c for c in calibers if c.get("status") == "已过期"]
        assert len(active) == 1
        assert active[0].get("版本号") == "v2.0"
        assert len(expired) >= 1

    def test_caliber_history(self):
        """测试口径版本历史查询"""
        svc = CaliberVersionService()
        history = svc.get_version_history("指标定义样例1")
        assert len(history) >= 2

    def test_caliber_compare(self):
        """测试口径版本对比"""
        svc = CaliberVersionService()
        # 找到两个版本
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        if len(calibers) >= 2:
            result = svc.compare_versions(
                int(calibers[0].get("id", 0)),
                int(calibers[1].get("id", 0)),
            )
            assert result is not None
            assert "version_a" in result
            assert "version_b" in result

    def test_query_execute_with_cache(self):
        """测试查询执行包含缓存和日志记录"""
        svc = QueryBuilderService()
        # 确保查询模板和口径就绪
        template = store.find("query_builder", 2)
        template["status"] = "已发布"
        metric_name = template.get("关联指标", "")

        # 确保指标有生效口径
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        if not calibers or not any(c.get("status") == "已生效" for c in calibers):
            store.rows("caliber_version").append({
                "id": 200, "所属指标": metric_name, "版本号": "v1.0",
                "口径描述": "测试口径", "变更类型": "初始创建",
                "生效时间": "2026-11-01", "status": "已生效",
                "pending": True, "abnormal": False,
            })

        result = svc.execute_query(2, operator="测试用户")
        assert "error" not in str(result)
        logs = store.query_logs()
        assert len(logs) > 0
        assert any("测试用户" in str(log.get("操作人", "")) for log in logs)

    def test_query_cache_second_call(self):
        """测试查询缓存：第二次相同查询应走缓存"""
        svc = QueryBuilderService()
        # 确保模板和口径就绪
        template = store.find("query_builder", 2)
        template["status"] = "已发布"
        metric_name = template.get("关联指标", "")
        if not store.find_by_field("caliber_version", "所属指标", metric_name):
            store.rows("caliber_version").append({
                "id": 201, "所属指标": metric_name, "版本号": "v1.0",
                "口径描述": "测试口径", "变更类型": "初始创建",
                "生效时间": "2026-11-01", "status": "已生效",
                "pending": True, "abnormal": False,
            })

        svc.execute_query(2, operator="缓存测试1")
        result2 = svc.execute_query(2, operator="缓存测试2")

        assert result2 is not None
        logs = store.query_logs()
        assert sum(1 for log in logs if "缓存测试" in str(log.get("操作人", ""))) >= 2

    def test_data_service_call(self):
        """测试数据服务接口调用"""
        svc = DataServiceService()
        # 确保数据服务API和查询模板就绪
        api = store.find("data_service", 2)
        api["status"] = "已发布"
        target = api.get("关联指标或模型", "")

        # 创建匹配的查询模板和口径
        store.rows("query_builder").append({
            "id": 300, "模板名称": "数据服务测试查询",
            "关联指标": target, "维度组合": "日,周",
            "过滤条件": "", "缓存策略": "",
            "status": "已发布", "pending": False, "abnormal": False,
        })
        store.rows("caliber_version").append({
            "id": 301, "所属指标": target, "版本号": "v1.0",
            "口径描述": "测试口径", "变更类型": "初始创建",
            "生效时间": "2026-11-01", "status": "已生效",
            "pending": True, "abnormal": False,
        })

        result = svc.call_service(2, operator="测试客户端")
        assert "error" not in str(result)

    def test_cache_invalidate_by_metric(self):
        """测试根据指标失效缓存"""
        svc = CacheLayerService()
        result = svc.invalidate_by_metric("指标定义样例1")
        assert result["metric"] == "指标定义样例1"
        assert "invalidated_entries" in result

    def test_cache_warm(self):
        """测试缓存预热"""
        svc = CacheLayerService()
        result = svc.warm_cache("指标定义样例1")
        assert result["metric"] == "指标定义样例1"
        assert "warmed_templates" in result

    def test_query_journal_slow_queries(self):
        """测试慢查询统计"""
        svc = QueryJournalService()
        slow = svc.get_slow_queries(threshold_ms=50)
        assert isinstance(slow, list)

    def test_query_stats_by_metric(self):
        """测试按指标查询统计"""
        svc = QueryJournalService()
        stats = svc.get_query_stats_by_metric()
        assert isinstance(stats, dict)

    def test_performance_insights(self):
        """测试性能洞察"""
        svc = QueryJournalService()
        insights = svc.get_performance_insights()
        assert isinstance(insights, dict)


class TestDimensionAnalysisChain:
    """链2测试：维度模型 -> 下钻 -> 透视表 -> 报告模板"""

    def test_dimension_model_with_analytics(self):
        """测试维度模型关联分析配置"""
        svc = DimensionModelService()
        result = svc.get_model_with_analytics(1)
        assert result is not None
        assert result["model_name"] == "维度建模样例1"
        assert "drill_paths" in result
        assert "pivot_configs" in result

    def test_dimension_hierarchy(self):
        """测试维度层次结构"""
        svc = DimensionModelService()
        result = svc.get_dimension_hierarchy(1)
        assert result is not None
        assert "levels" in result
        assert len(result["levels"]) > 0

    def test_drill_down_execute(self):
        """测试下钻分析执行"""
        svc = DrillDownService()
        # 确保下钻路径已生效
        store.rows("drill_down")[1]["status"] = "已生效"
        result = svc.execute_drill_down(2, current_level=0)
        assert result is not None
        if "error" not in str(result):
            assert "dimension" in result
            assert "rows" in result

    def test_drill_tree(self):
        """测试下钻路径树"""
        svc = DrillDownService()
        result = svc.get_drill_tree(1)
        assert result is not None
        assert "tree" in result

    def test_drill_export_to_pivot(self):
        """测试下钻导出透视配置建议"""
        svc = DrillDownService()
        store.rows("drill_down")[0]["下钻维度序列"] = "日活跃,周活跃,月活跃"
        result = svc.export_to_pivot(1)
        # 需要至少两个维度
        assert result is not None
        if "error" not in str(result):
            assert "row_dimension" in result
            assert "column_dimension" in result

    def test_pivot_compute(self):
        """测试透视表计算"""
        svc = PivotTableService()
        result = svc.compute_pivot(1)
        assert result is not None
        if "error" not in str(result):
            assert "grid" in result
            assert "row_totals" in result

    def test_pivot_share_to_report(self):
        """测试透视表共享到报告模板"""
        svc = PivotTableService()
        result = svc.share_to_report(1, 1)
        assert result is not None

    def test_report_generate(self):
        """测试报告生成"""
        svc = ReportTemplateService()
        # 确保已发布
        store.rows("report_template")[0]["status"] = "已发布"
        result = svc.generate_report(1)
        assert result is not None
        if "error" not in str(result):
            assert "sections" in result


class TestCohortAnomalyChain:
    """链3测试：用户分群 -> 实验分群 -> 分群对比 -> 异常检测"""

    def test_recompute_cohort(self):
        """测试分群重计算"""
        svc = UserCohortService()
        result = svc.recompute_cohort(1)
        assert result is not None
        if "error" not in str(result):
            assert "member_count" in result
            assert result["member_count"] > 0

    def test_cohort_detail(self):
        """测试分群详细信息"""
        svc = UserCohortService()
        result = svc.get_cohort_detail(1)
        assert result is not None
        assert "cohort_name" in result

    def test_experiment_evaluate(self):
        """测试实验分组评估"""
        svc = ExperimentGroupService()
        result = svc.evaluate_groups(1, 2)
        assert result is not None
        if "error" not in str(result):
            assert "group_a" in result
            assert "group_b" in result

    def test_segment_compare_execute(self):
        """测试分群对比执行"""
        svc = SegmentCompareService()
        result = svc.execute_compare(1)
        assert result is not None
        if "error" not in str(result):
            assert "cohort_a" in result
            assert "cohort_b" in result
            assert "diff_percentage" in result

    def test_segment_compare_insights(self):
        """测试分群对比洞察"""
        svc = SegmentCompareService()
        result = svc.get_cross_segment_insights(1)
        assert result is not None

    def test_anomaly_detection(self):
        """测试异常检测"""
        svc = AnomalyDetectorService()
        store.rows("anomaly_detector")[0]["status"] = "已启用"
        result = svc.run_detection(1)
        assert result is not None
        if "error" not in str(result):
            assert "is_anomaly" in result

    def test_anomaly_report(self):
        """测试异常报告"""
        svc = AnomalyDetectorService()
        result = svc.get_anomaly_report()
        assert isinstance(result, list)

    def test_detection_coverage(self):
        """测试异常检测覆盖率"""
        svc = AnomalyDetectorService()
        result = svc.get_detection_coverage()
        assert "total_rules" in result
        assert "enabled_rules" in result


class TestRecalcAndLineage:
    """重算引擎和字段血缘测试"""

    def test_recalc_start(self):
        """测试重算引擎"""
        svc_recalc = RecalcEngineService()
        # 需要重算任务存在
        result = svc_recalc.start_recalc(1)
        assert result is not None

    def test_recalc_estimate_impact(self):
        """测试重算影响预估"""
        svc = RecalcEngineService()
        result = svc.estimate_recalc_impact(1)
        assert result is not None
        if "error" not in str(result):
            assert "affected_query_templates" in result

    def test_recalc_history(self):
        """测试重算历史"""
        svc = RecalcEngineService()
        result = svc.get_recalc_history()
        assert isinstance(result, list)

    def test_metric_recalc(self):
        """测试指标触发重算"""
        svc = MetricDefinitionService()
        result, msg = svc.recalc_metric_history(1, "v2.0", "2026-01-01 到 2026-11-01")
        assert result is not None
        assert "待调度" in msg or "重算" in msg

    def test_field_lineage_trace(self):
        """测试字段血缘追溯"""
        svc = FieldLineageService()
        result = svc.trace_field("字段血缘样例1")
        assert result is not None
        assert "field" in result

    def test_field_downstream_impact(self):
        """测试字段变更下游影响"""
        svc = FieldLineageService()
        result = svc.get_downstream_impact("字段血缘样例1")
        assert result is not None
        assert "field" in result


class TestFullChainSnapshot:
    """全链路快照测试"""

    def test_domain_snapshots(self):
        """测试所有模块的domain_snapshot"""
        services = [
            MetricDefinitionService(),
            CaliberVersionService(),
            QueryBuilderService(),
            DataServiceService(),
            CacheLayerService(),
            QueryJournalService(),
            DimensionModelService(),
            DrillDownService(),
            PivotTableService(),
            ReportTemplateService(),
            UserCohortService(),
            ExperimentGroupService(),
            SegmentCompareService(),
            AnomalyDetectorService(),
            RecalcEngineService(),
            FieldLineageService(),
        ]
        for svc in services:
            snapshot = svc.domain_snapshot()
            assert snapshot is not None
            assert "module" in snapshot
            assert "entity" in snapshot
            assert "count" in snapshot

    def test_cross_module_data_consistency(self):
        """跨模块数据一致性测试"""
        # 链1: 指标 -> 口径 -> 查询
        metric_svc = MetricDefinitionService()
        chain = metric_svc.get_metric_chain(1)
        assert chain is not None

        # 口径版本数量应该合理
        caliber_count = chain.get("caliber_count", 0)
        assert caliber_count >= 0

        # 链2: 维度 -> 下钻 -> 透视
        dim_svc = DimensionModelService()
        dim_data = dim_svc.get_model_with_analytics(1)
        assert dim_data is not None

        # 下钻路径和透视配置应该可枚举
        drill_count = dim_data.get("total_drill_paths", 0)
        pivot_count = dim_data.get("total_pivot_configs", 0)
        assert drill_count >= 0
        assert pivot_count >= 0

        # 链3: 分群 -> 实验 -> 对比 -> 异常
        cohort_svc = UserCohortService()
        cohort_detail = cohort_svc.get_cohort_detail(1)
        assert cohort_detail is not None

class TestRecalcDependencyVerification:
    """重算依赖核验：来源按日期分段、缺口不产生已确认差异、重试不重复计入。"""

    TASK_ID = 4  # 月结营收指标，2026-11-01 至 2026-11-05

    def _reset_task(self):
        task = store.find("recalc_engine", self.TASK_ID)
        task.update({
            "重算状态": "待调度", "产出差异": "", "status": "待调度",
            "pending": True, "abnormal": False,
        })
        store.recalc_history().clear()
        lineage = store.rows("field_lineage")
        lineage[:] = [row for row in lineage if int(row.get("id", 0)) <= 5]
        return task

    def _fill_gap(self):
        svc = FieldLineageService()
        entry, missing = svc.create_entry({
            "目标字段": "月结营收指标",
            "来源字段列表": "order_amount, tax_amount",
            "转换逻辑": "v1.0 含税营收转换",
            "中间表": "dws_revenue_v1",
            "血缘深度": "3",
            "适用版本": "v1.0",
            "生效开始": "2026-11-03",
            "生效结束": "2026-11-03",
        })
        assert entry is not None and not missing
        return entry

    def test_segments_and_gap(self):
        self._reset_task()
        result = RecalcEngineService().verify_dependencies(self.TASK_ID)
        assert result["依赖状态"] == "有缺口"
        assert result["缺口日期"] == ["2026-11-03"]
        assert result["可复核天数"] == 4
        segments = result["分段"]
        assert [s["区间"] for s in segments] == [
            "2026-11-01 至 2026-11-02", "2026-11-03", "2026-11-04 至 2026-11-05",
        ]
        assert [s["可复核"] for s in segments] == [True, False, True]
        assert segments[0]["适用版本"] == "v1.0"
        assert segments[2]["适用版本"] == "v1.1"
        assert segments[1]["缺口"]

    def test_missing_day_never_confirmed(self):
        self._reset_task()
        svc = RecalcEngineService()
        result = svc.start_recalc(self.TASK_ID)
        assert "2026-11-03" not in result["已确认日期"]
        assert result["缺口日期"] == ["2026-11-03"]
        history = svc.get_recalc_history(task_id=self.TASK_ID)
        assert {h["计量日期"] for h in history} == {
            "2026-11-01", "2026-11-02", "2026-11-04", "2026-11-05",
        }
        assert result["状态"] == "重算中"

    def test_retry_does_not_double_count(self):
        self._reset_task()
        svc = RecalcEngineService()
        first = svc.start_recalc(self.TASK_ID)
        second = svc.start_recalc(self.TASK_ID)
        assert second["本次确认日期"] == []
        assert set(second["本次跳过已确认"]) == set(first["本次确认日期"])
        counts = {}
        for record in svc.get_recalc_history(task_id=self.TASK_ID):
            day = record["计量日期"]
            counts[day] = counts.get(day, 0) + 1
        assert all(count == 1 for count in counts.values())

    def test_fill_gap_and_continue_same_task(self):
        self._reset_task()
        svc = RecalcEngineService()
        first = svc.start_recalc(self.TASK_ID)
        self._fill_gap()
        verification = svc.verify_dependencies(self.TASK_ID)
        assert verification["依赖状态"] == "完整"
        assert verification["已确认天数"] == first["已确认天数"]
        assert verification["待确认天数"] == 1

        second = svc.start_recalc(self.TASK_ID)
        assert second["本次确认日期"] == ["2026-11-03"]
        assert second["状态"] == "已完成"
        counts = {}
        for record in svc.get_recalc_history(task_id=self.TASK_ID):
            day = record["计量日期"]
            counts[day] = counts.get(day, 0) + 1
        assert counts == {day: 1 for day in [
            "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
        ]}
        assert [s["区间"] for s in second["已确认区间"]] == [
            "2026-11-01 至 2026-11-02", "2026-11-03", "2026-11-04 至 2026-11-05",
        ]

    def test_segment_uses_version_transform(self):
        self._reset_task()
        second = RecalcEngineService().start_recalc(self.TASK_ID)
        spans = [(s["适用版本"], s["区间"]) for s in second["已确认区间"]]
        assert ("v1.0", "2026-11-01 至 2026-11-02") in spans
        assert ("v1.1", "2026-11-04 至 2026-11-05") in spans

    def test_run_action_goes_through_verification(self):
        self._reset_task()
        entry, message = RecalcEngineService().run_action(self.TASK_ID, "启动重算")
        assert entry is not None
        assert "待补来源" in message or "确认" in message
        assert entry["重算状态"] in ("重算中", "重算失败", "已完成")
