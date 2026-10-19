# myFab / Fabotron

**用离散事件仿真生成晶圆厂轨迹，用策略条件化的时空 Transformer 学习 Fab 状态演化。**

myFab 的研究重点已从调度规则对比扩展到晶圆厂的学习式状态预测。项目包含两个部分：

- **myFab DES**：运行工艺路线、设备与调度策略，提供事件日志和状态数据。
- **Fabotron**：将 Tool Group 状态编码为 Token，通过 Spatial–Temporal–Spatial（S-T-S）Transformer 预测下一时刻的内生状态。

长期希望研究学习式代理模型能否加速策略评价、支持调度与强化学习。当前发布的是 **Fabotron V1 研究原型**：已经完成 Tokenizer、单头 S-T-S、训练与基线评估；跨策略泛化、长期滚动和替代 DES 的能力仍待验证。

## 数据与模型

```text
SMT2020 工作簿 → SQLite 模型库 → DES + 调度策略
                                      ↓
                       事件日志 + Tool Group Token 序列
                                      ↓
                  输入投影 → Spatial → Temporal → Spatial
                                      ↓
                            下一帧内生状态预测
```

每个时刻的状态为 `X_t: [G,F]`，历史输入为 `[batch,T,G,F]`。
当前数据保留全部 **106 个 Tool Group**，每组一行，不压缩成单个 Fab Token。
默认历史长度为 8；现有数据每六小时采样一次，预测下一六小时边界的状态。
组数、特征维度与各列含义由数据库中的 Tokenizer schema 提供。

| 特征块 | 含义 |
|---|---|
| `W` | 本组 WIP 总量，按固定尺度缩放 |
| `P_WIP` | Recipe × 静态路线 Re-entry 次序 × 阶段的联合比例 |
| `Q` | Ready Queue 总量，按固定尺度缩放 |
| `P_Queue` | Ready Queue 的 Recipe × Re-entry 比例，Ready 阶段已隐含 |
| `C` | 总设备数、可用设备、占用槽位、停机设备的容量归一化特征 |
| `M` / `B` | PM / Breakdown 条件向量 |
| `policy` | 固定词表中的调度策略条件 |

当前冻结数据用 `product_id` 作为 Recipe 标识，使用 FIFO / SMT2020 两维 Policy 词表，总计 **796 维**。
Tokenizer 也支持显式静态 Recipe 映射。WIP 包含 Ready、Transport、Loading、Setup、Unloading 和剩余加工时间分桶。
Re-entry 按静态路线中对精确 Tool Group 的访问次序编码；实际返工仍使用所在静态工步的类别。
所有合并训练的轨迹必须具有相同 schema；扩展策略词表会改变维度和 schema ID。

### S-T-S V1

- 第一个 **Spatial**：同一时刻，所有 Tool Group 之间进行全局 Self-Attention，不加语言模型的位置编码。
- **Temporal**：各 Tool Group 独立沿时间交换信息，使用 RoPE 和因果遮罩，禁止读取未来输入。
- 第二个 **Spatial**：再次进行同一时刻的组间交换，最后取最新时刻并通过线性头预测状态。
- 每个 Block 包含 Pre-LN、残差、输出投影和 FFN。V1 默认 `d_model=64`、单头、每阶段一个 Block。

Spatial Attention 为 `softmax(QKᵀ / √d + βB)V`。
`counts[src,dst]` 汇总各产品静态路线的相邻转移次数；
`B = log1p(counts.T) / max(log1p(counts.T))`，使下游 Query 对上游 Key 获得先验偏置。
它不屏蔽其他组，也不是按实际流量估计的转移概率。Attention 权重和输入敏感性都不直接代表物理因果。

### 预测目标与 Loss

正式目标是**下一帧可预测的内生状态**，不是仅预测 WIP，也不是语言模型的离散词表分类。
线性头输出完整矩阵 `[batch,G,F]`，Loss 只监督以下部分：

- WIP / Queue 数量：Lot 单位的平滑加权 MSE，`(pred-true)^2/(true+c)`。
- 工艺组成：预测总量乘预测比例，恢复各类别 Lot 数，再计算 MAE。
- 设备动态状态：对已经按容量归一化的可用、占用、停机状态计算 MSE。

各项用**训练集标签**的零状态参考误差标定尺度，之后加权组合。
保留归一化分块 MSE 作为对照。`M`、`B`、`policy` 和静态总设备数不作为预测目标；
当前没有 Release / Completion 输出头，守恒项权重为 0。
这些是 V1 的候选设计，尚未证明最优。

## 当前状态与限制

V1 已能完成训练、验证选模、checkpoint 恢复和 Persistence 基线评估。
已有实验在部分 SMT2020 数量指标上优于保持当前状态，FIFO 预测仍有明显不足；
尚未确认完整 STS 超过容量接近的本组历史模型的稳定净收益。

- 当前训练使用轨迹内时间留出。两个策略分支来自同一仿真种子，不代表独立种子泛化验证。
- 输出未强制非负、组成和为 1、Queue≤WIP 或设备可行性。TV 指标的投影仅用于评估。
- 未监督的输出列不能用于预测；滚动预测还需要外部提供下一步的条件和控制。
- 尚未验证长期滚动、未见策略、策略切换效果或代理模型上的 RL 决策质量。
- 工作簿、SQLite、仿真 checkpoint、训练权重和研究报告不随仓库发布，使用者需自行准备数据。

## 使用

开发环境使用 Python 3.11、PyTorch 2.10、NumPy 和 openpyxl。CUDA 可选；CPU 可以运行单元检查。
请在安装了适合本机硬件的 PyTorch 的环境中运行。

```bash
git clone https://github.com/xiaozhe19/myFab.git
cd myFab
python -m pip install numpy openpyxl
```

### 导入工厂模型

准备 SMT2020 工作簿，导入 SQLite。时间单位为 minute，下面是 60 天运行、25 天预热的示例配置：

```bash
python -m fab.importer --input "path/to/SMT_2020_Model_Data_-_HVLM.xlsx" --database data/model/fab_model.sqlite --end-time 86400 --release-interval 30 --warmup-time 36000
```

SQLite 保存原始工作表及仿真配置，运行时在内存中构造 `FabModel`。
`warmup_time` 影响部分汇总统计；Tokenizer 的采集起点需要另外指定，Trace 默认记录全程事件。

### 运行 DES 并采集 Token

以下 Python 示例为两个策略使用统一 schema。请使用新的输出文件路径；示例从第 0 天采集，不代表稳态实验。

```python
from pathlib import Path
from fab.run_model import run_simulation_from_database
from fab.plugins import PluginManager, TokenizerPlugin, TracePlugin, OnlineMetricsPlugin
from fab.plugins.sqlite_storage import SQLiteStoragePlugin
from fab.plugins.tokenizer_core import TokenizerConfig

config = TokenizerConfig(
    processing_edges_minutes=(30, 60, 120),
    downtime_ratio_edges=(0.05, 0.10, 0.20),
    dispatch_rules=("FIFO", "SMT2020_BASELINE"),
    horizon_minutes=360, wip_scale=100, queue_scale=100,
)
for strategy, policy in (("FIFO", (1.0, 0.0)), ("SMT2020", (0.0, 1.0))):
    database = Path(f"data/result/{strategy}.sqlite")
    if database.exists():
        raise FileExistsError(database)
    plugins = PluginManager([
        SQLiteStoragePlugin(database), TracePlugin(), OnlineMetricsPlugin(),
        TokenizerPlugin(config, policy, start_time=0, sample_interval_minutes=360),
    ])
    run_simulation_from_database(
        strategy_spec=strategy,
        model_database=Path("data/model/fab_model.sqlite"),
        order_seed=2026061700, plugin_manager=plugins,
    )
```

仿真入口也支持 `python -m fab.run_model --help`。
目前继续使用的规则策略包括 FIFO、SMT2020_BASELINE、EDD 和 CR；
EDD / CR 通过 `strategy.edd:EDD`、`strategy.cr:CriticalRatio` 接入。
Dynamic DBR 代码保留为历史实现，已退出当前数据采集计划。
强化学习属于后续方向，当前没有发布可用的 RL 训练流程。

### 训练 Fabotron

数据需包含 Tokenizer 的 schema 和 snapshot 记录。显式指定数据库可以避免依赖本机历史目录：

```bash
python -m fabotron.train --database data/result/FIFO.sqlite data/result/SMT2020.sqlite --model-database data/model/fab_model.sqlite --epochs 100 --output-dir data/training/fabotron/example
```

训练器只读打开数据，默认每条轨迹先按时间切成 70% / 15% / 15%，再构造窗口；不跨轨迹或划分边界拼接。
验证集选择 checkpoint，训练结束后恢复最佳权重并报告测试集和 Persistence 基线。
输出目录必须为空，结果包含 `best.pt`、`config.json`、`history.json`、`summary.json`。
这条命令会运行训练、验证和最终测试，请由使用者自行启动。

Loss 配置、指标定义和恢复模型的方法见 [训练说明](fabotron/TRAINING.md)。
现有合成数据单元检查：

```bash
python -m unittest fabotron.test_training
```

## 下一阶段计划

以下内容尚未实现，不属于 V1 已具备的能力：

1. FIFO、SMT2020、EDD、CR 各运行 10 个仿真种子；各自从空厂运行 480 天，采集第 240～480 天。
2. 统一四策略 Policy 词表；若其余特征不变，Token 从 796 维变为 798 维。
3. 按完整种子分配 6 条训练、2 条验证、2 条测试；同种子的所有策略位于同一集合。
4. 补充采集时间过滤、每日统计、批次清单和适合多轨迹的数据加载方式。
5. 在固定数据划分下比较多头、深度与输出约束，保留容量配平的本组历史基线。

统一预热 240 天不自动证明各策略进入稳态；持续积压的轨迹应作为非稳态场景解释。

## 代码布局

| 路径 | 职责 |
|---|---|
| `fab/engine/` | DES 事件、加工、组批和停机逻辑 |
| `fab/model/`、`fab/importer/` | SMT2020 模型构造与原始表导入 |
| `fab/plugins/` | SQLite、事件、指标与 Tokenizer 采集 |
| `strategy/` | 调度策略及历史 DBR 实现 |
| `fabotron/model/` | S-T-S、Spatial / Temporal Block、Route Bias |
| `fabotron/dataset.py` | Token SQLite 读取和时间划分 |
| `fabotron/loss.py` | 候选状态 Loss 与评估指标 |
| `fabotron/train.py` | 训练、验证选模、测试和 checkpoint |

这是学习与研究项目，尚未接入真实生产数据，也不提供工业部署或生产调度保证。

Created by [xiaozhe19](https://github.com/xiaozhe19)
