from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

from fab.random_run import extract_run_metrics
from fab.run_sim import (
    DEFAULT_FACTORY_PATH,
    DEFAULT_MACHINE_PATH,
    DEFAULT_ORDERS_PATH,
    DEFAULT_PRODUCT_PATH,
    DEFAULT_RESULT_DIR,
    DEFAULT_SEEDS_PATH,
    DEFAULT_SIMULATION_PATH,
    build_order_pool,
    load_strategy_parameters,
    run_sim,
)
from fab.settings import (
    apply_factory_seeds,
    build_factory_config,
    load_json,
    load_seed_sets,
)
from machine.settings import load_machine_config
from strategy.rl_v1 import RLAgent, RLDBRMixStrategy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PARAMS_PATH = PROJECT_ROOT / "strategy" / "config" / "DBR_v2_config.json"
TRAINING_OUTPUT_PATH = DEFAULT_RESULT_DIR / "rl_v1_training_result.json"
Q_TABLE_OUTPUT_PATH = DEFAULT_RESULT_DIR / "rl_v1_q_table.json"


def q_table_to_json(q_table):
    """把 tuple 形式的 state_key 转成字符串，方便写入 JSON。

    训练时 Q-table 的 key 长这样：
        (2, 4, 3, 1, 0)

    JSON 的 key 只能稳定使用字符串，所以保存时转成：
        "2|4|3|1|0"
    """

    payload = {}
    for state_key, action_values in q_table.items():
        json_key = "|".join(str(part) for part in state_key)
        payload[json_key] = dict(action_values)
    return payload


def save_training_result(agent, episode_rows):
    """保存训练结果和最终 Q-table。

    这里保存两个文件：
    1. rl_v1_training_result.json：每个 episode 的指标，方便看训练过程
    2. rl_v1_q_table.json：最终学到的 Q-table，后面评估/部署会用
    """

    DEFAULT_RESULT_DIR.mkdir(exist_ok=True)

    q_payload = {
        "q_table": q_table_to_json(agent.q_table),
        "exploration_rate": agent.exploration_rate,
        "learning_rate": agent.learning_rate,
        "discount_factor": agent.discount_factor,
    }
    with Q_TABLE_OUTPUT_PATH.open("w", encoding="utf-8") as file:
        json.dump(q_payload, file, indent=2, ensure_ascii=False)
        file.write("\n")

    training_payload = {
        "result_kind": "rl_v1_training",
        "episodes": len(episode_rows),
        "final_q_state_count": len(agent.q_table),
        "final_exploration_rate": agent.exploration_rate,
        "episode_results": episode_rows,
        "td_error_track": agent.td_error_track,
        "q_table_path": str(Q_TABLE_OUTPUT_PATH),
    }
    with TRAINING_OUTPUT_PATH.open("w", encoding="utf-8") as file:
        json.dump(training_payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


def train_rl(episodes=20):
    # 训练

    # 读取一次静态配置。后面每个 episode 都复用这些配置。
    seed_sets = load_seed_sets(DEFAULT_SEEDS_PATH)
    strategy_params = load_strategy_parameters(STRATEGY_PARAMS_PATH)
    base_factory_config = build_factory_config(
        load_json(DEFAULT_FACTORY_PATH),
        load_json(DEFAULT_PRODUCT_PATH),
        load_machine_config(DEFAULT_MACHINE_PATH),
    )
    simulation_config = load_json(DEFAULT_SIMULATION_PATH)
    order_config = load_json(DEFAULT_ORDERS_PATH)

    # 只创建一个 agent。这个 agent 会跨 episode 保留 Q-table。
    agent = RLAgent()
    episode_rows = []

    for episode in range(1, episodes + 1):
        # 选择本 episode 使用哪组随机种子。
        # 如果 episode 数量超过 seed 数量，就从头循环使用。
        seed_index = (episode - 1) % len(seed_sets)
        seed_set = seed_sets[seed_index]

        # 每个 episode 都要重新构造 factory_config 和 order_pool。
        # 这相当于 reset environment。
        factory_config = apply_factory_seeds(
            deepcopy(base_factory_config),
            seed_set,
        )
        order_pool = build_order_pool(
            factory_config,
            deepcopy(order_config),
            seed_set["order_seed"],
        )

        # 每个 episode 创建一个新的 strategy。
        # 但是把同一个 agent 传进去，所以 Q-table 不会丢。
        strategy = RLDBRMixStrategy(
            rlagent=agent,
            **deepcopy(strategy_params),
        )

        epsilon_before = agent.exploration_rate
        td_error_start = len(agent.td_error_track)

        # 跑完整个仿真。
        # 在 run_sim 内部，strategy.push_next 会不断被调用；
        # RL agent 会在 release opportunity 时更新 Q-table。
        result = run_sim(
            strategy,
            factory_config,
            deepcopy(simulation_config),
            order_pool,
        )

        # 一个 episode 结束后，再衰减 epsilon。
        agent.decay_exploration()

        metrics = extract_run_metrics(result)
        episode_td_errors = agent.td_error_track[td_error_start:]
        mean_abs_td = (
            sum(abs(value) for value in episode_td_errors) / len(episode_td_errors)
            if episode_td_errors
            else 0.0
        )

        row = {
            "episode": episode,
            "seed_index": seed_index + 1,
            "epsilon_before": round(epsilon_before, 6),
            "epsilon_after": round(agent.exploration_rate, 6),
            "q_state_count": len(agent.q_table),
            "release_action_count": strategy.release_action_count,
            "hold_action_count": strategy.hold_action_count,
            "td_error_abs_mean": round(mean_abs_td, 6),
            "metrics": metrics,
        }
        episode_rows.append(row)

        print(
            f"Episode {episode}/{episodes} | "
            f"seed {seed_index + 1} | "
            f"throughput {metrics.get('throughput', 0):g} | "
            f"MCT {metrics.get('mct_average', 0):g} | "
            f"WIP {metrics.get('average_fab_wip', 0):g} | "
            f"epsilon {epsilon_before:.3f}->{agent.exploration_rate:.3f} | "
            f"Q states {len(agent.q_table)}"
        )

    save_training_result(agent, episode_rows)
    return {
        "agent": agent,
        "episode_results": episode_rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Train RL v1 with simple episodes.")
    parser.add_argument("--episodes", type=int, default=20)
    args = parser.parse_args()

    train_rl(episodes=args.episodes)
    print(f"Saved training result to {TRAINING_OUTPUT_PATH}")
    print(f"Saved Q-table to {Q_TABLE_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
