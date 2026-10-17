# SMT2020 checkpoint tools

This is an additive adapter. It does not edit `fab/`, `strategy/`, the model database,
or the original command-line entry point. Run commands from the project root with
the same Python version used to build the checkpoint. `-B` avoids bytecode writes.

```powershell
$python = 'C:\Users\ZZY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $python -B -m unittest checkpoint_tools.test_runtime

# Run each seed once (the experiment used three concurrent processes).
& $python -B -m checkpoint_tools build --seed 2026061700 --output data/checkpoints/smt2020_20261016/seed_2026061700
& $python -B -m checkpoint_tools build --seed 2026061701 --output data/checkpoints/smt2020_20261016/seed_2026061701
& $python -B -m checkpoint_tools build --seed 2026061702 --output data/checkpoints/smt2020_20261016/seed_2026061702

& $python -B -m checkpoint_tools assess --runs data/checkpoints/smt2020_20261016/seed_2026061700/run.json data/checkpoints/smt2020_20261016/seed_2026061701/run.json data/checkpoints/smt2020_20261016/seed_2026061702/run.json --output data/checkpoints/smt2020_20261016 --report report_smt2020_checkpoint.md

& $python -B -m checkpoint_tools inspect --checkpoint data/checkpoints/smt2020_20261016/seed_2026061700/day0240.fabcp
& $python -B -m checkpoint_tools resume --checkpoint data/checkpoints/smt2020_20261016/seed_2026061700/day0240.fabcp --duration-days 30 --output data/checkpoints/resumed_seed1700
```

Every output filename/directory must be new. The build records exact daily WIP
integrals, completions and cycle times for 360 simulated days. It captures at the
first safe pending-release boundary on/after days 240 and 360, then compares each
checkpoint's next two days against the uninterrupted process in a fresh Python
process. Thus the original build continues slightly past day 362. The underlying
model horizon remains 1080 days, preserving the original random event sampling
range; it is not replaced by an artificial end event at the checkpoint.

The assessment uses 90-day windows, a 30-day step, three consecutive passes,
5% mean/balance/trend tolerances, and 10% P95 tolerances against the last 180 days.
It produces `index.json`, including certified checkpoint hashes and a default
checkpoint for each seed. Resume requires this certification by default;
`--allow-uncertified` is only for intentionally loading development snapshots.

`.fabcp` is a ZIP containing a JSON manifest and a pickle payload. The entire
business object reference graph is saved; the observer, result-storage resources
and progress callback are excluded. The next event counter is stored as numeric
arguments. The loader checks payload integrity, exact Python/platform, source
fingerprints and model SHA-256 before loading. It restores the existing engine
without calling `__init__`/`begin_episode`, recreates the counter, and clears only
the setup-time cache keyed by old process `id(step)` values. Only load trusted,
locally generated checkpoints: pickle is executable data.

Absolute simulation time is retained. Resume creates a new observer initialized
with the existing WIP, so average WIP and completed-lot CT measurements cover the
continuation window rather than a second empty-fab warmup. Final partial days are
excluded from daily metric aggregates; exact start/end times and the full trace
are retained. Cycle time means a completed lot's full fab cycle, including time
before the checkpoint. The saved RNG states and pre-sampled event calendar are
retained: reloading one checkpoint reproduces one trajectory, not an independent
seed. Other scheduling policies are not certified by SMT2020's steady state.

No SQLite traces are generated. Each seed has a `run.json`, two `.fabcp` files,
two expected continuations and two verification results. Failed/incomplete builds
are not certified. Keep the tool sources, core simulator and model database with
the checkpoints, because their hashes form the compatibility contract.
