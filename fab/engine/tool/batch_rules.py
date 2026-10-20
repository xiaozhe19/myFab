"""组批规则。"""

from __future__ import annotations

import re

from fab.model.entities import FabModel, LotState, ToolGroupSpec, ToolState


class BatchRules:
    """两个lot是否可以同炉 一个lot算多少组批数量"""
    def __init__(self, model: FabModel):
        self.model = model
        #机器id -> 机器组
        self._group_by_tool_id = {
            tool_id : model.tool_groups.get(spec.tool_group_id or "")
            for tool_id, spec in model.tools.items()
        }

        self._batch_criteria_by_group: dict[str, dict[str, bool]] = {}
        for group_id, group in self.model.tool_groups.items():
            criterion = (group.batch_criterion or "").lower() 
            normalized = re.sub(r"[^a-z0-9]+", "", criterion) #去除空格
        
            self._batch_criteria_by_group[group_id] = {
                "same_product": "sameproduct" in normalized,
                "same_part_family": (
                    "samepartfam" in normalized or "samepartfamily" in normalized
                    ),
                "same_step": ("samestepname" in normalized or "samestep" in normalized),
                }


    def member_quantity(self, tool:ToolState, lot:LotState) -> int:
        """按toolgroups中BATCHING UNIT属性 换算为组批数量"""
        # 计算当前组批工具的计量单位
        group = self._group_by_tool_id[tool.tool_id]
        unit = (group.batching_unit or "wafer").strip().lower() if group else "wafer"
        if "wafer" in unit:
            return lot.wafer_count
        if "lot" in unit:
            return 1
        raise ValueError(
            f"不支持的组批单位：{group.batching_unit if group else unit}。"
        )

    def criteria(self, group:ToolGroupSpec | None) -> dict[str, bool]:
        if group is None:
            return {
                "same_product": False,
                "same_part_family":False,
                "same_step": False,
            }
        return self._batch_criteria_by_group[group.id]

    def part_family(self, product_id: str) -> str:
        """如果有产品族就输出 没有就输出产品ID"""
        product = self.model.products.get(product_id)
        if product is None:
            return product_id
        return product.part_family or product.id

    def same_batch_class(
        self, tool: ToolState, first: LotState, candidate: LotState
    ) -> bool:
        """判断两个 lot 是否满足工具组的组批条件。"""
        #选中一个lot，不断地和队列中剩余等待的lot比较是否能一批加工
        #TODO：是否可以通过分桶存放建立索引来避免循环比较？

        first_step = first.current_step
        candidate_step = candidate.current_step

        if first_step is None or candidate_step is None:
            return False
        if candidate_step.processing_unit != "batch":
            return False
        if candidate_step.tool_group_id != first_step.tool_group_id:
            return False
        
        group = self._group_by_tool_id[tool.tool_id]
        criteria = self.criteria(group)

        if criteria["same_product"] and candidate.product_id != first.product_id:
            return False
        if criteria["same_part_family"] and self.part_family(
            candidate.product_id
        ) != self.part_family(first.product_id):
            return False
        if criteria["same_step"]:
            return candidate_step.id == first_step.id
        if not any(criteria.values()):
            # 未声明 criterion 时也不允许把两道不同工艺混在一炉。
            return candidate_step == first_step
        return True

    def class_key(self, lot: LotState) -> tuple[object, ...]:
        """返回一炉内必须相同的静态属性。"""

        step = lot.current_step
        if step is None:
            return (None,)
        group = self.model.tool_groups.get(step.tool_group_id or "")
        criteria = self.criteria(group)
        key: list[object] = [step.tool_group_id]
        if criteria["same_product"]:
            key.append(lot.product_id)
        if criteria["same_part_family"]:
            key.append(self.part_family(lot.product_id))
        if criteria["same_step"]:
            key.append(step.id)
        elif not any(criteria.values()):
            key.append(step)
        return tuple(key)


