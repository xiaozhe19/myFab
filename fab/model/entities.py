"""
数据对象
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DistributionSpec:
    """SMT 文档中所有“时间/概率分布”字段的统一表示。"""

    kind: str
    mean: float
    offset: float = 0.0
    unit: str = "minute"


@dataclass(frozen=True)
class DispatchRuleSpec:
    """工具组的派工与 tool wake-up 排名规则。"""

    name: str
    rankings: tuple[str, ...] = ()
    tool_wake_up_ranking: str | None = None


@dataclass(frozen=True)
class ToolGroupSpec:
    """Toolgroups：一组同能力设备及其批处理/派工属性。"""

    id: str
    area: str
    name: str
    number_of_tools: int
    process: str
    cascading_tool: bool = False
    batching_tool: bool = False
    batch_criterion: str | None = None
    batching_unit: str | None = None
    loading_time: float = 0.0
    loading_time_unit: str = "minute"
    unloading_time: float = 0.0
    unloading_time_unit: str = "minute"
    location: str | None = None
    dispatch_rule: DispatchRuleSpec | None = None


@dataclass(frozen=True)
class ToolSpec:
    """一台具体设备。

    SMT 原始文件通常只给出工具组数量；本项目展开成单台设备，方便事件引擎派工。
    ``tool_group_id`` 表示精确加工资格；``process`` 是工艺/区域统计分类。
    前四个字段保留当前教学模型的构造方式。
    """

    id: str
    name: str
    process: str
    available_from: float = 0.0
    tool_group_id: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class PreventiveMaintenanceSpec:
    """PM：按时间或累计处理量触发的预防性维护。"""

    event_name: str
    valid_for_type: str
    type_name: str
    pm_type: str
    mean_time_before_pm: float
    mtbpm_unit: str
    repair_distribution: DistributionSpec
    first_one_at_distribution: DistributionSpec | None = None
    first_one_at: float | None = None
    first_one_at_unit: str | None = None


@dataclass(frozen=True)
class BreakdownSpec:
    """Breakdown：设备故障与维修的概率分布。"""

    event_name: str
    valid_for_type: str
    type_name: str
    down_type: str
    time_to_failure: DistributionSpec
    time_to_repair: DistributionSpec
    first_one_at_distribution: DistributionSpec | None = None
    first_one_at: float | None = None
    first_one_at_unit: str | None = None


@dataclass(frozen=True)
class SetupTransitionSpec:
    """Setups：由当前 setup 切换到新 setup 的时间与最小连续加工量。"""

    setup_group_name: str
    current_setup: str
    new_setup: str
    setup_time: DistributionSpec
    minimal_run_length: int = 0


@dataclass(frozen=True)
class TransportRuleSpec:
    """Transport：两个工具组位置之间的运输时间。"""

    from_location: str
    to_location: str
    transport_time: DistributionSpec


@dataclass(frozen=True)
class RecipeSpec:
    """项目扩展：用于把路线工步与具体加工配方关联。"""

    id: str
    name: str
    process: str


@dataclass(frozen=True)
class RouteStepSpec:
    """Route_Product_i 的一条路线工步。

    ``tool_group_id`` 是该工步的唯一精确加工资格；``process`` 是用于 metrics
    与策略的工艺/区域统计分类，不能替代工具组资格。
    """

    process: str
    process_time: float
    id: str | None = None
    description: str | None = None
    area: str | None = None
    tool_group_id: str | None = None
    processing_unit: str = "lot"
    processing_distribution: DistributionSpec | None = None
    cascading_interval: float | None = None
    cascading_interval_unit: str | None = None
    batch_minimum: int | None = None
    batch_maximum: int | None = None
    required_setup: str | None = None
    setup_when: str = "if_needed"
    setup_distribution: DistributionSpec | None = None
    lot_to_lens_dedication_step: str | None = None
    rework_probability: float = 0.0
    rework_unit: str | None = None
    rework_step: str | None = None
    sampling_probability: float = 1.0
    critical_queue_time_step: str | None = None
    critical_queue_time: float | None = None
    critical_queue_time_unit: str | None = None


@dataclass(frozen=True)
class ProductSpec:
    """产品及其路线；``route_name`` 对应 Lotrelease 表的 ROUTE NAME。"""

    id: str
    name: str
    route: tuple[RouteStepSpec, ...]
    route_name: str | None = None


@dataclass(frozen=True)
class LotTypeSpec:
    """表 Lotrelease 中可复用的 Lot 类型属性。"""

    id: str
    name: str
    priority: int = 1
    is_super_hot: bool = False
    wafer_count: int = 25


@dataclass(frozen=True)
class LotReleaseSpec:
    """外部投料计划中的一种产品/lot 类型供给规则。

    ``start_date`` 和 ``release_distribution`` 表示计划供给到达 release pool 的
    时刻；它们不等同于 lot 经策略许可后实际进入 fab 的时刻。
    """

    product_id: str
    route_name: str
    lot_type_id: str
    start_date: float
    release_distribution: DistributionSpec
    lots_per_release: int
    due_date: float | None = None


@dataclass(frozen=True)
class SyntheticOrderSpec:
    """自动生成投料源，沿用旧教学引擎的随机订单生成逻辑。"""

    arrival_interval: float
    lots_per_order: int
    waiting_capacity: int
    product_mix: tuple[tuple[str, float], ...]
    due_time_offset: float | None = None


@dataclass(frozen=True)
class SimulationSpec:
    start_time: float
    end_time: float
    # 默认每 120 分钟提供一次投料决策机会，约等于每天最多 12 个 lot。
    release_interval: float = 120.0
    warmup_time: float = 0.0


@dataclass(frozen=True)
class FabModel:
    """完整工厂静态模型；前七项维持当前加载器的构造顺序。"""

    id: str
    time_unit: str
    tools: dict[str, ToolSpec]
    products: dict[str, ProductSpec]
    synthetic_order: SyntheticOrderSpec | None
    simulation: SimulationSpec
    source_mode: str = "synthetic"
    tool_groups: dict[str, ToolGroupSpec] = field(default_factory=dict)
    recipes: dict[str, RecipeSpec] = field(default_factory=dict)
    lot_types: dict[str, LotTypeSpec] = field(default_factory=dict)
    lot_releases: tuple[LotReleaseSpec, ...] = ()
    preventive_maintenance: tuple[PreventiveMaintenanceSpec, ...] = ()
    breakdowns: tuple[BreakdownSpec, ...] = ()
    setup_transitions: tuple[SetupTransitionSpec, ...] = ()
    transport_rules: tuple[TransportRuleSpec, ...] = ()


@dataclass
class ToolState:
    """单台设备在一次仿真中的可变状态。"""

    tool_id: str
    name: str
    type: str
    available_time: float
    current_product_id: str | None = None
    in_process: bool = False
    current_recipe_id: str | None = None
    current_setup: str | None = None
    current_setup_run_length: int = 0
    processed_wafers: int = 0
    is_down: bool = False
    is_in_setup: bool = False
    down_reason: str | None = None
    active_lot_id: str | None = None
    active_step_id: str | None = None


@dataclass
class LotState:
    """一个 Lot 在一次仿真中的可变状态。"""

    id: str
    product_id: str
    order_id: str | None
    generation_time: float
    release_time: float
    due_time: float | None
    priority: int
    route: tuple[RouteStepSpec, ...]
    input_order: int
    # 候选 lot 到达 release pool 的计划时刻；release_time 是策略许可后的真实进厂时刻。
    planned_release_time: float | None = None
    operation_index: int = 0
    ready_time: float = 0.0
    in_process: bool = False
    start_time: float | None = None
    end_time: float | None = None
    processing_tool_id: str | None = None
    lot_type_id: str | None = None
    wafer_count: int = 25
    is_super_hot: bool = False
    # LTL 链式绑定：{目标工步 id: 绑定设备 id}。一个 lot 可同时有多个进行中的
    # LTL 绑定（如 step12 绑定供 step113 用、step57 绑定供 step79 用），
    # 各自独立互不覆盖。lot 到达目标工步时必须回对应绑定设备。
    dedicated_tools: dict[str, str] = field(default_factory=dict)
    operation_history: list[dict[str, object]] = field(default_factory=list)

    @property
    def step(self) -> int:
        return self.operation_index

    @property
    def completed(self) -> bool:
        return self.operation_index >= len(self.route)

    @property
    def current_step(self) -> RouteStepSpec | None:
        return None if self.completed else self.route[self.operation_index]

    @property
    def current_process(self) -> str | None:
        return self.current_step.process if self.current_step else None

    @property
    def current_process_time(self) -> float | None:
        return self.current_step.process_time if self.current_step else None
