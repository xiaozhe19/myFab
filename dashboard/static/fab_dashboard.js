const palette = ["#1d7a8c", "#b14f2a", "#5266a8", "#2f8f5b", "#8f5f2f", "#6f5aa8"];
const timelineState = {
  scale: 1,
  fitBase: 0.6,
  fullscreen: false,
};

const dashboardState = {
  resultId: "current",
  results: [],
  cache: {},
  strategySummaries: [],
  expandedStrategyId: null,
};

const animationState = {
  timeIndex: 0,
  times: [0],
  playing: false,
  timer: null,
};

const timelineLayout = {
  laneHeight: 26,
  laneGap: 6,
  blockHeight: 22,
  blockHeightFullscreen: 30,
  labelWidthThreshold: 56,
};

function fmt(value, digits = 2) {
  if (!Number.isFinite(Number(value))) return "0";
  return Number(value).toFixed(digits).replace(/\.?0+$/, "");
}

function displayTime(value, data) {
  return Number(value || 0) - Number(data.run.window_start || 0);
}

function pct(value) {
  return `${fmt(Number(value) * 100, 1)}%`;
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function renderDashboard(data) {
  window.__fabDashboardData = data;
  document.body.classList.toggle("summary-mode", Boolean(data.random_run));
  document.body.classList.toggle("detail-mode", !data.random_run);
  renderMetricCards(data);
  renderRandomRunPanel(data);
  renderSystemStructure(data);
  renderProductPerformance(data);
  renderOrderHistory(data);
  renderUtilization(data);
  renderQueues(data);
  renderWaitByProcess(data);
  renderBottlenecks(data);
  renderDebugPanels(data);
}

function metricValue(metrics, key) {
  return metrics && Object.prototype.hasOwnProperty.call(metrics, key) ? metrics[key] : 0;
}

function renderRandomRunPanel(data) {
  const section = document.getElementById("random-run-section");
  const averageRoot = document.getElementById("random-run-average");
  const runsRoot = document.getElementById("random-seed-runs");
  if (!section || !averageRoot || !runsRoot) return;

  const randomRun = data.random_run;
  section.hidden = !randomRun;
  averageRoot.innerHTML = "";
  runsRoot.innerHTML = "";
  if (!randomRun) return;

  const average = randomRun.average || {};
  setText("random-run-summary", `${randomRun.seed_count || 0} seeds · ${randomRun.strategy || "strategy"}`);
  [
    ["Throughput", "throughput"],
    ["MCT", "mct_average"],
    ["Avg WIP", "average_fab_wip"],
    ["Moves", "movements"],
    ["Setup Time", "total_setup_time"],
    ["Downtime", "total_downtime"],
  ].forEach(([label, key]) => {
    const item = document.createElement("div");
    item.innerHTML = `<span>${label}</span><strong>${fmt(metricValue(average, key), key.includes("utilization") ? 4 : 2)}</strong>`;
    averageRoot.appendChild(item);
  });

  (randomRun.runs || []).forEach((run) => {
    const metrics = run.metrics || {};
    const card = document.createElement("article");
    card.className = "seed-run-card";
    const seedIndex = Number(run.seed_index || 0);
    card.innerHTML = `
      <div class="seed-run-head">
        <strong>Seed ${seedIndex}</strong>
        <span>${fmt(metricValue(metrics, "throughput"), 0)} done</span>
      </div>
      <div class="seed-run-metrics">
        <span>MCT <strong>${fmt(metricValue(metrics, "mct_average"))}</strong></span>
        <span>WIP <strong>${fmt(metricValue(metrics, "average_fab_wip"))}</strong></span>
        <span>Moves <strong>${fmt(metricValue(metrics, "movements"), 0)}</strong></span>
        <span>Setup <strong>${fmt(metricValue(metrics, "setup_count"), 0)}</strong></span>
      </div>
      <button type="button" class="seed-detail-button" data-seed-index="${seedIndex}">查看详细过程</button>
    `;
    card.querySelector(".seed-detail-button").addEventListener("click", () => runSeedDetail(seedIndex));
    runsRoot.appendChild(card);
  });
}

function averageMetricBlock(label, value, digits = 2) {
  return `<div><span>${label}</span><strong>${fmt(value, digits)}</strong></div>`;
}

function renderStrategySummaries(payload) {
  const root = document.getElementById("strategy-average-grid");
  const status = document.getElementById("strategy-summary-status");
  if (!root || !status) return;
  dashboardState.strategySummaries = payload.strategies || [];
  root.innerHTML = "";
  status.textContent = `${dashboardState.strategySummaries.length} strategies · click a card to expand seeds`;
  setText("strategy", "All strategies");
  setText("run-id", "random seed averages");
  document.body.classList.add("summary-mode");

  dashboardState.strategySummaries.forEach((strategy) => {
    const average = strategy.average || {};
    const generated = Boolean(strategy.generated);
    const card = document.createElement("article");
    card.className = "strategy-average-card";
    if (!generated) card.classList.add("is-missing");
    card.dataset.strategyId = strategy.strategy_id;
    card.innerHTML = `
      <button type="button" class="strategy-average-head" aria-expanded="false" ${generated ? "" : "disabled"}>
        <div>
          <strong>${strategy.label}</strong>
          <span>${generated ? `${strategy.seed_count || 0} seeds` : "未生成平均成绩"}</span>
        </div>
        <i>${generated ? "展开" : "等待生成"}</i>
      </button>
      ${generated ? `
        <div class="strategy-average-metrics">
          ${averageMetricBlock("Throughput", metricValue(average, "throughput"))}
          ${averageMetricBlock("MCT", metricValue(average, "mct_average"))}
          ${averageMetricBlock("Avg WIP", metricValue(average, "average_fab_wip"))}
          ${averageMetricBlock("Moves", metricValue(average, "movements"), 0)}
          ${averageMetricBlock("Setup", metricValue(average, "total_setup_time"))}
          ${averageMetricBlock("Util", metricValue(average, "machine_utilization_average"), 4)}
        </div>
      ` : `
        <p class="missing-summary-note">这个策略还没有 random seed summary。</p>
        <button type="button" class="build-summary-button">生成平均成绩</button>
        <div class="seed-progress summary-progress" hidden>
          <div class="seed-progress-bar"><span></span></div>
          <small>正在跑该策略的全部 seed...</small>
        </div>
      `}
    `;
    const buildButton = card.querySelector(".build-summary-button");
    if (buildButton) {
      buildButton.addEventListener("click", () => buildStrategySummary(strategy.strategy_id, card));
    }
    const head = card.querySelector(".strategy-average-head");
    head.addEventListener("click", () => {
      dashboardState.expandedStrategyId = dashboardState.expandedStrategyId === strategy.strategy_id
        ? null
        : strategy.strategy_id;
      renderStrategySummaries({ strategies: dashboardState.strategySummaries });
    });
    if (dashboardState.expandedStrategyId === strategy.strategy_id) {
      card.classList.add("is-expanded");
      head.setAttribute("aria-expanded", "true");
      head.querySelector("i").textContent = "收起";
    }
    root.appendChild(card);
  });
  renderStrategySeedDetailPanel();
}

function renderStrategySeedDetailPanel() {
  const panel = document.getElementById("strategy-seed-detail-panel");
  const title = document.getElementById("strategy-seed-detail-title");
  const summary = document.getElementById("strategy-seed-detail-summary");
  const root = document.getElementById("strategy-seed-detail-grid");
  if (!panel || !root) return;
  const strategy = dashboardState.strategySummaries.find((item) => item.strategy_id === dashboardState.expandedStrategyId);
  if (!strategy) {
    panel.hidden = true;
    root.innerHTML = "";
    return;
  }
  panel.hidden = false;
  title.textContent = `${strategy.label} Seed Results`;
  summary.textContent = `${strategy.seed_count || 0} seeds`;
  renderStrategySeeds(root, strategy);
}

async function buildStrategySummary(strategyId, card) {
  const status = document.getElementById("simulation-status");
  const button = card.querySelector(".build-summary-button");
  const progress = card.querySelector(".summary-progress");
  const bar = card.querySelector(".seed-progress-bar span");
  const progressText = progress.querySelector("small");
  button.disabled = true;
  progress.hidden = false;
  bar.style.width = "0%";
  progressText.textContent = "正在启动后台任务...";
  status.textContent = `正在生成 ${strategyId} 的全部 seed 平均成绩...`;
  try {
    const started = await getJson(`/api/strategies/${encodeURIComponent(strategyId)}/random-summary-job`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const jobId = started.job_id;
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, 900));
      const payload = await getJson(`/api/random-summary-jobs/${encodeURIComponent(jobId)}`);
      const job = payload.job || {};
      const seedCount = Number(job.seed_count || 0);
      const completed = Number(job.completed || 0);
      const currentSeed = Number(job.current_seed || 0);
      const activeSeeds = Array.isArray(job.active_seeds) ? job.active_seeds : [];
      const ratio = seedCount > 0 ? Math.min(completed / seedCount, 1) : 0;
      bar.style.width = `${Math.round(ratio * 100)}%`;

      if (job.state === "running" || job.state === "submitted") {
        const activeText = activeSeeds.length ? activeSeeds.join(", ") : currentSeed || "-";
        progressText.textContent = `已完成 ${completed} / ${seedCount}，运行中 seeds: ${activeText}`;
        status.textContent = `${strategyId}: 已完成 ${completed} / ${seedCount}，运行中 seeds: ${activeText}`;
      } else if (job.state === "completed_seed") {
        const activeText = activeSeeds.length ? `，运行中 seeds: ${activeSeeds.join(", ")}` : "";
        progressText.textContent = `已完成 ${completed} / ${seedCount} 个 seed${activeText}`;
        status.textContent = `${strategyId}: 已完成 ${completed} / ${seedCount} 个 seed${activeText}`;
      } else if (job.state === "done") {
        bar.style.width = "100%";
        progressText.textContent = `已完成 ${completed} / ${seedCount} 个 seed`;
        status.textContent = `${strategyId} 平均成绩已生成。`;
        await loadStrategySummaries();
        break;
      } else if (job.state === "error") {
        throw new Error(job.error || "后台任务失败");
      } else {
        progressText.textContent = "正在等待任务开始...";
      }
    }
  } catch (error) {
    button.disabled = false;
    progress.hidden = true;
    status.textContent = `${strategyId} 平均成绩生成失败：${error.message}`;
  }
}

function renderStrategySeeds(root, strategy) {
  root.innerHTML = "";
  (strategy.runs || []).forEach((run) => {
    const metrics = run.metrics || {};
    const seedIndex = Number(run.seed_index || 0);
    const item = document.createElement("article");
    item.className = "strategy-seed-card";
    item.innerHTML = `
      <div class="seed-run-head">
        <strong>Seed ${seedIndex}</strong>
        <span>${fmt(metricValue(metrics, "throughput"), 0)} done</span>
      </div>
      <div class="seed-run-metrics">
        <span>MCT <strong>${fmt(metricValue(metrics, "mct_average"))}</strong></span>
        <span>WIP <strong>${fmt(metricValue(metrics, "average_fab_wip"))}</strong></span>
        <span>Moves <strong>${fmt(metricValue(metrics, "movements"), 0)}</strong></span>
        <span>Setup <strong>${fmt(metricValue(metrics, "setup_count"), 0)}</strong></span>
      </div>
      <button type="button" class="seed-detail-button">模拟详细过程</button>
      <div class="seed-progress" hidden>
        <div class="seed-progress-bar"><span></span></div>
        <small>正在运行该 seed 的完整模拟...</small>
      </div>
    `;
    item.querySelector(".seed-detail-button").addEventListener("click", () => {
      window.location.href = `/seed-detail/${encodeURIComponent(strategy.strategy_id)}/${seedIndex}`;
    });
    root.appendChild(item);
  });
}

async function loadStrategySummaries() {
  const status = document.getElementById("strategy-summary-status");
  if (status) {
    status.textContent = "正在准备所有策略平均成绩，缺失的 summary 会自动补跑...";
  }
  const payload = await getJson("/api/strategy-random-summaries");
  renderStrategySummaries(payload);
}

function renderDebugPanels(data) {
  const debug = document.getElementById("debug-section");
  if (!debug || !debug.open) return;
  renderScheduleAnimation(data);
  renderTimeline(data);
}

function setTimelineZoom(scale) {
  timelineState.scale = Math.min(Math.max(scale, 0.6), 4);
  document.getElementById("timeline-zoom-range").value = String(timelineState.scale);
  document.getElementById("timeline-zoom-value").textContent = `${timelineState.scale.toFixed(1)}x`;
  if (window.__fabDashboardData) {
    renderTimeline(window.__fabDashboardData);
  }
}

function applyTimelineFullscreen(fullscreen) {
  timelineState.fullscreen = fullscreen;
  document.body.classList.toggle("timeline-fullscreen", fullscreen);
  const panel = document.querySelector(".panel.wide");
  if (panel) {
    panel.classList.toggle("is-fullscreen", fullscreen);
  }
  const button = document.getElementById("timeline-fullscreen");
  if (button) {
    button.title = fullscreen ? "Exit fullscreen" : "Fullscreen timeline";
    button.textContent = fullscreen ? "🗗" : "⛶";
  }
  if (fullscreen) {
    requestAnimationFrame(() => {
      const fitButton = document.getElementById("timeline-zoom-fit");
      if (fitButton) {
        fitButton.click();
      }
    });
  } else if (window.__fabDashboardData) {
    renderTimeline(window.__fabDashboardData);
  }
}

function attachTimelineZoomControls() {
  const range = document.getElementById("timeline-zoom-range");
  const fullscreen = document.getElementById("timeline-fullscreen");
  const zoomIn = document.getElementById("timeline-zoom-in");
  const zoomOut = document.getElementById("timeline-zoom-out");
  const zoomReset = document.getElementById("timeline-zoom-reset");
  const zoomFit = document.getElementById("timeline-zoom-fit");

  range.addEventListener("input", () => {
    timelineState.scale = Number(range.value);
    document.getElementById("timeline-zoom-value").textContent = `${timelineState.scale.toFixed(1)}x`;
    if (window.__fabDashboardData) {
      renderTimeline(window.__fabDashboardData);
    }
  });
  zoomIn.addEventListener("click", () => setTimelineZoom(timelineState.scale + 0.2));
  zoomOut.addEventListener("click", () => setTimelineZoom(timelineState.scale - 0.2));
  zoomReset.addEventListener("click", () => setTimelineZoom(1));
  zoomFit.addEventListener("click", () => {
    const horizon = window.__fabDashboardData ? Math.max(Number(window.__fabDashboardData.run.horizon), 1) : 1;
    const reservedWidth = timelineState.fullscreen ? 180 : 260;
    const targetWidth = Math.min(Math.max(window.innerWidth - reservedWidth, 900), timelineState.fullscreen ? 2400 : 1600);
    const fitScale = targetWidth / (horizon * timelineState.fitBase);
    setTimelineZoom(fitScale);
  });
  fullscreen.addEventListener("click", () => applyTimelineFullscreen(!timelineState.fullscreen));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && timelineState.fullscreen) {
      applyTimelineFullscreen(false);
    }
  });
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function renderMetricCards(data) {
  const business = data.business;
  const efficiency = business.efficiency || {};
  setText("strategy", data.run.strategy);
  setText("run-id", data.run.run_id);
  setText("horizon", `measurement ${fmt(data.run.horizon)} ${data.run.time_unit}`);
  setText("warmup-time", `${fmt(data.run.warmup_time)} ${data.run.time_unit}`);
  setText("measurement-window", `0 → ${fmt(data.run.horizon)}`);
  setText("measurement-duration", `${fmt(data.run.horizon)} ${data.run.time_unit}`);
  setText("cycle-avg", fmt(business.mct.average));
  setText(
    "cycle-detail",
    `P50 ${fmt(business.mct.p50)} · P90 ${fmt(business.mct.p90)} · Max ${fmt(business.mct.max)}`
  );
  setText("throughput-count", business.throughput.completed);
  setText("throughput-detail", `Rate ${fmt(business.throughput.rate, 4)} / ${data.run.time_unit}`);
  setText("completion-ratio", pct(business.throughput.completion_ratio || 0));
  setText("movement-detail", `Movement ${business.movement.count} · Rate ${fmt(business.movement.rate, 4)}`);
  setText("average-wip", fmt(business.wip.average));
  setText("released-count", business.release.count);
  const waitingCapacity = Number(business.release.waiting_capacity || 0) > 0
    ? business.release.waiting_capacity
    : "∞";
  setText(
    "released-detail",
    `Rate ${fmt(business.release.rate, 4)} · Backlog ${business.release.waiting_backlog || 0} / ${waitingCapacity}`
  );
  setText("setup-ratio", pct(efficiency.setup_to_process_ratio || 0));
  setText(
    "setup-detail",
    `Setup ${fmt(efficiency.setup_time)} · Process ${fmt(efficiency.process_time)}`
  );
}

function renderSystemStructure(data) {
  const structure = data.structure || {};
  const factory = structure.factory || {};
  const orders = structure.orders || {};
  const policy = structure.policy || {};
  const unit = data.run.time_unit;
  const capacity = Number(orders.capacity || 0) > 0 ? orders.capacity : "∞";

  setText("factory-id", data.run.factory_id || "-");
  setText(
    "factory-detail",
    `${factory.machines || 0} machines · ${factory.stations || 0} stations · ${factory.products || 0} products`
  );
  setText("order-cadence", `Every ${fmt(orders.interval)} ${unit}`);
  setText(
    "order-detail",
    `${orders.lots_per_order || 0} lots/order · ${orders.orders_generated || 0} orders generated`
  );
  setText("release-cadence", `Every ${fmt(policy.release_interval)} ${unit}`);
  setText("external-backlog", orders.final_backlog || 0);
  setText(
    "backlog-detail",
    `${orders.lots_generated || 0} generated · capacity ${capacity}`
  );
}

function renderTimeline(data) {
  const root = document.getElementById("timeline");
  root.innerHTML = "";
  const horizon = Math.max(Number(data.run.horizon), 1);
  const allItems = data.timeline.machines.flatMap((machine) => machine.operations || []);
  const startBase = Number(data.run.window_start || 0);
  const pxPerUnit = timelineState.fitBase * timelineState.scale;
  const trackWidth = Math.max(560, Math.round(horizon * pxPerUnit));

  const axis = document.createElement("div");
  axis.className = "timeline-axis";
  const axisLabel = document.createElement("div");
  axisLabel.className = "machine-label axis-label";
  axisLabel.textContent = "time";
  const axisTrack = document.createElement("div");
  axisTrack.className = "track axis-track";
  axisTrack.style.width = `${trackWidth}px`;

  const tickCount = Math.max(6, Math.min(12, Math.round(horizon / 250)));
  const step = horizon / tickCount;
  for (let i = 0; i <= tickCount; i += 1) {
    const tickTime = startBase + step * i;
    const tick = document.createElement("div");
    tick.className = "timeline-tick";
    tick.style.left = `${((tickTime - startBase) / horizon) * 100}%`;
    if (i === 0) tick.classList.add("is-start");
    if (i === tickCount) tick.classList.add("is-end");
    tick.textContent = `${fmt(tickTime - startBase, 0)}`;
    axisTrack.appendChild(tick);
  }
  axis.append(axisLabel, axisTrack);
  root.appendChild(axis);

  function layoutEvents(events) {
    const sorted = [...events].sort(
      (a, b) => Number(a.start || 0) - Number(b.start || 0) || Number(a.end || 0) - Number(b.end || 0)
    );
    const lanes = [];
    return sorted.map((op) => {
      const start = Number(op.start || 0);
      const end = Number(op.end || start);
      let laneIndex = lanes.findIndex((laneEnd) => start >= laneEnd - 0.001);
      if (laneIndex === -1) {
        laneIndex = lanes.length;
        lanes.push(end);
      } else {
        lanes[laneIndex] = end;
      }
      return { op, laneIndex };
    });
  }

  data.timeline.machines.forEach((machine, index) => {
    const row = document.createElement("div");
    row.className = "timeline-row timeline-machine";
    const label = document.createElement("div");
    label.className = "machine-label";
    label.title = `${machine.name || machine.id} (${machine.type || "Unknown"})`;
    label.textContent = machine.name || machine.id;

    const events = [...(machine.operations || [])];
    const positioned = layoutEvents(events);
    const laneCount = Math.max(...positioned.map((item) => item.laneIndex), -1) + 1;
    const lanePitch = timelineLayout.laneHeight + timelineLayout.laneGap;
    const blockHeight = timelineState.fullscreen ? timelineLayout.blockHeightFullscreen : timelineLayout.blockHeight;
    const track = document.createElement("div");
    track.className = "track";
    track.style.width = `${trackWidth}px`;
    track.style.height = `${Math.max(1, laneCount) * lanePitch + 8}px`;

    positioned.forEach(({ op, laneIndex }) => {
      const kind = op.kind || "process";
      const block = document.createElement("div");
      block.className = `op ${kind}`;
      const leftPx = Math.max(0, Math.round((Number(op.start) - startBase) * pxPerUnit));
      const widthPx = Math.max(4, Math.round(Number(op.duration || Number(op.end) - Number(op.start)) * pxPerUnit));
      block.style.left = `${leftPx}px`;
      block.style.width = `${widthPx}px`;
      block.style.top = `${laneIndex * lanePitch + 3}px`;
      block.style.height = `${blockHeight}px`;
      if (kind === "process") {
        block.style.background = palette[index % palette.length];
        block.title = `${op.wafer_id} · ${op.process}: ${fmt(Number(op.start) - startBase)} -> ${fmt(Number(op.end) - startBase)}`;
        block.textContent = widthPx >= timelineLayout.labelWidthThreshold ? op.wafer_id || "" : "";
      } else if (kind === "setup") {
        block.title = `setup ${op.from_product_id || "None"} -> ${op.to_product_id}: ${fmt(Number(op.start) - startBase)} -> ${fmt(Number(op.end) - startBase)}`;
        block.textContent = widthPx >= timelineLayout.labelWidthThreshold ? "SET" : "";
      } else if (kind === "downtime") {
        block.title = `downtime · ${op.reason || "failure"}: ${fmt(Number(op.start) - startBase)} -> ${fmt(Number(op.end) - startBase)}`;
        block.textContent = widthPx >= timelineLayout.labelWidthThreshold ? "DOWN" : "";
      }
      track.appendChild(block);
    });

    row.append(label, track);
    root.appendChild(row);
  });
}

function collectProducts(data) {
  const products = Array.isArray(data.raw.products) ? data.raw.products : [];
  if (products.length > 0) return products;

  const grouped = new Map();
  (data.raw.wafers || []).forEach((wafer) => {
    const route = Array.isArray(wafer.route) ? wafer.route : [];
    if (route.length === 0) return;
    const id = wafer.product_id || "Product";
    if (!grouped.has(id)) {
      grouped.set(id, {
        id,
        name: wafer.product_name || id,
        route,
      });
    }
  });
  return Array.from(grouped.values());
}

function reentryProcesses(product) {
  if (Array.isArray(product.reentry_steps) && product.reentry_steps.length > 0) {
    return product.reentry_steps.map((item) => item.process);
  }

  const counts = new Map();
  (product.route || []).forEach((operation) => {
    const processName = typeof operation === "string" ? operation : operation.process;
    if (!processName) return;
    counts.set(processName, (counts.get(processName) || 0) + 1);
  });
  return Array.from(counts.entries())
    .filter(([, count]) => count > 1)
    .map(([processName]) => processName);
}

function renderProductPerformance(data) {
  const root = document.getElementById("product-performance");
  root.innerHTML = "";
  const products = collectProducts(data);
  if (products.length === 0) {
    const empty = document.createElement("p");
    empty.className = "status";
    empty.textContent = "当前结果没有产品工序数据。";
    root.appendChild(empty);
    return;
  }

  const performance = new Map((data.business.products || []).map((item) => [item.id, item]));
  products.forEach((product) => {
    const route = product.route || [];
    const stats = performance.get(product.id) || {};
    const card = document.createElement("article");
    card.className = "product-performance-card";

    const header = document.createElement("div");
    header.className = "product-route-header";
    const title = document.createElement("div");
    title.className = "product-route-title";
    const name = document.createElement("strong");
    name.textContent = product.name || product.id;
    const id = document.createElement("small");
    id.textContent = product.id || "";
    title.append(name, id);
    const meta = document.createElement("span");
    meta.className = "route-meta";
    meta.textContent = `${route.length} steps`;
    header.append(title, meta);

    const mix = document.createElement("div");
    mix.className = "product-mix";
    mix.innerHTML = `
      <div><span>Target</span><strong>${pct(stats.target_share || 0)}</strong></div>
      <div><span>Released</span><strong>${stats.released || 0}</strong><small>${pct(stats.release_share || 0)}</small></div>
      <div><span>Completed</span><strong>${stats.completed || 0}</strong></div>
      <div><span>Avg MCT</span><strong>${fmt(stats.mct_average || 0)}</strong></div>
    `;
    const mixBar = document.createElement("div");
    mixBar.className = "mix-bar";
    mixBar.innerHTML = `<span style="width:${Math.min((stats.target_share || 0) * 100, 100)}%"></span><i style="left:${Math.min((stats.release_share || 0) * 100, 100)}%"></i>`;
    mixBar.title = `Target ${pct(stats.target_share || 0)} · Actual release ${pct(stats.release_share || 0)}`;

    card.append(header, mix, mixBar);
    root.appendChild(card);
  });
}

function orderProductCounts(order) {
  const counts = new Map();
  (order.lots || []).forEach((lot) => {
    const productId = lot.product_id || "Unknown";
    counts.set(productId, (counts.get(productId) || 0) + 1);
  });
  return Array.from(counts.entries()).sort((a, b) => a[0].localeCompare(b[0]));
}

function renderOrderHistory(data) {
  const root = document.getElementById("order-history");
  const summary = document.getElementById("order-history-summary");
  root.innerHTML = "";
  const orders = (data.raw.order_events || []).filter((order) => order.accepted);
  summary.textContent = `${orders.length} orders`;
  if (orders.length === 0) {
    root.innerHTML = '<p class="status">当前结果没有订单到达记录。</p>';
    return;
  }

  orders.forEach((order) => {
    const card = document.createElement("article");
    card.className = "order-card";
    const counts = orderProductCounts(order);
    const mix = counts.length
      ? counts.map(([productId, count]) => `<span>${productId}<strong>${count}</strong></span>`).join("")
      : `<span>Lots<strong>${order.quantity || 0}</strong></span>`;
    card.innerHTML = `
      <div class="order-card-head">
        <strong>${order.order_id || "Order"}</strong>
        <span>t=${fmt(displayTime(order.time, data))} ${data.run.time_unit}</span>
      </div>
      <div class="order-mix">${mix}</div>
      <small>${order.quantity || 0} lots · queue after arrival ${order.queue_size || 0}</small>
    `;
    root.appendChild(card);
  });
}

function attachOrderHistoryToggle() {
  const panel = document.querySelector(".order-panel");
  const button = document.getElementById("order-history-toggle");
  button.addEventListener("click", () => {
    const collapsed = panel.classList.toggle("is-collapsed");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.textContent = collapsed ? "展开" : "收起";
  });
}

function attachDebugToggle() {
  const debug = document.getElementById("debug-section");
  if (!debug) return;
  debug.addEventListener("toggle", () => {
    if (debug.open && window.__fabDashboardData) {
      renderDebugPanels(window.__fabDashboardData);
    } else {
      stopAnimation();
    }
  });
}

function barRow(labelText, valueText, ratio, color) {
  const row = document.createElement("div");
  row.className = "bar-row";
  const label = document.createElement("div");
  label.className = "bar-label";
  label.innerHTML = `<span>${labelText}</span><strong>${valueText}</strong>`;
  const shell = document.createElement("div");
  shell.className = "bar-shell";
  const fill = document.createElement("div");
  fill.className = "bar-fill";
  fill.style.width = `${Math.max(Math.min(ratio, 1), 0) * 100}%`;
  fill.style.background = color || "var(--accent)";
  shell.appendChild(fill);
  row.append(label, shell);
  return row;
}

function renderUtilization(data) {
  const root = document.getElementById("utilization-bars");
  root.innerHTML = "";
  const utilization = data.health.machine_utilization || {};
  setText("utilization-summary", `Avg ${pct(utilization.average || 0)} · Max ${pct(utilization.max || 0)}`);
  data.health.machine_utilization.machines
    .slice()
    .sort((a, b) => b.utilization - a.utilization)
    .forEach((machine) => {
      root.appendChild(
        barRow(
          machine.name,
          `${pct(machine.utilization)} · setup ${fmt(machine.setup_time)} · down ${pct(machine.downtime_ratio || 0)}`,
          machine.utilization,
          "var(--accent)"
        )
      );
    });
}

function renderWaitByProcess(data) {
  const root = document.getElementById("wait-bars");
  root.innerHTML = "";
  const rows = data.health.wait_by_process || [];
  const maxWait = Math.max(...rows.map((row) => Number(row.max_wait)), 1);
  rows
    .slice()
    .sort((a, b) => b.max_wait - a.max_wait)
    .forEach((row) => {
      root.appendChild(
        barRow(
          row.process,
          `Avg ${fmt(row.average_wait)} · Max ${fmt(row.max_wait)}`,
          row.max_wait / maxWait,
          "var(--warn)"
        )
      );
    });
}

function renderQueues(data) {
  const root = document.getElementById("queue-bars");
  root.innerHTML = "";
  const maxQueue = Math.max(...data.health.queue_length.map((queue) => Number(queue.max_length)), 1);
  data.health.queue_length
    .slice()
    .sort((a, b) => b.max_length - a.max_length)
    .forEach((queue) => {
      root.appendChild(
        barRow(queue.queue, `Avg ${fmt(queue.average_length)} · Max ${fmt(queue.max_length)}`, queue.max_length / maxQueue, "var(--accent-2)")
      );
    });
}

function renderBottlenecks(data) {
  const root = document.getElementById("bottlenecks");
  root.innerHTML = "";
  data.health.bottlenecks.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "rank-item";
    row.innerHTML = `<strong>${index + 1}. ${item.name}</strong><small>${item.kind} · ${item.reason}</small>`;
    root.appendChild(row);
  });
}

function bufferColor(state) {
  if (state === "red") return "var(--danger)";
  if (state === "yellow") return "var(--warn)";
  if (state === "green") return "var(--good)";
  if (state === "over") return "var(--accent-2)";
  return "var(--muted)";
}

function collectAnimationTimes(data) {
  const times = new Set([
    Number(data.run.window_start || 0),
    Number(data.run.window_end || data.run.horizon || 0),
  ]);
  (data.timeline.machines || []).forEach((machine) => {
    (machine.operations || []).forEach((event) => {
      times.add(Number(event.start || 0));
      times.add(Number(event.end || event.start || 0));
    });
  });
  (data.raw.buffer_samples || []).forEach((sample) => times.add(Number(sample.time || 0)));
  (data.raw.order_events || []).forEach((event) => times.add(Number(event.time || 0)));
  (data.raw.release_events || []).forEach((event) => times.add(Number(event.release_time || 0)));
  return Array.from(times)
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b);
}

function activeMachineEvent(machine, currentTime) {
  const events = machine.operations || [];
  return (
    events.find((event) => {
      const start = Number(event.start || 0);
      const end = Number(event.end || start);
      return start <= currentTime && currentTime < end;
    }) || null
  );
}

function latestBufferSample(data, currentTime) {
  const samples = data.raw.buffer_samples || [];
  if (samples.length === 0) return null;
  let selected = samples[0];
  for (const sample of samples) {
    if (Number(sample.time || 0) <= currentTime) {
      selected = sample;
    } else {
      break;
    }
  }
  return selected;
}

function bufferPolicy(data) {
  return data.raw.buffer_policy || {};
}

function maxBufferScale(data) {
  const samples = data.raw.buffer_samples || [];
  const policy = bufferPolicy(data);
  const sampleMax = Math.max(...samples.map((sample) => Number(sample.buffer_time || 0)), 0);
  return Math.max(sampleMax, Number(policy.over_buffer_time || 0), Number(policy.target_buffer_time || 0), 1);
}

function setAnimationTimeIndex(index) {
  const nextIndex = Math.min(Math.max(index, 0), animationState.times.length - 1);
  animationState.timeIndex = nextIndex;
  const range = document.getElementById("animation-range");
  range.value = String(nextIndex);
  if (window.__fabDashboardData) {
    updateScheduleAnimation(window.__fabDashboardData);
  }
}

function stopAnimation() {
  animationState.playing = false;
  if (animationState.timer) {
    clearInterval(animationState.timer);
    animationState.timer = null;
  }
  const button = document.getElementById("animation-toggle");
  if (button) {
    button.textContent = "▶";
    button.title = "Play";
  }
}

function startAnimation() {
  stopAnimation();
  animationState.playing = true;
  const button = document.getElementById("animation-toggle");
  button.textContent = "Ⅱ";
  button.title = "Pause";
  const tick = () => {
    if (animationState.timeIndex >= animationState.times.length - 1) {
      stopAnimation();
      return;
    }
    const speed = Number(document.getElementById("animation-speed").value || 1);
    const step = Math.max(1, Math.round(speed));
    setAnimationTimeIndex(animationState.timeIndex + step);
  };
  animationState.timer = setInterval(tick, 360);
}

function renderScheduleAnimation(data) {
  animationState.times = collectAnimationTimes(data);
  animationState.timeIndex = Math.min(animationState.timeIndex, animationState.times.length - 1);
  const range = document.getElementById("animation-range");
  range.max = String(Math.max(animationState.times.length - 1, 0));
  range.value = String(animationState.timeIndex);

  const root = document.getElementById("animation-machines");
  root.innerHTML = "";
  (data.timeline.machines || []).forEach((machine) => {
    const row = document.createElement("div");
    row.className = "animation-machine";
    row.dataset.machineId = machine.id;
    row.innerHTML = `
      <div class="animation-machine-head">
        <strong>${machine.name || machine.id}</strong>
        <span>${machine.type || "Unknown"}</span>
      </div>
      <div class="animation-machine-body">
        <span class="machine-status">Idle</span>
        <strong class="machine-wafer">-</strong>
        <small class="machine-window">-</small>
      </div>
    `;
    root.appendChild(row);
  });

  renderBufferStrip(data);
  updateScheduleAnimation(data);
}

function renderBufferStrip(data) {
  const root = document.getElementById("waiting-list");
  root.innerHTML = "";
  const waiting = data.raw.waiting_list || {};
  const capacityLabel = waiting.capacity > 0 ? waiting.capacity : "∞";
  setText("waiting-size", `0 / ${capacityLabel}`);
  setText("waiting-head", "-");
  setText("generated-count", waiting.generated_count || 0);
}

function waitingListAtTime(data, currentTime) {
  const lots = [];
  const knownLots = new Map();
  (data.raw.wafers || []).forEach((lot) => knownLots.set(lot.id, lot));
  (data.raw.release_events || []).forEach((event) => {
    knownLots.set(event.wafer_id, {
      ...(knownLots.get(event.wafer_id) || {}),
      id: event.wafer_id,
      product_id: event.product_id,
    });
  });
  (data.raw.order_events || [])
    .filter((order) => order.accepted && Number(order.time || 0) <= currentTime)
    .forEach((order) => {
      const orderLots = Array.isArray(order.lots) && order.lots.length > 0
        ? order.lots
        : (order.wafer_ids || []).map((id) => ({
            id,
            ...(knownLots.get(id) || {}),
          }));
      orderLots.forEach((lot) => {
        lots.push({
          ...lot,
          order_id: order.order_id,
          generation_time: order.time,
        });
      });
    });
  const releasedIds = new Set(
    (data.raw.release_events || [])
      .filter((event) => Number(event.release_time || 0) <= currentTime)
      .map((event) => event.wafer_id)
  );
  return lots.filter((lot) => !releasedIds.has(lot.id));
}

function groupWaitingOrders(lots) {
  const groups = new Map();
  lots.forEach((lot) => {
    const orderId = lot.order_id || "Unknown";
    if (!groups.has(orderId)) {
      groups.set(orderId, {
        order_id: orderId,
        generation_time: lot.generation_time,
        lots: [],
        products: new Map(),
      });
    }
    const group = groups.get(orderId);
    group.lots.push(lot);
    const productId = lot.product_id || "Unknown";
    group.products.set(productId, (group.products.get(productId) || 0) + 1);
  });
  return Array.from(groups.values());
}

function updateWaitingList(data, currentTime) {
  const root = document.getElementById("waiting-list");
  root.innerHTML = "";
  const waiting = data.raw.waiting_list || {};
  let snapshot = waitingListAtTime(data, currentTime);
  if (snapshot.length === 0 && !(data.raw.order_events || []).some((order) => Array.isArray(order.wafer_ids))) {
    snapshot = currentTime >= Number(data.run.window_end || 0) ? waiting.final_snapshot || [] : [];
  }
  const orders = groupWaitingOrders(snapshot);
  const capacityLabel = waiting.capacity > 0 ? waiting.capacity : "∞";
  setText("waiting-size", `${snapshot.length} lots / ${capacityLabel}`);
  setText("waiting-head", orders[0] ? orders[0].order_id : "-");
  orders.slice(0, 12).forEach((order, index) => {
    const item = document.createElement("div");
    item.className = "waiting-order";
    const mix = Array.from(order.products.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([productId, count]) => `<span>${productId} ${count}</span>`)
      .join("");
    item.innerHTML = `
      <div class="waiting-order-head">
        <span>${index + 1}</span>
        <strong>${order.order_id}</strong>
        <small>${order.lots.length} lots</small>
      </div>
      <div class="waiting-order-mix">${mix}</div>
    `;
    item.title = `arrived ${fmt(displayTime(order.generation_time, data))} · ${order.lots.length} lots remaining`;
    root.appendChild(item);
  });
  if (orders.length > 12) {
    const more = document.createElement("div");
    more.className = "waiting-more";
    more.textContent = `+${orders.length - 12} more orders`;
    root.appendChild(more);
  }
}

function updateScheduleAnimation(data) {
  const currentTime = animationState.times[animationState.timeIndex] || 0;
  setText("animation-clock", `time ${fmt(displayTime(currentTime, data))} ${data.run.time_unit}`);
  updateWaitingList(data, currentTime);

  (data.timeline.machines || []).forEach((machine) => {
    const row = document.querySelector(`.animation-machine[data-machine-id="${CSS.escape(String(machine.id))}"]`);
    if (!row) return;
    const event = activeMachineEvent(machine, currentTime);
    const status = row.querySelector(".machine-status");
    const wafer = row.querySelector(".machine-wafer");
    const windowText = row.querySelector(".machine-window");
    row.classList.remove("is-process", "is-setup", "is-downtime", "is-idle");
    if (!event) {
      row.classList.add("is-idle");
      status.textContent = "Idle";
      wafer.textContent = "-";
      windowText.textContent = "waiting";
      return;
    }

    const kind = event.kind || "process";
    row.classList.add(`is-${kind}`);
    status.textContent = kind === "process" ? event.process || machine.type || "Process" : kind;
    wafer.textContent = kind === "process" ? event.wafer_id || "-" : `${event.from_product_id || ""}→${event.to_product_id || ""}`;
    windowText.textContent = `${fmt(displayTime(event.start, data))} -> ${fmt(displayTime(event.end, data))}`;
  });

}

function attachAnimationControls() {
  const toggle = document.getElementById("animation-toggle");
  const back = document.getElementById("animation-back");
  const forward = document.getElementById("animation-forward");
  const range = document.getElementById("animation-range");
  toggle.addEventListener("click", () => {
    if (animationState.playing) {
      stopAnimation();
    } else {
      if (animationState.timeIndex >= animationState.times.length - 1) {
        setAnimationTimeIndex(0);
      }
      startAnimation();
    }
  });
  back.addEventListener("click", () => {
    stopAnimation();
    setAnimationTimeIndex(animationState.timeIndex - 1);
  });
  forward.addEventListener("click", () => {
    stopAnimation();
    setAnimationTimeIndex(animationState.timeIndex + 1);
  });
  range.addEventListener("input", () => {
    stopAnimation();
    setAnimationTimeIndex(Number(range.value));
  });
}

async function refresh() {
  stopAnimation();
  if (dashboardState.cache[dashboardState.resultId]) {
    renderDashboard(dashboardState.cache[dashboardState.resultId]);
    return;
  }
  const suffix = dashboardState.resultId && dashboardState.resultId !== "current" ? `?result=${encodeURIComponent(dashboardState.resultId)}` : "";
  const data = await getJson(`/api/dashboard${suffix}`);
  dashboardState.cache[dashboardState.resultId] = data;
  renderDashboard(data);
}

function activateResultTab() {
  document.querySelectorAll(".result-tab").forEach((tab) => {
    const active = tab.dataset.resultId === dashboardState.resultId;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
}

async function selectResult(resultId) {
  dashboardState.resultId = resultId;
  activateResultTab();
  try {
    await refresh();
  } catch (error) {
    document.getElementById("submit-status").textContent = `切换结果失败：${error.message}`;
  }
}

async function loadResultTabs(preferredResultId) {
  const tabs = document.getElementById("result-tabs");
  const query = preferredResultId ? `?result=${encodeURIComponent(preferredResultId)}` : "";
  const response = await getJson(`/api/results${query}`);
  dashboardState.results = response.results || [];
  tabs.innerHTML = "";
  dashboardState.results.forEach((item) => {
    const button = document.createElement("button");
    button.className = "result-tab";
    button.type = "button";
    button.role = "tab";
    button.dataset.resultId = item.id;
    button.textContent = item.label || item.id;
    button.title = item.generated_at ? `Generated ${item.generated_at}` : item.id;
    button.addEventListener("click", () => selectResult(item.id));
    tabs.appendChild(button);
  });
  dashboardState.resultId = response.selected || "current";
  if (![...tabs.querySelectorAll(".result-tab")].some((tab) => tab.dataset.resultId === dashboardState.resultId)) {
    dashboardState.resultId = dashboardState.results[0] ? dashboardState.results[0].id : "current";
  }
  activateResultTab();
}

async function loadStrategyOptions() {
  const menu = document.getElementById("simulation-strategy");
  const response = await getJson("/api/strategies");
  menu.innerHTML = "";
  response.strategies.forEach((strategy) => {
    const option = document.createElement("option");
    option.value = strategy.id;
    option.textContent = strategy.label;
    menu.appendChild(option);
  });
  updateSimulationOutputName();
  menu.addEventListener("change", updateSimulationOutputName);
}

function updateSimulationOutputName() {
  const strategyId = document.getElementById("simulation-strategy").value || "simulation";
  document.getElementById("simulation-output").value = `${strategyId}_result.json`;
}

function setCurrentResultOption() {
  activateResultTab();
}

async function loadSamplePayload() {
  const sample = await getJson("/api/sample-run");
  document.getElementById("payload").value = JSON.stringify(sample, null, 2);
  document.getElementById("submit-status").textContent = "示例 JSON 已载入，可直接提交或修改后提交。";
}

async function submitRun() {
  const status = document.getElementById("submit-status");
  try {
    const payload = JSON.parse(document.getElementById("payload").value);
    const data = await getJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    status.textContent = "已更新看板。";
    dashboardState.resultId = "current";
    dashboardState.cache.current = data.dashboard;
    await loadResultTabs();
    dashboardState.resultId = "current";
    setCurrentResultOption();
    renderDashboard(data.dashboard);
  } catch (error) {
    status.textContent = `提交失败：${error.message}`;
  }
}

async function runSimulation() {
  const status = document.getElementById("simulation-status");
  const button = document.getElementById("run-simulation");
  button.disabled = true;
  status.textContent = "正在运行策略模拟...";
  try {
    const payload = {
      strategy_id: document.getElementById("simulation-strategy").value,
      seed_index: Number(document.getElementById("simulation-seed").value || 1),
      output_name: document.getElementById("simulation-output").value,
    };
    const data = await getJson("/api/simulations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    dashboardState.resultId = data.result_id || "current";
    dashboardState.cache[dashboardState.resultId] = data.dashboard;
    await loadResultTabs(dashboardState.resultId);
    setCurrentResultOption();
    renderDashboard(data.dashboard);
    status.textContent = `模拟完成，已保存到 ${data.result_path}。`;
  } catch (error) {
    status.textContent = `模拟失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function runRandomRun() {
  const status = document.getElementById("simulation-status");
  const button = document.getElementById("run-random-run");
  button.disabled = true;
  status.textContent = "正在运行全部种子评测...";
  try {
    const strategyId = document.getElementById("simulation-strategy").value;
    const payload = {
      strategy_id: strategyId,
      output_name: `${strategyId}_random_run_result.json`,
    };
    const data = await getJson("/api/random-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    dashboardState.resultId = data.result_id || "current";
    dashboardState.cache[dashboardState.resultId] = data.dashboard;
    await loadResultTabs(dashboardState.resultId);
    setCurrentResultOption();
    renderDashboard(data.dashboard);
    status.textContent = `全部种子评测完成，已保存到 ${data.result_path}。`;
  } catch (error) {
    status.textContent = `全部种子评测失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function runSeedDetail(seedIndex) {
  const status = document.getElementById("simulation-status");
  const resultId = dashboardState.resultId;
  if (!resultId || resultId === "current") {
    status.textContent = "请先选择一个已保存的 random-run summary。";
    return;
  }
  status.textContent = `正在运行 seed ${seedIndex} 的详细模拟...`;
  document.querySelectorAll(".seed-detail-button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const data = await getJson(`/api/random-runs/${encodeURIComponent(resultId)}/seeds/${seedIndex}/detail`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    dashboardState.resultId = data.result_id || "current";
    dashboardState.cache[dashboardState.resultId] = data.dashboard;
    await loadResultTabs(dashboardState.resultId);
    setCurrentResultOption();
    renderDashboard(data.dashboard);
    const debug = document.getElementById("debug-section");
    if (debug) debug.open = true;
    status.textContent = `Seed ${seedIndex} 详细过程已生成，保存到 ${data.result_path}。`;
  } catch (error) {
    status.textContent = `Seed ${seedIndex} 详细过程生成失败：${error.message}`;
  } finally {
    document.querySelectorAll(".seed-detail-button").forEach((button) => {
      button.disabled = false;
    });
  }
}

async function runStrategySeedDetail(strategyId, seedIndex, card) {
  const status = document.getElementById("simulation-status");
  const button = card.querySelector(".seed-detail-button");
  const progress = card.querySelector(".seed-progress");
  const bar = card.querySelector(".seed-progress-bar span");
  let progressValue = 8;
  let timer = null;
  button.disabled = true;
  progress.hidden = false;
  bar.style.width = `${progressValue}%`;
  status.textContent = `正在运行 ${strategyId} seed ${seedIndex} 的详细模拟...`;
  timer = setInterval(() => {
    progressValue = Math.min(progressValue + Math.max(1, (92 - progressValue) * 0.08), 92);
    bar.style.width = `${progressValue}%`;
  }, 400);

  try {
    const data = await getJson(`/api/strategies/${encodeURIComponent(strategyId)}/seeds/${seedIndex}/detail`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    clearInterval(timer);
    bar.style.width = "100%";
    document.body.classList.remove("summary-mode");
    dashboardState.resultId = data.result_id || "current";
    dashboardState.cache[dashboardState.resultId] = data.dashboard;
    renderDashboard(data.dashboard);
    const debug = document.getElementById("debug-section");
    if (debug) debug.open = true;
    status.textContent = `Seed ${seedIndex} 详细过程已生成，保存到 ${data.result_path}。`;
  } catch (error) {
    clearInterval(timer);
    status.textContent = `Seed ${seedIndex} 详细过程生成失败：${error.message}`;
    progress.hidden = true;
  } finally {
    button.disabled = false;
  }
}

function attachFabDashboardControls() {
  if (window.__fabDashboardControlsAttached) return;
  window.__fabDashboardControlsAttached = true;
  const loadSampleButton = document.getElementById("load-sample");
  const submitRunButton = document.getElementById("submit-run");
  if (loadSampleButton) loadSampleButton.addEventListener("click", loadSamplePayload);
  if (submitRunButton) submitRunButton.addEventListener("click", submitRun);
  attachTimelineZoomControls();
  attachAnimationControls();
  attachOrderHistoryToggle();
  attachDebugToggle();
  setTimelineZoom(1.4);
}

function showFabDashboardData(data) {
  attachFabDashboardControls();
  renderDashboard(data);
  const debug = document.getElementById("debug-section");
  if (debug) debug.open = true;
}

window.showFabDashboardData = showFabDashboardData;

function initializeFabDashboardPage() {
  attachFabDashboardControls();
  const initialResultId = window.__initialResultId || "";
  if (initialResultId) {
    dashboardState.resultId = initialResultId;
    document.body.classList.remove("summary-mode");
    refresh()
      .then(loadSamplePayload)
      .catch((error) => {
        const status = document.getElementById("submit-status");
        if (status) status.textContent = `加载失败：${error.message}`;
      });
  } else {
    loadStrategySummaries()
      .then(loadSamplePayload)
      .catch((error) => {
        const submitStatus = document.getElementById("submit-status");
        if (submitStatus) submitStatus.textContent = `加载失败：${error.message}`;
        const status = document.getElementById("strategy-summary-status");
        if (status) status.textContent = `加载失败：${error.message}`;
      });
  }
}

if (!window.__fabDashboardManual) {
  initializeFabDashboardPage();
}
