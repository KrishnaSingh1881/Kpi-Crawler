let allRuns = [];
let currentRun = null;
let currentArtifacts = [];
let activeCategory = 'ALL';
let searchQuery = '';

document.addEventListener('DOMContentLoaded', () => {
  initApp();
});

async function initApp() {
  setupEventListeners();
  await loadRuns();
}

function setupEventListeners() {
  // Search box
  document.getElementById('search-input').addEventListener('input', (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderArtifacts();
  });

  // Filter Pills
  document.getElementById('filter-pills').addEventListener('click', (e) => {
    const pill = e.target.closest('.pill');
    if (!pill) return;
    document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    activeCategory = pill.dataset.category;
    renderArtifacts();
  });

  // Export Run Button
  document.getElementById('btn-export-run').addEventListener('click', exportCurrentRun);

  // Modal Close
  document.getElementById('modal-close-btn').addEventListener('click', closeModal);
  document.getElementById('preview-modal').addEventListener('click', (e) => {
    if (e.target.id === 'preview-modal') closeModal();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
}

async function loadRuns() {
  try {
    const res = await fetch('/api/runs');
    allRuns = await res.json();
    renderRunsList();
    if (allRuns.length > 0) {
      selectRun(allRuns[0]);
    }
  } catch (err) {
    console.error('Failed to load runs:', err);
    document.getElementById('runs-list').innerHTML = `<div class="empty-state">Error loading runs</div>`;
  }
}

function renderRunsList() {
  const container = document.getElementById('runs-list');
  if (allRuns.length === 0) {
    container.innerHTML = `<div class="empty-state">No crawl runs found</div>`;
    return;
  }

  container.innerHTML = allRuns.map(run => `
    <div class="run-card ${currentRun && currentRun.site_id === run.site_id && currentRun.run_id === run.run_id ? 'active' : ''}"
         onclick="onRunClick('${run.site_id}', '${run.run_id}')">
      <div class="run-site">${run.site_id}</div>
      <div class="run-meta">
        <span>Run #${run.run_id} (${run.artifact_count || 0} files)</span>
        <span class="run-badge ${(run.status || '').toLowerCase()}">${run.status || 'DONE'}</span>
      </div>
    </div>
  `).join('');
}

function onRunClick(siteId, runId) {
  const run = allRuns.find(r => r.site_id === siteId && String(r.run_id) === String(runId));
  if (run) selectRun(run);
}

async function selectRun(run) {
  currentRun = run;
  renderRunsList();
  
  document.getElementById('current-run-title').innerText = `${run.site_id} (Run #${run.run_id})`;
  document.getElementById('current-run-subtitle').innerText = `Started: ${new Date(run.started_at).toLocaleString()} | Total Artifacts: ${run.artifact_count}`;
  document.getElementById('btn-export-run').disabled = false;

  const grid = document.getElementById('artifact-grid');
  grid.innerHTML = `<div class="empty-state">Loading artifacts...</div>`;

  try {
    const res = await fetch(`/api/runs/${run.site_id}/${run.run_id}/artifacts`);
    currentArtifacts = await res.json();
    updateCategoryCounts();
    renderArtifacts();
  } catch (err) {
    console.error('Failed to load artifacts:', err);
    grid.innerHTML = `<div class="empty-state">Failed to load artifacts</div>`;
  }
}

function updateCategoryCounts() {
  const counts = { ALL: currentArtifacts.length, html: 0, pdf: 0, json: 0, xml: 0, other: 0 };
  currentArtifacts.forEach(a => {
    const cat = detectCategory(a);
    if (counts[cat] !== undefined) counts[cat]++;
    else counts.other++;
  });

  document.getElementById('cnt-all').innerText = counts.ALL;
  document.getElementById('cnt-html').innerText = counts.html;
  document.getElementById('cnt-pdf').innerText = counts.pdf;
  document.getElementById('cnt-json').innerText = counts.json;
  document.getElementById('cnt-xml').innerText = counts.xml;
  document.getElementById('cnt-other').innerText = counts.other;
}

function detectCategory(artifact) {
  const ctype = (artifact.content_type || '').toLowerCase();
  const rawLoc = (artifact.raw_location || '').toLowerCase();
  
  if (ctype.includes('html') || rawLoc.includes('/html/')) return 'html';
  if (ctype.includes('pdf') || rawLoc.includes('/pdf/')) return 'pdf';
  if (ctype.includes('json') || rawLoc.includes('/json/')) return 'json';
  if (ctype.includes('xml') || ctype.includes('rss') || rawLoc.includes('/xml/')) return 'xml';
  return 'other';
}

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function renderArtifacts() {
  const grid = document.getElementById('artifact-grid');
  
  const filtered = currentArtifacts.filter(art => {
    const cat = detectCategory(art);
    if (activeCategory !== 'ALL' && cat !== activeCategory) return false;

    if (searchQuery) {
      const url = (art.source_url || '').toLowerCase();
      const raw = (art.raw_location || '').toLowerCase();
      const checksum = (art.checksum || '').toLowerCase();
      if (!url.includes(searchQuery) && !raw.includes(searchQuery) && !checksum.includes(searchQuery)) {
        return false;
      }
    }
    return true;
  });

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">🔍</div><div>No matching artifacts found</div></div>`;
    return;
  }

  grid.innerHTML = filtered.map(art => {
    const cat = detectCategory(art);
    const checksum = art.checksum;
    const previewUrl = `/api/preview/${currentRun.site_id}/${currentRun.run_id}/${checksum}`;
    const downloadUrl = `${previewUrl}?download=1`;
    const filename = art.filename_suggested || `${checksum.substring(0, 12)}.${extForCategory(cat)}`;

    return `
      <div class="artifact-card">
        <div class="card-top">
          <div class="card-header-row">
            <span class="type-badge ${cat}">${cat.toUpperCase()}</span>
            <span class="file-size">${formatBytes(art.content_size)}</span>
          </div>
          <div class="card-url" title="${art.source_url}">${art.source_url || 'Unknown Source URL'}</div>
          <div class="card-file-path" title="${art.raw_location}">${art.raw_location}</div>
        </div>
        <div class="card-actions">
          <button class="btn-card preview-btn" onclick="openPreview('${checksum}', '${cat}', '${escapeQuotes(art.source_url)}')">
            <span>👁️</span> Preview
          </button>
          <a class="btn-card" href="${downloadUrl}" download="${filename}">
            <span>📥</span> Download .${extForCategory(cat)}
          </a>
        </div>
      </div>
    `;
  }).join('');
}

function extForCategory(cat) {
  switch (cat) {
    case 'html': return 'html';
    case 'pdf': return 'pdf';
    case 'json': return 'json';
    case 'xml': return 'xml';
    default: return 'txt';
  }
}

function escapeQuotes(str) {
  return (str || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

async function openPreview(checksum, category, title) {
  const modal = document.getElementById('preview-modal');
  const modalTitle = document.getElementById('modal-title');
  const modalBody = document.getElementById('modal-body');
  const downloadBtn = document.getElementById('modal-download-btn');

  const previewUrl = `/api/preview/${currentRun.site_id}/${currentRun.run_id}/${checksum}`;
  const filename = `${checksum.substring(0, 12)}.${extForCategory(category)}`;

  modalTitle.innerText = `${category.toUpperCase()} Preview: ${title || checksum}`;
  downloadBtn.href = `${previewUrl}?download=1`;
  downloadBtn.download = filename;

  if (category === 'html' || category === 'pdf') {
    modalBody.innerHTML = `<iframe class="preview-iframe" src="${previewUrl}"></iframe>`;
  } else {
    modalBody.innerHTML = `<div class="code-viewer">Loading text content...</div>`;
    try {
      const res = await fetch(previewUrl);
      const text = await res.text();
      modalBody.innerHTML = `<pre class="code-viewer"><code>${escapeHtml(text)}</code></pre>`;
    } catch (err) {
      modalBody.innerHTML = `<div class="empty-state">Failed to load content</div>`;
    }
  }

  modal.classList.add('active');
}

function closeModal() {
  const modal = document.getElementById('preview-modal');
  modal.classList.remove('active');
  document.getElementById('modal-body').innerHTML = '';
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function exportCurrentRun() {
  if (!currentRun) return;

  const btn = document.getElementById('btn-export-run');
  const origText = btn.innerHTML;
  btn.innerHTML = `<span>⏳</span> Exporting...`;
  btn.disabled = true;

  try {
    const res = await fetch(`/api/export/${currentRun.site_id}/${currentRun.run_id}`, { method: 'POST' });
    const data = await res.json();
    alert(`Success! Exported ${data.exported_count} files with real extensions to:\n${data.export_dir}`);
  } catch (err) {
    alert('Export failed. Check server logs.');
  } finally {
    btn.innerHTML = origText;
    btn.disabled = false;
  }
}
