const summaryContainer = document.getElementById("summary-cards");
const generatedAt = document.getElementById("generated-at");
const nodeMemoryRow = document.getElementById("node-memory-row");
const modelsBody = document.getElementById("models-body");
const routingRequestsBody = document.getElementById("routing-requests-body");
const routingRequestsPrev = document.getElementById("routing-requests-prev");
const routingRequestsNext = document.getElementById("routing-requests-next");
const routingRequestsPage = document.getElementById("routing-requests-page");
const lastFallback = document.getElementById("last-fallback");
const summaryBreakdowns = document.getElementById("summary-breakdowns");
const trendCards = document.getElementById("trend-cards");
const requestSeries = document.getElementById("request-series");
const summaryFilterNode = document.getElementById("summary-filter-node");

const filterNode = document.getElementById("filter-node");
const filterModel = document.getElementById("filter-model");
const filterPathKind = document.getElementById("filter-path-kind");
const clearFilters = document.getElementById("clear-filters");

let latestSnapshot = null;
const paginationState = {
  routingRequests: { page: 1, pageSize: 15 },
};

function formatTime(epochSeconds) {
  if (!epochSeconds) return "-";
  return new Date(epochSeconds * 1000).toLocaleTimeString("de-DE");
}

function signalChips(signals) {
  if (!signals || typeof signals !== "object") {
    return '<span class="breakdown-empty">-</span>';
  }

  const entries = [];
  if (typeof signals.prompt_length === "string") {
    entries.push(`prompt=${signals.prompt_length}`);
  }
  if (typeof signals.history_length === "number") {
    entries.push(`msgs=${signals.history_length}`);
  }
  if (signals.has_tools) {
    entries.push("tools");
  }
  if (signals.code_intent) {
    entries.push("code");
  }
  if (signals.needs_long_context) {
    entries.push("long-context");
  }
  if (signals.needs_tool_capable_model) {
    entries.push("tool-capable");
  }

  if (!entries.length) {
    return '<span class="breakdown-empty">-</span>';
  }

  return `<div class="signal-chip-list">${entries.map((item) => `<span class="signal-chip">${escapeHtml(item)}</span>`).join("")}</div>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function pill(value) {
  const normalized = String(value || "unknown");
  return `<span class="pill ${escapeHtml(normalized)}">${escapeHtml(normalized)}</span>`;
}

function requestStatus(item) {
  const status = String(item?.status || "unknown");
  const statusCode = Number(item?.status_code || 0);

  if (status === "wake_failed") return "wake_failed";
  if (status === "network_error") return "network_error";
  if (status === "streaming") return "streaming";
  if (status === "ok") return "ok";
  if (statusCode >= 400 && statusCode < 500) return "bad_request";
  if (statusCode >= 500) return "error";
  return status;
}

function extractToolDomain(item, decision) {
  const raw = String(
    item?.route_rule
    || item?.route_reason
    || decision?.rule_name
    || decision?.reason
    || "",
  );

  if (!raw) return "-";
  if (raw === "tool-routing" || raw === "tool-calling-default") return "default";

  const followupMatch = raw.match(/^tool-([a-zA-Z0-9]+)-followup$/);
  if (followupMatch) {
    return followupMatch[1];
  }

  const directMatch = raw.match(/^tool-([a-zA-Z0-9]+)$/);
  if (directMatch) {
    return directMatch[1];
  }

  const legacyMatch = raw.match(/^tool-calling-([a-zA-Z0-9_-]+)$/);
  if (legacyMatch) {
    return legacyMatch[1];
  }

  return "-";
}

function currentFilters() {
  return {
    node: filterNode.value,
    model: filterModel.value,
    pathKind: filterPathKind.value,
  };
}

function currentSummaryFilter() {
  return {
    node: summaryFilterNode.value,
  };
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((left, right) => left.localeCompare(right));
}

function modelNodesMap(snapshot) {
  const mapping = new Map();
  for (const model of snapshot.models || []) {
    const nodes = Array.isArray(model.nodes) && model.nodes.length
      ? model.nodes
      : (model.node ? [model.node] : []);
    mapping.set(model.id, nodes);
  }
  return mapping;
}

function populateFilters(snapshot) {
  const previous = currentFilters();
  const previousSummary = currentSummaryFilter();
  const nodes = uniqueSorted((snapshot.nodes || []).map((node) => node.name));
  const models = uniqueSorted((snapshot.models || []).map((model) => model.id));

  summaryFilterNode.innerHTML = '<option value="">Alle Nodes</option>' + nodes.map((node) => (
    `<option value="${escapeHtml(node)}">${escapeHtml(node)}</option>`
  )).join("");
  filterNode.innerHTML = '<option value="">Alle Nodes</option>' + nodes.map((node) => (
    `<option value="${escapeHtml(node)}">${escapeHtml(node)}</option>`
  )).join("");
  filterModel.innerHTML = '<option value="">Alle Modelle</option>' + models.map((model) => (
    `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`
  )).join("");

  summaryFilterNode.value = nodes.includes(previousSummary.node) ? previousSummary.node : "";
  filterNode.value = nodes.includes(previous.node) ? previous.node : "";
  filterModel.value = models.includes(previous.model) ? previous.model : "";
  filterPathKind.value = previous.pathKind;
}

function applyRouteFilters(snapshot) {
  const filters = currentFilters();
  const modelNodeLookup = modelNodesMap(snapshot);

  const requests = (snapshot.requests || []).filter((item) => {
    if (filters.pathKind && item.path_kind !== filters.pathKind) return false;
    if (filters.model) {
      const requested = item.requested_model || "";
      const effective = item.effective_model || "";
      if (requested !== filters.model && effective !== filters.model) return false;
    }
    if (filters.node && item.node !== filters.node) return false;
    return true;
  });

  const decisions = (snapshot.routing?.recent_decisions || []).filter((item) => {
    if (filters.pathKind && item.path_kind !== filters.pathKind) return false;
    if (filters.model) {
      const requested = item.requested_model || "";
      const target = item.target_model || "";
      if (requested !== filters.model && target !== filters.model) return false;
    }
    if (filters.node) {
      const nodesForModel = modelNodeLookup.get(item.target_model || "") || [];
      if (!nodesForModel.includes(filters.node)) return false;
    }
    return true;
  });

  return {
    ...snapshot,
    requests,
    routing: {
      ...(snapshot.routing || {}),
      recent_decisions: decisions,
      last_fallback: (
        (() => {
          const fallback = snapshot.routing?.last_fallback;
          if (!fallback) return null;
          if (filters.pathKind && fallback.path_kind !== filters.pathKind) return null;
          if (filters.model) {
            const requested = fallback.requested_model || "";
            const target = fallback.target_model || "";
            if (requested !== filters.model && target !== filters.model) return null;
          }
          if (filters.node) {
            const nodesForModel = modelNodeLookup.get(fallback.target_model || "") || [];
            if (!nodesForModel.includes(filters.node)) return null;
          }
          return fallback;
        })()
      ),
    },
  };
}

function applySummaryFilter(snapshot) {
  const filter = currentSummaryFilter();
  if (!filter.node) {
    return snapshot;
  }

  const models = (snapshot.models || []).filter((model) => {
    const nodes = Array.isArray(model.nodes) && model.nodes.length
      ? model.nodes
      : (model.node ? [model.node] : []);
    return nodes.includes(filter.node);
  });

  const requests = (snapshot.requests || []).filter((item) => item.node === filter.node);

  const fallback = snapshot.routing?.last_fallback;
  const modelNodeLookup = modelNodesMap(snapshot);
  const filteredFallback = (() => {
    if (!fallback) return null;
    const nodesForModel = modelNodeLookup.get(fallback.target_model || "") || [];
    return nodesForModel.includes(filter.node) ? fallback : null;
  })();

  return {
    ...snapshot,
    models,
    requests,
    routing: {
      ...(snapshot.routing || {}),
      last_fallback: filteredFallback,
    },
  };
}

function countPaths(requests) {
  const counts = { direct: 0, "semantic-router": 0 };
  for (const item of requests) {
    const pathKind = item.path_kind || "direct";
    if (!(pathKind in counts)) counts[pathKind] = 0;
    counts[pathKind] += 1;
  }
  return counts;
}

function countStatuses(requests) {
  const counts = { ok: 0, error: 0, wake_failed: 0 };
  for (const item of requests) {
    const status = requestStatus(item);
    if (status === "wake_failed") counts.wake_failed += 1;
    else if (status === "error" || status === "network_error" || status === "bad_request") counts.error += 1;
    else if (status === "ok" || status === "streaming") counts.ok += 1;
  }
  return counts;
}

function buildSeries(requests, minutes = 12) {
  const now = Math.floor(Date.now() / 1000);
  const start = now - (minutes - 1) * 60;
  const buckets = Array.from({ length: minutes }, (_, index) => ({
    timestamp: start + index * 60,
    direct: 0,
    "semantic-router": 0,
    wake_failed: 0,
  }));

  for (const item of requests) {
    const timestamp = Number(item.timestamp || 0);
    if (!timestamp || timestamp < start) continue;
    const bucketIndex = Math.min(Math.floor((timestamp - start) / 60), minutes - 1);
    const bucket = buckets[bucketIndex];
    const pathKind = item.path_kind === "semantic-router" ? "semantic-router" : "direct";
    bucket[pathKind] += 1;
    if (item.status === "wake_failed") bucket.wake_failed += 1;
  }

  return buckets;
}

function sparkline(values, className) {
  const width = 280;
  const height = 58;
  const maxValue = Math.max(...values, 1);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values.map((value, index) => {
    const x = index * step;
    const y = height - (value / maxValue) * (height - 6) - 3;
    return `${x},${y}`;
  }).join(" ");
  return `
    <svg viewBox="0 0 ${width} ${height}" class="sparkline ${className}">
      <polyline points="${points}" />
    </svg>
  `;
}

function renderSummary(snapshot) {
  const requests = snapshot.requests || [];
  const statusCounts = countStatuses(requests);
  const totalRequests = requests.length;
  const cards = [
    ["Models", (snapshot.models || []).length],
    ["Awake", (snapshot.models || []).filter((item) => item.state === "awake").length],
    ["Sleeping", (snapshot.models || []).filter((item) => item.state === "sleeping").length],
    ["Wake-Fehler", statusCounts.wake_failed ?? 0],
    ["Requests", totalRequests],
    ["Fehler", statusCounts.error ?? 0],
  ];
  summaryContainer.innerHTML = cards.map(([label, value]) => (
    `<article class="card"><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong></article>`
  )).join("");
}

function topCounts(items, keyFn, limit = 6) {
  const counts = new Map();
  for (const item of items) {
    const key = keyFn(item);
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, limit);
}

function paginateItems(items, state) {
  const totalItems = items.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / state.pageSize));
  const page = Math.min(Math.max(state.page, 1), totalPages);
  state.page = page;
  const start = (page - 1) * state.pageSize;
  return {
    page,
    totalItems,
    totalPages,
    items: items.slice(start, start + state.pageSize),
  };
}

function renderPaginationControls(config, pageInfo) {
  const { prevButton, nextButton, pageSelect } = config;
  if (!prevButton || !nextButton || !pageSelect) {
    return;
  }

  const options = Array.from({ length: pageInfo.totalPages }, (_, index) => {
    const page = index + 1;
    return `<option value="${page}">Seite ${page}</option>`;
  }).join("");
  pageSelect.innerHTML = options;
  pageSelect.value = String(pageInfo.page);
  pageSelect.disabled = pageInfo.totalPages <= 1;
  prevButton.disabled = pageInfo.page <= 1;
  nextButton.disabled = pageInfo.page >= pageInfo.totalPages;
}

function renderBreakdownCard(title, entries) {
  if (!entries.length) {
    return `
      <article class="breakdown-card">
        <h3>${escapeHtml(title)}</h3>
        <div class="breakdown-empty">Noch keine Daten</div>
      </article>
    `;
  }

  return `
    <article class="breakdown-card">
      <h3>${escapeHtml(title)}</h3>
      <div class="breakdown-list">
        ${entries.map(([label, value]) => `
          <div class="breakdown-item">
            <span>${escapeHtml(label)}</span>
            <span>${escapeHtml(value)}</span>
          </div>
        `).join("")}
      </div>
    </article>
  `;
}

function renderSummaryBreakdowns(snapshot) {
  if (!summaryBreakdowns) {
    return;
  }

  const requests = snapshot.requests || [];
  const requested = topCounts(requests, (item) => item.requested_model || "-");
  const effective = topCounts(requests, (item) => item.effective_model || "-");
  const reasons = topCounts(
    requests,
    (item) => item.route_rule || item.route_reason || (item.path_kind === "direct" ? "direct" : "default-model"),
  );

  summaryBreakdowns.innerHTML = [
    renderBreakdownCard("Requested by model", requested),
    renderBreakdownCard("Effective by model", effective),
    renderBreakdownCard("Route reasons", reasons),
  ].join("");
}

function renderNodeMemory(snapshot) {
  if (!nodeMemoryRow) {
    return;
  }
  const rows = snapshot.node_memory || [];
  nodeMemoryRow.innerHTML = rows.map((item) => {
    if (item.error) {
      return `
        <article class="memory-card degraded">
          <strong>${escapeHtml(item.name || "-")}</strong>
          <span>${escapeHtml(item.error)}</span>
        </article>
      `;
    }
    return `
      <article class="memory-card">
        <strong>${escapeHtml(item.name || "-")}</strong>
        <span>Pod-RAM ${escapeHtml(item.pod_ram_gib)} GiB</span>
        <span>Node-Cache ${escapeHtml(item.node_cache_gib)} GiB</span>
        <span>Verfuegbar ${escapeHtml(item.available_gib)} GiB</span>
      </article>
    `;
  }).join("");
}

function renderTrendCards(snapshot) {
  if (!trendCards) {
    return;
  }
  trendCards.innerHTML = "";
}

function renderSeries(snapshot) {
  const series = buildSeries(snapshot.requests || []);
  const specs = [
    ["direct", "Direct Requests"],
    ["semantic-router", "Semantic Router"],
    ["wake_failed", "Wake-Fehler"],
  ];
  requestSeries.innerHTML = specs.map(([key, label]) => {
    const values = series.map((item) => item[key] || 0);
    return `
      <div class="series-card">
        <h3>${escapeHtml(label)}</h3>
        ${sparkline(values, key.replace("_", "-"))}
        <div class="sparkline-label">
          <span>${escapeHtml(formatTime(series[0]?.timestamp))}</span>
          <span>${escapeHtml(formatTime(series.at(-1)?.timestamp))}</span>
        </div>
      </div>
    `;
  }).join("");
}

function renderComponents(snapshot) {
  const components = new Map((snapshot.components || []).map((item) => [item.id, item]));
  for (const node of document.querySelectorAll("[data-component]")) {
    const component = components.get(node.dataset.component);
    const attachmentTrack = node.closest(".attachment-track");
    if (!component) {
      node.innerHTML = "";
      node.className = "node optional";
      if (attachmentTrack) {
        attachmentTrack.style.display = "none";
      }
      continue;
    }
    if (attachmentTrack) {
      attachmentTrack.style.display = "";
    }
    node.className = `node ${component.status || "down"}`;
    node.innerHTML = `
      <span class="node-label">${escapeHtml(component.label)}</span>
      <span class="node-detail">${escapeHtml(component.detail || component.status || "-")}</span>
    `;
  }
}

function renderFallback(snapshot) {
  const fallback = snapshot.routing?.last_fallback;
  lastFallback.textContent = fallback ? JSON.stringify(fallback, null, 2) : "Noch kein Fallback erfasst.";
}

function renderModels(snapshot) {
  modelsBody.innerHTML = (snapshot.models || []).map((model) => `
    <tr>
      <td>${escapeHtml(model.id)}</td>
      <td>${pill(model.state)}</td>
      <td>${escapeHtml((model.nodes || []).join(", ") || model.node || "-")}</td>
      <td>${escapeHtml(model.replicas ?? 0)}</td>
      <td>${escapeHtml(model.engine_inflight ?? 0)}</td>
    </tr>
  `).join("");
}

function findTargetForRequest(request, decisions) {
  if ((request.path_kind || "direct") !== "semantic-router") {
    return request.effective_model || request.requested_model || "-";
  }

  const requestTime = Number(request.timestamp || 0);
  const match = (decisions || []).find((decision) => {
    if ((decision.path_kind || "direct") !== "semantic-router") return false;
    if ((decision.requested_model || "") !== (request.requested_model || "")) return false;
    const decisionTime = Number(decision.timestamp || 0);
    return Math.abs(decisionTime - requestTime) <= 15;
  });

  return (
    request.target_model
    || match?.target_model
    || request.effective_model
    || request.requested_model
    || "-"
  );
}

function findDecisionForRequest(request, decisions) {
  if ((request.path_kind || "direct") !== "semantic-router") {
    return null;
  }

  const requestTime = Number(request.timestamp || 0);
  return (decisions || []).find((decision) => {
    if ((decision.path_kind || "direct") !== "semantic-router") return false;
    if ((decision.requested_model || "") !== (request.requested_model || "")) return false;
    const decisionTime = Number(decision.timestamp || 0);
    return Math.abs(decisionTime - requestTime) <= 15;
  }) || null;
}

function renderRoutingRequests(snapshot) {
  if (!routingRequestsBody) {
    return;
  }
  const decisions = snapshot.routing?.recent_decisions || [];
  const items = snapshot.requests || [];
  const pageInfo = paginateItems(items, paginationState.routingRequests);
  renderPaginationControls(
    {
      prevButton: routingRequestsPrev,
      nextButton: routingRequestsNext,
      pageSelect: routingRequestsPage,
    },
    pageInfo,
  );
  if (!items.length) {
    routingRequestsBody.innerHTML = `
      <tr>
        <td colspan="9"><span class="breakdown-empty">Noch keine Request-Daten vorhanden</span></td>
      </tr>
    `;
    return;
  }
  routingRequestsBody.innerHTML = pageInfo.items.map((item) => {
    const decision = findDecisionForRequest(item, decisions);
    const status = requestStatus(item);
    const rule =
      item.route_rule
      || item.route_reason
      || decision?.rule_name
      || decision?.reason
      || ((item.path_kind || "direct") === "semantic-router" ? "default-model" : "-");
    const toolDomain = extractToolDomain(item, decision);
    return `
      <tr>
        <td>${escapeHtml(formatTime(item.timestamp))}</td>
        <td>${escapeHtml(item.requested_model || "-")}</td>
        <td>${pill(item.path_kind)}</td>
        <td>${escapeHtml(rule)}</td>
        <td>${escapeHtml(toolDomain)}</td>
        <td>${escapeHtml(findTargetForRequest(item, decisions))}</td>
        <td>${escapeHtml(item.effective_model || "-")}</td>
        <td>${escapeHtml(item.node || "-")}</td>
        <td>${pill(status)}</td>
      </tr>
    `;
  }).join("");
}

function render(snapshot) {
  latestSnapshot = snapshot;
  populateFilters(snapshot);
  const routeFiltered = applyRouteFilters(snapshot);
  const summaryFiltered = applySummaryFilter(snapshot);
  generatedAt.textContent = `Stand: ${new Date(snapshot.generated_at * 1000).toLocaleString("de-DE")}`;
  renderNodeMemory(snapshot);
  renderSummary(summaryFiltered);
  renderSummaryBreakdowns(summaryFiltered);
  renderTrendCards(summaryFiltered);
  renderSeries(summaryFiltered);
  renderComponents(snapshot);
  renderFallback(summaryFiltered);
  renderModels(snapshot);
  renderRoutingRequests(routeFiltered);
}

async function load() {
  try {
    const response = await fetch("/api/overview", { cache: "no-store" });
    if (!response.ok) {
      generatedAt.textContent = `Fehler beim Laden: HTTP ${response.status}`;
      return;
    }
    const snapshot = await response.json();
    render(snapshot);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    generatedAt.textContent = `Fehler beim Aktualisieren: ${message}`;
  }
}

for (const element of [filterNode, filterModel, filterPathKind, summaryFilterNode]) {
  element.addEventListener("change", () => {
    paginationState.routingRequests.page = 1;
    if (latestSnapshot) {
      render(latestSnapshot);
    }
  });
}

clearFilters.addEventListener("click", () => {
  filterNode.value = "";
  filterModel.value = "";
  filterPathKind.value = "";
  paginationState.routingRequests.page = 1;
  if (latestSnapshot) {
    render(latestSnapshot);
  }
});

routingRequestsPrev.addEventListener("click", () => {
  paginationState.routingRequests.page = Math.max(1, paginationState.routingRequests.page - 1);
  if (latestSnapshot) {
    render(latestSnapshot);
  }
});

routingRequestsNext.addEventListener("click", () => {
  paginationState.routingRequests.page += 1;
  if (latestSnapshot) {
    render(latestSnapshot);
  }
});

routingRequestsPage.addEventListener("change", () => {
  paginationState.routingRequests.page = Number(routingRequestsPage.value || "1");
  if (latestSnapshot) {
    render(latestSnapshot);
  }
});

document.getElementById("refresh").addEventListener("click", () => void load());
void load();
window.setInterval(
  () => void load(),
  Number(window.GPU_HUB_REFRESH_INTERVAL_SECONDS || 5) * 1000,
);
