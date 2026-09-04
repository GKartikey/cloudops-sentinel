/* CloudOps Sentinel dashboard logic.
 *
 * Deliberately dependency-free. One aggregate poll of /api/v1/overview drives
 * almost the whole page, and the chart is inline SVG drawn by hand. Nothing is
 * fetched from a CDN, so the dashboard behaves identically on an air-gapped
 * machine and there is no npm dependency tree hanging off a monitoring tool.
 *
 * Every string that comes from the API is inserted with textContent, never
 * innerHTML, so a resource name or a log message can never become script.
 */

(() => {
  "use strict";

  const POLL_MS = 5000;
  const $ = (id) => document.getElementById(id);

  const state = {
    selected: null,
    logLevel: "",
    scenarios: [],
    resources: [],
    failures: 0,
  };

  // ------------------------------------------------------------ utilities
  const fmtMoney = (n) =>
    "$" + Number(n || 0).toLocaleString("en-US", { maximumFractionDigits: 0 });
  const fmtMoney2 = (n) =>
    "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtNum = (n, d = 1) =>
    n === undefined || n === null ? "-" : Number(n).toFixed(d);
  const clockOf = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB");
  const ago = (ts) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return Math.round(s / 3600) + "h";
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function severityClass(sev) {
    if (sev === "critical") return "crit";
    if (sev === "high" || sev === "warning" || sev === "medium") return "warn";
    return "info";
  }

  function utilClass(pct) {
    if (pct >= 90) return "crit";
    if (pct >= 75) return "warn";
    if (pct >= 5) return "ok";
    return "";
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(response.status + " " + (body || response.statusText));
    }
    return response.json();
  }

  // ------------------------------------------------------------------ KPIs
  function renderKpis(d) {
    const cards = [
      {
        label: "Fleet health",
        value: fmtNum(d.fleet.score, 1),
        foot: `${d.fleet.counts.healthy} healthy / ${d.fleet.counts.degraded} degraded / ${d.fleet.counts.unhealthy} unhealthy`,
        tone: d.fleet.status === "healthy" ? "ok" : d.fleet.status === "unhealthy" ? "crit" : "warn",
      },
      {
        label: "Resources monitored",
        value: String(d.fleet.total),
        foot: `${d.resources.filter((r) => r.source === "live").length} live containers, ${d.resources.filter((r) => r.source !== "live").length} simulated`,
        tone: "info",
      },
      {
        label: "Estimated spend",
        value: fmtMoney(d.cost.total_monthly),
        foot: `per month  ·  ${fmtMoney(d.cost.total_annual)}/yr`,
        tone: "",
      },
      {
        label: "Identified waste",
        value: fmtMoney(d.cost.waste_monthly),
        foot: `${fmtNum(d.cost.waste_pct, 1)}% of spend earning nothing`,
        tone: "warn",
      },
      {
        label: "Savings identified",
        value: fmtMoney(d.recommendations.summary.monthly_saving),
        foot: `${fmtMoney(d.recommendations.summary.annual_saving)}/yr across ${d.recommendations.summary.total} findings`,
        tone: "accent",
      },
      {
        label: "Active alerts",
        value: String(d.alerts.summary.firing),
        foot: `${d.alerts.summary.critical} critical  ·  ${d.alerts.summary.pending} pending`,
        tone: d.alerts.summary.critical ? "crit" : d.alerts.summary.firing ? "warn" : "ok",
      },
    ];

    const host = $("kpis");
    host.replaceChildren();
    for (const card of cards) {
      const box = el("div", "kpi " + card.tone);
      box.append(
        el("div", "label", card.label),
        el("div", "value", card.value),
        el("div", "foot", card.foot)
      );
      host.append(box);
    }
  }

  // ------------------------------------------------------------- resources
  function renderResources(d) {
    const body = $("res-body");
    body.replaceChildren();
    $("res-count").textContent = `${d.resources.length} resources`;

    for (const r of d.resources) {
      const row = el("tr");
      if (state.selected === r.id) row.classList.add("selected");
      row.addEventListener("click", () => selectResource(r.id));

      // name + meta
      const nameCell = el("td");
      const line = el("div");
      line.append(el("span", "status-dot s-" + r.status));
      line.append(el("span", "res-name", r.name));
      if (r.source === "live") line.append(document.createTextNode(" "), el("span", "badge b-live", "live"));
      nameCell.append(line, el("div", "res-meta", `${r.type} · ${r.sku || r.region}`));
      row.append(nameCell);

      row.append(el("td")).lastChild.append(el("span", "badge b-provider", r.provider));

      for (const metric of ["cpu_utilization", "memory_utilization"]) {
        const cell = el("td");
        const value = r.metrics[metric];
        if (value === undefined) {
          cell.append(el("span", "res-meta", "-"));
        } else {
          const bar = el("div", "bar " + utilClass(value));
          const fill = el("span");
          fill.style.width = Math.min(100, Math.max(1.5, value)) + "%";
          bar.append(fill);
          cell.append(bar, el("div", "res-meta", fmtNum(value, 0) + "%"));
        }
        row.append(cell);
      }

      const latency = r.metrics.latency_p95_ms;
      row.append(el("td", "num", latency === undefined ? "-" : fmtNum(latency, 0)));

      const err = r.metrics.error_rate;
      const errCell = el("td", "num", err === undefined ? "-" : (err * 100).toFixed(2) + "%");
      if (err >= 0.05) errCell.style.color = "var(--crit)";
      else if (err >= 0.02) errCell.style.color = "var(--warn)";
      row.append(errCell);

      const restarts = r.metrics.restart_count;
      const rsCell = el("td", "num", restarts === undefined ? "-" : fmtNum(restarts, 0));
      if (restarts >= 3) rsCell.style.color = "var(--crit)";
      row.append(rsCell);

      row.append(el("td", "num", r.monthly_cost ? fmtMoney(r.monthly_cost) : "-"));

      const scoreCell = el("td", "num", r.status === "not_monitored" ? "n/a" : String(r.score));
      scoreCell.style.color =
        r.score >= 85 ? "var(--ok)" : r.score >= 60 ? "var(--warn)" : "var(--crit)";
      if (r.status === "not_monitored") scoreCell.style.color = "var(--dim)";
      scoreCell.title = (r.reasons || []).join("; ");
      row.append(scoreCell);

      body.append(row);
    }
  }

  // ---------------------------------------------------------------- alerts
  function renderAlerts(d) {
    const host = $("alert-body");
    host.replaceChildren();
    const alerts = d.alerts.firing || [];
    $("alert-count").textContent = alerts.length
      ? `${d.alerts.summary.critical} critical · ${d.alerts.summary.warning} warning`
      : "all clear";

    if (!alerts.length) {
      host.append(el("p", "empty", "No alerts firing. Launch an incident above to see the pipeline work."));
      return;
    }

    for (const a of alerts) {
      const item = el("div", "item " + severityClass(a.severity));
      const top = el("div", "top");
      top.append(el("span", "badge b-" + a.severity, a.severity), el("span", "title", a.rule));
      top.append(el("span", "when", ago(a.started_at || a.last_seen) + " ago"));
      item.append(top, el("div", "desc", a.summary));
      item.append(el("div", "evidence", `${a.metric} = ${fmtNum(a.value, 2)} (threshold ${a.threshold}) · ${a.runbook || "no runbook"}`));

      const ack = el("button", null, a.acked_at ? "acknowledged" : "acknowledge");
      ack.disabled = Boolean(a.acked_at);
      ack.style.marginTop = "7px";
      ack.addEventListener("click", async (event) => {
        event.stopPropagation();
        ack.disabled = true;
        try {
          await api(`/api/v1/alerts/${encodeURIComponent(a.fingerprint)}/acknowledge`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ acknowledged_by: "dashboard" }),
          });
          ack.textContent = "acknowledged";
        } catch (err) {
          ack.disabled = false;
          ack.textContent = "retry";
        }
      });
      item.append(ack);
      host.append(item);
    }
  }

  // ------------------------------------------------------------- anomalies
  function renderAnomalies(d) {
    const host = $("anom-body");
    host.replaceChildren();
    const rows = d.anomalies || [];
    $("anom-count").textContent = `${rows.length} in the last hour`;
    if (!rows.length) {
      host.append(el("p", "empty", "No statistical anomalies in the last hour."));
      return;
    }
    for (const a of rows.slice(0, 25)) {
      const item = el("div", "item " + severityClass(a.severity));
      const top = el("div", "top");
      top.append(el("span", "badge b-" + a.severity, a.severity));
      top.append(el("span", "title", a.metric));
      top.append(el("span", "when", ago(a.ts) + " ago"));
      item.append(top);
      item.append(el("div", "desc", a.resource_id));
      item.append(
        el(
          "div",
          "evidence",
          `${fmtNum(a.value, 2)} vs baseline ${fmtNum(a.baseline, 2)} · ${a.direction} · score ${a.score} · ${a.method}`
        )
      );
      host.append(item);
    }
  }

  // ------------------------------------------------------- recommendations
  function renderRecommendations(d) {
    const host = $("rec-body");
    host.replaceChildren();
    const recs = d.recommendations.top || [];
    const s = d.recommendations.summary;
    $("rec-count").textContent = `${s.total} findings · ${fmtMoney(s.monthly_saving)}/mo identified`;

    if (!recs.length) {
      host.append(el("p", "empty", "No recommendations."));
      return;
    }
    for (const r of recs) {
      const item = el("div", "item " + severityClass(r.severity));
      const top = el("div", "top");
      top.append(el("span", "badge b-" + r.severity, r.severity));
      top.append(el("span", "title", r.title));
      if (r.monthly_saving > 0) {
        top.append(el("span", "when saving", fmtMoney2(r.monthly_saving) + "/mo"));
      } else {
        top.append(el("span", "when", r.category.replace(/_/g, " ")));
      }
      item.append(top);
      item.append(el("div", "desc", r.rationale));
      const action = el("div", "action");
      action.append(el("b", null, "Action: "), document.createTextNode(r.action));
      item.append(action);
      item.append(
        el("div", "evidence", `${r.resource_id} · ${r.category} · confidence ${r.confidence} · effort ${r.effort} · risk ${r.risk}`)
      );
      host.append(item);
    }
  }

  // ------------------------------------------------------------------ cost
  function renderCost(d) {
    const host = $("cost-body");
    host.replaceChildren();
    $("cost-total").textContent = `${fmtMoney(d.cost.total_monthly)}/mo · ${fmtMoney(d.cost.total_daily)}/day`;

    const groups = [
      ["By provider", d.cost.by_provider],
      ["By resource type", d.cost.by_type],
      ["By environment", d.cost.by_environment],
    ];
    for (const [title, mapping] of groups) {
      const entries = Object.entries(mapping || {}).filter(([, v]) => v > 0);
      if (!entries.length) continue;
      const max = Math.max(...entries.map(([, v]) => v));
      host.append(el("div", "label", title));
      const wrap = el("div");
      wrap.style.margin = "6px 0 14px";
      for (const [key, value] of entries.slice(0, 7)) {
        const row = el("div", "cost-row");
        row.append(el("span", "k", key.replace(/_/g, " ")));
        const bar = el("div", "bar");
        bar.style.flex = "1";
        const fill = el("span");
        fill.style.width = Math.max(2, (value / max) * 100) + "%";
        fill.style.background = "var(--accent)";
        bar.append(fill);
        row.append(bar, el("span", "v", fmtMoney(value)));
        wrap.append(row);
      }
      host.append(wrap);
    }
  }

  // ------------------------------------------------------------------ logs
  function renderLogs(d) {
    const host = $("log-body");
    host.replaceChildren();
    const counts = d.log_counts || {};
    $("log-count").textContent = Object.entries(counts)
      .map(([k, v]) => `${k.toLowerCase()} ${v}`)
      .join(" · ") || "no logs";

    let rows = d.logs || [];
    if (state.logLevel) {
      rows = rows.filter((r) =>
        state.logLevel === "WARN"
          ? r.level === "WARN" || r.level === "WARNING"
          : r.level === state.logLevel
      );
    }
    if (!rows.length) {
      host.append(el("p", "empty", "No log records match this filter."));
      return;
    }
    for (const line of rows.slice(0, 60)) {
      const row = el("div", "log-line");
      row.append(el("span", "log-ts", clockOf(line.ts)));
      row.append(el("span", "log-lvl lvl-" + line.level, line.level));
      row.append(el("span", "log-src", line.service || line.resource_id));
      row.append(el("span", "log-msg", line.message));
      row.title = JSON.stringify(line.context || {});
      host.append(row);
    }
  }

  // ------------------------------------------------------------- incidents
  function renderIncidents(d) {
    const host = $("incident-list");
    host.replaceChildren();
    const active = d.incidents || [];
    $("incident-count").textContent = active.length
      ? `${active.length} active`
      : "no active incidents";
    for (const inc of active) {
      const box = el("div", "incident");
      box.append(el("span", "label", (inc.params && inc.params.label) || inc.scenario));
      box.append(el("span", "target", inc.resource_id));
      box.append(el("span", "remaining", inc.remaining_seconds + "s remaining"));
      const stop = el("button", "danger", "stop");
      stop.addEventListener("click", async () => {
        stop.disabled = true;
        try {
          await api("/api/v1/incidents/" + encodeURIComponent(inc.id), { method: "DELETE" });
          refresh();
        } catch (err) {
          stop.disabled = false;
        }
      });
      box.append(stop);
      host.append(box);
    }
  }

  // ----------------------------------------------------------------- chart
  function drawChart(points, metric) {
    const host = $("chart-host");
    host.replaceChildren();
    if (!points || points.length < 2) {
      host.append(el("p", "chart-empty", "Not enough history yet for this series."));
      $("chart-now").textContent = "";
      return;
    }

    const W = 1000, H = 150, PAD_L = 46, PAD_R = 10, PAD_T = 12, PAD_B = 20;
    const values = points.map((p) => p.value);
    const times = points.map((p) => p.ts);
    let lo = Math.min(...values), hi = Math.max(...values);
    if (hi - lo < 1e-9) { hi = lo + (Math.abs(lo) || 1) * 0.1 + 1; }
    const pad = (hi - lo) * 0.12;
    lo = metric === "error_rate" || metric === "availability" ? Math.max(0, lo - pad) : Math.max(0, lo - pad);
    hi = hi + pad;

    const t0 = times[0], t1 = times[times.length - 1] || t0 + 1;
    const x = (t) => PAD_L + ((t - t0) / Math.max(t1 - t0, 1)) * (W - PAD_L - PAD_R);
    const y = (v) => PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B);

    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "chart");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${metric} over time`);

    // gridlines + y labels
    for (let i = 0; i <= 3; i++) {
      const value = lo + ((hi - lo) * i) / 3;
      const yy = y(value);
      const gl = document.createElementNS(NS, "line");
      gl.setAttribute("x1", PAD_L); gl.setAttribute("x2", W - PAD_R);
      gl.setAttribute("y1", yy); gl.setAttribute("y2", yy);
      gl.setAttribute("stroke", "rgba(255,255,255,0.07)");
      svg.append(gl);
      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", PAD_L - 6); label.setAttribute("y", yy + 3.5);
      label.setAttribute("text-anchor", "end");
      label.setAttribute("fill", "#6e7681");
      label.setAttribute("font-size", "10");
      label.setAttribute("font-family", "ui-monospace, monospace");
      label.textContent = value >= 100 ? value.toFixed(0) : value.toFixed(value < 1 ? 3 : 1);
      svg.append(label);
    }

    const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");

    const area = document.createElementNS(NS, "path");
    area.setAttribute("d", `${line} L${x(t1).toFixed(1)},${H - PAD_B} L${x(t0).toFixed(1)},${H - PAD_B} Z`);
    area.setAttribute("fill", "rgba(88,166,255,0.13)");
    svg.append(area);

    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", line);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#58a6ff");
    path.setAttribute("stroke-width", "1.6");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(path);

    for (const [t, anchor] of [[t0, "start"], [t1, "end"]]) {
      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", anchor === "start" ? PAD_L : W - PAD_R);
      label.setAttribute("y", H - 6);
      label.setAttribute("text-anchor", anchor);
      label.setAttribute("fill", "#6e7681");
      label.setAttribute("font-size", "10");
      label.setAttribute("font-family", "ui-monospace, monospace");
      label.textContent = clockOf(t);
      svg.append(label);
    }

    host.append(svg);
    const last = values[values.length - 1];
    $("chart-now").textContent = metric === "error_rate" ? (last * 100).toFixed(2) + "%" : fmtNum(last, 2);
  }

  async function loadChart() {
    if (!state.selected) return;
    const metric = $("chart-metric").value;
    const minutes = $("chart-range").value;
    try {
      const data = await api(
        `/api/v1/metrics/series?resource_id=${encodeURIComponent(state.selected)}&metric=${encodeURIComponent(metric)}&minutes=${minutes}`
      );
      drawChart(data.points, metric);
    } catch (err) {
      drawChart([], metric);
    }
  }

  function selectResource(id) {
    state.selected = id;
    const resource = state.resources.find((r) => r.id === id);
    $("chart-title").textContent = resource ? `${resource.name} (${resource.id})` : id;
    loadChart();
    renderResourceSelection();
  }

  function renderResourceSelection() {
    for (const row of document.querySelectorAll("#res-body tr")) row.classList.remove("selected");
  }

  // ------------------------------------------------------------- bootstrap
  async function loadScenarios() {
    const data = await api("/api/v1/incidents/scenarios");
    state.scenarios = data.scenarios;
    const select = $("sc-scenario");
    select.replaceChildren();
    for (const s of data.scenarios) {
      const option = el("option", null, s.label);
      option.value = s.id;
      option.title = s.description;
      select.append(option);
    }
    select.addEventListener("change", () => {
      const chosen = state.scenarios.find((s) => s.id === select.value);
      if (chosen) $("sc-duration").value = chosen.default_duration_seconds;
    });
    const first = data.scenarios[0];
    if (first) $("sc-duration").value = first.default_duration_seconds;
  }

  function fillTargets(resources) {
    const select = $("sc-target");
    if (select.dataset.filled === String(resources.length)) return;
    const previous = select.value;
    select.replaceChildren();
    const ordered = [...resources].sort((a, b) =>
      (a.source === "live" ? 0 : 1) - (b.source === "live" ? 0 : 1) || a.name.localeCompare(b.name)
    );
    for (const r of ordered) {
      const option = el("option", null, `${r.name}${r.source === "live" ? "  [live container]" : ""}`);
      option.value = r.id;
      select.append(option);
    }
    select.dataset.filled = String(resources.length);
    if (previous) select.value = previous;
  }

  async function launchIncident() {
    const button = $("sc-launch");
    const msg = $("sc-msg");
    button.disabled = true;
    msg.textContent = "launching...";
    try {
      const incident = await api("/api/v1/incidents", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          scenario: $("sc-scenario").value,
          resource_id: $("sc-target").value,
          duration_seconds: Number($("sc-duration").value) || 180,
        }),
      });
      const injected = incident.params && incident.params.chaos_injected;
      msg.textContent =
        incident.id + (injected ? " · chaos injected into the real container" : " · simulated effect applied");
      await refresh();
    } catch (err) {
      msg.textContent = "failed: " + err.message;
    } finally {
      button.disabled = false;
    }
  }

  async function stopAll() {
    const button = $("sc-stop");
    button.disabled = true;
    try {
      await api("/api/v1/incidents", { method: "DELETE" });
      $("sc-msg").textContent = "all incidents cancelled";
      await refresh();
    } catch (err) {
      $("sc-msg").textContent = "failed: " + err.message;
    } finally {
      button.disabled = false;
    }
  }

  // ----------------------------------------------------------------- poll
  async function refresh() {
    try {
      const d = await api("/api/v1/overview");
      state.failures = 0;
      state.resources = d.resources;

      const pill = $("fleet-pill");
      pill.className = "pill " + d.fleet.status;
      $("fleet-status").textContent = d.fleet.status;
      $("tick").textContent = "updated " + clockOf(d.ts);

      renderKpis(d);
      renderIncidents(d);
      fillTargets(d.resources);
      renderResources(d);
      renderAlerts(d);
      renderAnomalies(d);
      renderRecommendations(d);
      renderCost(d);
      renderLogs(d);
      if (state.selected) loadChart();
    } catch (err) {
      state.failures += 1;
      $("fleet-status").textContent = "connection lost (" + state.failures + ")";
      $("fleet-pill").className = "pill unhealthy";
    }
  }

  async function init() {
    try {
      const system = await api("/api/v1/system");
      $("build").textContent = `v${system.version} · ${system.environment}`;
    } catch (err) {
      $("build").textContent = "offline";
    }
    await loadScenarios().catch(() => {});

    $("sc-launch").addEventListener("click", launchIncident);
    $("sc-stop").addEventListener("click", stopAll);
    $("chart-metric").addEventListener("change", loadChart);
    $("chart-range").addEventListener("change", loadChart);

    for (const button of $("log-filters").querySelectorAll("button")) {
      button.addEventListener("click", () => {
        state.logLevel = button.dataset.level;
        for (const other of $("log-filters").querySelectorAll("button")) other.classList.remove("on");
        button.classList.add("on");
        refresh();
      });
    }

    await refresh();
    setInterval(refresh, POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
