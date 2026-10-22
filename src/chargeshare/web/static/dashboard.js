// 管委會儀表板：每 5 秒取得最近 1 小時的總功率，並畫出 30 kW 上限線。
(function () {
  const canvas = document.getElementById("power-chart");
  if (!canvas) return;
  const kw = (w) => w / 1000;
  const hhmm = (iso) => iso.slice(11, 16);

  const chart = new Chart(canvas, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "實際總功率", data: [], borderColor: "#2563eb", backgroundColor: "rgba(37,99,235,.12)",
        fill: true, tension: 0.2, pointRadius: 0 },
      { label: "已分配上限（含保留）", data: [], borderColor: "#d97706", stepped: true, pointRadius: 0, borderWidth: 1.5 },
      { label: "契約上限", data: [], borderColor: "#dc2626", borderDash: [6, 4], pointRadius: 0, borderWidth: 2 },
    ] },
    options: {
      animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { y: { min: 0, suggestedMax: 35, title: { display: true, text: "kW" } },
                x: { ticks: { maxTicksLimit: 12 } } },
      plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)} kW` } } },
    },
  });

  async function refresh() {
    const response = await fetch("/api/power");
    if (!response.ok) return;
    const { capacity_w, points } = await response.json();
    chart.data.labels = points.map((p) => hhmm(p.ts));
    chart.data.datasets[0].data = points.map((p) => kw(p.measured_w));
    chart.data.datasets[1].data = points.map((p) => kw(p.limit_w + p.reserved_w));
    chart.data.datasets[2].data = points.map(() => kw(capacity_w));
    chart.update();
  }
  refresh();
  setInterval(refresh, 5000);
})();
