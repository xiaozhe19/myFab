# SMT2020 策略从空 Fab 启动的预热时间实测报告

日期：2026-10-16（Asia/Hong_Kong）。项目：D:\Project\micron\myFab。

## 结论

**针对当前默认 HVLM 模型和原生 SMT2020 策略，按本文的 5% 工程稳态判据，建议预热 240 个仿真日，即 345,600 minute。** 三个随机种子测得的稳态窗口起点为 **120、210、210 日**。240 日是在最慢的 210 日结果上增加 30 日余量的建议值，并非直接测出的唯一临界时刻。

每个种子均从空厂运行到第 360 日（518,400 minute）。用于确认稳态的完整观察长度与应丢弃的预热长度不同：较慢两个种子需要观察到第 360 日，才能确认从第 210 日开始的三个连续滑动窗口均通过。

现有数据库配置的 **25 日预热不足**；60 日的原运行时长也不足以测量当前模型的稳态表现。上述建议未写回任何配置或数据库。

| 随机种子 | 实测预热起点（日） | 起点（minute） | 连续窗口确认完成日 | 单次运行墙钟秒 | 单次运行墙钟分钟 |
|---|---:|---:|---:|---:|---:|
| 2026061700 | 120 | 172800 | 270 | 1742.9 | 29.05 |
| 2026061701 | 210 | 302400 | 360 | 1732.2 | 28.87 |
| 2026061702 | 210 | 302400 | 360 | 1719.5 | 28.66 |

三次正式运行并行执行，最长一次约 29.05 分钟墙钟时间；该数字含观测开销，不是无插件内核的性能基准。前置检查与探测耗时不包含在这三个单次运行时间中。

## 输入、范围与运行方式

- 当前代码提交：`0d24557f242be6fb8a0ce0beb9c562e87e16d1bd`；开始时工作树干净。
- 输入库：`data/model/fab_model.sqlite`。其派生模型与 `smt2020/General Data/dataset 1/SMT_2020_Model_Data_-_HVLM.xlsx` 构造的模型逐项相等。
- 模型规模：1,443 台设备、106 个工具组，Product_3 为 583 道工序，Product_4 为 343 道工序；292 条 PM、11 条故障规则。
- 策略：CLI 名称 `SMT2020`，实现为 `SMT2020BaselineStrategy`，结果名称 `SMT2020_BASELINE`。沿用现有推式投料、优先级、setup、reservation 与 setup avoidance 逻辑。
- 五条原始投料计划全部保留。长期名义投入率为 57.1979038279 lot/日。投料池到达即放行；配置中的 30 minute 决策间隔不会把该策略的投料限流为每 30 分钟一批。
- 每次在 t=0 事件执行前，厂内 lot 和投料池均为 0；t=0 的正常订单到达仍照常执行。未载入 AutoSched 的初始 WIP。
- 设备初态、故障、PM、运输、抽检、返工、setup、组批均沿用当前代码与数据；`batch_from_strategy=False`、`rework_same_tool=False`。
- 原库 SimulationSpec：start=0、end=86,400（60 日）、release_interval=30、warmup=36,000（25 日）。仅在内存中将统计 warmup 设为 0，以保留完整启动过程。
- 本次内存模型的仿真上限设为 1,080 日，并通过引擎已有的 `SIMULATION_END` 事件在第 360 日结束。复现时应保留同一上限和结束事件，以保持未来事件预抽样的随机数消耗顺序一致。
- 观测器通过既有 `FabPlugin` 回调采集数据；未启用 SQLiteStorage、Trace 或其他落盘插件。Python 使用 `-B`，避免生成字节码缓存。
- Python：Codex 附带的 CPython 3.12.14；系统默认 Python 缺少 openpyxl，因此未使用它运行仿真，也未安装依赖。

本结论对应上述 HVLM 模型与当前策略实现，不覆盖 LVHM、HVLM_E、LVHM_E。

## 稳态判据

这是有限时域、有限种子的生产 KPI 预热估计，时间搜索分辨率为 30 日；120–210 日是本次观测范围，不是统计置信区间，也不是严格平稳性的证明。

每日 WIP 通过事件之间的实际持续时间积分得到，不使用日末 WIP 替代平均 WIP。周期时间为 lot 实际放行到最终完工的时长，按完工所在窗口归集；本策略没有投料池等待，因此也等于从订单到达到完工的时长。

每个种子独立使用第 180–360 日作为后期参考区间，按以下步骤判定：

1. 从 0 日起每隔 30 日取一个 90 日滑动窗口。
2. 窗口的平均 WIP、完工率、Regular_Lot_3 平均周期、Regular_Lot_4 平均周期，各自与同一种子的参考值偏差不超过 ±5%。两个普通 lot 类型的周期 P95 偏差不超过 ±10%。P95 使用线性插值。
3. 窗口内 `完工数 / 投入数 - 1` 的绝对值不超过 5%，防止以持续积压换取表面平稳。
4. 对每日平均 WIP 做 OLS 直线拟合。若斜率为 b lot/日，窗口平均 WIP 为 W，则要求 `abs(90 × b / W) ≤ 5%`，检查残余趋势。
5. 连续三个窗口全部通过，取第一个窗口的起点为该种子的预热估计。起点 t 对应窗口 [t,t+90)、[t+30,t+120)、[t+60,t+150)，故确认时刻为 t+150 日。

三个滑动窗口彼此重叠，只用于持续性确认，不能当作三个独立统计样本；独立重复来自三个随机种子。Hot/SuperHot 的统计保留作诊断，但由于样本少，不作为主要预热判据。稳态仍有随机波动，规则不要求每一个后续窗口都通过：例如种子 2026061700 的 210–300 日窗口因 WIP 趋势为 6.25% 而未通过，但后期 180 日整体趋势仅为 1.51%，其他均值仍接近平台。

### 容差敏感性

用同一批已采集数据重算，无额外仿真。下表同步调整均值偏差、投出差和趋势容差；P95 容差为其两倍。

| 容差 | 2026061700 起点 | 2026061701 起点 | 2026061702 起点 |
|---|---:|---:|---:|
| 3% | 360 日内未确认 | 360 日内未确认 | 360 日内未确认 |
| 5% | 120 日 | 210 日 | 210 日 |
| 10% | 90 日 | 90 日 | 90 日 |

因此 240 日应理解为 **5% 容差下的保守工程建议**。如果研究要求 3% 的严格稳定标准，本次 360 日数据不足以确认，不能把 240 日表述为该标准下已验证的结论。

## 实测数据

### 每 30 日的平均 WIP 与完工率

WIP 单位 lot，完工率单位 lot/日；三个数字列依次对应三个种子。

| 仿真日区间 | WIP 1700 | WIP 1701 | WIP 1702 | 完工率 1700 | 完工率 1701 | 完工率 1702 |
|---|---:|---:|---:|---:|---:|---:|
| 0–30 | 798.9 | 798.2 | 797.8 | 9.87 | 10.13 | 10.23 |
| 30–60 | 1610.9 | 1596.1 | 1602.1 | 46.40 | 46.47 | 45.77 |
| 60–90 | 1849.8 | 1819.6 | 1785.3 | 50.47 | 51.57 | 54.97 |
| 90–120 | 2045.8 | 1943.9 | 1926.5 | 52.70 | 55.50 | 53.10 |
| 120–150 | 2082.5 | 2012.2 | 1988.2 | 57.67 | 53.17 | 54.53 |
| 150–180 | 2119.7 | 2079.4 | 2045.2 | 54.77 | 57.40 | 56.57 |
| 180–210 | 2110.1 | 2129.4 | 2055.2 | 58.23 | 52.13 | 56.23 |
| 210–240 | 2075.5 | 2205.6 | 2089.7 | 57.50 | 56.40 | 55.27 |
| 240–270 | 2153.5 | 2200.1 | 2144.5 | 53.80 | 59.17 | 56.03 |
| 270–300 | 2171.4 | 2131.1 | 2116.6 | 59.20 | 58.40 | 59.23 |
| 300–330 | 2145.8 | 2133.3 | 2096.7 | 57.03 | 57.60 | 55.83 |
| 330–360 | 2142.4 | 2127.6 | 2193.5 | 57.43 | 57.13 | 55.07 |

第 25 日日末的 WIP 分别为 1,254、1,251、1,250 lot。三个种子的首个 Regular_Lot_3 完工时刻分别为 31.082、30.786、30.049 日；第 25 日连长路线普通 lot 的首批完工都未出现。第 31–60 日完工率仅约 45.8–46.5 lot/日，明显低于投入率。25 日预热和 60 日运行窗口会包含强烈的启动瞬态。

### 第 180–360 日参考平台

周期时间均为仿真日；CT 列格式为均值 / P95。趋势列表示 OLS 斜率折算的每 90 日相对变化，不是实际首尾 WIP 差。

| 种子 | 平均 WIP | 完工 lot/日 | 投出差 | WIP 趋势/90 日 | Regular_3 CT | Regular_4 CT |
|---|---:|---:|---:|---:|---:|---:|
| 2026061700 | 2133.11 | 57.200 | -0.010% | 1.514% | 48.677 / 51.308 | 26.529 / 28.764 |
| 2026061701 | 2154.50 | 56.806 | -0.699% | -0.999% | 49.110 / 51.788 | 26.876 / 29.209 |
| 2026061702 | 2116.03 | 56.278 | -1.622% | 2.808% | 48.306 / 51.219 | 26.278 / 28.623 |

三个种子合计参考样本：Regular_Lot_3 14,869 个，Regular_Lot_4 14,982 个。平均 WIP 为 2134.55 lot，平均完工率为 56.761 lot/日；两类普通 lot 合并样本的平均 CT / P95 分别为 48.698 / 51.464 日、26.562 / 28.938 日。合并只用于描述，预热起点仍按每个种子独立判定。

### 各滑动窗口是否通过

单元格给出联合判定及 WIP 趋势；通过要求同时满足所有指标，不只看趋势。

| 窗口（日） | 2026061700 | 2026061701 | 2026061702 |
|---|---|---|---|
| 0–90 | 未通过；114.05% | 未通过；112.15% | 未通过；108.57% |
| 30–120 | 未通过；35.83% | 未通过；29.29% | 未通过；27.25% |
| 60–150 | 未通过；17.80% | 未通过；15.43% | 未通过；15.73% |
| 90–180 | 未通过；6.01% | 未通过；9.76% | 未通过；9.09% |
| 120–210 | 通过；1.88% | 未通过；9.08% | 未通过；5.05% |
| 150–240 | 通过；-2.83% | 未通过；8.94% | 通过；3.40% |
| 180–270 | 通过；3.16% | 未通过；5.47% | 未通过；6.48% |
| 210–300 | 未通过；6.25% | 通过；-4.45% | 通过；2.08% |
| 240–330 | 通过；-0.27% | 通过；-4.25% | 通过；-2.81% |
| 270–360 | 通过；-2.28% | 通过；-0.24% | 通过；4.91% |

### 丢弃前 240 日后，第 240–360 日表现

| 种子 | 平均 WIP | 完工 lot/日 | 投出差 | Regular_3 CT 均值/P95（日） | Regular_4 CT 均值/P95（日） |
|---|---:|---:|---:|---:|---:|
| 2026061700 | 2153.26 | 56.867 | -0.583% | 49.029 / 51.560 | 26.904 / 28.954 |
| 2026061701 | 2148.01 | 58.075 | 1.530% | 49.055 / 51.659 | 26.714 / 29.072 |
| 2026061702 | 2137.81 | 56.542 | -1.151% | 48.710 / 51.471 | 26.653 / 28.971 |

三个种子在此区间共投入 20,592 lot、完成 20,578 lot，合计仅增加 14 lot WIP；每种子平均投入 57.200、完成 57.161 lot/日。这一后续区间支持 240 日作为本次条件下的建议删除点。

### 运行结束和数据一致性

| 种子 | 累计投入 | 累计完工 | 结束 WIP | 结束投料池 | 最老在制 lot 厂内年龄（日） |
|---|---:|---:|---:|---:|---:|
| 2026061700 | 20595 | 18452 | 2143 | 0 | 50.506 |
| 2026061701 | 20595 | 18452 | 2143 | 0 | 52.193 |
| 2026061702 | 20595 | 18385 | 2210 | 0 | 51.906 |

每次进程退出码均为 0。逐日验证累计投入减累计完工等于日末 WIP，各类型周期时间样本数等于该类型完工数，未发现不一致。结束时最老在制 lot 约 51–52 日龄，没有从启动初期遗留至第 360 日的长期停滞 lot。

## 文件不变性核验

实验全程的观测与统计驻留内存。创建本报告前，对最初记录的 **3,435 个现有文件**核对路径、大小和修改时间，新增、删除、变化均为 0；对 **120 个源码、配置、Markdown 与关键输入文件**复核 SHA-256，变化为 0。现有模型库、结果库及缓存文件均未改动。本次唯一新增文件是本报告。

| 对象 | SHA-256 |
|---|---|
| 模型库 | `825138242b263034fc944f99173655dac9d198b74b45788b71b78e0aa27bead0` |
| HVLM 工作簿 | `11c5cc9f477eafcbb1d37abf2a6802eade5964a95fa1b1fd3351ddce841c1a18` |
| fab/engine/engine.py | `6b399a641e93c3277b3c177e39aa1f1c229d7e2ec18920ea66e0156f6bef5eaa` |
| strategy/smt2020_baseline.py | `c7c3ddcf10de6ff80283fdc10a960b8128c6c98c8702e8a4a8d97e5c9cb95785` |

实现依据：[策略](D:/Project/micron/myFab/strategy/smt2020_baseline.py)、[引擎](D:/Project/micron/myFab/fab/engine/engine.py)、[插件接口](D:/Project/micron/myFab/fab/plugins/base.py)、[模型构造](D:/Project/micron/myFab/fab/model/smt_model_builder.py)。

## 不创建额外文件的复现方法

在项目根目录的 PowerShell 中，将下列 Python 代码放入变量 `$code` 的单引号 here-string，再执行下方命令。使用报告给出的 Python 路径；不需要安装依赖、改动模块、改写模型库或新增脚本文件。脚本只将进度和结果输出到标准输出。代码中的观测器只保留复现本报告所需的字段，实际结束时间仍为第 360 日、模型上限仍为 1,080 日。

```powershell
$python = 'C:\Users\ZZY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
# $code = @' 后放入下一段 Python 代码，并以 '@ 独立一行结束。
foreach ($seed in 2026061700,2026061701,2026061702) {
    & $python -B -u -c $code $seed 1080
}
```

以下代码可逐个种子顺序复现；本次正式实验为三进程并行，数据与并行调度无关。汇报中的复现代码已在内存中通过 Python 语法编译检查。

```python
import sys,json,time,math,sqlite3,statistics
from pathlib import Path
from dataclasses import replace
from collections import Counter,defaultdict
from fab.model.sqlite_loader import _simulation,_raw_workbook
from fab.model.smt_model_builder import build_fab_model
from fab.engine import FabEngine
from fab.plugins import FabPlugin,PluginManager
from strategy.smt2020_baseline import SMT2020BaselineStrategy
DAY=1440.0
seed=int(sys.argv[1]); days=int(sys.argv[2]); started=time.perf_counter()
from fab.engine.events import EventKind
p=Path('data/model/fab_model.sqlite')
with sqlite3.connect(p.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as c:
 c.row_factory=sqlite3.Row
 sim=_simulation(c); raw=_raw_workbook(c,p)
m=build_fab_model(raw,replace(sim,start_time=0.0,end_time=days*DAY,warmup_time=0.0),model_id=p.stem)
class Observe(FabPlugin):
 def __init__(self):
  self.wip=0; self.last=0.0; self.day=0; self.area=0.0; self.rows=[]; self.ins=Counter(); self.outs=Counter(); self.ct=defaultdict(list); self.first={}; self.totalin=0; self.totalout=0; self.pw=Counter()
 def on_time_advanced(self,t,lots):
  while self.day<days and t>=(self.day+1)*DAY:
   bound=(self.day+1)*DAY; self.area+=self.wip*(bound-self.last); self.last=bound
   self.rows.append(dict(day=self.day+1,wip=self.area/DAY,end_wip=self.wip,ins=dict(self.ins),outs=dict(self.outs),ct=dict(self.ct),product_wip=dict(self.pw)))
   self.day+=1; self.area=0.0; self.ins=Counter(); self.outs=Counter(); self.ct=defaultdict(list)
   if self.day%30==0:
    recent=self.rows[-30:]; groups=Counter(x.current_step.tool_group_id for x in lots if x.current_step)
    print(json.dumps(dict(progress=True,seed=seed,day=self.day,wall=round(time.perf_counter()-started,2),wip=self.wip,wip30=round(statistics.mean(r['wip'] for r in recent),2),out30=sum(sum(r['outs'].values()) for r in recent),in30=sum(sum(r['ins'].values()) for r in recent),top_queues=groups.most_common(5))),flush=True)
  dt=t-self.last
  self.area+=self.wip*dt
  self.last=t
 def on_lot_released(self,t,lot):
  self.wip+=1; self.totalin+=1; self.pw[lot.product_id]+=1; self.ins[lot.lot_type_id]+=1
 def on_lot_completed(self,t,lot):
  self.wip-=1; self.totalout+=1; self.pw[lot.product_id]-=1; self.outs[lot.lot_type_id]+=1; self.ct[lot.lot_type_id].append((t-lot.release_time)/DAY)
  self.first.setdefault(lot.lot_type_id,t/DAY)
 def build_result(self,context):
  self.on_time_advanced(engine.current_time,context.lots)
  groups=Counter(x.current_step.tool_group_id for x in context.lots if x.current_step)
  oldest=sorted([( (engine.current_time-x.release_time)/DAY,x.id,x.lot_type_id,x.operation_index,len(x.route),x.current_step.tool_group_id if x.current_step else None) for x in context.lots],reverse=True)[:10]
  print(json.dumps(dict(done=True,seed=seed,days=engine.current_time/DAY,wall=time.perf_counter()-started,initial_empty=initial_empty,first=self.first,totalin=self.totalin,totalout=self.totalout,endwip=self.wip,pool=context.release_pool_lots_at_end,groups=groups,oldest=oldest,rows=self.rows)),flush=True)
  return {}
obs=Observe(); engine=FabEngine(m,order_seed=seed,plugin_manager=PluginManager([obs])); initial_empty=len(engine.lots)==0 and len(engine.order_source.queue)==0
engine.schedule(360*DAY,EventKind.SIMULATION_END)
engine.run(SMT2020BaselineStrategy())

def summarize(rows):
 n=len(rows); w=statistics.mean(r['wip'] for r in rows)
 ins=sum(sum(r['ins'].values()) for r in rows)
 outs=sum(sum(r['outs'].values()) for r in rows)
 ct=defaultdict(list)
 for r in rows:
  for k,v in r['ct'].items(): ct[k].extend(v)
 def pct(v):
  v=sorted(v); p=.95*(len(v)-1); lo=math.floor(p); hi=math.ceil(p)
  return v[lo]+(v[hi]-v[lo])*(p-lo)
 b=sum((i-(n-1)/2)*(r['wip']-w) for i,r in enumerate(rows))/(n*(n*n-1)/12)
 return dict(wip=w,throughput=outs/n,balance=100*(outs/ins-1),drift90=100*b*90/w,
             ct={k:dict(n=len(v),mean=statistics.mean(v),p95=pct(v)) for k,v in ct.items()})
ref=summarize(obs.rows[-180:]); checks=[]
for t in range(0,271,30):
 s=summarize(obs.rows[t:t+90])
 errors=[s['wip']/ref['wip']-1,s['throughput']/ref['throughput']-1]
 for k in ('Regular_Lot_3','Regular_Lot_4'):
  errors.append(s['ct'].get(k,dict(mean=float('inf')))['mean']/ref['ct'][k]['mean']-1)
 p95ok=all(abs(s['ct'].get(k,dict(p95=float('inf')))['p95']/ref['ct'][k]['p95']-1)<=.10 for k in ('Regular_Lot_3','Regular_Lot_4'))
 ok=all(abs(e)<=.05 for e in errors) and p95ok and abs(s['balance'])<=5 and abs(s['drift90'])<=5
 checks.append(dict(start=t,passed=ok,drift90=s['drift90'],balance=s['balance']))
candidate=next((checks[i]['start'] for i in range(len(checks)-2) if all(x['passed'] for x in checks[i:i+3])),None)
print(json.dumps(dict(summary=True,seed=seed,warmup_days=candidate,confirmed_day=candidate+150 if candidate is not None else None,reference=ref,checks=checks)),flush=True)
```

## 补充实测：N 个事件中，多少会请求策略

### 直接回答

**按当前 HVLM + SMT2020 实现，稳态样本中每 100 个出堆事件，对应约 37.36 次真实策略请求。** 若按“在实际请求轮次中贡献触发通知的原始事件”计数，占比为 **38.56%**。两者的差异来自同刻事件合并，以及投料后的追加请求。

三个种子均从空厂重放至第 270 日，以前文建议的 240 日预热删除点为界，主结果取 **第 240–270 日**。这段共处理 **9,656,543 个事件**，产生 **3,607,322 次 `decide()` 调用**；三个种子的调用率为 **37.224%–37.573%**。若只关心产生新投料或派工动作的调用，则为 **2,053,023 次，占事件数的 21.260%**。

这三个比例分别回答“调用多少次”“哪些原始事件贡献了调用通知”“多少调用产出了新动作”。空动作不等于不必要调用，不能据此将请求率直接缩减为 21.26%。

| 种子 | 稳态事件 N | 策略调用 C | C/N | 通知事件 I/N | 产生投料/派工动作的调用数/N |
|---|---:|---:|---:|---:|---:|
| 2026061700 | 3,211,709 | 1,196,935 | 37.268% | 38.477% | 21.265% |
| 2026061701 | 3,249,847 | 1,221,076 | 37.573% | 38.786% | 21.258% |
| 2026061702 | 3,194,987 | 1,189,311 | 37.224% | 38.429% | 21.258% |
| 合计 | 9,656,543 | 3,607,322 | 37.356% | 38.565% | 21.260% |

### 计数口径

- **N：出堆事件总数。** 每次日历取出事件并交给 `_handle_event` 计一条；同刻的多条事件分别计数。包括因 token 失效而被忽略的旧完成事件、CQT 事件，以及每次运行 1 条结束控制事件。本稳态样本包含三次第 270 日结束事件。
- **C：真实策略调用次数。** 在 `_decide` 入口计数，包含 `release`、`dispatch` 和 `follow_up`；不包含 `initialize`。这是主调用率 C/N 的分子。
- **I：参与实际主请求轮次的通知事件数。** 在某条事件的父处理函数中观察到“有效 lot 就绪通知”“设备/槽位新变为空闲”“投料机会”或“强制唤醒”，且该结算轮次最终实际请求了策略，则这条原始事件计入 I。同一事件最多计一次，类型表中的 I 可相加。
- I 是运行中可观测的通知归因，不是对每条同刻事件分别做删除试验后证明的最小必要事件集合。同一轮内多个通知可能共同出现，无法把每个通知都等同于一次独立调用。

本稳态样本共有 9,379,853 个同刻结算轮次，其中 3,604,748 轮请求了策略，即 38.431%。这是按“结算轮次”而非按“原始事件”计算的另一种比例；同一模拟时刻在投料追加动作后仍可能有后续结算轮次。

共有 4,237,931 条原始事件报告了潜在的就绪/空闲/投料/强制唤醒通知，其中 513,893 条没有落入实际主请求轮次，占潜在通知事件的 12.126%。这体现了候选检查与同刻处理后的过滤效果，不是每个状态变化都请求策略。

### 具体是什么事件

本次实际贡献策略请求通知的类型只有以下七种：

| 事件 | 何时真正进入策略请求轮次 |
|---|---|
| `LOT_AVAILABLE` | 外部 lot 到达；SMT2020 的 `release_on_arrival=True` 使其产生 `release` 请求。实际放行后追加 `follow_up` 请求，让策略对新入厂 lot 派工。 |
| `TRANSPORT_COMPLETE` | lot 到达下一工序并进入 ready 索引；若同刻结算后候选集合非空，产生派工请求。 |
| `UNLOAD_COMPLETE` | 卸载使设备/槽位新变为空闲，且同刻结算后存在合法候选配对时，请求派工。级联设备此前已空闲或停机等情形不会自动形成新通知。 |
| `TOOL_AVAILABLE` | 设备或级联槽位重新可用；有效新增空闲状态且候选集合非空时，请求派工。 |
| `PM_COMPLETE` | 维护结束后释放设备/槽位，且存在合法候选配对时，请求派工。若继续处理积压停机或未释放可用槽位，则可能不请求。 |
| `REPAIR_COMPLETE` | 维修结束后出现可用设备/槽位且候选集合非空时，请求派工。仅恢复被中断加工并不必然产生请求。 |
| `STRATEGY_WAKEUP` | 显式强制唤醒，本策略主要用于 setup avoidance 等待阈值；即使没有候选，也会请求策略重新检查。 |

代码还规定 `BATCH_READY` 是强制唤醒事件，但本次使用默认引擎组批，实测未产生该类型事件。`ORDER_ARRIVAL`、`LOT_RELEASE`、`SAMPLING_DECISION` 同样未出现在本次样本；当前外部推式投料不产生固定 `LOT_RELEASE` 节拍。

本次 **`LOAD_COMPLETE`、`SETUP_COMPLETE`、`OPERATION_COMPLETE`、`PM_DUE`、`BREAKDOWN`、`REWORK_ROUTED`、`CQT_EXPIRE` 和 `SIMULATION_END` 均未直接贡献策略请求通知**。装载、换型、加工完成继续走既有物理链；返工先走路径与运输结算；故障开始/PM 到期主要减少可用资源；CQT 到期记录状态；结束事件直接停止。它们可以与其他事件同刻出现，不能因为共同出现在请求时刻，就把它们都计成直接触发者。

### 各事件类型的实测明细

I_i 是该类事件中“贡献通知且所在轮次实际请求了策略”的事件数；不是该类独立调用次数。I_i/N_i 表示该类事件参与实际请求轮次的比例。N_i=0 时用“—”，表示未观测到该事件类型。

| 事件类型 | 原始事件 N_i | 通知事件 I_i | I_i/N_i | I_i/全部事件 N |
|---|---:|---:|---:|---:|
| `ORDER_ARRIVAL`（自动订单到达） | 0 | 0 | — | 0.0000% |
| `LOT_AVAILABLE`（外部 lot 到达投料池） | 5,142 | 5,142 | 100.00% | 0.0532% |
| `LOT_RELEASE`（固定节拍投料机会） | 0 | 0 | — | 0.0000% |
| `LOAD_COMPLETE`（装载完成） | 2,079,923 | 0 | 0.00% | 0.0000% |
| `SETUP_COMPLETE`（换型完成） | 71,597 | 0 | 0.00% | 0.0000% |
| `OPERATION_COMPLETE`（加工完成） | 2,090,046 | 0 | 0.00% | 0.0000% |
| `UNLOAD_COMPLETE`（卸载完成） | 2,079,871 | 1,008,820 | 48.50% | 10.4470% |
| `TRANSPORT_COMPLETE`（运输完成 / lot 就绪） | 2,129,248 | 1,832,920 | 86.08% | 18.9811% |
| `TOOL_AVAILABLE`（设备或级联槽位可用） | 966,193 | 855,088 | 88.50% | 8.8550% |
| `PM_DUE`（PM 到期） | 15,646 | 0 | 0.00% | 0.0000% |
| `PM_COMPLETE`（PM 完成） | 15,715 | 12,666 | 80.60% | 0.1312% |
| `BREAKDOWN`（故障开始） | 13,245 | 0 | 0.00% | 0.0000% |
| `REPAIR_COMPLETE`（维修完成） | 13,240 | 2,587 | 19.54% | 0.0268% |
| `BATCH_READY`（显式组批唤醒） | 0 | 0 | — | 0.0000% |
| `REWORK_ROUTED`（返工路径结算） | 289 | 0 | 0.00% | 0.0000% |
| `SAMPLING_DECISION`（独立抽检决策事件） | 0 | 0 | — | 0.0000% |
| `CQT_EXPIRE`（CQT 到期） | 169,570 | 0 | 0.00% | 0.0000% |
| `STRATEGY_WAKEUP`（策略显式唤醒） | 6,815 | 6,815 | 100.00% | 0.0706% |
| `SIMULATION_END`（仿真结束） | 3 | 0 | 0.00% | 0.0000% |

### 真实调用与同刻合并

| 调用类型 | 次数 | 占全部策略调用 C | 次数/事件 N |
|---|---:|---:|---:|
| `dispatch` | 3,602,174 | 99.857% | 37.3029% |
| `release` | 2,574 | 0.071% | 0.0267% |
| `follow_up` | 2,574 | 0.071% | 0.0267% |

主请求（release + dispatch）共 3,604,748 次，追加 follow_up 共 2,574 次，两者相加为 C。

下面按**主请求的联合触发组**分类，每次主请求恰好归入一行，因此可以相加；同一轮内相同类型的多条通知合并到同一个类型名。运输完成、卸载完成、槽位可用及仅由它们组成的同刻组合，占全部主请求的 99.317%。

| 联合触发组 | 主请求次数 | 占全部主请求 |
|---|---:|---:|
| `transport_complete` | 1,716,244 | 47.611% |
| `unload_complete` | 892,157 | 24.749% |
| `tool_available` | 855,060 | 23.720% |
| `unload_complete+transport_complete` | 116,644 | 3.236% |
| `pm_complete` | 12,664 | 0.351% |
| `strategy_wakeup` | 6,796 | 0.189% |
| `repair_complete` | 2,587 | 0.072% |
| `lot_available` | 2,574 | 0.071% |
| 其余稀少组合 | 22 | 0.0006% |
| 合计 | 3,604,748 | 100.000% |

例如，种子 2026061700 的首 30 日有 1,718 条 `LOT_AVAILABLE`，但同刻订单到达被合并，仅形成 858 次 release 请求和 858 次 follow_up 请求；不能按每条到达事件推算两次独立调用。

### 请求是否产生实际动作

| 返回动作 | 调用次数 | 占全部调用 C | 调用次数/事件 N |
|---|---:|---:|---:|
| 投料或派工 | 2,053,023 | 56.913% | 21.260% |
| 仅登记未来唤醒 | 6,729 | 0.187% | 0.070% |
| 无上述外部动作 | 1,547,570 | 42.901% | 16.026% |
| 合计 | 3,607,322 | 100.000% | 37.356% |

候选检查只判断物理上存在合法配对，不保证策略愿意执行该配对；reservation、setup avoidance 等策略约束可能使其选择等待。策略还可能更新内部预留状态。因此，“无外部动作”不能直接标为“无用请求”，本报告没有据此提出或实施减少调用的代码改动。

### 全程与稳态对比

三次从空厂到第 270 日的运行，合计处理 80,284,753 个事件、请求策略 30,432,347 次，全程调用率为 37.906%；通知事件比例为 39.144%。各 30 日区间如下，均为三个种子的合并计数。

| 日区间 | 原始事件 N | 策略调用 C | C/N | I/N | 投料/派工动作调用数/N |
|---|---:|---:|---:|---:|---:|
| 0–30 | 4,724,944 | 1,818,380 | 38.485% | 39.889% | 21.524% |
| 30–60 | 8,801,196 | 3,325,055 | 37.780% | 39.036% | 21.316% |
| 60–90 | 9,202,927 | 3,455,078 | 37.543% | 38.776% | 21.284% |
| 90–120 | 9,457,697 | 3,576,624 | 37.817% | 39.040% | 21.261% |
| 120–150 | 9,532,971 | 3,605,451 | 37.821% | 39.049% | 21.267% |
| 150–180 | 9,680,632 | 3,686,402 | 38.080% | 39.299% | 21.256% |
| 180–210 | 9,587,671 | 3,626,122 | 37.821% | 39.045% | 21.261% |
| 210–240 | 9,640,172 | 3,731,913 | 38.712% | 39.951% | 21.259% |
| 240–270 | 9,656,543 | 3,607,322 | 37.356% | 38.565% | 21.260% |

所以用 **约 37%–38% 的实际策略调用次数/事件数** 描述本次模型较合适；若问题强调原始事件中哪些贡献了调用通知，则稳态观测约为 **39%**。这些都是当前内核与 SMT2020 策略的观测口径，不是任意策略的最小必要调用比例。

### 计数方法与校验

计数器只存在于临时 Python 进程中：通过内存观测派生类在父类事件处理、ready/idle 通知和 `_decide` 的入口/出口计数；业务对象仅被观测，父方法仍按原逻辑执行并原样返回。未修改仓库源码、调用判断、模型输入或持久化配置。模型上限保持 1,080 日，使用既有结束事件在第 270 日结束，以保持与上一轮相同的随机事件生成范围。

- 三个进程退出码均为 0，各有 9 个完整的 30 日计数区间。
- 三个种子的每个 30 日检查点累计完工数均与上一轮相同种子一致；第 270 日结束 WIP 和累计完工数也一致。
- 各事件类型之和等于 N，三种调用类型之和等于 C，各动作分类之和等于 C，联合触发组之和等于主请求数。
- 每个种子的主请求数等于有主请求的结算轮数，没有无法归因的主请求。逐类验证 I_i ≤ 潜在通知数 ≤ N_i。
- 单次 270 日计数运行墙钟时间为 22.71、22.73、22.85 分钟；三次并行执行。该耗时包含计数开销。
- 本补测仅更新同一份 Markdown 汇报；原源码、模型库、结果库等其他现有文件保持不变，并复核关键文件 SHA-256。

调用门槛可核对 [advance_until_release](D:/Project/micron/myFab/fab/engine/engine.py:299)、[投料后追加请求](D:/Project/micron/myFab/fab/engine/engine.py:397)、[事件类型与强制唤醒](D:/Project/micron/myFab/fab/engine/engine.py:456)、[ready 通知](D:/Project/micron/myFab/fab/engine/engine.py:1746)、[idle 通知](D:/Project/micron/myFab/fab/engine/engine.py:1810)。

### 复现计数，不创建额外文件

将下列 Python 代码作为 PowerShell 的 `$eventCode` 单引号 here-string，在项目根目录使用前文同一 `$python` 路径运行：

```powershell
# $eventCode = @' 后放入下方 Python 代码，并以 '@ 独立一行结束。
foreach ($seed in 2026061700,2026061701,2026061702) {
    & $python -B -u -c $eventCode $seed
}
```

脚本只打印标准输出；以最后一条 `done=true` JSON 的 `blocks` 为最终计数，`names` 给出数组各下标对应的事件名。中间进度 JSON 是重复展示，不能再与最终 blocks 相加。上述循环顺序运行，正式补测使用三个独立进程并行。

```python
import sys,json,time,sqlite3
from pathlib import Path
from dataclasses import replace
from collections import Counter
from fab.model.sqlite_loader import _simulation,_raw_workbook
from fab.model.smt_model_builder import build_fab_model
from fab.engine import FabEngine
from fab.engine.events import EventKind
from fab.plugins import FabPlugin,PluginManager
from strategy.smt2020_baseline import SMT2020BaselineStrategy
DAY=1440.0; BLOCK=30*DAY; STOP=270*DAY
seed=int(sys.argv[1]); started=time.perf_counter()
names=[k.value for k in EventKind]; idx={k:i for i,k in enumerate(EventKind)}; K=len(names)
def bucket():
 return dict(events=[0]*K,potential=[0]*K,trigger_events=[0]*K,trigger_requests=[0]*K,coevents=[0]*K,calls=Counter(),groups=Counter(),decision_actions=Counter(),dispatch_pairs=Counter(),epochs=0,called_epochs=0,primary_epochs=0,no_trigger_primary=0)
class CountEngine(FabEngine):
 def __init__(self,*a,**kw):
  self.blocks=[bucket() for _ in range(9)]; self.b=self.blocks[0]; self.event_context=None; self.notified=False; self.epoch_events=[]; self.epoch_triggers=[]; self.epoch_called=False; self.epoch_primary=False; self.epoch_open=False
  super().__init__(*a,**kw)
 def _handle_event(self,event,reasons):
  i=idx[event.kind]; self.event_context=i; self.notified=False; self.b['events'][i]+=1; self.epoch_events.append(i)
  outcome=super()._handle_event(event,reasons)
  if self.notified or outcome[0] or outcome[1]:
   self.b['potential'][i]+=1; self.epoch_triggers.append(i)
  self.event_context=None
  return outcome
 def _mark_ready(self,lot):
  valid=not lot.in_process and not lot.completed and lot.ready_time<=self.current_time and lot.current_step.tool_group_id is not None
  super()._mark_ready(lot)
  if valid and self.event_context is not None: self.notified=True
 def _mark_idle(self,tool):
  group=self._tool_group_by_id[tool.tool_id]; was=tool.tool_id in self._idle_tools_by_group.get(group,())
  super()._mark_idle(tool)
  if not was and self.event_context is not None: self.notified=True
 def _decide(self,state,kind):
  self.b['calls'][kind]+=1; self.epoch_called=True
  if kind!='follow_up':
   self.epoch_primary=True
   for i in self.epoch_events: self.b['coevents'][i]+=1
   for i in self.epoch_triggers: self.b['trigger_events'][i]+=1
   group=tuple(sorted(set(self.epoch_triggers)))
   self.b['groups']['+'.join(names[i] for i in group) or '(none)']+=1
   for i in group: self.b['trigger_requests'][i]+=1
   if not group: self.b['no_trigger_primary']+=1
  d=super()._decide(state,kind)
  released=bool(d.release_lot or d.release_lot_id is not None or d.release_lot_ids)
  action='release_or_dispatch' if released or d.dispatches or d.batches else ('wakeups_only' if d.wakeups else 'no_external_action')
  self.b['decision_actions'][kind+':'+action]+=1
  self.b['dispatch_pairs'][kind]+=len(d.dispatches)
  return d
 def close_epoch(self):
  if self.epoch_open:
   self.b['epochs']+=1; self.b['called_epochs']+=int(self.epoch_called); self.b['primary_epochs']+=int(self.epoch_primary)
class Epochs(FabPlugin):
 def __init__(self): self.next=BLOCK; self.completed=0
 def on_time_advanced(self,t,lots):
  engine.close_epoch()
  while t>=self.next and self.next<=STOP:
   j=int(self.next/BLOCK)-1; b=engine.blocks[j]
   print(json.dumps(dict(progress=True,seed=seed,day=int(self.next/DAY),wall=round(time.perf_counter()-started,2),wip=len(lots),completed=self.completed,block=b,names=names)),flush=True)
   self.next+=BLOCK
  engine.b=engine.blocks[min(int(t/BLOCK),8)]; engine.epoch_open=True; engine.epoch_events=[]; engine.epoch_triggers=[]; engine.epoch_called=False; engine.epoch_primary=False
 def on_lot_completed(self,t,lot): self.completed+=1
p=Path('data/model/fab_model.sqlite')
with sqlite3.connect(p.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as c:
 c.row_factory=sqlite3.Row; sim=_simulation(c); raw=_raw_workbook(c,p)
m=build_fab_model(raw,replace(sim,start_time=0.0,end_time=1080*DAY,warmup_time=0.0),model_id=p.stem)
obs=Epochs(); engine=CountEngine(m,order_seed=seed,plugin_manager=PluginManager([obs])); empty=not engine.lots and not engine.order_source.queue
engine.schedule(STOP,EventKind.SIMULATION_END); engine.run(SMT2020BaselineStrategy()); engine.close_epoch()
print(json.dumps(dict(done=True,seed=seed,wall=time.perf_counter()-started,day=engine.current_time/DAY,names=names,blocks=engine.blocks,wip=len(engine.lots),completed=obs.completed,pool=len(engine.order_source.queue),initial_empty=empty)),flush=True)
```

## 数据库检查与稳态状态恢复评估

### 清理前审计与执行依据

本轮用户已授权检查和清理数据库，继续保持项目源码不变。共发现 16 个 SQLite 文件，清理前合计 7,417,548,800 字节。正式模型库为 data/model/fab_model.sqlite，保留其内容与配置。

以下清理依据在执行前记入本报告：

- 删除 data/fab_model.sqlite：0 字节空库；正式模型位于 data/model/fab_model.sqlite。
- 删除 tmp/profile_fifo.sqlite：全部表逐行哈希相同，排除 elapsed_seconds；原耗时 323.812 秒，保留库耗时 142.929 秒；等价业务数据保留在 tmp/candidate_cache_fifo.sqlite。
- 删除 tmp/optimized_dbr_verified.sqlite：全部表逐行哈希相同，排除 elapsed_seconds；原耗时 259.941 秒，保留库耗时 133.726 秒；等价业务数据保留在 tmp/optimized_dbr_final.sqlite。
- 主结果库 simulation_results.sqlite 的 run 6、7、8，经全部表逐行哈希核验业务数据完全相同（归一化 run_id、自动记录 ID 和耗时元数据）；保留 run 6，删除 run 7、8 的关联记录。
- 压缩三个 data/result 结果库，回收已删除记录占据的空闲页，不改动表结构。检查时 simulation_results1.sqlite 已有 2,641,620,992 字节空闲页，simulation_results33.sqlite 已有 157,507,584 字节空闲页。
- 其他指标不同的策略/版本结果保留；不能仅凭 elapsed_seconds 为 NULL、throughput 为 0 或文件位于 tmp 就判定其无用。

临时重复库的业务校验摘要如下。对比时仅排除 runs.elapsed_seconds；原始耗时已在上文保留。

| 待删重复库 | 保留库 | 表 | 行数 | 行内容 SHA-256 |
|---|---|---|---:|---|
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | cqt_violations | 2,154 | f06a6b8f6d4cb727ade216239ee52e86c9889191adbf4696c97afcc7ca56e0a7 |
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | decision_log | 1,204,042 | 89bde7950e59c53d307ef1cfba6f187629f9183806cb14a8748cf13862732678 |
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | lots | 3,433 | 6b1148184277e940940d81dec11f3afc7a56025f7d3fc70d99f6cf97c13879b3 |
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | metrics | 5 | d47b7df0de3af05d418c661576604140f4a65c85585b5e74d29853596e2d901e |
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | operations | 624,067 | 1c8eeb58a02444d9a0ab92c6798028fae34ecdcab8b3beba2c015fc56020c428 |
| tmp/profile_fifo.sqlite | tmp/candidate_cache_fifo.sqlite | runs | 1 | dcae25bfa07a1e151b47c6c6afd0db3a8528215454f208c49e9b59099ab22cb1 |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | cqt_violations | 2,314 | 687e577a1cfb64cb6b1e8093f1ccbc7a6a9b6f6c1277e01a48771f7a531cf8f1 |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | decision_log | 0 | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | lots | 3,303 | 7504a1979a9b2cacde66ae4284ce8d45d4a41d1f2d2036f49e8b45094efeff6b |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | metrics | 5 | 247f693f354c936251a1fcf49cb0070d86d2689162a22e09111a9b4cc1bb1caf |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | operations | 557,586 | d137757baa26710a9f89ebcf211ece712d90a46b83ad08ffceb0dabe235fdb04 |
| tmp/optimized_dbr_verified.sqlite | tmp/optimized_dbr_final.sqlite | runs | 1 | 1e77524762394d739d3e92de7f4613f2fefb975281f25fc072d38408228b4e53 |

补充核验：simulation_results1.sqlite 的 run 2、3 也在全部表逐行归一化后完全相同，均无独立耗时元数据；保留 run 2，删除 run 3 关联记录。

### 清理执行结果

本节是用户随后授权的数据库清理；前文“仅新增/更新报告”的核验结论对应前两轮只读仿真，当时没有改动数据库。本轮仍不改项目源码。

SQLite 文件由 **16 个减少为 13 个**，总大小由 7,417,548,800 字节减少为 3,340,750,848 字节，实际回收 **4,076,797,952 字节，即 4.077 GB / 3.797 GiB**。其中结果库删除重复运行及压缩回收 3,675,103,232 字节，删除两个临时重复库回收 401,694,720 字节。0 字节空库仅作路径清理。

| SQLite 文件 | 清理前 MiB | 清理后 MiB | 处理 |
|---|---:|---:|---|
| data/fab_model.sqlite | 0.00 | 已删除 | 删除已核验的空库/重复副本 |
| data/model/fab_model.sqlite | 0.37 | 0.37 | 保留 |
| data/result/simulation_results.sqlite | 1439.96 | 605.92 | 压缩，删除重复 run 7/8 |
| data/result/simulation_results1.sqlite | 4200.69 | 1680.09 | 压缩，删除重复 run 3 |
| data/result/simulation_results33.sqlite | 296.16 | 145.95 | 压缩，保留全部逻辑记录 |
| tmp/bench_fifo.sqlite | 3.34 | 3.34 | 保留 |
| tmp/candidate_cache_fifo.sqlite | 339.71 | 339.71 | 保留 |
| tmp/fidelity_FIFO.sqlite | 40.22 | 40.22 | 保留 |
| tmp/fidelity_SMT2020.sqlite | 78.49 | 78.49 | 保留 |
| tmp/fidelity_SMT2020_compromise.sqlite | 78.46 | 78.46 | 保留 |
| tmp/fidelity_SMT2020_strict.sqlite | 78.25 | 78.25 | 保留 |
| tmp/optimized_dbr.sqlite | 43.30 | 43.30 | 保留 |
| tmp/optimized_dbr_final.sqlite | 43.37 | 43.37 | 保留 |
| tmp/optimized_dbr_verified.sqlite | 43.37 | 已删除 | 删除已核验的空库/重复副本 |
| tmp/optimized_fifo.sqlite | 48.51 | 48.51 | 保留 |
| tmp/profile_fifo.sqlite | 339.71 | 已删除 | 删除已核验的空库/重复副本 |

主结果库 run 7/8 的全部关联行在同一事务中删除，规范副本为 run 6；历史库 run 3 的规范副本为 run 2。删除的仅为经全表逐行确认的重复运行。所有保留运行的策略名、原始耗时和表结构均经前后核验保持一致。其他临时库即使指标较旧或吞吐为 0，只要不是已证实的重复记录，仍保留其独立实验信息。

清理后的三个结果库均通过 SQLite quick_check，空闲页均为 0；逐行重新计算每个保留 run、每张表的内容摘要，与清理前对应摘要全部一致，已删除 run 不再存在。对比归一化了 run_id、自动记录 ID 和 elapsed_seconds；elapsed_seconds 另用完整 runs 行逐条前后核对，未改动。

| 结果库 | 清理前字节 | 清理后字节 | 回收字节 | 保留 run_id |
|---|---:|---:|---:|---|
| data/result/simulation_results.sqlite | 1,509,908,480 | 635,355,136 | 874,553,344 | 1, 2, 4, 5, 6 |
| data/result/simulation_results1.sqlite | 4,404,744,192 | 1,761,701,888 | 2,643,042,304 | 1, 2, 16, 17, 18, 19, 20, 26, 27, 29, 32 |
| data/result/simulation_results33.sqlite | 310,546,432 | 153,038,848 | 157,507,584 | 1, 2, 3 |

正式模型库保持原始内容，quick_check 为 ok。simulation_config 仍为 (0, 86400, 30, 36000)，没有将预热建议写回配置。其原始表内容为：Toolgroups 106 行、Lotrelease 5 行、Route_Product_3 583 行、Route_Product_4 343 行、PM 292 行、Breakdown 11 行、Setups 13 行、Setup_Matrix_Implant_Gas 48 行、Transport 1 行。

### 能否直接从稳态状态出发

**方案本身可行；但当前已有数据库和原生启动入口，不能直接做到。** 不改仓库源码、允许用临时外部 Python 命令调用现有类时，完整对象跨进程保存/恢复机制已通过验证；若要求只改数据库或只使用现有 CLI 参数，则当前没有恢复入口。

现有数据有两个明确限制：

1. 正式模型库只保存原始 SMT 表和 SimulationSpec。构造 FabEngine 时会创建空的 lots、投料池和事件日历，并初始化设备、维护与随机数；它不读取结果库的 lots 或 final_state_snapshot 来恢复状态。把 start_time 改大不能恢复已有 WIP、加工进度或维护相位。
2. 查到的历史快照时刻全部为 **86400 minute，即第 60 日**，早于本文按 5% 判据建议的 240 日预热删除点；并且快照不是完整 checkpoint。前两轮长期实验按用户要求只保留内存统计，没有把第 240/270/360 日的完整引擎状态保存到数据库。因此目前没有可直接复用的持久化稳态 checkpoint。

### 现有快照为什么不足

主结果库保留 run 6，以及四个 fidelity 临时库中的 final_state_snapshot，均位于第 60 日。快照包含以下字段：

| 对象 | 实际持久化字段 | 关键缺项 |
|---|---|---|
| lot | id、product_id、generation_time、release_time、due_time、end_time、completed、step | in_process、ready_time、当前加工设备、运输/剩余加工时间、批次成员、LTL 与返工设备绑定、已加工设备历史等 |
| tool | id、name、process | 忙闲/故障/换型状态、current_setup、最小连续加工计数、processed_wafers、级联槽位、可用时间、正在加工/暂停的活动等 |
| 引擎与来源 | 无完整持久化状态 | 未来事件日历及 payload、事件序号与 token、PM/故障剩余阶段、累计 PM 阈值、输入源队列和计数、两套 RNG 状态、pending 决策、策略预留/等待状态等 |

旧版快照还会包含已完工 lot，不能把快照行数直接当作当前 WIP。历史 operations/decision_log 是结果轨迹，也不能替代尚未发生、但已采样并排入日历的未来事件。runs 表缺少完整的模型版本、种子和时间配置记录，不能指望只用这些旧库无损逆推出当时整个运行对象。

完整保存至少需要覆盖：

- 当前绝对仿真时间；所有在制 lot 与投料池 lot 的完整动态状态。
- 每台设备的 setup、维护、故障、级联槽位、活动/暂停加工及批次状态。
- 未来事件堆及 payload、同刻优先级/序号、token 和下一事件序号。
- 引擎 RNG 与投料源 RNG 的 getstate()，订单来源的计数和下一次到达状态。
- 维护阈值、LTL/返工绑定、ready/idle 索引或其可验证的重建规则。
- 策略内部状态，尤其是 reservation、setup avoidance 的计时与待唤醒状态。
- 代码版本、模型 SHA-256、策略及运行参数、Python/格式版本和 checkpoint 时刻。

### 不改源码的跨进程机制验证

本轮另做了一次**非稳态、非持久化**的机制试验，使用当前 HVLM、SMT2020、种子 2026061700，原生 FabEngine、空 PluginManager、无存储线程、无进度回调。选择投料决策已经生成、尚未应用的暂停边界，完全在内存中 pickle.dumps；将自生成的字节流通过管道交给新 Python 进程 pickle.loads。没有创建 checkpoint 文件，也没有新增或修改 Python 文件。

| 项目 | 实测结果 |
|---|---:|
| checkpoint 仿真时刻 | 14421.510000 minute（第 10.0149375 日） |
| 厂内 WIP | 575 lot |
| 未来事件 | 2429 条 |
| 活跃加工活动 / 暂停设备项 | 314 / 8 |
| 投料池 / pending release | 2 lot / 是 |
| 内存序列化字节数 | 1,622,610 |
| 序列化耗时 | 0.017692 秒 |
| 新进程反序列化耗时 | 0.053706 秒 |
| 恢复后对象引用关系 | active/ready 中的 lot 均指向恢复引擎中的同一 lot 对象 |
| 原进程与恢复进程继续到 | 第 12.0251042 日，WIP 689 lot |
| 加载瞬间状态摘要 | 一致 |
| 继续运行后的 44 组语义状态摘要 | 全部一致 |

摘要覆盖 lot/设备状态、事件日历、活跃与暂停活动、pending 决策、维护阈值、订单计数/队列、两套 RNG、事件序号、策略预留/等待状态、ready/idle 索引及候选集合等。反序列化时间只指 pickle.loads，不包含启动 Python、模块导入、指标初始化或随后仿真；第 10 日的大小和耗时也不能当作第 240 日稳态快照的实测值。

首次未经规范化的缓存比较，仅发现 _entries_by_group 内部列表顺序不同；源码最终会统一按 available_time、tool_id 排序，实际候选顺序、业务状态、RNG 和事件日历均一致。按该排序语义规范化缓存摘要后重新运行，44 组摘要全部一致。该处理只改变比较方法，没有改动引擎对象的业务状态。

恢复过程还处理了一个具体兼容点：SMT2020 的 _expected_setup_cache 使用 id(step) 作键，不能把原进程对象地址当作新进程有效缓存键。本次加载后清空了 220 条这类性能缓存，再由原策略按需重算；没有改变策略规则。

### 落地前必须区分的边界

- **现有 run()/begin_episode() 不能当恢复入口。** 它们负责首次初始化，已经初始化过的引擎再次 begin_episode 会拒绝；恢复后应从保存的暂停边界继续 apply_release_action / advance_until_release 的事件循环，不能重新生成投料、PM 和故障日历。
- **现有落盘插件不能直接随引擎 pickle。** 实测 SQLiteStoragePlugin 即使尚未启动，也报 TypeError: cannot pickle '_thread.lock' object。实际运行时还有队列、线程和存储连接；业务 checkpoint 必须与这些资源分开，恢复后另接新运行的日志/结果存储。
- **指标从 checkpoint 时刻重新计量。** OnlineMetricsPlugin 的 WIP 集合和累计量必须与已存在的在制 lot 对齐，不能直接调用“空厂开始”初始化后就认为指标正确。保留绝对时钟最简单，评估区间从 checkpoint 时刻开始；若强行归零，必须一致平移订单、due time、设备、事件、策略等待等全部绝对时间。
- **保持代码、模型、配置与运行时版本。** 本次为 CPython 3.12.14。实际序列化时出现 itertools 序列化未来版本弃用警告；长期 checkpoint 格式应显式保存事件序号等值，不应把本次整对象 pickle 视为已完成的通用长期格式。
- **同一完整状态重复加载并不产生独立重复。** 保留 RNG 和已采样的未来日历，会重现同一条轨迹。独立实验应准备不同种子各自预热的 checkpoint，不能只复制一个文件并将其视为多个独立样本。
- **稳态依赖策略和负荷。** SMT2020 的稳态状态可作为其他策略的共同 warm start，但不能据此宣称其他策略一启动就处于自己的稳态。

因此，后续合理方向是一次性预热并在安全暂停边界保存完整业务 checkpoint，之后通过明确的外部恢复入口复用，优先保留绝对仿真时间。**本轮完成的是数据库清理和机制评估；没有修改代码、没有实现新的 CLI/恢复功能，也没有生成持久化稳态 checkpoint。**
