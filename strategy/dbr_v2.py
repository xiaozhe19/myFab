<<<<<<< ours
import math


class DynamicDBR:
    """
    动态 DBR 策略对象。

    __init__ 接收算法参数；initialize(context) 接收工厂静态信息；
    push_next(state, trigger) 将在下一步实现，用于读取实时状态并返回决策。
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
        self.factory_config = None
        self.simulation_config = None
        self.products = {}
        self.product_count = 0
        self.recipes = {}
        self.machine_configs = []
        self.machine_groups = {}
        self.machine_parameters = {}
        self.process_names = ()

        # DBR 跨事件、跨更新周期需要保存的算法状态。
        self.main_bottleneck = None
        self.sub_bottleneck = None
        self.previous_BDm = {}
        self.last_main_bottleneck_update = None
        self.last_sub_bottleneck_update = None
        self.timers_started = False

    def initialize(self, context):
        """由 Engine 在每次仿真开始时调用一次。"""
        self.factory_config = context.factory_config
        self.simulation_config = context.simulation_config
        self.products = context.products
        self.product_count = len(self.products)
        self.process_names = context.process_names

        # 保留每个 recipe 的 process 和 process_time，供负荷及 Layer 计算。
        self.recipes = {
            product_id: [
                {
                    **operation,
                    "process": str(operation["process"]),
                    "process_time": float(operation["process_time"]),
                }
                for operation in product["route"]
            ]
            for product_id, product in self.products.items()
        }

        self.machine_configs = list(
            self.factory_config.get("machines", [])
        )
        downtime_models = self.factory_config.get(
            "machine_downtime", {}
        ).get("models", {})

        # 同一 process 对应多台并行设备。
        self.machine_groups = {}
        self.machine_parameters = {}
        for machine in self.machine_configs:
            machine_id = machine["id"]
            process = machine["type"]
            self.machine_groups.setdefault(process, []).append(machine_id)
            self.machine_parameters[machine_id] = {
                **machine,
                "downtime_model": downtime_models.get(machine_id),
            }

        # 复用策略对象开始新仿真时，不能沿用上一轮动态状态。
        self.main_bottleneck = None
        self.sub_bottleneck = None
        self.previous_BDm = {
            process: 0.0 for process in self.process_names
        }
        self.last_main_bottleneck_update = None
        self.last_sub_bottleneck_update = None
        self.timers_started = False


    # 产能与瓶颈识别
    def cal_machine_availability(self, machine): #单个机器的Cm
        """计算单台设备长期平均可用率。"""
        model = machine.downtime_model
        if model:
            mtbf = (model["mtbf_min"] + model["mtbf_max"]) / 2
            mttr = (model["mttr_min"] + model["mttr_max"]) / 2
            return mtbf / (mtbf + mttr)
        return 1.0

    def cal_capacity_rate(self, machines): #一个工序的Cm
        """计算工作站组有效产能率，单位 machine-minute/minute。"""
        if not isinstance(machines, (list, tuple)):
            machines = [machines]
        return sum(
            self.cal_machine_availability(machine)
            for machine in machines
        )

    def cal_Cm(self, machine, window=720): # 主瓶颈的Cm
        """计算单台设备在指定窗口内的有效产能。"""
        return window * self.cal_machine_availability(machine)

    def get_mbottolneck(
        self,
        machines,
        waiting_list,
        products,
        window=720,
    ):
        """返回主瓶颈、负荷率、各工序需求和各工序产能。"""
        lots = (
            waiting_list.queue
            if hasattr(waiting_list, "queue")
            else waiting_list
        )
        products = (
            products
            if isinstance(products, dict)
            else {product["id"]: product for product in products}
        )

        product_demand = {}
        for lot in lots:
            product_demand[lot.product_id] = (
                product_demand.get(lot.product_id, 0)
                + getattr(lot, "quantity", 1)
            )

        machine_groups = {}
        for machine in machines:
            machine_groups.setdefault(machine.type, []).append(machine)

        Dm_list = {}
        Cm_list = {}
        load_ratio_list = {}

        for process, process_machines in machine_groups.items():
            Dm = 0.0
            for product_id, demand in product_demand.items():
                process_time = sum(
                    step["process_time"]
                    for step in products[product_id]["route"]
                    if step["process"] == process
                )
                Dm += demand * process_time

            # 同一工序的并行机器产能需要相加。
            Cm = sum(
                self.cal_Cm(machine, window)
                for machine in process_machines
            )
            Dm_list[process] = Dm
            Cm_list[process] = Cm
            load_ratio_list[process] = (
                Dm / Cm if Cm else float("inf")
            )

        if not load_ratio_list:
            return None, 0.0, {}, {}

        bottleneck = max(load_ratio_list, key=load_ratio_list.get)
        return (
            bottleneck,
            load_ratio_list[bottleneck],
            Dm_list,
            Cm_list,
        )

    def cal_IBD(self, state, process, lam=None, window=None):
        """计算某个工作站组的瞬时瓶颈度。"""
        lam = self.lam if lam is None else lam
        window = (
            self.sub_bottleneck_window
            if window is None
            else window
        )
        process_machines = [
            machine
            for machine in state.machines
            if machine.type == process
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
        Cm = sum(
            self.cal_Cm(machine, window)
            for machine in process_machines
        )
        return (
            1 - math.exp(-lam * QLm / Cm)
            if Cm > 0
            else 0.0
        )

    def get_sbottleneck(
        self,
        state,
        previous_BDm=None,
        alpha=None,
        window=None,
        lam=None,
        main_bottleneck=None,
    ):
        """使用瞬时瓶颈度和指数平滑识别次瓶颈。"""
        previous_BDm = (
            self.previous_BDm
            if previous_BDm is None
            else previous_BDm
        )
        alpha = self.alpha if alpha is None else alpha
        window = (
            self.sub_bottleneck_window
            if window is None
            else window
        )
        lam = self.lam if lam is None else lam
        main_bottleneck = (
            self.main_bottleneck
            if main_bottleneck is None
            else main_bottleneck
        )

        processes = list(
            dict.fromkeys(machine.type for machine in state.machines)
        )
        BDm_list = {}
        for process in processes:
            IBDm = self.cal_IBD(
                state,
                process,
                lam=lam,
                window=window,
            )
            BDm_list[process] = (
                alpha * IBDm
                + (1 - alpha) * previous_BDm.get(process, 0.0)
            )

        candidates = {
            process: degree
            for process, degree in BDm_list.items()
            if process != main_bottleneck
        }
        if not candidates:
            return None, 0.0, BDm_list
        bottleneck = max(candidates, key=candidates.get)
        return bottleneck, candidates[bottleneck], BDm_list

    # Layer 与 Drum
    def build_layer_list(self, mbottleneck, wafer):
        """按第几次经过主瓶颈，将 lot 路线划分为 Layer。"""
        layer_list = []
        layer_start = 0

        for step, operation in enumerate(wafer.route):
            if operation["process"] != mbottleneck:
                continue

            layer_operations = wafer.route[layer_start : step + 1]
            layer_list.append(
                {
                    "layer": len(layer_list) + 1,
                    "start_step": layer_start,
                    "end_step": step,
                    "PT": float(operation["process_time"]),
                    "FT": sum(
                        float(item["process_time"])
                        for item in layer_operations
                    ),
                }
            )
            layer_start = step + 1

        return layer_list

    def cal_PT(self, layer_list):
        """返回各 Layer 的主瓶颈加工时间 PT。"""
        return {
            layer["layer"]: float(layer["PT"])
            for layer in layer_list
        }

    def cal_FT(self, layer_list):
        """返回各 Layer 的理想流动时间 FT。"""
        return {
            layer["layer"]: float(layer["FT"])
            for layer in layer_list
        }

    def get_layer(self, layer_list, wafer):
        """返回 lot 当前所属 Layer；最后一次主瓶颈后返回 None。"""
        if wafer.completed:
            return None
        for layer in layer_list:
            if wafer.step <= layer["end_step"]:
                return layer["layer"]
        return None

    def get_layer_load(self, mbottleneck, wafers):
        """统计各层当前 WIP 的 Layer Load。"""
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
            layer_load_list[layer] = (
                layer_load_list.get(layer, 0.0) + PT / FT
            )
        return layer_load_list

    def cal_bottleneck_heartbeat(
        self,
        state,
        mbottleneck,
        layer_list,
    ):
        """计算主瓶颈 Drum 节拍 Dt。"""
        process_machines = [
            machine
            for machine in state.machines
            if machine.type == mbottleneck
        ]
        capacity_rate = self.cal_capacity_rate(process_machines)
        if capacity_rate <= 0:
            return float("inf")
        total_bottleneck_pt = sum(
            self.cal_PT(layer_list).values()
        )
        return total_bottleneck_pt / capacity_rate
    
    # Operation Buffer
    def get_operation_key(self, wafer, step=None):
        """用 (process, visit_number) 表示多产品环境中的操作 i。"""
        step = wafer.step if step is None else step
        if step < 0 or step >= len(wafer.route):
            return None
        process = wafer.route[step]["process"]
        visit_number = sum(
            1
            for operation in wafer.route[: step + 1]
            if operation["process"] == process
        )
        return process, visit_number

    def wafer_is_waiting_for_operation(
        self,
        wafer,
        operation_key,
        current_time,
    ):
        """判断 lot 是否正在目标操作 i 前等待。"""
        return (
            not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= current_time
            and self.get_operation_key(wafer) == operation_key
        )

    def cal_Bi(self, state, operation_key):
        """计算操作 i 前 Buffer 的预计清空时间。"""
        if operation_key is None:
            return 0.0
        waiting_work = sum(
            float(wafer.current_process_time or 0.0)
            for wafer in state.wafers
            if self.wafer_is_waiting_for_operation(
                wafer,
                operation_key,
                state.current_time,
            )
        )
        machines = [
            machine
            for machine in state.machines
            if machine.type == operation_key[0]
        ]
        capacity_rate = self.cal_capacity_rate(machines)
        return (
            waiting_work / capacity_rate
            if capacity_rate > 0
            else float("inf")
        )

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
        if wafer.route[step]["process"] != mbottleneck:
            return None
        return sum(
            1
            for operation in wafer.route[: step + 1]
            if operation["process"] == mbottleneck
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
            priority += m2 * (
                layer_loads.get(j + 1, 0.0) / L0 - 1.0
            )
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
            and wafer.route[next_step]["process"] == sbottleneck
        ):
            return s1 * self.cal_Bi(
                state,
                self.get_operation_key(wafer, next_step),
            )
        if (
            previous_step >= 0
            and wafer.route[previous_step]["process"] == sbottleneck
        ):
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

        ti = state.last_operation_completion.get(
            wafer.current_operation_id
        )
        if ti is None:
            # 首次加工时让 Drum 偏差项初始化为 0。
            ti = state.current_time - Dt
        return (
            p1 * ((state.current_time - ti) / Dt - 1.0)
            + p2 * (wafer.step + 1) / len(wafer.route)
        )

    def get_remaining_bottleneck_load(
        self,
        state,
        mbottleneck,
    ):
        """计算 Fab 内所有 lot 的剩余主瓶颈工作量。"""
        return sum(
            float(operation["process_time"])
            for wafer in state.wafers
            if not wafer.completed
            for operation in wafer.route[wafer.step :]
            if operation["process"] == mbottleneck
        )

    def cal_Pri(self, state, mbottleneck, BL, r):
        """计算投料子优先级 PRi。"""
        if BL <= 0:
            raise ValueError("BL must be greater than zero.")
        load = self.get_remaining_bottleneck_load(
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
        parameters = (
            self.parameters
            if parameters is None
            else parameters
        )
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

    def estimate_setup_time(self, machine, wafer):
        """
        估计把当前机器切换到候选 lot 产品所需的平均 setup 时间。

        Engine 真正执行 setup 时会在配置区间内随机采样；策略决策阶段不能
        预知该随机值，因此使用区间均值。若机器继续加工相同产品，惩罚为 0。
        """
        setup = self.factory_config.get("setup", {})
        if not setup.get("enabled", False):
            return 0.0
        if (
            machine.current_product_id is None
            or machine.current_product_id == wafer.product_id
        ):
            return 0.0

        transition = (
            f"{machine.current_product_id}->{wafer.product_id}"
        )
        model = setup.get("product_change_overrides", {}).get(
            transition,
            setup.get("default_product_change_time", {}),
        )
        low = float(model.get("min", 0.0))
        high = float(model.get("max", low))
        mean_setup = (low + high) / 2
        multiplier = float(
            setup.get("machine_type_multiplier", {}).get(
                machine.type,
                1.0,
            )
        )
        return mean_setup * multiplier

    def push_next(self, state, trigger):
        """
        在每批事件结算后生成下一步决策。

        1. 更新主瓶颈和次瓶颈；
        2. 判断本次投料机会是否释放一个新 lot；
        3. 为每台空闲机器选择复合优先级最高的 lot。
        """
        from fab.core import (
            StrategyDecision,
            StrategyWakeup,
            ready_candidates,
        )

        decision = StrategyDecision()

        # 第一次进入策略时先建立初始瓶颈状态，并把两个周期事件注入 Engine。
        if not self.timers_started:
            self.update_main_bottleneck(state, force=True)
            self.update_sub_bottleneck(state, force=True)
            decision.wakeups.extend(
                [
                    StrategyWakeup(
                        time=state.current_time
                        + self.sub_bottleneck_window,
                        reason="sub_bottleneck_update",
                    ),
                    StrategyWakeup(
                        time=state.current_time
                        + self.main_bottleneck_window,
                        reason="main_bottleneck_update",
                    ),
                ]
            )
            self.timers_started = True

        # 以后只在 Engine 准时传回策略定时事件时更新，并立即预约下一次。
        if "main_bottleneck_update" in trigger.strategy_reasons:
            self.update_main_bottleneck(state, force=True)
            decision.wakeups.append(
                StrategyWakeup(
                    time=state.current_time
                    + self.main_bottleneck_window,
                    reason="main_bottleneck_update",
                )
            )

        if "sub_bottleneck_update" in trigger.strategy_reasons:
            self.update_sub_bottleneck(state, force=True)
            decision.wakeups.append(
                StrategyWakeup(
                    time=state.current_time
                    + self.sub_bottleneck_window,
                    reason="sub_bottleneck_update",
                )
            )

        if self.main_bottleneck is None:
            return decision

        # 当前时刻所有候选共用一份 Layer Load，避免反复扫描全厂 WIP。
        layer_loads = self.get_layer_load(
            self.main_bottleneck,
            state.wafers,
        )

        #  PRi大于0 则放入新的lot。
        if trigger.release_opportunity and state.waiting_list.queue:
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
        reserved_wafer_ids = set()
        for machine in sorted(
            state.machines,
            key=lambda item: (item.available_time, item.id),
        ):
            if machine.available_time > state.current_time:
                continue

            candidates = [
                wafer
                for wafer in ready_candidates(
                    state.wafers,
                    machine,
                    state.current_time,
                )
                if wafer.id not in reserved_wafer_ids
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
                setup_penalty = (
                    self.parameters.get("setup_penalty_weight", 0.0)
                    * self.estimate_setup_time(machine, wafer)
                )
                priority = compound_priority - setup_penalty
                # 优先级相同时保持 FIFO，保证结果稳定且避免随机选择。
                return (
                    priority,
                    -wafer.ready_time,
                    -wafer.release_time,
                    -wafer.input_order,
                )

            selected = max(candidates, key=priority_key)
            decision.dispatches[machine.id] = selected
            reserved_wafer_ids.add(selected.id)

        return decision

    def update_main_bottleneck(self, state, force=False):
        """按照长周期更新主瓶颈，避免每次事件都重新计算。"""
        should_update = (
            force
            or self.last_main_bottleneck_update is None
            or state.current_time
            >= self.last_main_bottleneck_update
            + self.main_bottleneck_window
        )
        if not should_update:
            return

        (
            main_bottleneck,
            _,
            _,
            _,
        ) = self.get_mbottolneck(
            state.machines,
            state.waiting_list,
            state.products,
            window=self.main_bottleneck_window,
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
            >= self.last_sub_bottleneck_update
            + self.sub_bottleneck_window
        )
        if not should_update:
            return

        (
            self.sub_bottleneck,
            _,
            self.previous_BDm,
        ) = self.get_sbottleneck(
            state,
            previous_BDm=self.previous_BDm,
            alpha=self.alpha,
            window=self.sub_bottleneck_window,
            lam=self.lam,
            main_bottleneck=self.main_bottleneck,
        )
        self.last_sub_bottleneck_update = state.current_time

    def sample(self, state):
        """记录动态瓶颈和 Layer Load，便于验证 DBR 的运行过程。"""
        return {
            "dbr_v2_samples": {
                "time": state.current_time,
                "main_bottleneck": self.main_bottleneck,
                "sub_bottleneck": self.sub_bottleneck,
                "layer_loads": (
                    self.get_layer_load(
                        self.main_bottleneck,
                        state.wafers,
                    )
                    if self.main_bottleneck is not None
                    else {}
                ),
                "BDm": dict(self.previous_BDm),
            }
        }

    def result_fields(self):
        """将策略参数写入结果 JSON，保证实验可以复查。"""
        return {
            "dbr_v2_parameters": dict(self.parameters),
            "dbr_v2_update_policy": {
                "main_bottleneck_window": self.main_bottleneck_window,
                "sub_bottleneck_window": self.sub_bottleneck_window,
                "alpha": self.alpha,
                "lambda": self.lam,
            },
        }
=======
"""适合实验用的简化 DBR 策略。

它只保留三个可解释规则：以路线负荷/设备数识别 Drum，控制 Drum 前的
工作量，并优先推进即将到达 Drum 的 lot。复杂的二级瓶颈、参数寻优和
多层优先级模型不适合这个小型仿真器，也难以验证其实际收益。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fab.engine.eligibility import eligible_lots
from fab.model.entities import FabModel, LotState, ToolState
from fab.strategy import StrategyDecision, StrategyState


class DynamicDBR:
    """一个小而可复现的 Drum-Buffer-Rope 派工策略。"""

    name = "Dynamic DBR"

    def __init__(
        self,
        parameters: dict[str, Any] | None = None,
        sub_bottleneck_window: float | None = None,
        **_: Any,
    ) -> None:
        values = parameters or {}
        self.parameters = {"BL": float(values.get("BL", 1000.0))}
        self.buffer_limit = self.parameters["BL"]
        self.sub_bottleneck_window = sub_bottleneck_window
        self.model: FabModel | None = None
        self.main_bottleneck: str | None = None
        self.sub_bottleneck: str | None = None
        self.previous_BDm: dict[str, float] = {}

    def initialize(self, model: FabModel) -> None:
        self.model = model
        self.main_bottleneck = self._find_bottleneck(model)

    def decide(self, state: StrategyState) -> StrategyDecision:
        if self.main_bottleneck is None:
            self.main_bottleneck = self._find_bottleneck(state.model)
        self.previous_BDm = self._buffer_pressure(state)
        self.sub_bottleneck = self._largest_pressure_except(self.main_bottleneck)

        dispatches: dict[str, LotState] = {}
        explanations: dict[str, object] = {}
        reserved_lot_ids: set[str] = set()
        for tool in sorted(
            state.tool_states.values(), key=lambda item: (item.available_time, item.id)
        ):
            candidates = [
                lot
                for lot in eligible_lots(
                    state.model, tool, list(state.lots), state.current_time
                )
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue
            selected = min(candidates, key=lambda lot: self._priority(lot, tool))
            dispatches[tool.tool_id] = selected
            explanations[tool.tool_id] = {
                "rule": "按距 Drum 工作量、lot 优先级和 FIFO 排序。",
                "work_to_drum": self._work_to_drum(selected),
                "selected_lot_id": selected.id,
            }
            reserved_lot_ids.add(selected.id)

        buffer_work = self.get_remaining_bottleneck_load(state, self.main_bottleneck)
        can_release = state.release_opportunity and state.waiting_lot_count > 0
        return StrategyDecision(
            dispatches=dispatches,
            release_lot=can_release and buffer_work < self.buffer_limit,
            diagnostics={
                "strategy": {
                    "name": self.name,
                    "drum": self.main_bottleneck,
                    "buffer_work": round(buffer_work, 3),
                    "buffer_limit": self.buffer_limit,
                    "secondary_pressure": self.previous_BDm,
                },
                "release": {
                    "rule": "Drum 前工作量低于 buffer_limit 时投料。",
                    "action": "release" if can_release and buffer_work < self.buffer_limit else "hold",
                },
                "dispatches": explanations,
            },
        )

    def cal_capacity_rate(self, tools: list[ToolState]) -> float:
        """返回并行设备数，供 RL 把累计加工时间换算成大致覆盖时间。"""

        return float(len(tools))

    def get_remaining_bottleneck_load(
        self, state: StrategyState, bottleneck: str | None
    ) -> float:
        """计算所有未完成 lot 到下一次 Drum 前（含 Drum）的平均加工时间。"""

        if bottleneck is None:
            return 0.0
        return round(
            sum(
                work
                for lot in state.lots
                if not lot.completed
                for work in [self._work_to_process(lot, bottleneck)]
                if work is not None
            ),
            3,
        )

    def result_fields(self) -> dict[str, object]:
        return {
            "dbr_parameters": {"buffer_limit": self.buffer_limit},
            "dbr_drum": self.main_bottleneck,
        }

    def _find_bottleneck(self, model: FabModel) -> str:
        demand: dict[str, float] = defaultdict(float)
        capacity: dict[str, int] = defaultdict(int)
        product_weights = dict(model.synthetic_order.product_mix)
        for product in model.products.values():
            weight = product_weights.get(product.id, 1.0)
            for operation_id in product.operation_ids:
                profile = model.operation_profiles[operation_id]
                demand[profile.process_family] += weight * profile.mean_process_time
        for tool in model.tools.values():
            capacity[model.tool_groups[tool.tool_group_id].process_family] += 1
        return max(demand, key=lambda process: demand[process] / capacity[process])

    def _buffer_pressure(self, state: StrategyState) -> dict[str, float]:
        work: dict[str, float] = defaultdict(float)
        tools: dict[str, int] = defaultdict(int)
        for tool in state.tool_states.values():
            tools[tool.type] += 1
        for lot in state.lots:
            if not lot.completed and not lot.in_process and lot.ready_time <= state.current_time:
                work[lot.current_process or "Unknown"] += lot.current_process_time or 0.0
        return {
            process: round(total / max(tools[process], 1) / self.buffer_limit, 4)
            for process, total in work.items()
        }

    def _largest_pressure_except(self, process: str | None) -> str | None:
        candidates = [item for item in self.previous_BDm.items() if item[0] != process]
        return max(candidates, key=lambda item: item[1])[0] if candidates else None

    def _priority(self, lot: LotState, tool: ToolState) -> tuple[float, ...]:
        same_product = 0 if lot.product_id == tool.current_product_id else 1
        return (
            self._work_to_drum(lot),
            -float(lot.priority),
            same_product,
            lot.ready_time,
            lot.release_time,
            float(lot.input_order),
        )

    def _work_to_drum(self, lot: LotState) -> float:
        return self._work_to_process(lot, self.main_bottleneck) or float("inf")

    @staticmethod
    def _work_to_process(lot: LotState, process: str | None) -> float | None:
        if process is None or lot.completed:
            return None
        work = 0.0
        for operation in lot.route[lot.operation_index :]:
            work += float(operation["process_time"])
            if operation["process"] == process:
                return work
        return None
>>>>>>> theirs
