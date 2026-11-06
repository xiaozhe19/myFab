# myFab

**基于 SMT2020 的晶圆厂离散事件仿真器（DES），用于比较派工与投料策略。**

myFab 按 SMT2020 HVLM 数据集构建一座半导体前道工厂：1443 台设备、106 个设备组，包含多产品重入路线、组批、setup、PM、故障和 CQT。仓库包含三部分：

- **仿真引擎**：工艺路线、设备、组批与停机。
- **可替换策略**：派工规则和投料方式可以分别替换、任意组合。
- **实验脚本**：多种子配对比较派工规则。

主要结果：

- **基线校验**：SMT2020 默认规则下，四类 Lot 的平均周期与论文参考值偏差均小于 2%。
- **规则对比**：6 种派工规则 × 4 个种子的配对比较。考虑设备当前 setup 的 FIFO，周期与 WIP 下降约 4%；CR 把普通 Lot 准时率从 91% 提到 99.9%。
- **性能**：稳态下 CPython 约 2.6 秒/仿真日，PyPy 约 1.3 秒/仿真日。

## SMT2020 基线一致性校验

使用 SMT2020 的 **HVLM（dataset 1）** 模型，将 myFab 的 SMT2020 基线（推式投料 + FIFO）与 [SMT2020 论文](https://doi.org/10.1109/TSM.2020.3001933)电子附录 Table AI-2 比较。周期从 Lot 放行算到完工，表中为各类型完工 Lot 的平均周期：

| Lot 类型 | 论文参考（天） | myFab（天） | 相对偏差 |
|---|---:|---:|---:|
| Regular Lot 3 | 51.21 | 51.14 | −0.13% |
| Regular Lot 4 | 28.38 | 27.93 | −1.60% |
| Hot Lot 3 | 31.73 | 32.33 | +1.90% |
| Hot Lot 4 | 18.46 | 18.70 | +1.31% |

论文附录报告的是 20 次重复的均值，预热 1 年后统计 1 年；myFab 用 4 个种子，均从空厂运行 480 天，统计第 240–480 天。两者的起始状态和观察窗口不完全相同，因此这是**基线输出一致性检验**，不是同条件下的逐次复现，也不能当作整个模拟器的统一准确率。

与 SMT2020 随附的单次 AutoSched 参考报表比较：平均 WIP 为 2241 对 2265 Lot（−1.07%），完工量为 56.85 对约 57.21 Lot/天（−0.63%）。该报表的观察窗口约 3 年，只作为辅助对照。

已知偏差：

- **Hot Lot 准时率偏低**：Hot Lot 3 / 4 的准时率为 83.3% / 84.6%，低于论文的 90.8% / 89.5%。诊断发现两边 Hot Lot 的换型时间不同，但 AutoSched 的 Lot 级换型统计口径尚未核实。
- **部分设备组的换型行为未完全对齐**：DE_BE_13 / 66 使用 setup 矩阵，myFab 在这两组上的换型行为介于 AutoSched 的两种可能解释之间，未完全对齐。

审计报告见 `docs/audits/`。

## 派工规则对比

在相同的推式投料和公共派工机制下，比较 FIFO、EDD、SPT、SRPT 和 CR。FIFO SA 是 FIFO 的变体，按设备当前 setup 计算换型时间。各规则只差派工排序的最后一层（`rank_key`）；FIFO SA 与 FIFO 的 `rank_key` 相同，只差 setup 的计法。

每种规则跑 4 个种子，同一种子下各规则的投料序列相同。每次都从空厂运行 480 天，统计第 240–480 天完工的 Lot。

![SMT2020 HVLM 派工规则比较：普通 Lot 周期时间与准时率](experiments/rule_comparison_20261028/figures/rule_comparison.png)

下表中 FIFO 行为绝对值，其余行为相对 FIFO 的同种子配对差均值。`*` 表示 95% t 置信区间（自由度 3）不含 0。普通 Lot 准时率按 Regular Lot 3 和 4 合并计算。

| 规则 | Regular 3 周期 | Regular 4 周期 | 普通 Lot 准时率 | 产出 | 平均 WIP | CQT 违规 / Lot |
|---|---:|---:|---:|---:|---:|---:|
| FIFO | 51.1 天 | 27.9 天 | 91.3% | 56.9 Lot/天 | 2241 | 4.18 |
| FIFO SA | −4.6%\* | −4.0%\* | 99.1% | ±0.0% | −4.3%\* | −2.5% |
| CR | +2.2% | +2.0% | 99.9% | +0.6% | +2.1% | −10.3% |
| EDD | +12.2%\* | +9.1%\* | 21.5%\* | −1.6%\* | +10.9%\* | −29.6%\* |
| SRPT | +30.9%\* | −25.2%\* | 50.9%\* | −1.3%\* | +10.9%\* | −16.7%\* |
| SPT | +36.0%\* | +17.5%\* | 11.0%\* | −3.9%\* | +29.1%\* | −3.0% |

- **FIFO SA**：优先加工与设备当前 setup 相同的 Lot，换型减少，周期和 WIP 都显著下降约 4%。它偏离了 AutoSched 按名义换型时间（STIME）排序的做法，属于改进候选，不是基线复现。
- **CR**：平均周期与 FIFO 无显著差异，准时率却达到 99.9%，种子间波动也最小。它不改变整体速度，只把落后于交期的 Lot 提前。
- **EDD / SPT / SRPT**：普通 Lot 的周期和 WIP 都变差。交期大约落在 FIFO 周期分布的 P90 附近，所以周期整体变长约 10%，准时率就会大幅下降。SRPT 缩短了 Regular Lot 4 的周期，代价是 Regular Lot 3 变长，体现了不同产品路线之间的取舍。
- **Hot Lot / Super Hot Lot**：在各规则下的变化多在 ±5% 以内，因为它们在第一层优先级中已经排在最前，`rank_key` 很少起作用。

局限：

- **种子少**：只有 4 个种子，FIFO 准时率在种子间波动较大（83.5%–97.3%），FIFO SA 与 CR 在准时率上的优势还不显著。
- **只统计窗口内完工的 Lot**：窗口末尚未完工的 Lot 没有计入。
- **稳态未逐一确认**：240 天预热不能保证每种规则都已进入稳态，产出偏低的规则尤其可能仍在持续积压。

这些结果于 2026-10-28 生成。之后代码有两处重构：投料拆分为 ReleasePolicy，派工加入空闲组缓存。重构后，6 种规则在同一种子下运行 60 天，窗口指标与旧代码逐字节一致。

完整指标见[实验汇总](experiments/rule_comparison_20261028/summary.md)和[分析](experiments/rule_comparison_20261028/ANALYSIS.md)，各种子结果见[原始结果](experiments/rule_comparison_20261028/results/)。

## 仿真速度

使用 SMT2020 基线从空厂运行 120 天，统计第 60–120 天的稳态段（平均 WIP 约 1890 Lot，每仿真日约 5.9 万个事件），在 Windows 笔记本上测得：

| 解释器 | 秒/仿真日 | 微秒/事件 | 内存峰值 |
|---|---:|---:|---:|
| CPython 3.11 | 2.59 | — | 约 58 MB |
| PyPy 3.12 | 约 1.3 | 21.5 | 约 135 MB |

CPython 的数值为加入空闲组缓存后的结果（加入前为 2.97 秒/仿真日、49.8 微秒/事件）；PyPy 为加入缓存前的结果。

主要优化：

- **合并装载事件**：事件调用次数减少约 40%。
- **派工空闲组缓存**：Implant 等设备组被 setup avoidance 挡住后会长时间空闲，而策略原本在每次决策时都要重新评估这些组。现在只要输入不变，就直接跳过评估。`decide` 的耗时占比从 45% 降到 30%。

两项优化前后的结果指纹逐位一致。

## 架构

```text
SMT2020 工作簿 → SQLite 模型库 → FabModel
                                    ↓
            FabEngine（事件队列：到达、装载、加工、卸载、PM、故障、投料）
              ↑ 派工决策          ↑ 投料触发
         Strategy（BaseRule）   ReleasePolicy
                                    ↓
             插件：指标、CQT、事件日志、SQLite、Tokenizer
```

**派工**

`strategy/base_rule.py` 是公共底座，候选 Lot 按以下顺序排序：

1. 优先级
2. setup 时长
3. `rank_key`

此外，公共底座还负责 Super Hot 设备预留、setup avoidance 和组批。

`strategy/{fifo,edd,spt,srpt,cr}.py` 各自只覆盖 `rank_key`。`strategy/smt2020_baseline.py` 就是底座加 FIFO。

**投料**

投料方式通过 `release=` 参数传入，位于 `strategy/release/`：

- `PushRelease`：默认的推式投料，Lot 到达就放行。
- `FixedIntervalRelease`：固定间隔投料。

ReleasePolicy 声明自己关心的触发事件，引擎在派工结束后的结算点统一交给它处理。因此新增 CONWIP 这类拉式投料时不需要改引擎。

**指标**

CQT 只做测量，由插件统计违规；派工本身不感知 CQT，与 AutoSched 的默认做法一致。

## 使用

开发环境为 Python 3.11，仿真本身只需要 NumPy 和 openpyxl；作图需要 Pillow，Fabotron 训练需要 PyTorch。

**解释器：仿真和数据采集默认用 PyPy，训练用 CPython。**

| 用途 | 解释器 | 说明 |
|---|---|---|
| 仿真、规则实验、Fabotron 数据采集 | PyPy 3.12（v8.0.1） | 稳态比 CPython 快约 1.7–2 倍；只用标准库，不需要装包。结果与 CPython 逐位相同（指纹核对过） |
| 导入 SMT2020 工作簿（openpyxl）、作图（Pillow）、Fabotron 训练（PyTorch） | CPython 3.11+ | 这些第三方包在 PyPy 下不可用或没有安装 |

本地 Windows 使用便携版 PyPy（解压即用），把下面命令中的 `python` 换成 `pypy3.exe` 的路径即可。

**Vanda HPC 上必须先下载 PyPy**（一次即可，放在 scratch，不需要管理员权限）：

```bash
cd /scratch/$USER
wget https://downloads.python.org/pypy/pypy3.12-v8.0.1-linux64.tar.gz
tar xzf pypy3.12-v8.0.1-linux64.tar.gz
/scratch/$USER/pypy3.12-v8.0.1-linux64/bin/pypy3 --version
```

采集脚本 `fabotron/data_collection/hpc/vanda_branches.pbs` 默认使用这个路径；放在别处时用 `BRANCH_PYTHON=<pypy3 路径>` 覆盖。预热检查点（pickle）只能由同一解释器读取，本地与 Vanda 之间不要混用。

```bash
git clone https://github.com/xiaozhe19/myFab.git
cd myFab
python -m pip install -r requirements.txt
```

**导入工厂模型。** 准备 SMT2020 HVLM 工作簿，导入 SQLite，时间单位为 minute：

```bash
python -m fab.importer --input "path/to/SMT_2020_Model_Data_-_HVLM.xlsx" --database data/model/fab_model.sqlite --end-time 86400 --release-interval 30 --warmup-time 36000
```

**运行仿真。** `--strategy` 可以写 `SMT2020`，也可以写 `module:Class`。构造参数放在 JSON 文件里，通过 `--strategy-params` 传入：

```bash
python -m fab.run_model --strategy SMT2020 --order-seed 2026061700
python -m fab.run_model --strategy strategy.cr:CRRule --order-seed 2026061700
```

在 Python 中组合派工规则与投料方式：

```python
from strategy.edd import EDDRule
from strategy.release import FixedIntervalRelease

strategy = EDDRule(release=FixedIntervalRelease())
```

**复现规则对比。** 实验可中断，重跑时会跳过已有结果。单个 480 天运行在 CPython 下约需 20–30 分钟，每个进程约占 1 GB 内存。

```bash
python -m experiments.rule_comparison_20261028.run_all 4      # 并行进程数
python -m experiments.rule_comparison_20261028.analyze        # 汇总 + 配对差
```

**测试：**

```bash
python -m unittest discover tests
```

## Fabotron（已暂停）

Fabotron 是附带的学习式代理模型实验，目前已暂停。它把 106 个设备组的状态编码为 Token，序列为 `[batch, T, G, F]`，每 6 小时一帧。模型是 Spatial–Temporal–Spatial Transformer，用于预测下一帧的 WIP、Queue 和设备状态。

V1 已完成以下流程：

- Tokenizer
- 训练与验证选模
- 与 Persistence 基线的对比

已有结果：

- **SMT2020 策略**：部分数量指标优于"保持当前状态"的基线。
- **FIFO 策略**：预测仍明显不足。
- **跨种子、跨策略泛化，长期滚动预测**：都还没有验证。

暂停期间的已知问题：`fabotron/data_collection/collection_plan*.json` 引用的旧版 EDD/CR 策略已经删除，需要更新后才能重新采集。代码在 `fabotron/` 和 `fab/plugins/tokenizer*.py`。

## 限制

- 只在 SMT2020 HVLM 上做了校验，其他数据集和真实工厂的准确性未经验证。
- 工作簿、SQLite 模型库和仿真结果不随仓库发布，需要使用者自行准备。
- `strategy/dbr_v2.py` 是历史实现，投料重构后已不能运行。
- 这是学习与研究项目，没有接入真实生产数据，也不提供生产调度保证。

## 代码布局

| 路径 | 职责 |
|---|---|
| `fab/engine/` | DES 事件、加工、组批、停机与投料结算 |
| `fab/model/`、`fab/importer/` | SMT2020 模型构造与原始表导入 |
| `fab/strategy.py` | 策略与 ReleasePolicy 接口 |
| `fab/plugins/` | 指标、事件日志、SQLite 与 Tokenizer 插件 |
| `strategy/` | 派工规则（`base_rule.py` + 各排序键）与投料方式（`release/`） |
| `experiments/` | 规则对比实验脚本与结果 |
| `tests/` | 单元测试与基线语义测试 |
| `perf_tools/` | 长时间 profile 工具 |
| `fabotron/` | 学习式代理模型（已暂停） |

Created by [xiaozhe19](https://github.com/xiaozhe19)
