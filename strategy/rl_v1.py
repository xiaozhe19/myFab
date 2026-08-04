import random
from strategy import dbr_v2 as dbr

"""
先把问题按照MDP的方式定义:
S: 什么叫一个状态？ FabSimCore返回的status
A: agent 能做什么动作？ 阶段性实现，先让RL接管Release部分的选择,也就是投料还是不投料
P: 动作执行后环境怎么变化？ FabSimCore推进
R: 怎么评价刚才动作好不好？ 先试试WIP和throughout的组合
gamma: 未来收益打几折？ 先试试0.9

dbr v2 中，release 是由PRi这个优先级控制的，其数学表达式为 r * (1.0 - remaining_bottleneck_load / BL(常数，目标瓶颈负载上限))，这个式子计算出来大于0，则投料
其实际意义是，如果当前瓶颈的工作负载没有超出设定的上限，则可以投料
所以换成RL Agent之后，Agent只需要做出 投料-1 不投料-0 这样的决策就好了

Observation:
state_key = (
    load_ratio_bucket, 描述生产中的lot会消耗多少主要瓶颈的产能
    fab_wip_bucket, 系统中所有正在加工的lot,代表整个系统的负载
    waiting_list_bucket, 正在等待加工的order
    main_buffer_bucket, 主瓶颈的负载
    sub_bottleneck_bucket, 子瓶颈的负载
)
总体考虑了主要瓶颈的供料以及整个系统的健康程度

Reward:
因为thoughout 和 平均WIP这样的指标需要一次simulation之后才能计算出来
但是，RL只接手Release部分，所以我们可以统计当前release到下一次release之间的环境变化：
    1. 两次决策之间完成多少个 工序 注意，并非生产好的产品 -> 鼓励系统流动 -> *1
    2. 两次决策之间完成了多少个 lot -> 鼓励最终产出 -> *5
    3. 这段时间的平均WIP -> 惩罚一直投料 -> * -0.1 -> *-0.5
    4. 主瓶颈在这段时间的平均负载 -> 惩罚主瓶颈过载 -> * -0.02 -> * -0.005 这个要不要换成buffer紧张程度？

算法打算使用Q-Learning，不需要value function approximation
该Agent的决策点和FabSimCore的决策点是可以公用的
"""


class RLAgent:
    # 定义RL的逻辑
    def __init__(self, actions=("hold", "release")):
        self.actions = tuple(actions)
        self.q_table = {}
        self.learning_rate = 0.1
        self.discount_factor = 0.9
        self.exploration_rate = 1.0
        self.exploration_decay = 0.5
        self.min_exploration_rate = 0.01
        self.td_error_track = []

    def bucket(self, value, thresholds):
        for index, threshold in enumerate(thresholds):
            if value < threshold:
                return index
        return len(thresholds)

    # 以下的分层都需要后续手工调整
    def get_load_ratio_bucket(self, state, dbr_strategy):
        remaining_load = dbr_strategy.get_remaining_bottleneck_load(
            state, dbr_strategy.main_bottleneck
        )
        BL = dbr_strategy.parameters["BL"]
        load_ratio = remaining_load / BL
        return self.bucket(load_ratio, [0.5, 0.75, 1.0, 1.25])

    def get_fab_wip_bucket(self, state):
        fab_wip = sum(1 for wafer in state.wafers if not wafer.completed)

        return self.bucket(fab_wip, [10, 20, 30, 40])

    def get_waiting_list_bucket(self, state):
        waiting_count = len(state.waiting_list.queue)
        return self.bucket(waiting_count, [5, 10, 15, 20])

    def get_main_buffer_bucket(self, state, dbr_strategy):
        # 计算主瓶颈的buffer的状态
        main_buffer_work = sum(
            wafer.current_process_time or 0.0
            for wafer in state.wafers
            if not wafer.completed
            and not wafer.in_process
            and wafer.ready_time <= state.current_time
            and wafer.current_process == dbr_strategy.main_bottleneck
        )
        bottleneck_machines = [
            machine
            for machine in state.machines
            if machine.type == dbr_strategy.main_bottleneck
        ]
        capacity_rate = dbr_strategy.cal_capacity_rate(bottleneck_machines)
        main_buffer_cover = (
            main_buffer_work / capacity_rate if capacity_rate > 0 else 0.0
        )
        return self.bucket(main_buffer_cover, [20, 60, 100])

    def get_sub_bottleneck_bucket(self, state, dbr_strategy):
        # 检测是否存在次瓶颈堵塞
        sub_pressure = max(dbr_strategy.previous_BDm.values(), default=0.0)
        return self.bucket(sub_pressure, [0.2, 0.6])

    def get_state_key(self, state, dbr_strategy):
        load_ratio_bucket = self.get_load_ratio_bucket(state, dbr_strategy)
        fab_wip_bucket = self.get_fab_wip_bucket(state)
        waiting_list_bucket = self.get_waiting_list_bucket(state)
        main_buffer_bucket = self.get_main_buffer_bucket(state, dbr_strategy)
        sub_bottleneck_bucket = self.get_sub_bottleneck_bucket(state, dbr_strategy)

        state_key = (
            load_ratio_bucket,
            fab_wip_bucket,
            waiting_list_bucket,
            main_buffer_bucket,
            sub_bottleneck_bucket,
        )
        return state_key

    def get_snapshot(self, state, dbr_strategy):
        # 将SimCore内部数据转换成我们需要的数据
        completed_operations = sum(
            len(wafer.operation_history) for wafer in state.wafers
        )
        completed_lots = sum(1 for wafer in state.wafers if wafer.completed)
        wip = sum(1 for wafer in state.wafers if not wafer.completed)
        main_bottleneck_load = dbr_strategy.get_remaining_bottleneck_load(
            state, dbr_strategy.main_bottleneck
        )

        return {
            "time": state.current_time,
            "completed_operations": completed_operations,
            "completed_lots": completed_lots,
            "wip": wip,
            "main_bottleneck_load": main_bottleneck_load,
        }

    def get_reward(self, prev, current):
        # 计算reward
        completed_operations = (
            current["completed_operations"] - prev["completed_operations"]
        )
        completed_lots = current["completed_lots"] - prev["completed_lots"]
        average_wip = (prev["wip"] + current["wip"]) / 2

        average_main_bottleneck_load = (
            prev["main_bottleneck_load"] + current["main_bottleneck_load"]
        ) / 2

        reward = (
            completed_operations * 1.0
            + completed_lots * 5.0
            - average_wip * 0.5
            - average_main_bottleneck_load * 0.005  #
        )

        return reward

    def ensure_state(self, state_key):
        # 确保状态在Q表中存在
        if state_key not in self.q_table:
            self.q_table[state_key] = {
                "hold": 0.0,
                "release": 0.0,
            }
        return self.q_table[state_key]

    def select_action(self, state_key, available_actions, epsilon):
        actions_q = self.ensure_state(state_key)
        # 按照epsilon-greedy来选择动作
        if random.random() < epsilon:
            return random.choice(available_actions)
        return max(available_actions, key=lambda action: actions_q[action])

    def decay_exploration(self):
        self.exploration_rate = max(
            self.min_exploration_rate,
            self.exploration_rate * self.exploration_decay,
        )

    def update_q_value(
        self,
        state_key,
        action,
        reward,
        next_state_key,
        next_available_actions,
        alpha,
        gamma,
        done=False,
    ):
        # 更新Q值
        current_q_values = self.ensure_state(state_key)
        old_q = current_q_values[action]

        if done:
            target_q = reward
        else:
            next_q_values = self.ensure_state(next_state_key)
            next_max_q = max(
                next_q_values[next_action] for next_action in next_available_actions
            )
            target_q = reward + gamma * next_max_q
        td_error = target_q - old_q
        self.td_error_track.append(td_error)
        current_q_values[action] = old_q + alpha * (target_q - old_q)


class RLDBRMixStrategy(dbr.DynamicDBR):
    name = "RL DBR Mixture"
    run_id = "rl-v1"

    def __init__(
        self,
        parameters,
        rlagent=None,
        release_interval=38,
        main_bottleneck_window=720,
        sub_bottleneck_window=5,
        alpha=0.5,
        lam=0.5,
    ):
        super().__init__(
            parameters,
            release_interval,
            main_bottleneck_window,
            sub_bottleneck_window,
            alpha,
            lam,
        )
        self.agent = rlagent or RLAgent()
        self.pending_transition = None
        self.release_decision_count = 0
        self.release_action_count = 0
        self.hold_action_count = 0

    def initialize(self, context):
        super().initialize(context)
        self.pending_transition = None
        self.release_decision_count = 0
        self.release_action_count = 0
        self.hold_action_count = 0

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
                        time=state.current_time + self.sub_bottleneck_window,
                        reason="sub_bottleneck_update",
                    ),
                    StrategyWakeup(
                        time=state.current_time + self.main_bottleneck_window,
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
                    time=state.current_time + self.main_bottleneck_window,
                    reason="main_bottleneck_update",
                )
            )

        if "sub_bottleneck_update" in trigger.strategy_reasons:
            self.update_sub_bottleneck(state, force=True)
            decision.wakeups.append(
                StrategyWakeup(
                    time=state.current_time + self.sub_bottleneck_window,
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

        # RL 接管 DBR v2 原本的 PRi > 0 投料判断。
        if trigger.release_opportunity:
            available_actions = ["hold"]
            if state.waiting_list.queue:
                available_actions.append("release")

            state_key = self.agent.get_state_key(state, self)
            snapshot = self.agent.get_snapshot(state, self)

            if self.pending_transition is not None:
                reward = self.agent.get_reward(
                    self.pending_transition["snapshot"],
                    snapshot,
                )
                self.agent.update_q_value(
                    self.pending_transition["state_key"],
                    self.pending_transition["action"],
                    reward,
                    state_key,
                    available_actions,
                    self.agent.learning_rate,
                    self.agent.discount_factor,
                    done=False,
                )

            action = self.agent.select_action(
                state_key,
                available_actions,
                self.agent.exploration_rate,
            )
            decision.release_lot = action == "release"

            self.release_decision_count += 1
            if decision.release_lot:
                self.release_action_count += 1
            else:
                self.hold_action_count += 1

            self.pending_transition = {
                "state_key": state_key,
                "action": action,
                "snapshot": snapshot,
            }

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
                setup_penalty = self.parameters.get(
                    "setup_penalty_weight", 0.0
                ) * self.estimate_setup_time(machine, wafer)
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

    def result_fields(self):
        fields = super().result_fields()
        fields["rl_v1"] = {
            "q_state_count": len(self.agent.q_table),
            "release_decision_count": self.release_decision_count,
            "release_action_count": self.release_action_count,
            "hold_action_count": self.hold_action_count,
            "learning_rate": self.agent.learning_rate,
            "discount_factor": self.agent.discount_factor,
            "exploration_rate": self.agent.exploration_rate,
            "td_error_track": self.agent.td_error_track,
        }
        return fields
