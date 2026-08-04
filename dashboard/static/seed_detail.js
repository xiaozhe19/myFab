function fmt(value, digits = 2) {
  if (!Number.isFinite(Number(value))) return "0";
  return Number(value).toFixed(digits).replace(/\.?0+$/, "");
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function metricBlock(label, value, digits = 2) {
  const item = document.createElement("div");
  item.innerHTML = `<span>${label}</span><strong>${fmt(value, digits)}</strong>`;
  return item;
}

function renderSeedSummary(data) {
  const metrics = data.metrics || {};
  document.getElementById("seed-page-title").textContent = `${data.strategy_label} · Seed ${data.seed_index}`;
  document.getElementById("seed-summary-title").textContent = `${data.strategy_label} Seed ${data.seed_index}`;
  document.getElementById("seed-summary-subtitle").textContent = `${data.seed_index} / ${data.seed_count}`;

  const grid = document.getElementById("seed-summary-grid");
  grid.innerHTML = "";
  [
    ["Throughput", metrics.throughput],
    ["MCT", metrics.mct_average],
    ["Avg WIP", metrics.average_fab_wip],
    ["Moves", metrics.movements, 0],
    ["Setup Count", metrics.setup_count, 0],
    ["Setup Time", metrics.total_setup_time],
  ].forEach(([label, value, digits]) => {
    grid.appendChild(metricBlock(label, value, digits ?? 2));
  });

  const seedSet = document.getElementById("seed-set-grid");
  seedSet.innerHTML = "";
  Object.entries(data.seed_set || {}).forEach(([key, value]) => {
    seedSet.appendChild(metricBlock(key, value, 0));
  });
}

async function loadSeedSummary() {
  const { strategyId, seedIndex } = window.__seedDetail;
  const data = await getJson(`/api/strategies/${encodeURIComponent(strategyId)}/seeds/${seedIndex}/summary`);
  renderSeedSummary(data);
}

async function generateDetail() {
  const { strategyId, seedIndex } = window.__seedDetail;
  const button = document.getElementById("generate-detail");
  const progress = document.getElementById("detail-progress");
  const bar = progress.querySelector(".seed-progress-bar span");
  const progressText = progress.querySelector("small");
  const complete = document.getElementById("detail-complete");
  const status = document.getElementById("detail-status");

  button.disabled = true;
  progress.hidden = false;
  complete.hidden = true;
  bar.style.width = "0%";
  progressText.textContent = "正在运行完整模拟，这一步才会生成详细 trace...";
  status.textContent = "正在生成详细过程";

  let running = true;
  let pulse = 0;
  const timer = setInterval(() => {
    pulse = (pulse + 1) % 4;
    bar.style.width = `${25 + pulse * 18}%`;
  }, 650);

  try {
    const data = await getJson(`/api/strategies/${encodeURIComponent(strategyId)}/seeds/${seedIndex}/detail`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    running = false;
    clearInterval(timer);
    bar.style.width = "100%";
    progressText.textContent = "完成，正在展示详细过程...";
    status.textContent = "详细过程已生成";
    complete.hidden = false;
    complete.textContent = `完成：${data.result_path}`;
    const section = document.getElementById("embedded-detail-section");
    const meta = document.getElementById("embedded-detail-meta");
    section.hidden = false;
    meta.textContent = data.result_path;
    window.showFabDashboardData(data.dashboard);
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    if (running) clearInterval(timer);
    button.disabled = false;
    status.textContent = "详细过程生成失败";
    progressText.textContent = error.message;
  }
}

document.getElementById("generate-detail").addEventListener("click", generateDetail);
loadSeedSummary().catch((error) => {
  document.getElementById("seed-summary-subtitle").textContent = `加载失败：${error.message}`;
});
