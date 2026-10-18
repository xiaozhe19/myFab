# S-T-S V1 训练

在项目根目录、已有的 `torch` Conda 环境中运行。模型源文件仍在 `fabotron/model/`。

```powershell
conda activate torch
python -m fabotron.train --epochs 50
```

默认读取 `data/result/training_240d_seed2026061700_20261018/` 下的 FIFO.sqlite 和
SMT2020.sqlite。也可以只训练一个数据库：

```powershell
python -m fabotron.train --database data/result/training_240d_seed2026061700_20261018/SMT2020.sqlite --epochs 50
```

## 输入、标签与划分

- 每个数据库/run_id 作为独立轨迹，SQLite 只读打开，不跨轨迹构造窗口。
- 输入 8 个状态 `[B,8,106,F]`，标签取第 9 个状态的完整 matrix `[B,106,F]`。
- F 从 schema 读取；当前准备的数据是 796 维。
- 每条轨迹先按快照时间切成 70%/15%/15%，再在各自片段内构造窗口。训练和验证不共享任何原始快照。
- 每条 961 快照的轨迹产生 664/136/137 个训练/验证/测试样本。短片段不够 history_len+1 会报错。
- 这属于轨迹内时间留出。当前两个策略分支来自同一仿真种子，不代表独立种子泛化验证。

## 候选 V1 Loss

默认 `--loss v1`。全部内生状态参与训练，PM/Breakdown/Policy 作为输入，输出对应列不监督。
静态 C.total_tools 也不监督。没有 Release/Completion 预测头，因此守恒权重固定为 0。

1. W、Q：从 Tokenizer 固定缩放恢复 Lot 数，计算 `(pred-true)^2/(true+c)`，默认 c=1 Lot。
2. P_WIP、P_Queue：分别用预测总量乘预测比例、真实总量乘真实比例，比较各类别 Lot 数的 MAE。
3. E：直接比较已经按容量归一化的 available_tools、occupied_slots、down_tools，用 MSE。
4. WIP 和 Queue 分布分别标定尺度，归一化后平均成 P 项，避免 702 与 78 个桶的数量差异改变重要性。

默认只用**训练标签**标定每个项的典型尺度：计算“零状态预测”相对于训练目标的平均该项损失。
各项除以自己的尺度后，按 λW、λQ、λP、λE 相加。分布项是两个归一化子项的平均。
这等价于按训练集典型量级确定有效权重；尺度小于等于 1e-8 时回退为 1，避免除零和无限权重。
它是明确的候选标定规则，并非已经证明最优。

可用 `--lambda-w`、`--lambda-q`、`--lambda-p`、`--lambda-e` 调整模块权重，
`--smooth-count` 调整 c。`--loss-normalization none` 关闭训练集尺度标定。
实际尺度和权重保存在 config.json、summary.json 和 best.pt 中。

保留归一化分块 MSE 对照：

```powershell
python -m fabotron.train --loss block-mse --epochs 50
```

该对照用 W、Q、P_WIP、P_Queue、设备动态状态的分块 MSE，训练标签均方值（RMS 的平方）定标后加权平均。
遵守 V1 的目标范围，排除 PM/Breakdown/Policy/静态容量，因此不是旧的八块全列监督。
不同 Loss 的标量大小不可直接比较；比较同一留出集的 Lot MAE、类别计数误差与组成 TV。

## 输出与检查

每次运行新建 `data/training/fabotron/<香港时间戳>/`，或用 `--output-dir` 指定空目录。
已有非空目录不会被覆盖。

- best.pt：按验证 Loss 选择的模型、优化器、模型配置、schema、Loss 配置与源码哈希。
- config.json：数据来源、分割边界、超参数、训练标签拟合的尺度。
- history.json：每轮训练/验证结果。
- summary.json：恢复最优模型后的验证/测试结果、保持当前状态基线，以及各轨迹单独的指标。

仅验证集选择 epoch；测试集只在训练结束后评分。默认最多 50 epoch，10 epoch 无改善早停。
预测头以零初始化，便于各个尺度归一化项从可解释的零状态参考开始。

**当前输出仍是未约束的线性回归值。**训练不会自动保证非负、概率和为 1、Queue≤WIP 或设备可行性。
summary 同时报告负数量比例、Queue 超出 WIP 的比例和原始分布质量误差。
组成 TV 仅在真实非空组上计算：评估时将预测比例截负、归一化；无正质量时回退均匀分布。
这个投影只用于 TV 指标，不用于训练，也不掩盖原始输出的非法概率质量。
PM/Breakdown/Policy 和静态容量输出列未训练，不能作为预测结果使用；下一帧的这些条件需要外部提供。
本脚本暂不执行长期滚动，单步训练成功不能证明可替代 DES。

短流程检查和单元检查：

```powershell
python -m unittest fabotron.test_training
python -m fabotron.train --max-snapshots 80 --epochs 2 --batch-size 8
```

恢复模型：

```python
import torch
from fabotron.model.fabotron import Fabotron

checkpoint = torch.load("路径/best.pt", map_location="cpu", weights_only=True)
state = checkpoint["model_state"]
model = Fabotron(**checkpoint["model_config"], route_bias=state.get("route_bias"))
model.load_state_dict(state)
model.eval()
```

文件职责：dataset.py 负责读取与分割，loss.py 负责目标和指标，train.py 负责训练、验证和保存。

## 实现验证（2026-10-18）

8 项 unittest 已通过，包括跨轨迹/跨分割检查、完整下一帧标签对齐、未监督字段零梯度、
训练集独占权重标定、零尺度处理、参数更新和 checkpoint 恢复。
在真实 FIFO 与 SMT2020 数据上，V1 与 block-mse 都以每条轨迹前 80 张快照运行了 2 epoch，
训练、验证、测试与新模型实例恢复均通过。该短检查没有超过 persistence 基线，不能视为预测能力验收。
测试临时目录与权重已清理；这不是已经完成的 50 epoch 正式训练。
