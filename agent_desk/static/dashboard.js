let settingsDirty = false;
let settingsProjectPath = '';
let currentRepoName = '';
let pickerRepo = null;
let issuesLoading = false;
let latestState = null;
const dirtyRunAiScopes = new Set();
async function fetchState() {
  const res = await fetch('/api/state');
  return await res.json();
}
async function action(path) {
  await fetch(path, { method: 'POST' });
  await refresh();
}
function restartHazards(state) {
  const hazards = [];
  if (issuesLoading) {
    hazards.push('Issue sync or dependency analysis is in progress.');
  }
  (state.runs || []).forEach(run => {
    if (run.state !== 'running') return;
    const stage = String(run.stage || 'running');
    const label = `#${run.issue_number} ${stage}`;
    if (stage === 'claimed') {
      hazards.push(`${label} is being claimed before its supervisor is recorded.`);
    } else if (stage === 'request-changes queued') {
      hazards.push(`${label} is queued before its supervisor is recorded.`);
    } else if (!run.supervisor_pid) {
      hazards.push(`${label} has no supervisor_pid yet.`);
    }
  });
  return hazards;
}
async function restartWithGuard() {
  const state = latestState || await fetchState();
  const hazards = restartHazards(state);
  if (hazards.length) {
    const message = `Restart Agent Desk anyway?\n\n${hazards.map(item => `- ${item}`).join('\n')}\n\nCancel keeps the service running.`;
    if (!confirm(message)) return;
  }
  await fetch('/api/actions/restart', { method: 'POST' });
  const health = document.getElementById('health');
  if (health) health.textContent = 'Restarting...';
}
function shutdownRunLine(run) {
  const pid = run.supervisor_pid ? `pid ${run.supervisor_pid}` : 'no pid';
  const mode = run.killable ? 'will stop' : 'not verified';
  return `#${run.issue_number} run ${run.run_id}: ${run.stage || run.state || 'running'} (${pid}, ${mode})`;
}
async function shutdownAll() {
  const previewRes = await fetch('/api/actions/shutdown-preview');
  if (!previewRes.ok) {
    alert(await previewRes.text());
    return;
  }
  const preview = await previewRes.json();
  const runs = preview.runs || [];
  const lines = runs.length ? runs.map(shutdownRunLine) : ['No running jobs are recorded.'];
  const message = `Shutdown Agent Desk and stop ${runs.length} running job(s)?\n\n${lines.join('\n')}\n\nInterrupted runs will keep resume notes and a dashboard Resume action.`;
  if (!confirm(message)) return;
  const shutdownRes = await fetch('/api/actions/shutdown-all', { method: 'POST' });
  if (!shutdownRes.ok) {
    alert(await shutdownRes.text());
    return;
  }
  const result = await shutdownRes.json();
  const health = document.getElementById('health');
  if (health) health.textContent = `Shutdown recorded: ${result.shutdown_id}`;
}
async function postJson(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  if (!res.ok) throw new Error(await res.text());
  await refresh();
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function aiCatalog(state) {
  return state.ai_models || [];
}
function aiOption(state, model) {
  return aiCatalog(state).find(item => item.id === model);
}
function modelOptionsHtml(state) {
  return aiCatalog(state).map(item =>
    `<option value="${esc(item.id)}">${esc(item.label || item.id)}</option>`
  ).join('');
}
function reasoningOptionsHtml(state, model) {
  const option = aiOption(state, model);
  const efforts = option ? option.reasoning_efforts || [] : ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  return efforts.map(effort =>
    `<option value="${esc(effort)}">${esc(effort)}</option>`
  ).join('');
}
function reasoningValueForModelChange(state, model, current) {
  const option = aiOption(state, model);
  if (!option) return current || 'xhigh';
  const efforts = option.reasoning_efforts || [];
  return efforts.includes(current) ? current : (option.default_reasoning_effort || 'xhigh');
}
function jsString(value) {
  // Produce a JS string literal that is also safe inside a double-quoted HTML
  // attribute (e.g. onclick="fn(...)"); the entities decode back to real quotes
  // before the handler runs.
  return JSON.stringify(String(value ?? ''))
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');
}
async function copyResume(command) {
  await navigator.clipboard.writeText(command);
}
function closeBrowser() {
  const panel = document.getElementById('fs-browser');
  if (panel) panel.style.display = 'none';
}
async function addProject(path) {
  path = (path || '').trim();
  if (!path) return;
  try {
    await postJson('/api/projects', { path });
    closeBrowser();
  } catch (error) {
    alert(error.message || String(error));
  }
}
async function cloneProject() {
  const input = document.getElementById('clone-spec');
  const repo = input.value.trim();
  if (!repo) return;
  try {
    await postJson('/api/projects/clone', { repo });
    input.value = '';
  } catch (error) {
    alert(error.message || String(error));
  }
}
function renderIssueTools(state) {
  const tools = document.getElementById('issue-tools');
  if (!tools) return;
  const path = selectedProjectPath();
  const project = path ? projectForPath(state, path) : null;
  currentRepoName = project ? project.name : '';
  if (!issuesLoading) {
    tools.innerHTML = currentRepoName
      ? `<button onclick="syncIssues()">Sync issues</button>`
      : '<div class="muted">Select a project folder to see its issues.</div>';
  }
  if (!currentRepoName) {
    pickerRepo = null;
    document.getElementById('issue-picker').innerHTML = '';
  } else if (pickerRepo !== currentRepoName && !issuesLoading) {
    // New repo selected: show its synced issues from disk (no GitHub call).
    loadIssues();
  }
}
async function loadIssues() {
  const repo = currentRepoName;
  const picker = document.getElementById('issue-picker');
  if (!repo) return;
  issuesLoading = true;
  pickerRepo = repo;
  try {
    const res = await fetch(`/api/issues?repo=${encodeURIComponent(repo)}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderIssuePicker(repo, data.issues || []);
  } catch (error) {
    pickerRepo = null;
    picker.innerHTML = `<div class="muted" style="padding:10px">Failed to load: ${esc(error.message || String(error))}</div>`;
  } finally {
    issuesLoading = false;
  }
}
async function syncIssues() {
  const repo = currentRepoName;
  const picker = document.getElementById('issue-picker');
  const tools = document.getElementById('issue-tools');
  if (!repo) { alert('Select a project folder first'); return; }
  issuesLoading = true;
  pickerRepo = repo;
  tools.innerHTML = '<button disabled>Syncing…</button>';
  picker.innerHTML = '<div class="muted" style="padding:10px">Syncing from GitHub…</div>';
  try {
    const res = await fetch('/api/actions/sync-issues', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo })
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    renderIssuePicker(repo, data.issues || []);
  } catch (error) {
    pickerRepo = null;
    picker.innerHTML = `<div class="muted" style="padding:10px">Sync failed: ${esc(error.message || String(error))}</div>`;
  } finally {
    issuesLoading = false;
    tools.innerHTML = `<button onclick="syncIssues()">Sync issues</button>`;
  }
}
function renderIssuePicker(repo, issues) {
  const picker = document.getElementById('issue-picker');
  pickerRepo = repo;
  if (!issues.length) {
    picker.innerHTML = '<div class="muted" style="padding:10px">No issues yet — click Sync issues.</div>';
    return;
  }
  const rows = issues.map(issue => {
    const attrs = issue.on_desk ? 'disabled' : '';
    return `<div class="issue-row ${issue.on_desk ? 'on-desk' : ''}">
      <input type="checkbox" value="${issue.number}" ${attrs}>
      <a class="issue-title" href="${esc(issue.url || '')}" target="_blank" rel="noopener noreferrer"><strong>#${issue.number}</strong> ${esc(issue.title)}</a>
    </div>`;
  }).join('');
  picker.innerHTML = `<div class="issue-picker">
    <div class="issue-head">
      <strong>${esc(repo)}</strong>
      <div class="issue-actions">
        <button class="primary issue-action" title="Analyze dependencies" aria-label="Analyze dependencies for selected issues" onclick="addSelected('analyze')">Analyze</button>
        <button class="issue-action" title="Add all directly" aria-label="Add selected issues directly" onclick="addSelected('direct')">Add</button>
      </div>
    </div>
    <div class="issue-list">${rows}</div>
  </div>`;
}
function blockedByText(items) {
  if (!items || !items.length) return '';
  return items.map(item => {
    const repo = item.repo || '';
    const number = item.number || '';
    const state = item.state ? ` (${item.state})` : '';
    return `${repo ? repo + '#' : '#'}${number}${state}`;
  }).join(', ');
}
function dependencyLabel(item) {
  const repo = item.repo || '';
  const number = item.number || '';
  const state = item.state ? ` (${item.state})` : '';
  return `${repo ? repo + '#' : '#'}${number}${state}`;
}
function satisfyDependency(issueNumber, repoName, dependencyRepo, dependencyNumber) {
  const reason = prompt('Reason for marking this dependency satisfied?', 'manual override');
  if (reason === null) return;
  return postJson('/api/actions/dependency-override', {
    repo: repoName,
    issue: issueNumber,
    dependency_repo: dependencyRepo,
    dependency: dependencyNumber,
    satisfied: true,
    reason: reason || 'manual override'
  });
}
function clearDependencyOverride(issueNumber, repoName, dependencyRepo, dependencyNumber) {
  return postJson('/api/actions/dependency-override', {
    repo: repoName,
    issue: issueNumber,
    dependency_repo: dependencyRepo,
    dependency: dependencyNumber,
    satisfied: false
  });
}
function addDependencyEdge(issueNumber, repoName) {
  const dependencyRepo = prompt('Dependency repo?', repoName);
  if (dependencyRepo === null) return;
  const rawNumber = prompt('Dependency issue number?');
  if (rawNumber === null) return;
  const dependencyNumber = Number(rawNumber);
  if (!Number.isInteger(dependencyNumber) || dependencyNumber <= 0) {
    alert('Dependency issue number must be positive');
    return;
  }
  const evidence = prompt('Evidence or note?', 'manual dependency repair');
  if (evidence === null) return;
  return postJson('/api/actions/dependency-edge', {
    repo: repoName,
    issue: issueNumber,
    dependency_repo: dependencyRepo || repoName,
    dependency: dependencyNumber,
    present: true,
    evidence: evidence || 'manual dependency repair'
  });
}
function removeDependencyEdge(issueNumber, repoName, dependencyRepo, dependencyNumber) {
  return postJson('/api/actions/dependency-edge', {
    repo: repoName,
    issue: issueNumber,
    dependency_repo: dependencyRepo,
    dependency: dependencyNumber,
    present: false
  });
}
function dependencyEdgesHtml(run) {
  const items = run.dependencies || [];
  if (!items.length) return '';
  const rows = items.map(item => {
    const repo = item.repo || run.repo_name || '';
    const number = Number(item.number || 0);
    const evidence = item.evidence ? ` · ${esc(item.evidence)}` : '';
    return `<div class="dependency-row">
      <span>${esc(repo)}#${number}${evidence}</span>
      <button onclick="removeDependencyEdge(${run.issue_number}, ${jsString(run.repo_name)}, ${jsString(repo)}, ${number})">Remove</button>
    </div>`;
  }).join('');
  return `<div class="dependency-list"><div class="muted">Depends on</div>${rows}</div>`;
}
function blockedDependenciesHtml(run) {
  const items = run.blocked_by || [];
  if (!items.length) return '';
  const rows = items.map(item => {
    const repo = item.repo || run.repo_name || '';
    const number = Number(item.number || 0);
    return `<div class="dependency-row">
      <span>${esc(dependencyLabel(item))}</span>
      <button onclick="satisfyDependency(${run.issue_number}, ${jsString(run.repo_name)}, ${jsString(repo)}, ${number})">Satisfy</button>
    </div>`;
  }).join('');
  return `<div class="dependency-list"><div class="muted">Waiting for</div>${rows}</div>`;
}
function dependencyOverridesHtml(run) {
  const overrides = (run.dependency_overrides || []).filter(item => item.state === 'satisfied');
  if (!overrides.length) return '';
  const rows = overrides.map(item => {
    const repo = item.repo || run.repo_name || '';
    const number = Number(item.number || 0);
    const reason = item.reason ? ` · ${esc(item.reason)}` : '';
    return `<div class="dependency-row">
      <span>Manually satisfied ${esc(repo)}#${number}${reason}</span>
      <button onclick="clearDependencyOverride(${run.issue_number}, ${jsString(run.repo_name)}, ${jsString(repo)}, ${number})">Undo</button>
    </div>`;
  }).join('');
  return `<div class="dependency-list">${rows}</div>`;
}
async function addSelected(mode) {
  const repo = currentRepoName;
  if (!repo) { alert('Select a project folder first'); return; }
  const picker = document.getElementById('issue-picker');
  const checked = [...picker.querySelectorAll('input[type=checkbox]:checked:not([disabled])')];
  const issues = checked.map(box => parseInt(box.value, 10)).filter(Number.isInteger);
  if (!issues.length) { alert('Select at least one issue to add'); return; }
  const buttons = [...picker.querySelectorAll('.issue-head button')];
  const activeText = mode === 'direct' ? 'Adding…' : 'Analyzing…';
  buttons.forEach(btn => { btn.disabled = true; });
  const active = buttons.find(btn => btn.getAttribute('onclick')?.includes(`'${mode}'`));
  const originalText = active ? active.textContent : '';
  if (active) active.textContent = activeText;
  try {
    const res = await fetch('/api/actions/include-issues', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, issues, dependency_mode: mode || 'analyze' })
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok) { alert(result.message || 'Could not add the selected issues'); return; }
  } catch (error) {
    alert(error.message || String(error));
    return;
  } finally {
    buttons.forEach(btn => { btn.disabled = false; });
    if (active) active.textContent = originalText;
  }
  await loadIssues();
  await refresh();
}
async function removeIssue(number, repoName) {
  const repo = repoName || currentRepoName;
  if (!repo) { alert('Select a project folder first'); return; }
  try {
    const res = await fetch('/api/actions/remove-issue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, issue: number })
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok) { alert(result.message || 'Could not remove the issue'); return; }
    if (!result.started) { alert(result.message || 'Issue cannot be removed'); return; }
  } catch (error) {
    alert(error.message || String(error));
    return;
  }
  if (repo === currentRepoName) await loadIssues();
  await refresh();
}
async function toggleBrowser() {
  const panel = document.getElementById('fs-browser');
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    await browseTo('');
  } else {
    panel.style.display = 'none';
  }
}
async function browseTo(path) {
  const res = await fetch('/api/fs?path=' + encodeURIComponent(path || ''));
  if (!res.ok) { alert(await res.text()); return; }
  renderBrowser(await res.json());
}
function crumbHtml(fullPath) {
  const sep = fullPath.includes('\\') ? '\\' : '/';
  const parts = fullPath.split(sep).filter(Boolean);
  const root = fullPath.startsWith(sep) ? sep : '';
  let acc = root;
  const crumbs = [];
  if (root) crumbs.push(`<button class="fs-crumb" onclick="browseTo(${jsString(root)})">${sep}</button>`);
  parts.forEach((part, i) => {
    acc = (acc.endsWith(sep) ? acc : acc + sep) + part;
    const last = i === parts.length - 1;
    crumbs.push(last
      ? `<span class="fs-crumb current">${esc(part)}</span>`
      : `<button class="fs-crumb" onclick="browseTo(${jsString(acc)})">${esc(part)}</button>`);
  });
  return crumbs.join('<span class="fs-sep">›</span>');
}
function renderBrowser(data) {
  const panel = document.getElementById('fs-browser');
  const up = data.parent
    ? `<button class="fs-up" title="Up one level" onclick="browseTo(${jsString(data.parent)})">&uarr;</button>`
    : '<button class="fs-up" disabled>&uarr;</button>';
  const here = data.is_git
    ? `<button class="primary" onclick="selectFolder(${jsString(data.path)})">Select this repo</button>`
    : '';
  const rows = (data.entries || []).map(entry => `
    <li class="${entry.is_git ? 'is-repo' : ''}">
      <button class="fs-dir" onclick="browseTo(${jsString(entry.path)})">
        <span class="fs-icon">${entry.is_git ? '&#128193;&#10003;' : '&#128193;'}</span>
        <span class="fs-name">${esc(entry.name)}</span>
        ${entry.is_git ? '<span class="git-badge">git</span>' : ''}
      </button>
      ${entry.is_git ? `<button class="primary fs-select" onclick="selectFolder(${jsString(entry.path)})">Select</button>` : ''}
    </li>`).join('') || '<li class="muted">No subfolders</li>';
  panel.innerHTML = `
    <div class="fs-head">
      ${up}
      <div class="fs-crumbs">${crumbHtml(data.path)}</div>
      ${here}
    </div>
    <ul class="fs-list">${rows}</ul>`;
}
async function selectFolder(path) {
  await addProject(path);
}
function markSettingsDirty() {
  settingsDirty = true;
  const status = document.getElementById('settings-status');
  if (status) status.textContent = 'Unsaved changes';
}
function settingsControls() {
  return [
    document.getElementById('auto-start-ready'),
    document.getElementById('max-concurrent-runs'),
    document.getElementById('worker-timeout-hours'),
    document.getElementById('default-ai-model'),
    document.getElementById('default-ai-reasoning-effort'),
    document.getElementById('requires-human-review'),
    document.getElementById('enable-ai-review'),
    document.getElementById('single-closeout-per-workspace'),
    document.getElementById('settings-save')
  ];
}
function setSettingsDisabled(disabled) {
  settingsControls().forEach(control => {
    if (control) control.disabled = disabled;
  });
}
function projectForPath(state, path) {
  return (state.projects || []).find(item => item.path === path);
}
function renderSettings(state) {
  const path = selectedProjectPath();
  if (path !== settingsProjectPath) {
    settingsDirty = false;
    settingsProjectPath = path;
  }
  if (settingsDirty) return;
  const project = path ? projectForPath(state, path) : null;
  const settings = project && project.settings ? project.settings : {
    auto_start_ready: false,
    max_concurrent_runs: 1,
    requires_human_review: true,
    enable_ai_review: false,
    single_closeout_per_workspace: true,
    worker_timeout_seconds: 28800,
    default_ai_model: 'gpt-5.5',
    default_ai_reasoning_effort: 'xhigh'
  };
  setSettingsDisabled(!project);
  document.getElementById('auto-start-ready').checked = !!settings.auto_start_ready;
  document.getElementById('max-concurrent-runs').value = Number(settings.max_concurrent_runs || 1);
  const timeoutHours = Number(settings.worker_timeout_seconds || 28800) / 3600;
  document.getElementById('worker-timeout-hours').value = Number.isInteger(timeoutHours)
    ? String(timeoutHours)
    : String(Math.round(timeoutHours * 100) / 100);
  document.getElementById('default-ai-model-options').innerHTML = modelOptionsHtml(state);
  document.getElementById('default-ai-model').value = settings.default_ai_model || 'gpt-5.5';
  document.getElementById('default-ai-reasoning-options').innerHTML = reasoningOptionsHtml(
    state,
    settings.default_ai_model || 'gpt-5.5'
  );
  document.getElementById('default-ai-reasoning-effort').value = settings.default_ai_reasoning_effort || 'xhigh';
  document.getElementById('requires-human-review').checked = settings.requires_human_review !== false;
  document.getElementById('enable-ai-review').checked = !!settings.enable_ai_review;
  document.getElementById('single-closeout-per-workspace').checked = settings.single_closeout_per_workspace !== false;
  document.getElementById('settings-status').textContent = project ? `Settings for ${project.name}` : 'Select a folder';
}
function onWorkspaceModelChange() {
  const state = latestState || { ai_models: [] };
  const model = document.getElementById('default-ai-model').value;
  const input = document.getElementById('default-ai-reasoning-effort');
  const effort = reasoningValueForModelChange(state, model, input.value || 'xhigh');
  document.getElementById('default-ai-reasoning-options').innerHTML = reasoningOptionsHtml(state, model);
  input.value = effort;
}
async function saveSettings() {
  const path = selectedProjectPath();
  if (!path) {
    document.getElementById('settings-status').textContent = 'Select a folder';
    return;
  }
  const maxInput = document.getElementById('max-concurrent-runs');
  const max = Math.max(1, Number(maxInput.value || 1));
  const timeoutInput = document.getElementById('worker-timeout-hours');
  const timeoutHours = Number(timeoutInput.value || 8);
  const timeoutSeconds = Math.max(
    60,
    Math.round((Number.isFinite(timeoutHours) && timeoutHours > 0 ? timeoutHours : 8) * 3600)
  );
  settingsDirty = false;
  try {
    await postJson('/api/settings', {
      workspace_path: path,
      auto_start_ready: document.getElementById('auto-start-ready').checked,
      max_concurrent_runs: max,
      worker_timeout_seconds: timeoutSeconds,
      default_ai_model: document.getElementById('default-ai-model').value,
      default_ai_reasoning_effort: document.getElementById('default-ai-reasoning-effort').value,
      requires_human_review: document.getElementById('requires-human-review').checked,
      enable_ai_review: document.getElementById('enable-ai-review').checked,
      single_closeout_per_workspace: document.getElementById('single-closeout-per-workspace').checked
    });
    document.getElementById('settings-status').textContent = 'Saved';
  } catch (error) {
    settingsDirty = true;
    document.getElementById('settings-status').textContent = 'Save failed';
    alert(error.message || String(error));
  }
}
function selectedProjectPath() {
  if (!location.hash.startsWith('#project=')) return '';
  return decodeURIComponent(location.hash.slice('#project='.length));
}
function selectProject(path) {
  location.hash = `project=${encodeURIComponent(path)}`;
  refresh();
}
function selectProjectByPath(button) {
  selectProject(button.dataset.path || '');
}
function backToProjects() {
  history.pushState('', document.title, location.pathname + location.search);
  refresh();
}
function logLinks(run) {
  const files = run.log_files || [];
  if (!files.length) return '';
  const links = files.map(name => {
    const action = name.endsWith('.jsonl') ? 'view' : 'file';
    const href = `/api/run/${run.id}/${action}?name=${encodeURIComponent(name)}`;
    return `<a href="${href}" target="_blank" rel="noopener">${esc(name)}</a>`;
  }).join('');
  return `<div class="log-links">${links}</div>`;
}
function resumeCommand(run) {
  const command = run.resume_command || '';
  if (!command) return '';
  return `<div class="resume-command"><code>${esc(command)}</code><button onclick="copyResume(${jsString(command)})">Copy</button></div>`;
}
function requestChanges(runId) {
  const box = document.getElementById(`feedback-${runId}`);
  const feedback = box ? box.value : '';
  return postJson(`/api/run/${runId}/request-changes`, { feedback });
}
function resumeInterrupted(runId) {
  return postJson(`/api/run/${runId}/resume-interrupted`, {});
}
async function interruptRun(runId) {
  if (!confirm('Interrupt this running Codex job? It will be marked interrupted and can be resumed.')) return;
  try {
    const res = await fetch(`/api/run/${runId}/interrupt`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok) { alert(result.message || 'Could not interrupt the run'); return; }
    if (!result.started) { alert(result.message || 'Run could not be interrupted'); return; }
  } catch (error) {
    alert(error.message || String(error));
    return;
  }
  await refresh();
}
function prStatus(run) {
  if (!run.pr_url) return '';
  const status = run.pr_ci_status || 'unknown';
  const labels = {
    pending: 'CI running',
    success: 'CI passed',
    failure: 'CI failed',
    no_ci: 'No CI',
    unknown: 'CI unknown'
  };
  const summary = run.pr_ci_summary ? ` · ${esc(run.pr_ci_summary)}` : '';
  const attempts = Number(run.ci_fix_attempts || 0);
  const fixes = attempts ? ` · fixes ${attempts}/3` : '';
  const label = labels[status] || labels.unknown;
  return `<div class="pr-status pr-status-${esc(status)}"><strong>${esc(label)}</strong><span class="muted">${summary}${fixes}</span></div>`;
}
function aiReviewStatus(run) {
  const status = run.ai_review_status || '';
  const running = String(run.stage || '').startsWith('ai-review') && run.state === 'running';
  if (!status && !running) return '';
  const labels = {
    approved: 'AI review approved',
    changes_requested: 'AI review changes requested',
    blocked: 'AI review blocked'
  };
  const label = running ? 'AI review running' : (labels[status] || 'AI review');
  const summary = run.ai_review_summary ? ` · ${esc(run.ai_review_summary)}` : '';
  const cls = running ? 'running' : esc(status || 'unknown');
  return `<div class="ai-review-status ai-review-status-${cls}"><strong>${esc(label)}</strong><span class="muted">${summary}</span></div>`;
}
function isDependencyWaiting(run) {
  return run.state === 'waiting_dependencies' || (run.state === 'blocked' && run.stage === 'waiting for dependencies');
}
function needsAttention(run) {
  return ['blocked','failed','interrupted','needs_review'].includes(run.state) && !isDependencyWaiting(run);
}
function canEditRunAiSettings(run) {
  return run.state !== 'running';
}
function runAiScope(scope) {
  return String(scope || 'runs').replace(/[^a-zA-Z0-9_-]/g, '-');
}
function runAiDirtyKey(runId, scope) {
  return `${runId}:${runAiScope(scope)}`;
}
function runAiElementId(kind, runId, scope) {
  return `run-ai-${kind}-${runId}-${runAiScope(scope)}`;
}
function runAiCardId(runId, scope) {
  return `run-card-${runId}-${runAiScope(scope)}`;
}
function markRunAiDirty(runId, scope) {
  dirtyRunAiScopes.add(runAiDirtyKey(runId, scope));
}
function clearRunAiDirty(runId, scope) {
  dirtyRunAiScopes.delete(runAiDirtyKey(runId, scope));
}
function hasDirtyRunAiInScope(scope) {
  const suffix = `:${runAiScope(scope)}`;
  return Array.from(dirtyRunAiScopes).some(key => key.endsWith(suffix));
}
function runsRenderedInScope(state, scope) {
  if (runAiScope(scope) === 'attention') {
    return (state.runs || []).filter(run => needsAttention(run)).slice(0, 8);
  }
  const path = selectedProjectPath();
  if (!path) return [];
  return (state.runs || []).filter(run => run.project_path === path).slice(0, 24);
}
function reconcileRunAiDirtyScope(state, scope) {
  const safeScope = runAiScope(scope);
  const suffix = `:${safeScope}`;
  const visible = new Map(
    runsRenderedInScope(state, safeScope).map(run => [String(run.id), run])
  );
  Array.from(dirtyRunAiScopes).forEach(key => {
    if (!key.endsWith(suffix)) return;
    const runId = key.slice(0, -suffix.length);
    const run = visible.get(runId);
    if (!run || !canEditRunAiSettings(run)) {
      dirtyRunAiScopes.delete(key);
    }
  });
}
function aiSettingsHtml(run, scope = 'runs') {
  const state = latestState || { ai_models: [] };
  const disabled = canEditRunAiSettings(run) ? '' : 'disabled';
  const model = run.ai_model || 'gpt-5.5';
  const effort = run.ai_reasoning_effort || 'xhigh';
  const safeScope = runAiScope(scope);
  const modelId = runAiElementId('model', run.id, safeScope);
  const modelOptionsId = runAiElementId('model-options', run.id, safeScope);
  const reasoningId = runAiElementId('reasoning', run.id, safeScope);
  const reasoningOptionsId = runAiElementId('reasoning-options', run.id, safeScope);
  const dirty = canEditRunAiSettings(run)
    ? `oninput="markRunAiDirty(${run.id}, ${jsString(safeScope)})" onchange="onRunModelChange(${run.id}, ${jsString(safeScope)})"`
    : '';
  const effortDirty = canEditRunAiSettings(run)
    ? `oninput="markRunAiDirty(${run.id}, ${jsString(safeScope)})" onchange="markRunAiDirty(${run.id}, ${jsString(safeScope)})"`
    : '';
  return `<div class="ai-settings">
    <span>AI</span>
    <input id="${modelId}" list="${modelOptionsId}" value="${esc(model)}" ${disabled} ${dirty}>
    <datalist id="${modelOptionsId}">${modelOptionsHtml(state)}</datalist>
    <input id="${reasoningId}" list="${reasoningOptionsId}" value="${esc(effort)}" ${disabled} ${effortDirty}>
    <datalist id="${reasoningOptionsId}">${reasoningOptionsHtml(state, model)}</datalist>
    ${canEditRunAiSettings(run) ? `<button onclick="saveRunAiSettings(${run.id}, ${jsString(safeScope)})">Save</button>` : '<span class="muted">running</span>'}
  </div>`;
}
function onRunModelChange(runId, scope = 'runs') {
  const state = latestState || { ai_models: [] };
  const modelInput = document.getElementById(runAiElementId('model', runId, scope));
  const reasoningInput = document.getElementById(runAiElementId('reasoning', runId, scope));
  const options = document.getElementById(runAiElementId('reasoning-options', runId, scope));
  const effort = reasoningValueForModelChange(state, modelInput.value, reasoningInput.value || 'xhigh');
  options.innerHTML = reasoningOptionsHtml(state, modelInput.value);
  reasoningInput.value = effort;
  markRunAiDirty(runId, scope);
}
async function saveRunAiSettings(runId, scope = 'runs') {
  const model = document.getElementById(runAiElementId('model', runId, scope)).value;
  const effort = document.getElementById(runAiElementId('reasoning', runId, scope)).value;
  try {
    const res = await fetch(`/api/run/${runId}/ai-settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ai_model: model,
        ai_reasoning_effort: effort
      })
    });
    if (!res.ok) throw new Error(await res.text());
    clearRunAiDirty(runId, scope);
    await refresh();
  } catch (error) {
    markRunAiDirty(runId, scope);
    throw error;
  }
}
function runActions(run) {
  if (run.state === 'running') {
    return `<div class="run-actions">
      <button onclick="interruptRun(${run.id})">Interrupt</button>
    </div>`;
  }
  if (run.state === 'ready') {
    return `<div class="run-actions">
      <button class="primary" onclick="action('/api/run/${run.id}/start')">Run</button>
      <button onclick="addDependencyEdge(${run.issue_number}, ${jsString(run.repo_name)})">Add dependency</button>
      <button onclick="removeIssue(${run.issue_number}, ${jsString(run.repo_name)})">Remove</button>
    </div>`;
  }
  if (isDependencyWaiting(run)) {
    return `<div class="run-actions">
      <button onclick="addDependencyEdge(${run.issue_number}, ${jsString(run.repo_name)})">Add dependency</button>
      <button onclick="removeIssue(${run.issue_number}, ${jsString(run.repo_name)})">Remove</button>
    </div>`;
  }
  if (run.state === 'pr_open') {
    return `<textarea id="feedback-${run.id}" class="feedback-box" placeholder="Review feedback"></textarea>
      <div class="run-actions">
        <button onclick="requestChanges(${run.id})">Request changes</button>
        <button class="primary" onclick="action('/api/run/${run.id}/approve-finish')">Approve & finish</button>
      </div>`;
  }
  if (run.state === 'failed') {
    return `<div class="run-actions">
      <button onclick="removeIssue(${run.issue_number}, ${jsString(run.repo_name)})">Remove</button>
    </div>`;
  }
  if (run.state === 'interrupted') {
    const remove = `<button onclick="removeIssue(${run.issue_number}, ${jsString(run.repo_name)})">Remove</button>`;
    if (run.resume_available) {
      return `<div class="run-actions">
        <button class="primary" onclick="resumeInterrupted(${run.id})">Resume</button>
        ${remove}
      </div>`;
    }
    return `<div class="muted">Resume unavailable: ${esc(run.resume_unavailable_reason || 'missing resume metadata')}</div>
      <div class="run-actions">${remove}</div>`;
  }
  return '';
}
function runHtml(run, scope = 'runs') {
  const safeScope = runAiScope(scope);
  return `<div class="run" id="${runAiCardId(run.id, safeScope)}">
    <strong>#${run.issue_number} ${esc(run.issue_title)}</strong>
    <div class="muted">${esc(run.repo_name)} · ${esc(run.branch_name)}</div>
    <div>State: <span class="state-${esc(run.state)}">${esc(run.state)}</span></div>
    <div>Stage: ${esc(run.stage)}</div>
    ${aiSettingsHtml(run, safeScope)}
    ${dependencyEdgesHtml(run)}
    ${blockedDependenciesHtml(run)}
    ${dependencyOverridesHtml(run)}
    ${run.pr_url ? `<div><a href="${esc(run.pr_url)}">Pull request</a></div>` : ''}
    ${prStatus(run)}
    ${aiReviewStatus(run)}
    ${resumeCommand(run)}
    ${runActions(run)}
    ${logLinks(run)}
  </div>`;
}
function runWithDirtyAiValues(run, scope) {
  const modelInput = document.getElementById(runAiElementId('model', run.id, scope));
  const reasoningInput = document.getElementById(runAiElementId('reasoning', run.id, scope));
  return {
    ...run,
    ai_model: modelInput ? modelInput.value : run.ai_model,
    ai_reasoning_effort: reasoningInput ? reasoningInput.value : run.ai_reasoning_effort
  };
}
function renderRunCard(run, scope = 'runs') {
  const safeScope = runAiScope(scope);
  if (dirtyRunAiScopes.has(runAiDirtyKey(run.id, safeScope))) {
    return runHtml(runWithDirtyAiValues(run, safeScope), safeScope);
  }
  return runHtml(run, safeScope);
}
function renderRunCards(runs, scope, emptyHtml) {
  return runs.map(run => renderRunCard(run, scope)).join('') || emptyHtml;
}
function stateCounts(runs) {
  return runs.reduce((counts, run) => {
    counts[run.state] = (counts[run.state] || 0) + 1;
    return counts;
  }, {});
}
function stateSummary(runs) {
  const counts = stateCounts(runs);
  return Object.entries(counts).sort().map(([key, value]) => `${value} ${key}`).join(' · ') || 'nothing queued';
}
function projectHtml(project, state) {
  const runs = state.runs.filter(run => run.project_path === project.path);
  return `<button class="project-row" data-path="${esc(project.path)}" onclick="selectProjectByPath(this)">
    <strong>${esc(project.name)}</strong>
    <div class="muted">${esc(project.path)}</div>
    <div>${esc(stateSummary(runs))}</div>
  </button>`;
}
function renderProjectIndex(state) {
  const projects = state.projects || [];
  document.getElementById('runs-title').textContent = 'Tasks';
  document.getElementById('project-back').style.display = 'none';
  return projects.map(project => projectHtml(project, state)).join('') || '<div class="muted">No project folders</div>';
}
function renderSelectedProject(state, path) {
  const project = (state.projects || []).find(item => item.path === path);
  const runs = state.runs.filter(run => run.project_path === path);
  document.getElementById('runs-title').textContent = project ? project.name : 'Tasks';
  document.getElementById('project-back').style.display = '';
  return renderRunCards(runs.slice(0, 24), 'runs', '<div class="muted">No tasks in this folder</div>');
}
function renderRuns(state) {
  const path = selectedProjectPath();
  return path ? renderSelectedProject(state, path) : renderProjectIndex(state);
}
async function refresh() {
  const state = await fetchState();
  latestState = state;
  const stats = state.stats || {};
  document.getElementById('health').textContent = `${state.scheduler.paused ? 'Paused' : 'Active'} · ${Object.values(stats).reduce((a,b) => a + b, 0)} runs tracked`;
  renderSettings(state);
  renderIssueTools(state);
  document.getElementById('stats').innerHTML = Object.entries(stats).sort().map(([key, value]) =>
    `<div class="metric-row"><span>${esc(key)}</span><strong>${value}</strong></div>`
  ).join('') || '<div class="muted">No runs yet</div>';
  reconcileRunAiDirtyScope(state, 'runs');
  document.getElementById('runs').innerHTML = renderRuns(state);
  reconcileRunAiDirtyScope(state, 'attention');
  document.getElementById('attention').innerHTML = renderRunCards(
    state.runs.filter(run => needsAttention(run)).slice(0, 8),
    'attention',
    '<div class="muted">Nothing needs you</div>'
  );
  document.getElementById('events').innerHTML = state.events.slice(0, 20).map(event =>
    `<div class="event ${esc(event.level)}">
      <div><strong>${esc(event.message)}</strong></div>
      <div class="muted">${esc(event.repo_name)} #${event.issue_number} · ${esc(event.created_at)}</div>
    </div>`
  ).join('') || '<div class="muted">No events</div>';
}
refresh();
window.addEventListener('hashchange', refresh);
setInterval(refresh, 2000);
