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
        # 优化：每次 decide 开始时一次性汇总各 operation 的排队工作量与设备产能，
        # 供 cal_Bi 查表，避免对全 WIP 反复线性扫描。
        self._operation_buffers: dict[tuple[str, int], float] = {}
        self._operation_capacity_cache: dict[str, float] = {}
        self.previous_BDm = {process: 0.0 for process in self.process_names}
        self.main_bottleneck = None
        self.sub_bottleneck = None
        self.timers_started = False

    def _build_layers_for_product(self, mbottleneck: str, product_id: str):
        """构建并缓存一个产品在指定主瓶颈下的 Layer 结构。"""
        key = (mbottleneck, product_id)
        cached = self._layer_cache.get(key)
        if cached is not None:
            return cached
        wafer = self.model.products[product_id]
        layers = []
        layer_start = 0
        for step, operation in enumerate(wafer.route):
            if operation.process != mbottleneck:
                continue
            layer_operations = wafer.route[layer_start : step + 1]
            layers.append(
                {
                    "layer": len(layers) + 1,
                    "start_step": layer_start,
                    "end_step": step,
                    "PT": float(operation.process_time),
                    "FT": sum(float(item.process_time) for item in layer_operations),
                }
            )
            layer_start = step + 1
        self._layer_cache[key] = layers
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
        lots = state.waiting_list.queue

        product_demand = {}
        for lot in lots:
            product_demand[lot.product_id] = (
                product_demand.get(lot.product_id, 0) + lot.wafer_count
            )

        machine_groups = {}
        for machine in state.machines:
            machine_groups.setdefault(machine.type, []).append(machine)

        Dm_list = {}
        Cm_list = {}
        load_ratio_list = {}

        for process, process_machines in machine_groups.items():
            Dm = 0.0
            for product_id, demand in product_demand.items():
                process_time = sum(
                    step.process_time
                    for step in state.products[product_id]["route"]
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
            machine for machine in state.machines if machine.type == process
        ]
        if not process_machines:
            return 0.0

        QLm = sum(
            float(wafer.current_process_time or 0.0)
            for wafer in state.wafers
            if not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= state.current_time
            and wafer.current_process == process
        )
        Cm = sum(self.cal_Cm(machine, window) for machine in process_machines)
        return 1 - math.exp(-self.lam * QLm / Cm) if Cm > 0 else 0.0

    def get_sbottleneck(self, state):
        """使用瞬时瓶颈度和指数平滑识别次瓶颈。"""
        processes = list(dict.fromkeys(machine.type for machine in state.machines))
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
            if wafer.step <= layer["end_step"]:
                return layer["layer"]
        return None

    def get_layer_load(self, mbottleneck, wafers):
        """统计各层当前 WIP 的 Layer Load。

        优化：只依赖各 lot 当前所属 layer 与其 (PT, FT)，而 (PT, FT) 由静态
        模型决定。调用方（decide）在瓶颈未变化时复用上次结果，避免每次决策
        都全量扫描 WIP；这里仍保留完整计算，供瓶颈刷新时重算。
        """
        layer_load_list = {}
        for wafer in wafers:
            if wafer.completed:
                continue
            layer_list = self.build_layer_list(
                mbottleneck,
                wafer,
            )
            layer = self.get_layer(layer_list, wafer)
            if layer is None:
                continue
            PT = self.cal_PT(layer_list).get(layer, 0.0)
            FT = self.cal_FT(layer_list).get(layer, 0.0)
            if FT <= 0:
                continue
            layer_load_list[layer] = layer_load_list.get(layer, 0.0) + PT / FT
        return layer_load_list

    def cal_bottleneck_heartbeat(
        self,
        state,
        mbottleneck,
        layer_list,
    ):
        """计算主瓶颈 Drum 节拍 Dt。"""
        process_machines = [
            machine for machine in state.machines if machine.type == mbottleneck
        ]
        capacity_rate = self.cal_capacity_rate(process_machines)
        if capacity_rate <= 0:
            return float("inf")
        total_bottleneck_pt = sum(self.cal_PT(layer_list).values())
        return total_bottleneck_pt / capacity_rate

    # Operation Buffer
    def get_operation_key(self, wafer, step=None):
        """用 (process, visit_number) 表示多产品环境中的操作 i。"""
        step = wafer.step if step is None else step
        if step < 0 or step >= len(wafer.route):
            return None
        process = wafer.route[step].process
        visit_number = sum(
            1 for operation in wafer.route[: step + 1] if operation.process == process
        )
        return process, visit_number

    def _build_operation_buffer(self, state):
        """按 operation_key 汇总各操作前排队 lot 的加工工作量。

        优化：cal_Bi 原本对每个调用点都全量扫描 state.wafers 并按路线前缀统计
        visit_number（随 WIP 与路线长度放大）。同一时刻所有候选中，等待某个
        operation 的 lot 集合相同，因此这里一次性按 operation_key 分组求和，
        之后的 cal_Bi 直接查表。
        """
        current_time = state.current_time
        buffers: dict[tuple[str, int], float] = {}
        for wafer in state.wafers:
            if wafer.completed or wafer.in_process or wafer.ready_time > current_time:
                continue
            key = self.get_operation_key(wafer)
            if key is None:
                continue
            buffers[key] = buffers.get(key, 0.0) + float(
                wafer.current_process_time or 0.0
            )
        return buffers

    def _operation_capacity(self, state, process):
        """返回某 process 对应设备的有效产能率。"""
        return self.cal_capacity_rate(
            [machine for machine in state.machines if machine.type == process]
        )

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
        step = wafer.step if step is None else step
        if step < 0 or step >= len(wafer.route):
            return None
        if wafer.route[step].process != mbottleneck:
            return None
        return sum(
            1
            for operation in wafer.route[: step + 1]
            if operation.process == mbottleneck
        )

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
        next_step = wafer.step + 1
        previous_step = wafer.step - 1

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
            wafer.step + 1
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
            float(wafer.current_process_time or 0.0)
            for wafer in state.wafers
            if not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= state.current_time
            and wafer.current_process == mbottleneck
        )

    def cal_Pri(self, state, mbottleneck, BL, r):
        """计算投料子优先级 PRi。

        ``BL`` 表示瓶颈前允许积压的工作量目标（单位与 load 相同）；
        ``load`` 为当前主瓶颈前排队 lot 的加工工作量。PRi > 0 表示
        瓶颈前积压低于目标，可放行新 lot。
        """
        if BL <= 0:
            raise ValueError("BL must be greater than zero.")
        load = self.get_bottleneck_queue_load(
            state,
            mbottleneck,
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
            self.get_layer_load(mbottleneck, state.wafers)
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

    def estimate_setup_time(self, tool, lot):
        # 估算换型时间以支持换型惩罚 尽量避免频繁换型
        step = lot.current_step
        if step is None or not step.required_setup:
            return 0.0
        if step.required_setup == tool.current_setup:
            return 0.0

        matches = [
            item.setup_time.mean
            for item in self.model.setup_transitions
            if item.current_setup == (tool.current_setup or "")
            and item.new_setup == step.required_setup
        ]
        return matches[0] if matches else 0.0

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

        # 优化：一次决策内所有候选共享同一份操作排队工作量，只在进入 decide 时
        # 汇总一次（cal_Bi 查表）；设备产能是静态的，永久缓存在
        # ``_operation_capacity_cache``。
        self._operation_buffers = self._build_operation_buffer(state)

        # 当前时刻所有候选共用一份 Layer Load（依赖当前 WIP，每次决策重算；
        # build_layer_list 已按产品缓存，因此这里只是轻量的逐 lot 查表）。
        layer_loads = self.get_layer_load(
            self.main_bottleneck,
            state.wafers,
        )

        #  PRi大于0 则放入新的lot。
        if state.release_opportunity and state.waiting_lot_count > 0:
            decision.release_lot = (
                self.cal_Pri(
                    state,
                    self.main_bottleneck,
                    self.parameters["BL"],
                    self.parameters["r"],
                )
                > 0
            )

        # 对每台空闲机器，从该机器当前可加工的 lot 中选择最高优先级。
        # 优化：遍历引擎预排序的"有候选设备"列表，避免对全部可用设备排序过滤。
        reserved_lot_ids = set()
        for tool in state.dispatchable_tools:
            candidates = [
                lot
                for lot in state.eligible_lots_by_tool.get(tool.tool_id, ())
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue

            def priority_key(wafer):
                compound_priority = self.cal_compound_priority(
                    state=state,
                    wafer=wafer,
                    mbottleneck=self.main_bottleneck,
                    sbottleneck=self.sub_bottleneck,
                    layer_loads=layer_loads,
                )["total"]
                setup_penalty = self.parameters.get(
                    "setup_penalty_weight", 0.0
                ) * self.estimate_setup_time(tool, wafer)
                return (
                    compound_priority - setup_penalty,
                    -wafer.ready_time,
                    -wafer.release_time,
                    -wafer.input_order,
                )

            selected = max(candidates, key=priority_key)
            decision.dispatches[tool.tool_id] = selected
            reserved_lot_ids.add(selected.id)

        # 记录本次决策的内部参数，供引擎统一落库与事后调试。
        decision.diagnostics = {
            "time": state.current_time,
            "main_bottleneck": self.main_bottleneck,
            "sub_bottleneck": self.sub_bottleneck,
            "layer_loads": {
                str(layer): round(load, 4) for layer, load in layer_loads.items()
            },
            "release_lot": decision.release_lot,
            "release_opportunity": state.release_opportunity,
            "waiting_lot_count": state.waiting_lot_count,
            "num_dispatches": len(decision.dispatches),
            "dispatched_lot_ids": [lot.id for lot in decision.dispatches.values()],
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
