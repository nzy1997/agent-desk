const FILE_URL = "__FILE_URL__";
const view = document.getElementById('view');
const live = document.getElementById('live');
let last = null;
function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
// Strip ANSI/control sequences so terminal output stays clean.
function clean(s) {
  return String(s).replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\r(?!\n)/g, '');
}
function looksJson(s) {
  const t = s.trim();
  return (t[0] === '{' && t[t.length - 1] === '}') || (t[0] === '[' && t[t.length - 1] === ']');
}
// Render an agent_message: unwrap the structured {summary,...} envelope when present.
function renderMessage(text) {
  let body = text, meta = '';
  if (looksJson(text)) {
    try {
      const o = JSON.parse(text);
      if (o && typeof o === 'object' && !Array.isArray(o)) {
        body = o.summary || o.text || o.message || text;
        const bits = [];
        for (const f of ['status', 'pr_url']) if (o[f]) bits.push(f + '=' + o[f]);
        for (const f of ['tests', 'questions', 'risks']) {
          if (Array.isArray(o[f]) && o[f].length) bits.push(o[f].length + ' ' + f);
        }
        if (bits.length) meta = '  (' + bits.join(', ') + ')';
      }
    } catch (e) { /* fall through to raw text */ }
  }
  return `<div class="blk msg"><span class="who">assistant</span> ${esc(body)}`
    + (meta ? `<span class="meta">${esc(meta)}</span>` : '') + `</div>`;
}
function renderCommand(it) {
  const running = it.status === 'in_progress' || it.exit_code == null;
  const out = clean(it.aggregated_output || '');
  let foot = '';
  if (running) foot = `<div class="out running">… running</div>`;
  else if (it.exit_code) foot = `<div class="out fail">exited ${esc(it.exit_code)}</div>`;
  const body = out ? `<div class="out">${esc(out)}</div>` : '';
  return `<div class="blk cmd"><div class="prompt"><span class="sym">❯</span>${esc(it.command || '')}</div>`
    + body + foot + `</div>`;
}
function renderFileChange(it) {
  const sym = { add: '+', delete: '-', remove: '-', update: '~', modify: '~' };
  const cls = { add: 'add', delete: 'del', remove: 'del', update: 'mod', modify: 'mod' };
  const rows = (it.changes || []).map(c => {
    const k = (c.kind || 'update').toLowerCase();
    return `<span class="${cls[k] || 'mod'}">${sym[k] || '~'} ${esc(c.path || '')}</span>`;
  }).join('\n');
  return `<div class="blk files"><span class="who">edit</span>\n${rows}</div>`;
}
function renderTool(it) {
  const prompt = it.prompt ? clean(it.prompt).trim() : '';
  const arg = prompt ? `<div class="arg">${esc(prompt)}</div>` : '';
  return `<div class="blk tool"><span class="who">→ ${esc(it.tool || 'tool')}</span>${arg}</div>`;
}
function renderItem(it) {
  switch (it.type) {
    case 'agent_message': return renderMessage(it.text || '');
    case 'reasoning': return `<div class="blk think">${esc(clean(it.text || it.summary || ''))}</div>`;
    case 'command_execution': return renderCommand(it);
    case 'file_change': return renderFileChange(it);
    case 'collab_tool_call': return renderTool(it);
    default: {
      const rest = {...it}; delete rest.id; delete rest.type;
      return `<div class="blk raw">${esc(it.type || 'item')}: ${esc(JSON.stringify(rest))}</div>`;
    }
  }
}
function render(raw) {
  const atBottom = Math.abs(view.scrollHeight - view.clientHeight - view.scrollTop) < 40;
  // Merge item.started/item.completed by id so each item renders once, in
  // first-seen order; the latest state (usually completed) wins.
  const order = [];
  const items = new Map();
  const blocks = [];
  for (let line of raw.split('\n')) {
    line = line.trim();
    if (!line) continue;
    let o;
    try { o = JSON.parse(line); } catch (e) { blocks.push({ raw: `<div class="blk raw">${esc(line)}</div>` }); continue; }
    const type = o.type || (o.msg && o.msg.type) || 'event';
    if ((type === 'item.started' || type === 'item.completed' || type === 'item.updated') && o.item) {
      const id = o.item.id != null ? o.item.id : ('_' + order.length);
      if (!items.has(id)) { order.push(id); items.set(id, { idx: blocks.length }); blocks.push(null); }
      const slot = items.get(id);
      blocks[slot.idx] = { item: o.item };
    } else if (type === 'thread.started' || type === 'turn.started' || type === 'turn.completed') {
      blocks.push({ sys: `<div class="blk sys">— ${esc(type.replace('.', ' '))} —</div>` });
    }
    // other event types are intentionally dropped to keep the transcript clean
  }
  const html = blocks.filter(Boolean).map(b =>
    b.item ? renderItem(b.item) : (b.raw || b.sys)
  ).join('');
  view.innerHTML = html || '<div class="blk raw">(empty)</div>';
  if (atBottom) view.scrollTop = view.scrollHeight;
}
async function tick() {
  try {
    const res = await fetch(FILE_URL, { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const raw = await res.text();
    if (raw !== last) { last = raw; render(raw); }
    live.textContent = '● live';
    live.className = 'live';
  } catch (e) {
    live.textContent = '● disconnected';
    live.className = 'live stale';
  }
}
tick();
setInterval(tick, 1500);
