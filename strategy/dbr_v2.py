import math
from types import SimpleNamespace

from fab.model.entities import FabModel
from fab.strategy import StrategyDecision, StrategyState


class DynamicDBR:
    """
    动态 DBR 策略对象。
    """

    name = "Dynamic DBR"
    run_id = "dynamic-dbr-v2"
    # DBR 只使用 Engine 已筛好的 dispatchable_candidates，不需要完整空闲设备快照。
    def __init__(
        self,
        parameters,
        release_interval=38.0,
        main_bottleneck_window=720.0,
        sub_bottleneck_window=5.0,
        alpha=0.5,
        lam=0.5,
    ):
        # 经验参数需要自己尝试 因此由外部传入，未来也可以由优化算法搜索。
        self.parameters = dict(parameters)
        self.release_interval = float(release_interval)
        self.main_bottleneck_window = float(main_bottleneck_window)
        self.sub_bottleneck_window = float(sub_bottleneck_window)
        self.alpha = float(alpha)
        self.lam = float(lam)

        # initialize(context) 注入的静态工厂信息。
        self.model = None
        self.process_names = ()

        # DBR 跨事件、跨更新周期需要保存的算法状态。
        self.main_bottleneck = None
        self.sub_bottleneck = None
        self.previous_BDm = {}
        self.last_main_bottleneck_update = None
        self.last_sub_bottleneck_update = None
        self.timers_started = False
        # 新 lot 通过 Rope gate 后，在其首次派工前保留 PR 加分。
        # 这对应论文中“release and process a new lot”的同一决策语义。
        self._release_priority_bonus_lot_id = None
        self._release_priority_bonus = 0.0

    def initialize(self, model: FabModel):
        """由 Engine 在每次仿真开始时调用一次。"""
        self.model = model
        self.process_names = tuple(
            dict.fromkeys(tool.process for tool in model.tools.values())
        )
        # 优化：可用率只依赖静态模型（MTTF/MTTR/PM 均值），与仿真状态无关，
        # 因此只在 initialize 时预计算一次，避免每次 decide 重复扫描全部
        # breakdown/PM 规则（原热点 _downtime_matches 被调用近千万次）。
        # 这里用 SimpleNamespace 临时包装 ToolSpec，使其具有 ToolState 的 tool_id 属性，
        # 以便复用 _compute_availability / _downtime_matches 的现有匹配逻辑。
        self.machine_availability = {
            spec.id: self._compute_availability(SimpleNamespace(tool_id=spec.id))
            for spec in model.tools.values()
        }
        # 优化：Layer 结构只依赖静态模型（lot 路线 + 主瓶颈工艺），与仿真状态无关。
        # 每个 (mbottleneck, product_id) 的 layer_list 完全相同，因此按产品预计算一次，
        # 避免每次决策对每个 lot 重复扫描整条路线（原热点 build_layer_list 被调用数万次）。
        self._layer_cache: dict[tuple[str, str], list[dict[str, object]]] = {}
        # 同一静态 Layer 的两个反向索引：step -> layer 用于当前 lot 定位，
        # layer -> PT/FT 用于累计 Layer load，避免在每个 lot 上再次扫路线。
        self._layer_by_step: dict[tuple[str, str], tuple[int | None, ...]] = {}
        self._layer_load_weight: dict[tuple[str, str], dict[int, float]] = {}
        # 这两个值均只取决于路线和静态平均可用率，因此可跨所有决策复用。
        self._heartbeat_cache: dict[tuple[str, int], float] = {}
        self._operation_keys_by_product: dict[str, tuple[tuple[str, int], ...]] = {}
        self._bottleneck_visits: dict[
            tuple[str, str], tuple[int | None, ...]
        ] = {}
        # 以下三个容器是“当前决策时刻”的动态快照，会在 decide 中一起重建；
        # 不跨事件缓存，避免 WIP 改变后读到陈旧值。
        self._operation_buffers: dict[tuple[str, int], float] = {}
        self._layer_loads: dict[int, float] = {}
        self._bottleneck_queue_load = 0.0
        self._wip_index_bottleneck: str | None = None
        self._wip_contributions: dict[
            str,
            tuple[int | None, float, tuple[str, int] | None, float, float],
        ] = {}
        # 产能是同工艺设备长期平均可用率之和，按本 DBR 定义不随瞬时故障改变，
        # 所以在 initialize 中一次求好即可。
        self._operation_capacity_cache: dict[str, float] = {}
        # (主瓶颈工艺, 产品, lot wafer 数) -> 从每一步开始的剩余主瓶颈工作量。
        # 值只依赖静态路线和 lot 大小，可跨决策缓存。
        self._remaining_bottleneck_work_cache: dict[
            tuple[str, str, int], tuple[float, ...]
        ] = {}
        self._setup_time_by_transition: dict[tuple[str, str], float] = {}
        for product_id, product in model.products.items():
            visits: dict[str, int] = {}
            keys: list[tuple[str, int]] = []
            for step in product.route:
                visits[step.process] = visits.get(step.process, 0) + 1
                keys.append((step.process, visits[step.process]))
            self._operation_keys_by_product[product_id] = tuple(keys)
        for spec in model.tools.values():
            self._operation_capacity_cache[spec.process] = (
                self._operation_capacity_cache.get(spec.process, 0.0)
                + self.machine_availability[spec.id]
            )
        for transition in model.setup_transitions:
            self._setup_time_by_transition.setdefault(
                (transition.current_setup, transition.new_setup),
                transition.setup_time.mean,
            )
        self.previous_BDm = {process: 0.0 for process in self.process_names}
        self.main_bottleneck = None
        self.sub_bottleneck = None
        self.timers_started = False
        self._release_priority_bonus_lot_id = None
        self._release_priority_bonus = 0.0

    def _build_layers_for_product(self, mbottleneck: str, product_id: str):
        """构建并缓存一个产品在指定主瓶颈下的 Layer 结构。"""
        key = (mbottleneck, product_id)
        cached = self._layer_cache.get(key)
        if cached is not None:
            return cached
        wafer = self.model.products[product_id]
        layers = []
        layer_by_step: list[int | None] = [None] * len(wafer.route)
        load_weight: dict[int, float] = {}
        layer_start = 0
        for step, operation in enumerate(wafer.route):
            if operation.process != mbottleneck:
                continue
            layer_operations = wafer.route[layer_start : step + 1]
            layer_number = len(layers) + 1
            pt = float(operation.process_time)
            ft = sum(float(item.process_time) for item in layer_operations)
            layers.append(
                {
                    "layer": layer_number,
                    "start_step": layer_start,
                    "end_step": step,
                    "PT": pt,
                    "FT": ft,
                }
            )
            for index in range(layer_start, step + 1):
                layer_by_step[index] = layer_number
            if ft > 0:
                load_weight[layer_number] = pt / ft
            layer_start = step + 1
        # layers 是策略对外使用的原结构；两个辅助索引仅服务性能，不改变公式。
        self._layer_cache[key] = layers
        self._layer_by_step[key] = tuple(layer_by_step)
        self._layer_load_weight[key] = load_weight
        return layers

    # 产能与瓶颈识别
    def _compute_availability(self, machine):
        """计算单台设备长期平均可用率（只在 initialize 时调用一次）。

        仅依赖静态模型（MTTF/MTTR/PM 均值），与仿真状态无关。
        """
        unavailable = 0.0
        for i in self._matching_breakdowns(machine):
            mttf = i.time_to_failure.mean
            mttr = i.time_to_repair.mean
            if mttf > 0:
                unavailable += mttr / (mttf + mttr)

        for i in self._matching_pms(machine):
            if i.pm_type != "time_based":
                continue
            mtbpm = i.mean_time_before_pm
            repair = i.repair_distribution.mean
            if mtbpm > 0:
                unavailable += repair / (mtbpm + repair)

        return max(0.0, min(1.0, 1.0 - unavailable))

    def cal_machine_availability(self, machine):  # 单个机器的Cm
        """返回单台设备长期平均可用率。

        优化：结果与仿真状态无关，initialize 时已预计算进
        ``self.machine_availability``；此处直接查表返回。
        """
        return self.machine_availability[machine.tool_id]

    def _matching_breakdowns(self, machine):
        for breakdown in self.model.breakdowns:
            if self._downtime_matches(
                breakdown.valid_for_type, breakdown.type_name, machine
            ):
                yield breakdown

    def _matching_pms(self, machine):
        for pm in self.model.preventive_maintenance:
            if self._downtime_matches(pm.valid_for_type, pm.type_name, machine):
                yield pm

    def _downtime_matches(self, valid_for_type, type_name, machine):
        kind = valid_for_type.lower()
        if kind == "tool":
            return machine.tool_id == type_name

        tool_spec = self.model.tools.get(machine.tool_id)
        if tool_spec is None:
            return False

        if kind == "toolgroup":
            return tool_spec.tool_group_id == type_name

        if kind == "area":
            group = self.model.tool_groups.get(tool_spec.tool_group_id or "")
            return group is not None and group.area == type_name
        return False

    def cal_capacity_rate(self, machines):  # 一个工序的Cm
        """计算工作站组有效产能率，单位 machine-minute/minute。"""
        if not isinstance(machines, (list, tuple)):
            machines = [machines]
        return sum(self.cal_machine_availability(machine) for machine in machines)

    def cal_Cm(self, machine, window=720):  # 主瓶颈的Cm
        """计算单台设备在指定窗口内的有效产能。"""
        return window * self.cal_machine_availability(machine)

    def get_mbottolneck(self, state, window=720):
        """返回主瓶颈、负荷率、各工序需求和各工序产能。"""
        lots = state.waiting_lots

        product_demand = {}
        for lot in lots:
            product_demand[lot.product_id] = (
                product_demand.get(lot.product_id, 0) + lot.wafer_count
            )

        machine_groups = {}
        for machine in state.tool_states.values():
            machine_groups.setdefault(machine.type, []).append(machine)

        Dm_list = {}
        Cm_list = {}
        load_ratio_list = {}

        for process, process_machines in machine_groups.items():
            Dm = 0.0
            for product_id, demand in product_demand.items():
                process_time = sum(
                    step.process_time
                    for step in state.model.products[product_id].route
                    if step.process == process
                )
                Dm += demand * process_time

            # 同一工序的并行机器产能需要相加。
            Cm = sum(self.cal_Cm(machine, window) for machine in process_machines)
            Dm_list[process] = Dm
            Cm_list[process] = Cm
            load_ratio_list[process] = Dm / Cm if Cm else float("inf")

        if not load_ratio_list:
            return None, 0.0, {}, {}

        bottleneck = max(load_ratio_list, key=load_ratio_list.get)
        return (
            bottleneck,
            load_ratio_list[bottleneck],
            Dm_list,
            Cm_list,
        )

    def cal_IBD(self, state, process):
        """计算某个工作站组的瞬时瓶颈度。"""
        window = self.sub_bottleneck_window
        process_machines = [
            machine
            for machine in state.tool_states.values()
            if machine.type == process
        ]
        if not process_machines:
            return 0.0

        QLm = sum(
            float(wafer.current_process_time or 0.0)
            for wafer in state.lots
            if not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= state.current_time
            and wafer.current_process == process
        )
        Cm = sum(self.cal_Cm(machine, window) for machine in process_machines)
        return 1 - math.exp(-self.lam * QLm / Cm) if Cm > 0 else 0.0

    def get_sbottleneck(self, state):
        """使用瞬时瓶颈度和指数平滑识别次瓶颈。"""
        processes = list(
            dict.fromkeys(machine.type for machine in state.tool_states.values())
        )
        BDm_list = {}
        for process in processes:
            IBDm = self.cal_IBD(state, process)
            BDm_list[process] = self.alpha * IBDm + (
                1 - self.alpha
            ) * self.previous_BDm.get(process, 0.0)

        candidates = {
            process: degree
            for process, degree in BDm_list.items()
            if process != self.main_bottleneck
        }
        if not candidates:
            return None, 0.0, BDm_list
        bottleneck = max(candidates, key=candidates.get)
        return bottleneck, candidates[bottleneck], BDm_list

    # Layer 与 Drum
    def build_layer_list(self, mbottleneck, wafer):
        """按第几次经过主瓶颈，将 lot 路线划分为 Layer。

        优化：Layer 结构只依赖静态模型（路线 + 主瓶颈工艺），同一产品的所有 lot
        完全一致。这里直接返回按产品预计算的缓存结果。
        """
        if mbottleneck is None:
            return []
        return self._build_layers_for_product(mbottleneck, wafer.product_id)

    def cal_PT(self, layer_list):
        """返回各 Layer 的主瓶颈加工时间 PT。"""
        return {layer["layer"]: float(layer["PT"]) for layer in layer_list}

    def cal_FT(self, layer_list):
        """返回各 Layer 的理想流动时间 FT。"""
        return {layer["layer"]: float(layer["FT"]) for layer in layer_list}

    def get_layer(self, layer_list, wafer):
        """返回 lot 当前所属 Layer；最后一次主瓶颈后返回 None。"""
        if wafer.completed:
            return None
        for layer in layer_list:
            if wafer.operation_index <= layer["end_step"]:
                return layer["layer"]
        return None

    def get_layer_load(self, mbottleneck, wafers):
        """统计各层当前 WIP 的 Layer Load。

        Layer 归属和 PT / FT 权重均为静态缓存；每次决策只扫描动态 WIP 并累加。
        """
        layer_load_list = {}
        for wafer in wafers:
            step = wafer.operation_index
            if step >= len(wafer.route):
                continue
            key = (mbottleneck, wafer.product_id)
            layer_by_step = self._layer_by_step.get(key)
            if layer_by_step is None:
                self._build_layers_for_product(*key)
                layer_by_step = self._layer_by_step[key]
            layer = layer_by_step[step]
            if layer is None:
                continue
            weight = self._layer_load_weight[key].get(layer)
            if weight is None:
                continue
            layer_load_list[layer] = layer_load_list.get(layer, 0.0) + weight
        return layer_load_list

    def cal_bottleneck_heartbeat(
        self,
        state,
        mbottleneck,
        layer_list,
    ):
        """计算主瓶颈 Drum 节拍 Dt。

        ``layer_list`` 来自静态 layer cache，且产能也为静态平均值，故同一
        主瓶颈与同一列表对象的结果可安全跨决策缓存。
        """
        key = (mbottleneck, id(layer_list))
        cached = self._heartbeat_cache.get(key)
        if cached is not None:
            return cached
        capacity_rate = self._operation_capacity_cache.get(mbottleneck, 0.0)
        if capacity_rate <= 0:
            return float("inf")
        heartbeat = sum(float(layer["PT"]) for layer in layer_list) / capacity_rate
        self._heartbeat_cache[key] = heartbeat
        return heartbeat

    # Operation Buffer
    def get_operation_key(self, wafer, step=None):
        """用 (process, visit_number) 表示多产品环境中的操作 i。"""
        step = wafer.operation_index if step is None else step
        if step < 0 or step >= len(wafer.route):
            return None
        return self._operation_keys_by_product[wafer.product_id][step]

    def _build_operation_buffer(self, state):
        """按 operation_key 汇总各操作前排队 lot 的加工工作量。

        优化：cal_Bi 原本对每个调用点都全量扫描 state.lots 并按路线前缀统计
        visit_number（随 WIP 与路线长度放大）。同一时刻所有候选中，等待某个
        operation 的 lot 集合相同，因此这里一次性按 operation_key 分组求和，
        之后的 cal_Bi 直接查表。
        """
        current_time = state.current_time
        buffers: dict[tuple[str, int], float] = {}
        for wafer in state.lots:
            step = wafer.operation_index
            if (
                step >= len(wafer.route)
                or wafer.in_process
                or wafer.ready_time > current_time
            ):
                continue
            key = self._operation_keys_by_product[wafer.product_id][step]
            buffers[key] = buffers.get(key, 0.0) + float(
                wafer.route[step].process_time
            )
        return buffers

    def _build_wip_indexes(self, state, mbottleneck):
        """一次扫描当前 WIP，汇总 DBR 本轮决策需要的所有动态量。

        Layer load 包含所有尚未完成的 lot；operation buffer 和瓶颈前队列只
        包含已经到站、且未在加工的 lot。这保持原三个独立扫描的统计口径。
        """

        current_time = state.current_time
        buffers: dict[tuple[str, int], float] = {}
        layer_loads: dict[int, float] = {}
        bottleneck_queue_load = 0.0
        for wafer in state.lots:
            step = wafer.operation_index
            if step >= len(wafer.route):
                continue
            layer_key = (mbottleneck, wafer.product_id)
            layer_by_step = self._layer_by_step.get(layer_key)
            if layer_by_step is None:
                self._build_layers_for_product(*layer_key)
                layer_by_step = self._layer_by_step[layer_key]
            layer = layer_by_step[step]
            if layer is not None:
                weight = self._layer_load_weight[layer_key].get(layer)
                if weight is not None:
                    layer_loads[layer] = layer_loads.get(layer, 0.0) + weight

            if wafer.in_process or wafer.ready_time > current_time:
                continue
            operation_key = self._operation_keys_by_product[wafer.product_id][step]
            process_time = float(wafer.route[step].process_time)
            buffers[operation_key] = buffers.get(operation_key, 0.0) + process_time
            if operation_key[0] == mbottleneck:
                bottleneck_queue_load += self._effective_queue_load(
                    wafer, wafer.route[step]
                )
        return layer_loads, buffers, bottleneck_queue_load

    def _wip_contribution(self, wafer, mbottleneck, current_time):
        """返回一个 lot 对 DBR 三个动态索引的当前贡献。"""

        step = wafer.operation_index
        if step >= len(wafer.route):
            return None, 0.0, None, 0.0, 0.0

        layer_key = (mbottleneck, wafer.product_id)
        layer_by_step = self._layer_by_step.get(layer_key)
        if layer_by_step is None:
            self._build_layers_for_product(*layer_key)
            layer_by_step = self._layer_by_step[layer_key]
        layer = layer_by_step[step]
        layer_weight = (
            self._layer_load_weight[layer_key].get(layer, 0.0)
            if layer is not None
            else 0.0
        )
        if wafer.in_process or wafer.ready_time > current_time:
            return layer, layer_weight, None, 0.0, 0.0

        operation_key = self._operation_keys_by_product[wafer.product_id][step]
        process_time = float(wafer.route[step].process_time)
        bottleneck_work = (
            self._effective_queue_load(wafer, wafer.route[step])
            if operation_key[0] == mbottleneck
            else 0.0
        )
        return layer, layer_weight, operation_key, process_time, bottleneck_work

    @staticmethod
    def _effective_queue_load(wafer, step):
        """估算一个 lot 在当前工步占用的设备时间（分钟）。

        仅用于 DBR 的主瓶颈放料 gate：wafer 工步按实际 wafer 数换算，
        batch 工步按名义满炉容量平均分摊。引擎加工和 DBR 派工排序不变。
        """

        process_time = float(step.process_time)
        unit = step.processing_unit.lower()
        if unit == "wafer":
            return process_time * wafer.wafer_count
        if unit == "batch" and step.batch_maximum is not None:
            lots_per_batch = max(1, step.batch_maximum // wafer.wafer_count)
            return process_time / lots_per_batch
        return process_time

    def _apply_wip_contribution(self, contribution, direction):
        layer, layer_weight, operation_key, process_time, bottleneck_work = contribution
        if layer is not None and layer_weight:
            updated = self._layer_loads.get(layer, 0.0) + direction * layer_weight
            if updated:
                self._layer_loads[layer] = updated
            else:
                self._layer_loads.pop(layer, None)
        if operation_key is not None and process_time:
            updated = (
                self._operation_buffers.get(operation_key, 0.0)
                + direction * process_time
            )
            if updated:
                self._operation_buffers[operation_key] = updated
            else:
                self._operation_buffers.pop(operation_key, None)
        self._bottleneck_queue_load += direction * bottleneck_work

    def _refresh_wip_indexes(self, state, mbottleneck):
        """按引擎提供的变更 lot 增量更新 DBR WIP 索引。"""

        if self._wip_index_bottleneck != mbottleneck:
            self._wip_index_bottleneck = mbottleneck
            self._operation_buffers = {}
            self._layer_loads = {}
            self._bottleneck_queue_load = 0.0
            self._wip_contributions = {}
            changed_lots = state.lots
        else:
            changed_lots = state.changed_lots

        for wafer in changed_lots:
            previous = self._wip_contributions.get(wafer.id)
            if previous is not None:
                self._apply_wip_contribution(previous, -1.0)
            current = self._wip_contribution(
                wafer, mbottleneck, state.current_time
            )
            self._wip_contributions[wafer.id] = current
            self._apply_wip_contribution(current, 1.0)

        return (
            self._layer_loads,
            self._operation_buffers,
            self._bottleneck_queue_load,
        )

    def _operation_capacity(self, state, process):
        """返回某 process 对应设备的有效产能率。"""
        return self._operation_capacity_cache.get(process, 0.0)

    def cal_Bi(self, state, operation_key):
        """计算操作 i 前 Buffer 的预计清空时间。

        优化：排队工作量由调用方通过 ``_operation_buffers`` 提供（一次性汇总），
        capacity 通过 ``_operation_capacity_cache`` 提供；这里只做查表，避免
        每次调用都全量扫描 WIP 与全部设备。
        """
        if operation_key is None:
            return 0.0
        waiting_work = self._operation_buffers.get(operation_key, 0.0)
        process = operation_key[0]
        capacity_rate = self._operation_capacity_cache.get(process)
        if capacity_rate is None:
            capacity_rate = self._operation_capacity(state, process)
            # 产能只依赖静态设备可用率，可永久缓存。
            self._operation_capacity_cache[process] = capacity_rate
        return waiting_work / capacity_rate if capacity_rate > 0 else float("inf")

    def cal_BNj(self, state, mbottleneck, j):
        """计算第 j 次主瓶颈操作前的 Buffer 清空时间。"""
        if j is None or j < 1:
            return 0.0
        return self.cal_Bi(state, (mbottleneck, j))

    def get_bottleneck_visit(
        self,
        wafer,
        mbottleneck,
        step=None,
    ):
        """返回指定 step 是第几次主瓶颈访问。"""
        step = wafer.operation_index if step is None else step
        if step < 0 or step >= len(wafer.route):
            return None
        key = (mbottleneck, wafer.product_id)
        visits = self._bottleneck_visits.get(key)
        if visits is None:
            count = 0
            values: list[int | None] = []
            for operation in wafer.route:
                if operation.process == mbottleneck:
                    count += 1
                    values.append(count)
                else:
                    values.append(None)
            visits = tuple(values)
            self._bottleneck_visits[key] = visits
        return visits[step]

    # Compound Priority

    def cal_Pmi(
        self,
        state,
        wafer,
        mbottleneck,
        layer_loads,
        L0,
        m0,
        m1,
        m2,
    ):
        """计算主瓶颈子优先级 PMi。"""
        if wafer.current_process != mbottleneck:
            return 0.0
        if L0 <= 0:
            raise ValueError("L0 must be greater than zero.")

        layer_list = self.build_layer_list(mbottleneck, wafer)
        j = self.get_bottleneck_visit(wafer, mbottleneck)
        if j is None:
            return 0.0

        Bi = self.cal_Bi(state, self.get_operation_key(wafer))
        Lj = layer_loads.get(j, 0.0)
        priority = m1 * (Lj / L0 - 1.0) + m0 * Bi
        if j < len(layer_list):
            priority += m2 * (layer_loads.get(j + 1, 0.0) / L0 - 1.0)
        return priority

    def cal_Pni(
        self,
        state,
        wafer,
        mbottleneck,
        layer_loads,
        L0,
        n0,
        n1,
        n2,
    ):
        """计算非瓶颈子优先级 PNi。"""
        if wafer.current_process == mbottleneck:
            return 0.0
        if L0 <= 0:
            raise ValueError("L0 must be greater than zero.")

        Bi = self.cal_Bi(state, self.get_operation_key(wafer))
        layer_list = self.build_layer_list(mbottleneck, wafer)
        j = self.get_layer(layer_list, wafer)
        if j is None:
            return n0 * Bi
        return (
            n0 * Bi
            + n1 * (layer_loads.get(j, 0.0) / L0 - 1.0)
            + n2 * self.cal_BNj(state, mbottleneck, j)
        )

    def cal_Psi(self, state, wafer, sbottleneck, s1, s2):
        """计算次瓶颈相邻操作子优先级 PSi。"""
        if sbottleneck is None:
            return 0.0
        next_step = wafer.operation_index + 1
        previous_step = wafer.operation_index - 1

        if (
            next_step < len(wafer.route)
            and wafer.route[next_step].process == sbottleneck
        ):
            return s1 * self.cal_Bi(
                state,
                self.get_operation_key(wafer, next_step),
            )
        if previous_step >= 0 and wafer.route[previous_step].process == sbottleneck:
            return s2 * self.cal_Bi(
                state,
                self.get_operation_key(wafer, previous_step),
            )
        return 0.0

    def cal_Poi(self, state, wafer, mbottleneck, p1, p2):
        """计算 Drum/back-to-front 子优先级 POi。"""
        layer_list = self.build_layer_list(mbottleneck, wafer)
        Dt = self.cal_bottleneck_heartbeat(
            state,
            mbottleneck,
            layer_list,
        )
        if not math.isfinite(Dt) or Dt <= 0:
            return 0.0

        ti = state.last_operation_completion.get(self.get_operation_key(wafer))
        if ti is None:
            # 首次加工时让 Drum 偏差项初始化为 0。
            ti = state.current_time - Dt
        return p1 * ((state.current_time - ti) / Dt - 1.0) + p2 * (
            wafer.operation_index + 1
        ) / len(wafer.route)

    def get_bottleneck_queue_load(
        self,
        state,
        mbottleneck,
    ):
        """计算主瓶颈前排队 lot 的加工工作量。

        只统计已经到达主瓶颈、正在排队等待（未加工、已就绪）的 lot 的
        加工时间之和，不含尚未到达主瓶颈的 lot，避免被重入式路线放大。
        """
        return sum(
            self._effective_queue_load(wafer, wafer.current_step)
            for wafer in state.lots
            if not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= state.current_time
            and wafer.current_process == mbottleneck
        )

    def _remaining_bottleneck_work_by_step(self, wafer, mbottleneck):
        """返回 lot 从每个工步开始的剩余主瓶颈工作量。

        论文式 (11) 的 ``sum_{l=j}^J PT_jl`` 是 lot 当前 layer 到最后一次
        主瓶颈访问的总加工时间。这里将该量按 route 反向预计算，既覆盖尚未
        到达主瓶颈的 WIP，也覆盖重入后未来的主瓶颈访问。
        """
        key = (mbottleneck, wafer.product_id, wafer.wafer_count)
        cached = self._remaining_bottleneck_work_cache.get(key)
        if cached is not None:
            return cached

        route = wafer.route
        remaining = [0.0] * (len(route) + 1)
        for step_index in range(len(route) - 1, -1, -1):
            step = route[step_index]
            remaining[step_index] = remaining[step_index + 1]
            if step.process == mbottleneck:
                remaining[step_index] += self._effective_queue_load(wafer, step)
        cached = tuple(remaining)
        self._remaining_bottleneck_work_cache[key] = cached
        return cached

    def get_remaining_bottleneck_load(self, state, mbottleneck):
        """计算全厂 WIP 的剩余主瓶颈工作量，作为 Rope 的负荷口径。

        与仅统计主瓶颈前 ready queue 的旧口径不同，所有已 release 且尚未
        完成的 lot 都会贡献其后续要经过的主瓶颈工作量。正在加工的 lot 也按
        论文中的名义 PT 计入，避免刚开始加工就从 Rope 负荷中消失。
        """
        total = 0.0
        for wafer in state.lots:
            if wafer.completed:
                continue
            remaining = self._remaining_bottleneck_work_by_step(
                wafer, mbottleneck
            )
            total += remaining[wafer.operation_index]
        return total

    def cal_Pri(self, state, mbottleneck, BL, r, bottleneck_load=None):
        """计算投料子优先级 PRi。

        ``BL`` 表示全厂 WIP 的剩余主瓶颈工作量目标（单位与 load 相同）；
        ``load`` 覆盖当前 lot 及其重入后续仍会占用的主瓶颈加工时间。PRi > 0
        表示该负荷低于目标，允许新 lot 参与投料竞争。
        """
        if BL <= 0:
            raise ValueError("BL must be greater than zero.")
        load = (
            self.get_remaining_bottleneck_load(state, mbottleneck)
            if bottleneck_load is None
            else bottleneck_load
        )
        return r * (1.0 - load / BL)

    def cal_compound_priority(
        self,
        state,
        wafer,
        mbottleneck,
        sbottleneck,
        parameters=None,
        layer_loads=None,
        release_operation=False,
    ):
        """计算五项子优先级及总优先级。"""
        parameters = self.parameters if parameters is None else parameters
        layer_loads = (
            self.get_layer_load(mbottleneck, state.lots)
            if layer_loads is None
            else layer_loads
        )
        PMi = self.cal_Pmi(
            state,
            wafer,
            mbottleneck,
            layer_loads,
            parameters["L0"],
            parameters["m0"],
            parameters["m1"],
            parameters["m2"],
        )
        PNi = self.cal_Pni(
            state,
            wafer,
            mbottleneck,
            layer_loads,
            parameters["L0"],
            parameters["n0"],
            parameters["n1"],
            parameters["n2"],
        )
        PSi = self.cal_Psi(
            state,
            wafer,
            sbottleneck,
            parameters["s1"],
            parameters["s2"],
        )
        POi = self.cal_Poi(
            state,
            wafer,
            mbottleneck,
            parameters["p1"],
            parameters["p2"],
        )
        PRi = (
            self.cal_Pri(
                state,
                mbottleneck,
                parameters["BL"],
                parameters["r"],
            )
            if release_operation
            else 0.0
        )
        return {
            "PMi": PMi,
            "PNi": PNi,
            "PSi": PSi,
            "POi": POi,
            "PRi": PRi,
            "total": PMi + PNi + PSi + POi + PRi,
        }

    def _compound_priority_total(
        self,
        state,
        wafer,
        mbottleneck,
        sbottleneck,
        layer_loads,
    ):
        """快速计算复合优先级总分，和 ``cal_compound_priority`` 公式等价。

        派工仅使用 total；直接访问当前轮索引和静态路线缓存，避免每个候选构造
        五项诊断字典以及重复的 layer/operation 查找。
        """

        step = wafer.operation_index
        route = wafer.route
        product_id = wafer.product_id
        process = route[step].process
        parameters = self.parameters
        operation_key = self._operation_keys_by_product[product_id][step]

        capacity = self._operation_capacity_cache.get(process, 0.0)
        Bi = (
            self._operation_buffers.get(operation_key, 0.0) / capacity
            if capacity > 0
            else float("inf")
        )
        layer_key = (mbottleneck, product_id)
        layer_list = self._layer_cache.get(layer_key)
        if layer_list is None:
            layer_list = self._build_layers_for_product(*layer_key)
        layer = self._layer_by_step[layer_key][step]

        if process == mbottleneck:
            visits = self._bottleneck_visits.get(layer_key)
            if visits is None:
                visit = 0
                values: list[int | None] = []
                for operation in route:
                    if operation.process == mbottleneck:
                        visit += 1
                        values.append(visit)
                    else:
                        values.append(None)
                visits = tuple(values)
                self._bottleneck_visits[layer_key] = visits
            visit = visits[step]
            Pmi = 0.0
            if visit is not None:
                L0 = parameters["L0"]
                Pmi = parameters["m1"] * (
                    layer_loads.get(visit, 0.0) / L0 - 1.0
                ) + parameters["m0"] * Bi
                if visit < len(layer_list):
                    Pmi += parameters["m2"] * (
                        layer_loads.get(visit + 1, 0.0) / L0 - 1.0
                    )
            Pni = 0.0
        else:
            Pmi = 0.0
            Pni = parameters["n0"] * Bi
            if layer is not None:
                L0 = parameters["L0"]
                bottleneck_buffer = self._operation_buffers.get(
                    (mbottleneck, layer), 0.0
                )
                bottleneck_capacity = self._operation_capacity_cache.get(
                    mbottleneck, 0.0
                )
                Bnj = (
                    bottleneck_buffer / bottleneck_capacity
                    if bottleneck_capacity > 0
                    else float("inf")
                )
                Pni += parameters["n1"] * (
                    layer_loads.get(layer, 0.0) / L0 - 1.0
                ) + parameters["n2"] * Bnj

        Psi = 0.0
        if sbottleneck is not None:
            if step + 1 < len(route) and route[step + 1].process == sbottleneck:
                key = self._operation_keys_by_product[product_id][step + 1]
                capacity = self._operation_capacity_cache.get(key[0], 0.0)
                buffer = self._operation_buffers.get(key, 0.0)
                Psi = parameters["s1"] * (
                    buffer / capacity if capacity > 0 else float("inf")
                )
            elif step > 0 and route[step - 1].process == sbottleneck:
                key = self._operation_keys_by_product[product_id][step - 1]
                capacity = self._operation_capacity_cache.get(key[0], 0.0)
                buffer = self._operation_buffers.get(key, 0.0)
                Psi = parameters["s2"] * (
                    buffer / capacity if capacity > 0 else float("inf")
                )

        heartbeat_key = (mbottleneck, id(layer_list))
        Dt = self._heartbeat_cache.get(heartbeat_key)
        if Dt is None:
            bottleneck_capacity = self._operation_capacity_cache.get(mbottleneck, 0.0)
            Dt = (
                sum(float(layer["PT"]) for layer in layer_list)
                / bottleneck_capacity
                if bottleneck_capacity > 0
                else float("inf")
            )
            self._heartbeat_cache[heartbeat_key] = Dt
        if not math.isfinite(Dt) or Dt <= 0:
            Poi = 0.0
        else:
            last_completion = state.last_operation_completion.get(operation_key)
            if last_completion is None:
                last_completion = state.current_time - Dt
            Poi = parameters["p1"] * (
                (state.current_time - last_completion) / Dt - 1.0
            ) + parameters["p2"] * (step + 1) / len(route)

        return Pmi + Pni + Psi + Poi

    def estimate_setup_time(self, tool, lot):
        # 估算换型时间以支持换型惩罚 尽量避免频繁换型
        step = lot.current_step
        if step is None or not step.required_setup:
            return 0.0
        if step.required_setup == tool.current_setup:
            return 0.0

        return self._setup_time_by_transition.get(
            (tool.current_setup or "", step.required_setup), 0.0
        )

    def decide(self, state: StrategyState) -> StrategyDecision:
        """
        在每批事件结算后生成下一步决策。

        1. 更新主瓶颈和次瓶颈；
        2. 判断本次投料机会是否释放一个新 lot；
        3. 为每台空闲机器选择复合优先级最高的 lot。
        """

        decision = StrategyDecision()

        # 第一次进入策略时先建立初始瓶颈状态，并把两个周期事件注入 Engine。
        if not self.timers_started:
            self.update_main_bottleneck(state, force=True)
            self.update_sub_bottleneck(state, force=True)
            decision.wakeups.extend(
                [
                    (
                        state.current_time + self.main_bottleneck_window,
                        "main_bottleneck_update",
                    ),
                    (
                        state.current_time + self.sub_bottleneck_window,
                        "sub_bottleneck_update",
                    ),
                ]
            )
            self.timers_started = True

        # 到了瓶颈刷新时间，更新瓶颈
        if "main_bottleneck_update" in state.strategy_reasons:
            self.update_main_bottleneck(state, force=True)
            decision.wakeups.append(
                (
                    state.current_time + self.main_bottleneck_window,
                    "main_bottleneck_update",
                )
            )

        if "sub_bottleneck_update" in state.strategy_reasons:
            self.update_sub_bottleneck(state, force=True)
            decision.wakeups.append(
                (
                    state.current_time + self.sub_bottleneck_window,
                    "sub_bottleneck_update",
                )
            )

        if self.main_bottleneck is None:
            return decision

        # 即使当前没有可执行的派工/投料，事件仍可能改变某个 lot 的 WIP 贡献。
        # 先吸收这些增量；否则这轮 state 被引擎消费后，下次实际决策会读到陈旧索引。
        (
            layer_loads,
            self._operation_buffers,
            self._bottleneck_queue_load,
        ) = self._refresh_wip_indexes(state, self.main_bottleneck)
        self._layer_loads = layer_loads

        # 无可派工设备且没有投料机会时，策略不可能产生动作。定时瓶颈更新已在
        # 上方处理完毕，因此无需为这个空决策扫描全部 WIP、计算 layer load 或
        # buffer。全量仿真中这类调用约占一半。
        if not state.dispatchable_candidates and not state.release_opportunity:
            return decision

        priority_cache = {}

        def dispatch_score(tool, wafer, bonus=0.0):
            base = priority_cache.get(wafer.id)
            if base is None:
                base = self._compound_priority_total(
                    state,
                    wafer,
                    self.main_bottleneck,
                    self.sub_bottleneck,
                    layer_loads,
                )
                priority_cache[wafer.id] = base
            return base + bonus - (
                self.parameters.get("setup_penalty_weight", 0.0)
                * self.estimate_setup_time(tool, wafer)
            )

        # Rope：新 lot 必须同时满足剩余主瓶颈负荷低于 BL，且在其首工序
        # 工具组内不劣于现有候选 lot。release event 的 dispatches 不会被引擎
        # 执行，因此完成 gate 后立即返回，避免无效的整厂派工计算。
        if state.release_opportunity:
            if not state.waiting_lot_count:
                return decision
            candidate = state.waiting_lots[0]
            remaining_load = self.get_remaining_bottleneck_load(
                state, self.main_bottleneck
            )
            pri = self.cal_Pri(
                state,
                self.main_bottleneck,
                self.parameters["BL"],
                self.parameters["r"],
                remaining_load,
            )
            entry_group = candidate.current_step.tool_group_id
            entry_candidates = [
                (tool, candidates)
                for tool, candidates in state.dispatchable_candidates
                if self.model.tools[tool.tool_id].tool_group_id == entry_group
            ]
            if entry_candidates:
                candidate_score = max(
                    dispatch_score(tool, candidate, pri)
                    for tool, _ in entry_candidates
                )
            else:
                candidate_score = self._compound_priority_total(
                    state,
                    candidate,
                    self.main_bottleneck,
                    self.sub_bottleneck,
                    layer_loads,
                ) + pri
            best_old_score = float("-inf")
            competing_count = 0
            for tool, candidates in entry_candidates:
                for wafer in candidates:
                    competing_count += 1
                    best_old_score = max(best_old_score, dispatch_score(tool, wafer))
            if pri > 0 and candidate_score >= best_old_score:
                decision.release_lot = True
                decision.release_lot_id = candidate.id
                self._release_priority_bonus_lot_id = candidate.id
                self._release_priority_bonus = pri
            if state.record_diagnostics:
                decision.diagnostics = {
                    "time": state.current_time,
                    "main_bottleneck": self.main_bottleneck,
                    "sub_bottleneck": self.sub_bottleneck,
                    "remaining_bottleneck_load": remaining_load,
                    "release_pri": pri,
                    "release_candidate_id": candidate.id,
                    "release_candidate_score": candidate_score,
                    "release_best_old_score": best_old_score,
                    "release_competing_candidate_count": competing_count,
                    "release_lot": decision.release_lot,
                    "release_lot_id": decision.release_lot_id,
                }
            return decision

        # 对每台空闲机器，从该机器当前可加工的 lot 中选择最高优先级。
        # 遍历引擎预排序的设备与候选 Lot 配对。
        reserved_lot_ids = set()
        for tool, eligible_candidates in state.dispatchable_candidates:
            candidates = [
                lot
                for lot in eligible_candidates
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue

            def priority_key(wafer):
                release_bonus = (
                    self._release_priority_bonus
                    if wafer.id == self._release_priority_bonus_lot_id
                    else 0.0
                )
                return (
                    dispatch_score(tool, wafer, release_bonus),
                    -wafer.ready_time,
                    -wafer.release_time,
                    -wafer.input_order,
                )

            selected = max(candidates, key=priority_key)
            decision.dispatches[tool.tool_id] = selected
            reserved_lot_ids.add(selected.id)
            if selected.id == self._release_priority_bonus_lot_id:
                self._release_priority_bonus_lot_id = None
                self._release_priority_bonus = 0.0

        # 记录本次决策的内部参数，供引擎统一落库与事后调试。
        if state.record_diagnostics:
            decision.diagnostics = {
                "time": state.current_time,
                "main_bottleneck": self.main_bottleneck,
                "sub_bottleneck": self.sub_bottleneck,
                "layer_loads": {
                    str(layer): round(load, 4)
                    for layer, load in layer_loads.items()
                },
                "release_lot": decision.release_lot,
                "release_lot_id": decision.release_lot_id,
                "release_opportunity": state.release_opportunity,
                "waiting_lot_count": state.waiting_lot_count,
                "num_dispatches": len(decision.dispatches),
                "dispatched_lot_ids": [
                    lot.id for lot in decision.dispatches.values()
                ],
            }

        return decision

    def update_main_bottleneck(self, state, force=False):
        """按照长周期更新主瓶颈，避免每次事件都重新计算。"""
        should_update = (
            force
            or self.last_main_bottleneck_update is None
            or state.current_time
            >= self.last_main_bottleneck_update + self.main_bottleneck_window
        )
        if not should_update:
            return

        main_bottleneck, _, _, _ = self.get_mbottolneck(
            state, window=self.main_bottleneck_window
        )
        if main_bottleneck is not None:
            self.main_bottleneck = main_bottleneck
        self.last_main_bottleneck_update = state.current_time

    def update_sub_bottleneck(self, state, force=False):
        """
        按短周期更新次瓶颈。

        新的 BDm_list 保存到 previous_BDm，供下一更新周期执行指数平滑。
        """
        should_update = (
            force
            or self.last_sub_bottleneck_update is None
            or state.current_time
            >= self.last_sub_bottleneck_update + self.sub_bottleneck_window
        )
        if not should_update:
            return

        self.sub_bottleneck, _, self.previous_BDm = self.get_sbottleneck(state)
        self.last_sub_bottleneck_update = state.current_time
