"""口径重算引擎领域服务：根据新口径版本重算历史数据。

跨模块数据流: metric_definition -> caliber_version -> field_lineage -> recalc_engine
             recalc_engine -> query_builder(缓存失效)

一次月结申请的日期范围可能跨越不同版本的字段转换口径。重算前必须先做
“重算依赖核验”：读取 field_lineage，按日期分段给出可执行来源和缺口；
只有来源可核验的日期才允许确认差异。已确认的日期写入重算历史，重试时
按“任务ID + 计量日期”跳过，保证同一天最多计入一次。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any

from app.store import store
from app.services.base import BaseModuleService

_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])v\d+(?:\.\d+)?", re.IGNORECASE)
_MAX_RANGE_DAYS = 3660
_CONFIRMED = "已确认"


def _parse_dates(text: Any) -> list[date]:
    """从一段文本里按出现顺序解析日期，支持 2026-11-01 / 2026年11月1日。"""
    dates: list[date] = []
    for year, month, day in _DATE_RE.findall(str(text or "")):
        try:
            dates.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return dates


def _parse_date(text: Any) -> date | None:
    dates = _parse_dates(text)
    return dates[0] if dates else None


def _split_fields(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,，、;；\n]+", text) if part.strip()]


class RecalcEngineService(BaseModuleService):
    MODULE = "recalc_engine"
    ENTITY = "重算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关联指标", "新口径版本", "历史时间范围", "重算状态", "产出差异"]
    REQUIRED_FIELDS = ["关联指标", "新口径版本", "历史时间范围"]
    STATUS_ORDER = ["待调度", "重算中", "已完成", "重算失败"]
    ACTION_RULES = {"启动重算": "重算中", "取消重算": "重算失败"}
    NEGATIVE_ACTIONS = []

    # ── 日期范围 ──

    def _parse_range(self, time_range: str) -> tuple[date, date] | tuple[None, None]:
        dates = _parse_dates(time_range)
        if not dates:
            return None, None
        start, end = dates[0], dates[-1]
        if start > end:
            start, end = end, start
        if (end - start).days + 1 > _MAX_RANGE_DAYS:
            return None, None
        return start, end

    @staticmethod
    def _iter_dates(start: date, end: date) -> list[date]:
        return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

    # ── 来源解析 ──

    def _lineage_rows(self, metric_name: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows("field_lineage")
            if str(row.get("目标字段", "")).strip() == metric_name
        ]
        if rows:
            return rows
        # 兼容“目标字段”写成指标名一部分的历史登记
        return [
            row for row in store.rows("field_lineage")
            if metric_name and metric_name in str(row.get("目标字段", ""))
        ]

    def _source_from_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """把一条血缘登记整理成可执行的来源；不可执行时返回 None。

        生效区间优先取显式字段；历史登记没有这些字段时，从“转换逻辑/中间表”
        文本里解析日期和版本，保证通过通用登记接口补录的来源也能生效。
        """
        if row.get("status") != "已建立":
            return None
        fields = _split_fields(row.get("来源字段列表"))
        if not fields:
            return None
        transform = str(row.get("转换逻辑", ""))
        intermediate = str(row.get("中间表", ""))
        version = str(row.get("适用版本") or "").strip()
        if not version:
            match = _VERSION_RE.search(transform) or _VERSION_RE.search(intermediate)
            version = match.group(0) if match else ""
        start = _parse_date(row.get("生效开始"))
        end = _parse_date(row.get("生效结束"))
        if start is None and end is None:
            text_dates = _parse_dates(transform) or _parse_dates(intermediate)
            if len(text_dates) == 1:
                start = end = text_dates[0]
            elif len(text_dates) >= 2:
                start, end = text_dates[0], text_dates[1]
        if start and end and start > end:
            start, end = end, start
        return {
            "血缘ID": int(row.get("id", 0)),
            "适用版本": version,
            "来源字段列表": fields,
            "转换逻辑": transform,
            "中间表": intermediate,
            "开始": start,
            "结束": end,
            "覆盖天数": (end - start).days if start and end else _MAX_RANGE_DAYS,
        }

    @staticmethod
    def _source_applies(source: dict[str, Any], day: date) -> bool:
        start, end = source.get("开始"), source.get("结束")
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def _resolve_source(
        self, sources: list[dict[str, Any]], day: date, target_version: str
    ) -> dict[str, Any] | None:
        candidates = [source for source in sources if self._source_applies(source, day)]
        if not candidates:
            return None
        candidates.sort(
            key=lambda source: (
                0 if source["适用版本"] and source["适用版本"] == target_version else 1,
                source["覆盖天数"],
                -source["血缘ID"],
            )
        )
        return candidates[0]

    def _gap_reason(self, rows: list[dict[str, Any]], day: date) -> str:
        if not rows:
            return f"{day.isoformat()} 未登记该指标的字段血缘，无可用来源"
        usable = [row for row in rows if row.get("status") == "已建立"
                  and _split_fields(row.get("来源字段列表"))]
        if not usable:
            states = "、".join(sorted({str(row.get("status", "未知")) for row in rows}))
            return f"{day.isoformat()} 字段血缘状态不可用（{states}），无可用来源"
        return f"{day.isoformat()} 无适用的字段转换来源，需补充当日依据"

    # ── 版本历史 ──

    def _metric_id(self, metric_name: str) -> int:
        metric = next(
            (row for row in store.rows("metric_definition")
             if row.get("指标名称") == metric_name),
            None,
        )
        return int(metric.get("id", 0)) if metric else 0

    def _caliber_versions(self, metric_name: str) -> list[dict[str, Any]]:
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        return sorted(calibers, key=lambda row: str(row.get("生效时间", "")))

    def _version_span(self, metric_name: str, new_version: str) -> tuple[str, str]:
        """返回（旧口径, 新口径）；旧口径取新版本之前最近的一个版本。"""
        calibers = self._caliber_versions(metric_name)
        versions = [str(row.get("版本号", "")) for row in calibers if row.get("版本号")]
        if new_version in versions:
            index = versions.index(new_version)
            if index > 0:
                return versions[index - 1], new_version
        active = next((row for row in calibers if row.get("status") == "已生效"), None)
        old = str(active.get("版本号", "")) if active else (versions[0] if versions else "unknown")
        return old or "unknown", new_version

    # ── 历史记录 ──

    def _task_history(self, task_id: int) -> list[dict[str, Any]]:
        return [
            record for record in store.recalc_history()
            if int(record.get("任务ID", -1)) == task_id
            and record.get("状态", _CONFIRMED) == _CONFIRMED
        ]

    def _diff_for(self, metric_name: str, day: date, version: str) -> float:
        """按指标 + 日期 + 口径版本生成稳定的模拟差异。"""
        seed = f"{metric_name}|{day.isoformat()}|{version}".encode("utf-8")
        digest = int(hashlib.sha256(seed).hexdigest()[:8], 16)
        return round(0.5 + (digest % 451) / 100, 2)

    # ── 依赖核验 ──

    def verify_dependencies(self, entry_id: int) -> dict[str, Any]:
        """核验重算依赖：读取字段血缘，按日期分段列出可执行来源与缺口。

        跨模块数据流: field_lineage -> recalc_engine
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = str(task.get("关联指标", "")).strip()
        target_version = str(task.get("新口径版本", "")).strip()
        time_range = str(task.get("历史时间范围", "")).strip()

        start, end = self._parse_range(time_range)
        if start is None or end is None:
            return {
                "error": f"重算任务 {entry_id} 的历史时间范围无法解析：{time_range or '空'}",
                "任务ID": entry_id,
                "关联指标": metric_name,
                "历史时间范围": time_range,
            }

        rows = self._lineage_rows(metric_name)
        sources = [source for row in rows if (source := self._source_from_row(row))]

        history = self._task_history(entry_id)
        confirmed_by_date: dict[str, dict[str, Any]] = {
            str(record.get("计量日期")): record for record in history
        }
        count_by_date: dict[str, int] = {}
        for record in history:
            day_text = str(record.get("计量日期"))
            count_by_date[day_text] = count_by_date.get(day_text, 0) + 1

        days = self._iter_dates(start, end)
        date_details: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        missing_dates: list[str] = []

        def flush(segment_days: list[dict[str, Any]]) -> None:
            if not segment_days:
                return
            first = segment_days[0]
            last = segment_days[-1]
            readable = (
                first["计量日期"] if first["计量日期"] == last["计量日期"]
                else f"{first['计量日期']} 至 {last['计量日期']}"
            )
            segments.append({
                "区间": readable,
                "开始日期": first["计量日期"],
                "结束日期": last["计量日期"],
                "天数": len(segment_days),
                "日期列表": [item["计量日期"] for item in segment_days],
                "可复核": bool(first["可复核"]),
                "适用版本": first.get("适用版本", ""),
                "来源字段列表": first.get("来源字段列表", []),
                "转换逻辑": first.get("转换逻辑", ""),
                "中间表": first.get("中间表", ""),
                "血缘ID": first.get("血缘ID"),
                "缺口": first.get("缺口"),
            })

        current_key: tuple[Any, Any] | None = None
        current_days: list[dict[str, Any]] = []

        for day in days:
            key_text = day.isoformat()
            source = self._resolve_source(sources, day, target_version)
            confirmed = confirmed_by_date.get(key_text)
            detail: dict[str, Any] = {
                "计量日期": key_text,
                "可复核": source is not None,
                "适用版本": source["适用版本"] if source else "",
                "来源字段列表": source["来源字段列表"] if source else [],
                "转换逻辑": source["转换逻辑"] if source else "",
                "中间表": source["中间表"] if source else "",
                "血缘ID": source["血缘ID"] if source else None,
                "已确认": confirmed is not None,
                "差异": confirmed.get("差异") if confirmed else None,
                "计入次数": count_by_date.get(key_text, 0),
                "缺口": None if source else self._gap_reason(rows, day),
            }
            date_details.append(detail)
            if source is None:
                missing_dates.append(key_text)

            segment_key = (
                ("source", source["血缘ID"], source["适用版本"], source["转换逻辑"])
                if source else ("gap",)
            )
            if current_key is not None and segment_key != current_key:
                flush(current_days)
                current_days = []
            current_key = segment_key
            current_days.append(detail)

        flush(current_days)

        confirmed_segments = self._confirmed_segments(date_details)
        missing_count = len(missing_dates)
        confirmed_count = len(confirmed_by_date)
        status = "有缺口" if missing_count else "完整"

        result: dict[str, Any] = {
            "任务ID": entry_id,
            "关联指标": metric_name,
            "新口径版本": target_version,
            "历史时间范围": time_range,
            "开始日期": start.isoformat(),
            "结束日期": end.isoformat(),
            "依赖状态": status,
            "可复核天数": len(days) - missing_count,
            "缺口天数": missing_count,
            "已确认天数": confirmed_count,
            "待确认天数": len(days) - missing_count - confirmed_count,
            "分段": segments,
            "按日核验": date_details,
            "缺口日期": missing_dates,
            "已确认区间": confirmed_segments,
            "按日计入次数": {
                item["计量日期"]: item["计入次数"] for item in date_details
            },
            "产出差异": task.get("产出差异", ""),
            "状态": task.get("status", ""),
        }

        task["重算依赖明细"] = {
            key: result[key]
            for key in ("任务ID", "关联指标", "新口径版本", "历史时间范围",
                        "开始日期", "结束日期", "依赖状态", "缺口日期", "分段")
        }
        task["依赖核验时间"] = date.today().isoformat()
        return result

    def _confirmed_segments(
        self, date_details: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把已确认日期归并成连续区间，便于按区间复核差异。"""
        segments: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        current_key: tuple[Any, Any] | None = None

        def flush() -> None:
            if not current:
                return
            first, last = current[0], current[-1]
            readable = (
                first["计量日期"] if first["计量日期"] == last["计量日期"]
                else f"{first['计量日期']} 至 {last['计量日期']}"
            )
            segments.append({
                "区间": readable,
                "天数": len(current),
                "适用版本": first.get("适用版本", ""),
                "来源字段列表": first.get("来源字段列表", []),
                "转换逻辑": first.get("转换逻辑", ""),
                "差异合计": round(sum(float(item.get("差异") or 0) for item in current), 2),
                "日期列表": [item["计量日期"] for item in current],
            })

        for detail in date_details:
            if not detail["已确认"]:
                if current:
                    flush()
                current, current_key = [], None
                continue
            key = (detail.get("血缘ID"), detail.get("适用版本"))
            if current and key != current_key:
                flush()
                current = []
            current_key = key
            current.append(detail)
        flush()
        return segments

    # ── 重算执行 ──

    def start_recalc(self, entry_id: int) -> dict[str, Any] | None:
        """执行/继续重算：仅确认来源可核验的日期，已确认日期不重复计入。

        跨模块数据流: field_lineage -> recalc_engine -> query_builder(缓存失效)
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        verification = self.verify_dependencies(entry_id)
        if "error" in verification:
            return verification

        metric_name = verification["关联指标"]
        target_version = verification["新口径版本"]
        time_range = verification["历史时间范围"]
        old_version, _ = self._version_span(metric_name, target_version)
        metric_id = self._metric_id(metric_name)

        already_confirmed = {item["计量日期"] for item in verification["按日核验"]
                             if item["已确认"]}
        new_dates: list[str] = []
        skipped_dates: list[str] = []

        task["status"] = "重算中"
        task["pending"] = True

        for detail in verification["按日核验"]:
            day_text = detail["计量日期"]
            if not detail["可复核"]:
                # 缺口日期只登记缺口，绝不生成已确认差异
                continue
            if day_text in already_confirmed:
                skipped_dates.append(day_text)
                continue

            diff = self._diff_for(metric_name, date.fromisoformat(day_text), target_version)
            store.record_recalc(
                metric_id=metric_id,
                old_version=old_version,
                new_version=target_version,
                time_range=time_range,
                diff_summary=f"差异 {diff:.2f}%",
                任务ID=entry_id,
                关联指标=metric_name,
                计量日期=day_text,
                适用版本=detail["适用版本"],
                来源字段=detail["来源字段列表"],
                转换逻辑=detail["转换逻辑"],
                血缘ID=detail["血缘ID"],
                差异=diff,
                状态=_CONFIRMED,
            )
            new_dates.append(day_text)

        # 以重算历史为准回算状态和差异，保证重试不会重复累加
        history = self._task_history(entry_id)
        confirmation = self.verify_dependencies(entry_id)
        confirmed_dates = sorted({str(record.get("计量日期")) for record in history})
        total_diff = round(sum(float(record.get("差异") or 0) for record in history), 2)
        missing_dates = confirmation["缺口日期"]

        if missing_dates:
            status = "重算中" if history else "重算失败"
            pending = True
            abnormal = not history
        else:
            status = "已完成"
            pending = False
            abnormal = False

        task["status"] = status
        task["pending"] = pending
        task["abnormal"] = abnormal
        task["重算状态"] = status
        summary = f"已确认 {len(confirmed_dates)} 天，累计差异 {total_diff:.2f}%"
        if missing_dates:
            summary += f"；缺 {len(missing_dates)} 天待补来源（{'、'.join(missing_dates)}）"
        task["产出差异"] = summary
        task["最近重算时间"] = date.today().isoformat()

        # 失效该指标所有缓存
        from app.services.cache_layer import CacheLayerService
        cache_svc = CacheLayerService()
        invalidation = cache_svc.invalidate_by_metric(metric_name)

        return {
            "任务ID": entry_id,
            "关联指标": metric_name,
            "旧口径": old_version,
            "新口径版本": target_version,
            "历史时间范围": time_range,
            "本次确认日期": new_dates,
            "本次跳过已确认": skipped_dates,
            "已确认天数": len(confirmed_dates),
            "已确认日期": confirmed_dates,
            "缺口日期": missing_dates,
            "已确认区间": confirmation["已确认区间"],
            "产出差异": task["产出差异"],
            "状态": status,
            "缓存失效条目": invalidation.get("invalidated_entries", 0),
            "核验明细": confirmation,
        }

    # ── 动作与历史 ──

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """启动重算走依赖核验流程；取消沿用通用状态流转。"""
        if action == "启动重算":
            result = self.start_recalc(entry_id)
            if result is None or "error" in result:
                message = result.get("error") if result else "重算任务不存在"
                return None, str(message)
            entry = store.find(self.MODULE, entry_id)
            message = (
                f"重算已执行：本次确认 {len(result['本次确认日期'])} 天，"
                f"累计已确认 {result['已确认天数']} 天"
            )
            if result["缺口日期"]:
                message += f"，{len(result['缺口日期'])} 天待补来源"
            return entry, message
        return super().run_action(entry_id, action)

    def get_recalc_history(
        self, metric_name: str | None = None, task_id: int | None = None
    ) -> list[dict[str, Any]]:
        """获取重算历史记录，可按指标或任务过滤。"""
        history = store.recalc_history()
        if metric_name:
            history = [h for h in history if h.get("关联指标") == metric_name]
        if task_id is not None:
            history = [h for h in history if int(h.get("任务ID", -1)) == task_id]
        return history

    def estimate_recalc_impact(self, entry_id: int) -> dict[str, Any] | None:
        """预估算重算影响范围。

        跨模块数据流: recalc_engine -> query_builder（预估缓存失效范围）
                     recalc_engine -> data_service（预估接口影响）
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")

        # 查找关联的查询模板和服务接口
        queries = store.find_by_field("query_builder", "关联指标", metric_name)
        services = [row for row in store.rows("data_service")
                    if metric_name in str(row.get("关联指标或模型", ""))]
        subscriptions = store.find_by_field("data_subscription", "关联指标", metric_name)

        return {
            "metric": metric_name,
            "affected_query_templates": len(queries),
            "affected_service_apis": len(services),
            "affected_subscriptions": len(subscriptions),
            "estimated_cache_invalidation": len([q for q in queries if q.get("缓存策略")]),
            "recommended_action": "confirm_and_proceed",
        }
