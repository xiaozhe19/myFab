# SMT2020 稳态 checkpoint 实测与恢复报告

当前 HVLM、SMT2020_BASELINE，3 个独立种子，从空厂运行。

工程稳态容差：均值/投出差/90日趋势 ±5%，周期 P95 ±10%。
90日滑动窗口、30日步长，连续3窗通过；后180日作参考，并检查其投出差与趋势。

推荐预热：**240 仿真日**。首次通过的窗口起点与完成确认的时刻分别列出。

| 种子 | 稳态起点（日） | 确认完成（日） | 参考平均WIP | 参考完工/日 | 参考趋势/90日 |
|---|---:|---:|---:|---:|---:|
| 2026061700 | 120 | 270 | 2133.111 | 57.200 | 1.514% |
| 2026061701 | 210 | 360 | 2154.504 | 56.806 | -0.999% |
| 2026061702 | 210 | 360 | 2116.031 | 56.278 | 2.808% |

## 已保存的完整状态

checkpoint 保留绝对时钟，停在已生成、尚未应用的 release 决策边界；实际时刻略晚于整数目标日。
保存业务对象完整引用图、事件堆、活动/暂停加工、lot/设备/来源/策略、RNG及事件序号；不保存线程或存储插件。

| 种子 | 目标日 | 实际日 | WIP | 文件MiB | 已认证 | 文件 |
|---|---:|---:|---:|---:|---|---|
| 2026061700 | 240 | 240.03543750 | 2102 | 1.818 | True | [day0240.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061700/day0240.fabcp) |
| 2026061700 | 360 | 360.03520833 | 2140 | 1.928 | True | [day0360.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061700/day0360.fabcp) |
| 2026061701 | 240 | 240.03543750 | 2245 | 1.991 | True | [day0240.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061701/day0240.fabcp) |
| 2026061701 | 360 | 360.03520833 | 2142 | 1.910 | True | [day0360.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061701/day0360.fabcp) |
| 2026061702 | 240 | 240.03543750 | 2130 | 1.893 | True | [day0240.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061702/day0240.fabcp) |
| 2026061702 | 360 | 360.03520833 | 2207 | 1.960 | True | [day0360.fabcp](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/seed_2026061702/day0360.fabcp) |

## 跨进程续跑验证

每个 checkpoint 均由新 Python 进程读取，在相同未来时刻与未中断原进程比较。
验证初态/末态44组语义状态摘要、两套RNG、未来事件堆和事件序号，以及期间每次投料、工序完成、整lot完成的顺序轨迹哈希。
_entries_by_group 内部无业务意义的列表顺序规范化后比较；真正的全局候选顺序仍原样比较。

| 种子 | 目标日 | 起点一致 | 末态一致 | 轨迹一致 | 工序完成数 | 完成lot数 | 续跑到（日） |
|---|---:|---|---|---|---:|---:|---:|
| 2026061700 | 240 | True | True | True | 47997 | 127 | 242.04560417 |
| 2026061700 | 360 | True | True | True | 46666 | 148 | 362.04537500 |
| 2026061701 | 240 | True | True | True | 48146 | 122 | 242.04560417 |
| 2026061701 | 360 | True | True | True | 48364 | 100 | 362.04537500 |
| 2026061702 | 240 | True | True | True | 48785 | 133 | 242.04560417 |
| 2026061702 | 360 | True | True | True | 48520 | 94 | 362.04537500 |

## 使用

使用本次 CPython 和项目根目录，-B 避免写入既有字节码缓存；恢复命令不会重新预热或调用 begin_episode。

```powershell
$python = 'C:\Users\ZZY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $python -B -m checkpoint_tools inspect --checkpoint 'D:\Project\micron\myFab\data\checkpoints\smt2020_20261016\seed_2026061700\day0240.fabcp'
& $python -B -m checkpoint_tools resume --checkpoint 'D:\Project\micron\myFab\data\checkpoints\smt2020_20261016\seed_2026061700\day0240.fabcp' --duration-days 30 --output 'data/checkpoints/resumed_seed1700'
```

输出目录必须是新目录。恢复时为当前在制lot初始化新的WIP计量，不将它们当作空厂；CT统计为计量期完工lot的完整实际厂内周期。
checkpoint 仍包含已采样的未来事件，保留原1080日上限；命令会拒绝超出该上限的续跑。

## 适用范围与文件不变性

只新增 checkpoint_tools 包和本轮输出；既有 fab/、strategy/、模型库及旧实验报告不需要改动。
完整payload校验、代码/模型SHA-256和Python精确版本/平台校验均在加载时执行；旧进程id(step)性能缓存会清空后按需重算。
只加载可信、自行生成的 checkpoint；pickle 是可执行数据。

本认证是5%容差下、三个种子的工程稳态估计，不是严格平稳性或其他策略稳态的证明。
同一checkpoint重复加载会重现同一随机轨迹；独立重复请使用不同seed的默认checkpoint。
完整指标、逐日数据、每个滑动窗口和验证结果分别保存在 index.json、各seed/run.json和 *_verification.json。

## 补充核验与推荐选择

推荐值取三个种子最晚的稳态窗口起点 210 日，再加 30 日余量，故默认使用第 240 日 checkpoint；实际安全暂停时刻为第 240.03543750 日（345651.03 minute）。第 360 日文件作为另一组较晚的已验证状态保留。首次起点的搜索分辨率为 30 日，120/210/210 日不是统计置信区间。

### 容差敏感性（复用本轮数据）

| 均值/投出差/趋势容差 | 2026061700 起点 | 2026061701 起点 | 2026061702 起点 |
|---|---:|---:|---:|
| 3% | 360 日内未确认 | 360 日内未确认 | 360 日内未确认 |
| 5% | 120 日 | 210 日 | 210 日 |
| 10% | 90 日 | 90 日 | 90 日 |

P95 容差为上表容差的两倍；结果说明认证依赖所选工程判据。稳态仍有短窗随机波动，不要求任意两日窗口的投出差都在 5% 内。

### 已实跑默认恢复命令

默认认证 checkpoint 的恢复命令已直接运行成功，未使用 --allow-uncertified：从第 240.03543750 日、2102 lot 开始，续跑到第 242.04560417 日、2089 lot。完整两日计量窗口平均 WIP 为 2093.461 lot；恢复结果的轨迹哈希、末态44组摘要及计量指标均与未中断对照一致。两日续跑只验证恢复正确性，不单独用两日指标判定稳态。完整结果在 [恢复示例](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/resume_example_seed1700/result.json) 与 [一致性核对](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/resume_example_validation.json)。

第240日三个 checkpoint 在独立进程中加载（含完整性/兼容性/状态校验）的实测时间为 1.562、1.639、1.557 秒；不含Python进程启动和之后的仿真计算。

### 本轮新增工具没有改变旧模型结果

与前一轮保留的同种子完整数据逐日比较，360日 × 3种子的投入数、完工数、周期时间样本序列全部一致，逐日平均WIP最大绝对差为 0 lot。

原有 109 个 Python 文件 SHA-256 全部不变；原模型库及所有其他原有非Git文件也未修改。新文件仅为 checkpoint_tools、新checkpoint输出和本报告。详细证据在 [file_integrity.json](D:/Project/micron/myFab/data/checkpoints/smt2020_20261016/file_integrity.json)。

工具测试4项通过：新进程续跑与未中断过程一致、损坏payload拒绝、版本不符拒绝、已有checkpoint拒绝覆盖。各独立种子完整构建及验证耗时约 31.45、31.20、31.22 分钟，三组并行。
