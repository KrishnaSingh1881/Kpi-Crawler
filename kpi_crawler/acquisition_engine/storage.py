"""Run-scoped, isolated storage for Program 1 acquisition runs.

Ensures every run is completely self-contained and reproducible under:
.data/
└── runs/
    └── <safe-site-id>/
        └── <run-id>/
            ├── raw/
            │   ├── html/
            │   ├── json/
            │   ├── pdf/
            │   └── other/
            ├── evidence/
            │   ├── attempts.jsonl
            │   └── adaptive_decisions.jsonl
            ├── manifest.json
            └── program2/
                ├── artifacts.json
                ├── metadata.json
                └── provenance.json
"""

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from ..errors import StorageError
from .contract import (
    CONTRACT_VERSION,
    AdaptiveDecisionRecord,
    ArtifactRecord,
    AttemptRecord,
    RunSummary,
)


def safe_site_id(root_url: str) -> str:
    """Derive a safe, human-readable filesystem identifier from a root URL.

    Replaces colons and special characters with underscores, folds to lowercase,
    and strips leading/trailing dots/underscores.
    """
    parts = urlsplit(root_url)
    netloc = parts.netloc.lower()
    if not netloc:
        netloc = parts.path.strip("/").replace("/", "_") or "local"
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", netloc).strip("._")
    return safe or "unknown_site"


def classify_content_type(content_type: str | None, url: str = "") -> str:
    """Classify an artifact into one of the four required raw categories:

    'html', 'json', 'pdf', or 'other'.
    """
    ct = (content_type or "").lower().split(";")[0].strip()
    url_lower = url.lower().split("?")[0]
    if "html" in ct or url_lower.endswith((".html", ".htm")):
        return "html"
    if "json" in ct or url_lower.endswith(".json"):
        return "json"
    if "pdf" in ct or url_lower.endswith(".pdf"):
        return "pdf"
    return "other"


def _iso(val: datetime | None) -> str | None:
    return val.isoformat() if val is not None else None


_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Acquisition report</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e4e6eb;
    --muted: #8b92a3; --accent: #4da3ff; --ok: #35c47a; --bad: #ef5a5a;
    --warn: #e8b64b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  header { padding: 16px 24px; border-bottom: 1px solid var(--border); }
  header h1 { margin: 0 0 4px; font-size: 18px; }
  header .sub { color: var(--muted); font-size: 12px; word-break: break-all; }
  nav { display: flex; gap: 4px; padding: 0 24px; border-bottom: 1px solid var(--border); }
  nav button {
    background: none; border: none; color: var(--muted); padding: 10px 16px;
    font-size: 13px; cursor: pointer; border-bottom: 2px solid transparent;
  }
  nav button.active { color: var(--text); border-bottom-color: var(--accent); }
  main { padding: 20px 24px; }
  .tab { display: none; }
  .tab.active { display: block; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 16px; min-width: 100px;
  }
  .card .n { font-size: 20px; font-weight: 600; }
  .card .l { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  h2 { font-size: 14px; margin: 24px 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; cursor: pointer; user-select: none; white-space: nowrap; }
  th:hover { color: var(--text); }
  tr.row { cursor: pointer; }
  tr.row:hover { background: rgba(255,255,255,0.03); }
  tr.detail-row > td { background: rgba(255,255,255,0.02); }
  .mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; word-break: break-all; }
  .muted { color: var(--muted); }
  input[type=text] {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 6px 10px; border-radius: 4px; font-size: 13px; width: 280px; margin-bottom: 10px;
  }
  .pill {
    display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
    border: 1px solid var(--border);
  }
  .pill.ok { color: var(--ok); border-color: var(--ok); }
  .pill.bad { color: var(--bad); border-color: var(--bad); }
  .pill.warn { color: var(--warn); border-color: var(--warn); }
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 4px 0; }
  .bar-label { width: 140px; flex: 0 0 auto; font-size: 12px; }
  .bar-track { flex: 1; background: var(--panel); border-radius: 3px; height: 14px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--accent); }
  .bar-count { width: 40px; text-align: right; font-size: 12px; color: var(--muted); }
  .empty { color: var(--muted); font-style: italic; padding: 12px 0; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  iframe.raw-preview { width: 100%; height: 420px; border: 1px solid var(--border); border-radius: 4px; background: #fff; }
  .section { margin-bottom: 28px; }
  .chip {
    display: inline-block; background: var(--panel); border: 1px solid var(--border);
    border-radius: 4px; padding: 2px 8px; font-size: 11px; margin-right: 4px;
  }
</style>
</head>
<body>
<header>
  <h1 id="report-title">Acquisition report</h1>
  <div class="sub" id="report-sub"></div>
</header>
<nav>
  <button data-tab="overview" class="active">Overview</button>
  <button data-tab="sources">Sources</button>
  <button data-tab="artifacts">Artifacts</button>
  <button data-tab="errors">Errors</button>
</nav>
<main>
  <section id="tab-overview" class="tab active"></section>
  <section id="tab-sources" class="tab"></section>
  <section id="tab-artifacts" class="tab"></section>
  <section id="tab-errors" class="tab"></section>
</main>
<script type="application/json" id="report-data">__REPORT_DATA_JSON__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("report-data").textContent);

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtBytes(n) {
    if (n === null || n === undefined) return "";
    var units = ["B", "KB", "MB", "GB"];
    var i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + " " + units[i];
  }
  function fmtDuration(startIso, endIso) {
    if (!startIso || !endIso) return "–";
    var ms = new Date(endIso).getTime() - new Date(startIso).getTime();
    if (isNaN(ms) || ms < 0) return "–";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60), rem = Math.round(s % 60);
    return m + "m " + rem + "s";
  }
  function statusPill(state) {
    var ok = state === "SUCCESS";
    var bad = ["FAILED", "NOT_FOUND", "NETWORK_ERROR", "ACCESS_DENIED", "TIMEOUT",
      "RATE_LIMITED", "BLOCKED", "BROWSER_ERROR", "UNSUPPORTED"].indexOf(state) !== -1;
    var cls = ok ? "ok" : (bad ? "bad" : "warn");
    return '<span class="pill ' + cls + '">' + esc(state) + "</span>";
  }
  var PREVIEWABLE = ["text/html", "text/plain", "application/json", "text/css"];
  function baseContentType(ct) {
    return (ct || "").split(";")[0].trim().toLowerCase();
  }

  // ---------- Overview ----------
  function renderOverview() {
    var s = DATA.summary;
    var el = document.getElementById("tab-overview");
    document.getElementById("report-title").textContent = "Acquisition report — run #" + s.run_id;
    document.getElementById("report-sub").textContent = s.root_source_url || "";

    var cards = [
      ["Status", s.status],
      ["Discovered", s.discovered],
      ["Attempted", s.attempted],
      ["Acquired", s.acquired],
      ["Failed", s.failed],
      ["Retries", s.retries],
      ["Duration", fmtDuration(s.started_at, s.completed_at)],
    ];
    var html = '<div class="cards">' + cards.map(function (c) {
      return '<div class="card"><div class="n">' + esc(c[1]) + '</div><div class="l">' + esc(c[0]) + '</div></div>';
    }).join("") + "</div>";

    html += '<div class="section"><h2>Run</h2><table><tbody>';
    html += rowKV("Run ID", s.run_id);
    html += rowKV("Root source URL", '<span class="mono">' + esc(s.root_source_url) + "</span>");
    html += rowKV("Started at", esc(s.started_at));
    html += rowKV("Completed at", esc(s.completed_at));
    html += rowKV("Rate limited", s.rate_limited);
    html += rowKV("Browser pages", s.browser_pages);
    html += rowKV("Proxy failures", s.proxy_failures);
    html += "</tbody></table></div>";

    html += '<div class="section"><h2>By state</h2>';
    var byState = s.by_state || {};
    var keys = Object.keys(byState);
    if (!keys.length) {
      html += '<div class="empty">No attempts recorded.</div>';
    } else {
      var max = Math.max.apply(null, keys.map(function (k) { return byState[k]; }));
      html += keys.sort().map(function (k) {
        var pct = max > 0 ? (byState[k] / max) * 100 : 0;
        return '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div>' +
          '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div>' +
          '<div class="bar-count">' + byState[k] + "</div></div>";
      }).join("");
    }
    html += "</div>";

    html += '<div class="section"><h2>Adaptive decisions timeline</h2>';
    var decisions = (DATA.decisions || []).slice().sort(function (a, b) {
      return (a.occurred_at || "").localeCompare(b.occurred_at || "");
    });
    if (!decisions.length) {
      html += '<div class="empty">No adaptive decisions were made this run.</div>';
    } else {
      html += "<table><thead><tr><th>Occurred at</th><th>Domain</th><th>Decision</th><th>Reason</th><th>Before → After</th></tr></thead><tbody>";
      html += decisions.map(function (d) {
        return "<tr><td>" + esc(d.occurred_at) + "</td><td>" + esc(d.domain) + "</td><td>" +
          esc(d.decision_type) + "</td><td>" + esc(d.reason) + "</td><td>" +
          esc(d.before_value) + " → " + esc(d.after_value) + "</td></tr>";
      }).join("");
      html += "</tbody></table>";
    }
    html += "</div>";

    el.innerHTML = html;
  }
  function rowKV(k, v) {
    return "<tr><td class=\\"muted\\">" + esc(k) + "</td><td>" + v + "</td></tr>";
  }

  // ---------- Sources ----------
  var sourcesState = { sortKey: "url", sortDir: 1, filter: "" };

  function groupByUrl() {
    var groups = {};
    (DATA.attempts || []).forEach(function (a) {
      (groups[a.url] = groups[a.url] || []).push(a);
    });
    Object.keys(groups).forEach(function (u) {
      groups[u].sort(function (a, b) {
        if (a.retry_number !== b.retry_number) return a.retry_number - b.retry_number;
        return (a.occurred_at || "").localeCompare(b.occurred_at || "");
      });
    });
    return groups;
  }

  function renderSources() {
    var el = document.getElementById("tab-sources");
    var groups = groupByUrl();
    var rows = Object.keys(groups).map(function (url) {
      var g = groups[url];
      var last = g[g.length - 1];
      return {
        url: url, final_result: last.final_result, http_status: last.http_status,
        acquisition_method: last.acquisition_method, retry_count: g.length,
        session_id: last.session_id, attempts: g,
      };
    });

    var html = '<input type="text" id="source-filter" placeholder="Filter by URL…" value="' + esc(sourcesState.filter) + '">';
    var filtered = rows.filter(function (r) {
      return !sourcesState.filter || r.url.toLowerCase().indexOf(sourcesState.filter.toLowerCase()) !== -1;
    });
    var cols = [["url", "URL"], ["final_result", "Result"], ["http_status", "HTTP"],
      ["acquisition_method", "Method"], ["retry_count", "Attempts"], ["session_id", "Session"]];
    filtered.sort(function (a, b) {
      var x = a[sourcesState.sortKey], y = b[sourcesState.sortKey];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * sourcesState.sortDir;
    });

    if (!filtered.length) {
      html += '<div class="empty">No sources match.</div>';
    } else {
      html += "<table><thead><tr>" + cols.map(function (c) {
        var arrow = sourcesState.sortKey === c[0] ? (sourcesState.sortDir === 1 ? " ▲" : " ▼") : "";
        return '<th data-sort="' + c[0] + '">' + c[1] + arrow + "</th>";
      }).join("") + "</tr></thead><tbody>";
      filtered.forEach(function (r, i) {
        html += '<tr class="row" data-idx="' + i + '"><td class="mono">' + esc(r.url) + "</td><td>" +
          statusPill(r.final_result) + "</td><td>" + esc(r.http_status) + "</td><td>" +
          esc(r.acquisition_method) + "</td><td>" + r.retry_count + "</td><td>" + esc(r.session_id) + "</td></tr>";
        html += '<tr class="detail-row" data-detail-for="' + i + '" style="display:none"><td colspan="6"></td></tr>';
      });
      html += "</tbody></table>";
    }
    el.innerHTML = html;

    var filterInput = document.getElementById("source-filter");
    filterInput.addEventListener("input", function () {
      sourcesState.filter = filterInput.value;
      renderSources();
      document.getElementById("source-filter").focus();
    });
    el.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort");
        if (sourcesState.sortKey === key) sourcesState.sortDir *= -1;
        else { sourcesState.sortKey = key; sourcesState.sortDir = 1; }
        renderSources();
      });
    });
    el.querySelectorAll("tr.row").forEach(function (tr) {
      tr.addEventListener("click", function () {
        var idx = tr.getAttribute("data-idx");
        var detail = el.querySelector('tr[data-detail-for="' + idx + '"]');
        var showing = detail.style.display !== "none";
        detail.style.display = showing ? "none" : "";
        if (!showing) {
          var r = filtered[idx];
          var dh = "<table><thead><tr><th>Retry</th><th>Occurred at</th><th>Session</th><th>HTTP</th><th>Error</th></tr></thead><tbody>";
          dh += r.attempts.map(function (a) {
            return "<tr><td>" + a.retry_number + "</td><td>" + esc(a.occurred_at) + "</td><td>" +
              esc(a.session_id) + "</td><td>" + esc(a.http_status) + "</td><td>" + esc(a.error_message) + "</td></tr>";
          }).join("") + "</tbody></table>";
          detail.querySelector("td").innerHTML = dh;
        }
      });
    });
  }

  window.jumpToSource = function (url) {
    showTab("sources");
    sourcesState.filter = url;
    renderSources();
  };

  // ---------- Artifacts ----------
  function renderArtifacts() {
    var el = document.getElementById("tab-artifacts");
    var artifacts = DATA.artifacts || [];
    var bySourceUrl = {};
    artifacts.forEach(function (a) { bySourceUrl[a.source_url] = a; });

    if (!artifacts.length) {
      el.innerHTML = '<div class="empty">No artifacts were acquired this run.</div>';
      return;
    }
    var html = "<table><thead><tr><th>Content type</th><th>Source URL</th><th>Discovered from</th>" +
      "<th>Checksum</th><th>Size</th><th>Fetched at</th></tr></thead><tbody>";
    artifacts.forEach(function (a, i) {
      html += '<tr class="row" data-idx="' + i + '"><td>' + esc(a.content_type) + "</td><td class=\\"mono\\">" +
        esc(a.source_url) + "</td><td class=\\"mono\\">" + esc(a.discovered_from || "–") + "</td><td class=\\"mono\\">" +
        esc(a.checksum ? a.checksum.slice(0, 12) + "…" : "") + "</td><td>" + fmtBytes(a.content_size) +
        "</td><td>" + esc(a.fetched_at) + "</td></tr>";
      html += '<tr class="detail-row" data-detail-for="' + i + '" style="display:none"><td colspan="6"></td></tr>';
    });
    html += "</tbody></table>";
    el.innerHTML = html;

    el.querySelectorAll("tr.row").forEach(function (tr) {
      tr.addEventListener("click", function () {
        var idx = tr.getAttribute("data-idx");
        var detail = el.querySelector('tr[data-detail-for="' + idx + '"]');
        var showing = detail.style.display !== "none";
        detail.style.display = showing ? "none" : "";
        if (!showing) {
          var a = artifacts[idx];
          var dh = "";
          var ct = baseContentType(a.content_type);
          if (PREVIEWABLE.indexOf(ct) !== -1 && a.raw_location) {
            dh += '<iframe class="raw-preview" src="' + esc(a.raw_location) + '"></iframe>';
          } else if (a.raw_location) {
            dh += '<a href="' + esc(a.raw_location) + '" target="_blank">Open raw file</a>';
          } else {
            dh += '<span class="muted">No raw file recorded.</span>';
          }
          if (a.discovered_from && bySourceUrl[a.discovered_from]) {
            dh += '<div style="margin-top:10px">Discovered from artifact: <a href="#" onclick="jumpToSource(' +
              JSON.stringify(a.discovered_from) + ');return false;" class="mono">' + esc(a.discovered_from) + "</a></div>";
          } else if (a.discovered_from) {
            dh += '<div style="margin-top:10px">Discovered from: <a href="#" onclick="jumpToSource(' +
              JSON.stringify(a.discovered_from) + ');return false;" class="mono">' + esc(a.discovered_from) + "</a></div>";
          }
          detail.querySelector("td").innerHTML = dh;
        }
      });
    });
  }

  // ---------- Errors ----------
  function renderErrors() {
    var el = document.getElementById("tab-errors");
    var failed = (DATA.attempts || []).filter(function (a) { return a.final_result !== "SUCCESS"; });
    if (!failed.length) {
      el.innerHTML = '<div class="empty">No failed attempts this run.</div>';
      return;
    }
    var groups = {};
    failed.forEach(function (a) { (groups[a.final_result] = groups[a.final_result] || []).push(a); });
    var html = "";
    Object.keys(groups).sort().forEach(function (state) {
      var rows = groups[state].slice().sort(function (a, b) {
        return (a.occurred_at || "").localeCompare(b.occurred_at || "");
      });
      html += '<div class="section"><h2>' + statusPill(state) + " (" + rows.length + ")</h2>";
      html += "<table><thead><tr><th>URL</th><th>Classification</th><th>Error</th><th>Occurred at</th><th>Retry</th></tr></thead><tbody>";
      html += rows.map(function (a) {
        return '<tr><td class="mono">' + esc(a.url) + "</td><td>" + esc(a.failure_classification) +
          "</td><td>" + esc(a.error_message) + "</td><td>" + esc(a.occurred_at) + "</td><td>" + a.retry_number + "</td></tr>";
      }).join("");
      html += "</tbody></table></div>";
    });
    el.innerHTML = html;
  }

  // ---------- Tabs ----------
  function showTab(name) {
    document.querySelectorAll("nav button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    document.querySelectorAll("main .tab").forEach(function (t) {
      t.classList.toggle("active", t.id === "tab-" + name);
    });
  }
  window.showTab = showTab;
  document.querySelectorAll("nav button").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.getAttribute("data-tab")); });
  });

  renderOverview();
  renderSources();
  renderArtifacts();
  renderErrors();
})();
</script>
</body>
</html>
"""


class RunStorage:
    """Manages the isolated filesystem hierarchy for a single acquisition run."""

    def __init__(self, base_dir: Path, site_id: str, run_id: int):
        self.base_dir = Path(base_dir)
        self.site_id = site_id
        self.run_id = run_id

        runs_root = self.base_dir if self.base_dir.name == "runs" else self.base_dir / "runs"
        self.run_dir = (runs_root / self.site_id / str(self.run_id)).resolve()

        self.raw_dir = self.run_dir / "raw"
        self.raw_html_dir = self.raw_dir / "html"
        self.raw_json_dir = self.raw_dir / "json"
        self.raw_pdf_dir = self.raw_dir / "pdf"
        self.raw_other_dir = self.raw_dir / "other"
        self.evidence_dir = self.run_dir / "evidence"
        self.program2_dir = self.run_dir / "program2"

        self._init_dirs()

    def _init_dirs(self) -> None:
        self.raw_html_dir.mkdir(parents=True, exist_ok=True)
        self.raw_json_dir.mkdir(parents=True, exist_ok=True)
        self.raw_pdf_dir.mkdir(parents=True, exist_ok=True)
        self.raw_other_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.program2_dir.mkdir(parents=True, exist_ok=True)

    def write_raw_artifact(
        self,
        content: bytes,
        checksum: str,
        content_type: str | None = None,
        url: str = "",
    ) -> Path:
        """Atomically persist a raw artifact into its classified raw subdirectory.

        Guarantees that artifacts cannot escape their run directory.
        """
        category = classify_content_type(content_type, url)
        target_dir = self.raw_dir / category
        path = (target_dir / f"{checksum}.raw").resolve()

        # Path containment guard: artifact cannot escape its run directory
        try:
            path.relative_to(self.run_dir)
        except ValueError as exc:
            raise StorageError(
                f"security violation: artifact path {path} escapes run directory {self.run_dir}"
            ) from exc

        if path.exists():
            return path

        temporary_path: Path | None = None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StorageError(f"failed to persist raw artifact {checksum}: {exc}") from exc

        return path

    def write_evidence(
        self,
        attempts: list[AttemptRecord],
        decisions: list[AdaptiveDecisionRecord],
    ) -> None:
        """Write run-scoped evidence ledger rows to disk in JSONL format."""
        attempts_path = self.evidence_dir / "attempts.jsonl"
        with attempts_path.open("w", encoding="utf-8") as f:
            for a in attempts:
                record = {
                    "attempt_id": a.attempt_id,
                    "run_id": a.run_id,
                    "occurred_at": _iso(a.occurred_at),
                    "url": a.url,
                    "domain": a.domain,
                    "acquisition_method": a.acquisition_method,
                    "session_id": a.session_id,
                    "proxy_id": a.proxy_id,
                    "proxy_status": a.proxy_status,
                    "http_status": a.http_status,
                    "latency_ms": a.latency_ms,
                    "retry_number": a.retry_number,
                    "retry_budget": a.retry_budget,
                    "timeout_seconds": a.timeout_seconds,
                    "backoff_applied_seconds": a.backoff_applied_seconds,
                    "concurrency_at_attempt": a.concurrency_at_attempt,
                    "rate_limit_detected": a.rate_limit_detected,
                    "failure_classification": a.failure_classification,
                    "adaptive_decision": a.adaptive_decision,
                    "final_result": a.final_result.value,
                    "artifact_id": a.artifact_id,
                    "error_message": a.error_message,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        decisions_path = self.evidence_dir / "adaptive_decisions.jsonl"
        with decisions_path.open("w", encoding="utf-8") as f:
            for d in decisions:
                record = {
                    "decision_id": d.decision_id,
                    "run_id": d.run_id,
                    "occurred_at": _iso(d.occurred_at),
                    "domain": d.domain,
                    "decision_type": d.decision_type,
                    "reason": d.reason,
                    "before_value": d.before_value,
                    "after_value": d.after_value,
                    "details": d.details,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _portable_raw_location(self, raw_location: str) -> str:
        """Express a raw artifact path relative to this run's own directory.

        The DB row (`acq.artifacts.raw_location`) keeps the absolute path —
        useful for the machine that ran the acquisition — but the exported
        Program-2 package is meant to travel as a self-contained folder
        (moved, zipped, handed to a different machine), so a path baked in
        as absolute (e.g. `/home/<user>/...`) would silently stop resolving
        the moment the folder leaves that machine. Falls back to the
        original string on any path this run doesn't actually own.
        """
        try:
            return str(Path(raw_location).resolve().relative_to(self.run_dir))
        except ValueError:
            return raw_location

    def write_program2_handoff(
        self,
        artifacts: list[ArtifactRecord],
        summary: RunSummary,
    ) -> None:
        """Generate the clean Program-2 handoff package inside program2/.

        Does not leak Crawlee/Playwright/session/proxy internals.
        """
        # 1. program2/artifacts.json
        artifacts_data = [
            {
                "contract_version": a.contract_version,
                "artifact_id": a.artifact_id,
                "run_id": a.run_id,
                "source_url": a.source_url,
                "canonical_url": a.canonical_url,
                "discovered_from": a.discovered_from,
                "fetched_at": _iso(a.fetched_at),
                "content_type": a.content_type,
                "http_status": a.http_status,
                "acquisition_method": a.acquisition_method,
                "raw_location": self._portable_raw_location(a.raw_location),
                "content_size": a.content_size,
                "checksum": a.checksum,
                "encoding": a.encoding,
                "final_url": a.final_url,
                "status": a.status.value,
            }
            for a in artifacts
        ]
        (self.program2_dir / "artifacts.json").write_text(
            json.dumps(artifacts_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 2. program2/metadata.json
        categories = Counter(classify_content_type(a.content_type, a.source_url) for a in artifacts)
        metadata = {
            "contract_version": summary.contract_version,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "started_at": _iso(summary.started_at),
            "completed_at": _iso(summary.completed_at),
            "status": summary.status.value,
            "artifact_count": len(artifacts),
            "artifacts_by_category": dict(categories),
            "discovered": summary.discovered,
            "attempted": summary.attempted,
            "acquired": summary.acquired,
            "failed": summary.failed,
            "retries": summary.retries,
            "rate_limited": summary.rate_limited,
            "browser_pages": summary.browser_pages,
            "proxy_failures": summary.proxy_failures,
        }
        (self.program2_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 3. program2/provenance.json
        provenance = {
            "contract_version": summary.contract_version,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "source_url": a.source_url,
                    "canonical_url": a.canonical_url,
                    "discovered_from": a.discovered_from,
                    "redirect_chain": [{"url": hop.url, "status": hop.status} for hop in a.redirect_chain],
                    "fetched_at": _iso(a.fetched_at),
                    "http_status": a.http_status,
                    "acquisition_method": a.acquisition_method,
                    "checksum": a.checksum,
                }
                for a in artifacts
            ],
        }
        (self.program2_dir / "provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_report(
        self,
        summary: RunSummary,
        artifacts: list[ArtifactRecord],
        attempts: list[AttemptRecord],
        decisions: list[AdaptiveDecisionRecord],
    ) -> Path:
        """Write a self-contained report.html for the run.

        Shows only what the acquisition engine itself produced this run —
        the same run/artifact/attempt/decision records `write_evidence` and
        `write_program2_handoff` already take, embedded inline as JSON and
        rendered client-side. No KPI/extraction/confidence data: that
        doesn't exist in this schema yet.
        """
        report_data = {
            "summary": {
                "run_id": summary.run_id,
                "contract_version": summary.contract_version,
                "root_source_url": summary.root_source_url,
                "started_at": _iso(summary.started_at),
                "completed_at": _iso(summary.completed_at),
                "status": summary.status.value,
                "discovered": summary.discovered,
                "attempted": summary.attempted,
                "acquired": summary.acquired,
                "partial": summary.partial,
                "failed": summary.failed,
                "retries": summary.retries,
                "rate_limited": summary.rate_limited,
                "browser_pages": summary.browser_pages,
                "proxy_failures": summary.proxy_failures,
                "by_state": summary.by_state,
            },
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "run_id": d.run_id,
                    "occurred_at": _iso(d.occurred_at),
                    "domain": d.domain,
                    "decision_type": d.decision_type,
                    "reason": d.reason,
                    "before_value": d.before_value,
                    "after_value": d.after_value,
                    "details": d.details,
                }
                for d in decisions
            ],
            "attempts": [
                {
                    "attempt_id": a.attempt_id,
                    "run_id": a.run_id,
                    "occurred_at": _iso(a.occurred_at),
                    "url": a.url,
                    "domain": a.domain,
                    "acquisition_method": a.acquisition_method,
                    "session_id": a.session_id,
                    "proxy_id": a.proxy_id,
                    "proxy_status": a.proxy_status,
                    "http_status": a.http_status,
                    "latency_ms": a.latency_ms,
                    "retry_number": a.retry_number,
                    "retry_budget": a.retry_budget,
                    "timeout_seconds": a.timeout_seconds,
                    "backoff_applied_seconds": a.backoff_applied_seconds,
                    "concurrency_at_attempt": a.concurrency_at_attempt,
                    "rate_limit_detected": a.rate_limit_detected,
                    "failure_classification": a.failure_classification,
                    "adaptive_decision": a.adaptive_decision,
                    "final_result": a.final_result.value,
                    "artifact_id": a.artifact_id,
                    "error_message": a.error_message,
                }
                for a in attempts
            ],
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "run_id": a.run_id,
                    "source_url": a.source_url,
                    "canonical_url": a.canonical_url,
                    "discovered_from": a.discovered_from,
                    "fetched_at": _iso(a.fetched_at),
                    "content_type": a.content_type,
                    "http_status": a.http_status,
                    "acquisition_method": a.acquisition_method,
                    "raw_location": self._portable_raw_location(a.raw_location),
                    "content_size": a.content_size,
                    "checksum": a.checksum,
                    "encoding": a.encoding,
                    "final_url": a.final_url,
                    "session_id": a.session_id,
                    "status": a.status.value,
                }
                for a in artifacts
            ],
        }

        data_json = json.dumps(report_data, ensure_ascii=False).replace("</", "<\\/")
        html = _REPORT_TEMPLATE.replace("__REPORT_DATA_JSON__", data_json)
        report_path = self.run_dir / "report.html"
        report_path.write_text(html, encoding="utf-8")
        return report_path

    def write_manifest(
        self,
        summary: RunSummary,
        artifacts: list[ArtifactRecord],
    ) -> Path:
        """Write the top-level manifest.json for the run."""
        categories = Counter(classify_content_type(a.content_type, a.source_url) for a in artifacts)
        manifest_data = {
            "contract_version": CONTRACT_VERSION,
            "run_id": summary.run_id,
            "site_id": self.site_id,
            "root_source_url": summary.root_source_url,
            "started_at": _iso(summary.started_at),
            "completed_at": _iso(summary.completed_at),
            "status": summary.status.value,
            "discovered": summary.discovered,
            "attempted": summary.attempted,
            "acquired": summary.acquired,
            "failed": summary.failed,
            "retries": summary.retries,
            "rate_limited": summary.rate_limited,
            "browser_pages": summary.browser_pages,
            "proxy_failures": summary.proxy_failures,
            "by_state": summary.by_state,
            "artifact_count": len(artifacts),
            "artifacts_by_category": dict(categories),
            "paths": {
                "run_dir": str(self.run_dir),
                "raw_dir": str(self.raw_dir),
                "evidence_dir": str(self.evidence_dir),
                "program2_dir": str(self.program2_dir),
            },
        }
        manifest_path = self.run_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path
