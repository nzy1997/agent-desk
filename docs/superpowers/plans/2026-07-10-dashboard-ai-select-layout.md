# Dashboard AI Select Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Workspace Settings sidebar overflow and make AI model/reasoning controls selectable from the supported catalog.

**Architecture:** Keep the backend contract unchanged. Update the static dashboard HTML/CSS for a wider, grid-based settings panel and update `dashboard.js` helpers so model/reasoning controls render as `<select>` elements populated from `/api/state.ai_models`, with temporary options for unknown current values.

**Tech Stack:** Python 3.11 stdlib `unittest`, static HTML/CSS, plain JavaScript, no runtime dependencies.

## Global Constraints

- Do not add Python runtime dependencies; `pyproject.toml` dependencies remain empty.
- Defaults stay `gpt-5.5` and `xhigh`.
- Save payload field names stay unchanged: `default_ai_model`, `default_ai_reasoning_effort`, `ai_model`, `ai_reasoning_effort`.
- Existing unknown model or reasoning values must remain visible in controls and saveable.
- Existing dirty-refresh behavior must continue to read `.value` by stable element IDs.

---

### Task 1: Stabilize Workspace Settings Layout

**Files:**
- Modify: `agent_desk/static/dashboard.html`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Existing static `HTML` fixture in `tests/test_dashboard.py`.
- Produces: CSS classes and layout rules later task can rely on: `.settings-panel`, `.setting-row`, `.setting-control`.

- [ ] **Step 1: Write the failing test**

Add assertions to `DashboardTests.test_dashboard_html_renders_workspace_settings_controls`:

```python
self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(112px, 150px)", HTML)
self.assertIn(".setting-row span", HTML)
self.assertIn('class="setting-control"', HTML)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_settings_controls -v
```

Expected: FAIL because the grid columns, label span rule, and `setting-control` class are not present yet.

- [ ] **Step 3: Write minimal implementation**

In `agent_desk/static/dashboard.html`:

- Change the main desktop grid first column to `minmax(260px, 340px)`.
- Change `.setting-row` to:

```css
.setting-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(112px, 150px);
  gap: 10px;
  align-items: center;
  min-height: 30px;
}
.setting-row span {
  min-width: 0;
  overflow-wrap: anywhere;
}
.setting-control {
  min-width: 0;
  width: 100%;
}
```

- Add `class="setting-control"` to the Workspace Settings number, checkbox, model, and reasoning controls.
- Keep checkbox size rules by targeting `.setting-row input[type="checkbox"]`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_settings_controls -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_desk/static/dashboard.html tests/test_dashboard.py
git commit -m "fix: align workspace settings sidebar"
```

---

### Task 2: Render AI Model Controls As Selects

**Files:**
- Modify: `agent_desk/static/dashboard.html`
- Modify: `agent_desk/static/dashboard.js`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `aiCatalog(state)`, `aiOption(state, model)`, `reasoningValueForModelChange(state, model, current)`, stable IDs from `runAiElementId(kind, runId, scope)`.
- Produces: `modelOptionsHtml(state, current) -> string`, `reasoningOptionsHtml(state, model, current) -> string`, and select-based controls that still expose `.value`.

- [ ] **Step 1: Write failing HTML tests**

Update existing dashboard AI control tests:

```python
def test_dashboard_html_renders_workspace_ai_settings_controls(self):
    self.assertIn('<select id="default-ai-model"', HTML)
    self.assertIn('<select id="default-ai-reasoning-effort"', HTML)
    self.assertNotIn('id="default-ai-model" list=', HTML)
    self.assertIn("default_ai_model", HTML)
    self.assertIn("default_ai_reasoning_effort", HTML)

def test_dashboard_model_options_are_select_options(self):
    self.assertNotIn('list="default-ai-model-options"', HTML)
    self.assertNotIn("<datalist", HTML)
    self.assertIn("modelOptionsHtml(state, settings.default_ai_model", HTML)
```

- [ ] **Step 2: Write failing JavaScript tests**

Replace `test_dashboard_ai_controls_accept_custom_model_and_effort_values` with two select-focused tests:

```python
def test_dashboard_ai_controls_render_supported_model_select_options(self):
    run_dashboard_js(
        """
        vm.runInContext(`latestState = {
          ai_models: [{
            id: 'gpt-5.5',
            label: 'GPT-5.5',
            default_reasoning_effort: 'medium',
            reasoning_efforts: ['low', 'medium', 'high', 'xhigh']
          }, {
            id: 'gpt-5.6-terra',
            label: 'GPT-5.6 Terra',
            default_reasoning_effort: 'medium',
            reasoning_efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
          }]
        }`, context);
        const html = vm.runInContext(`aiSettingsHtml({
          id: 42,
          state: 'ready',
          ai_model: 'gpt-5.6-terra',
          ai_reasoning_effort: 'max'
        }, 'runs')`, context);
        assert(html.includes('<select id="run-ai-model-42-runs"'), html);
        assert(html.includes('<option value="gpt-5.6-terra" selected>GPT-5.6 Terra</option>'), html);
        assert(html.includes('<option value="max" selected>max</option>'), html);
        """
    )

def test_dashboard_ai_controls_preserve_unknown_current_select_values(self):
    run_dashboard_js(
        """
        vm.runInContext(`latestState = {
          ai_models: [{
            id: 'gpt-5.5',
            label: 'GPT-5.5',
            default_reasoning_effort: 'medium',
            reasoning_efforts: ['low', 'medium', 'high', 'xhigh']
          }]
        }`, context);
        const html = vm.runInContext(`aiSettingsHtml({
          id: 42,
          state: 'ready',
          ai_model: 'gpt-new-preview',
          ai_reasoning_effort: 'warp'
        }, 'runs')`, context);
        assert(html.includes('<option value="gpt-new-preview" selected>gpt-new-preview</option>'), html);
        assert(html.includes('<option value="warp" selected>warp</option>'), html);
        """
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_ai_settings_controls \
  tests.test_dashboard.DashboardTests.test_dashboard_model_options_are_select_options \
  tests.test_dashboard.DashboardTests.test_dashboard_ai_controls_render_supported_model_select_options \
  tests.test_dashboard.DashboardTests.test_dashboard_ai_controls_preserve_unknown_current_select_values -v
```

Expected: FAIL because controls are still text inputs with datalists.

- [ ] **Step 4: Write minimal implementation**

In `agent_desk/static/dashboard.js`, replace option helpers with selected-aware select option helpers:

```javascript
function optionHtml(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label || value)}</option>`;
}
function modelOptionsHtml(state, current) {
  const seen = new Set();
  const options = aiCatalog(state).map(item => {
    seen.add(item.id);
    return optionHtml(item.id, item.label || item.id, item.id === current);
  });
  if (current && !seen.has(current)) {
    options.unshift(optionHtml(current, current, true));
  }
  return options.join('');
}
function reasoningOptionsHtml(state, model, current) {
  const option = aiOption(state, model);
  const efforts = option ? option.reasoning_efforts || [] : ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  const seen = new Set();
  const options = efforts.map(effort => {
    seen.add(effort);
    return optionHtml(effort, effort, effort === current);
  });
  if (current && !seen.has(current)) {
    options.unshift(optionHtml(current, current, true));
  }
  return options.join('');
}
```

Update Workspace Settings in `dashboard.html`:

```html
<select id="default-ai-model" class="setting-control" onchange="onWorkspaceModelChange(); markSettingsDirty()"></select>
<select id="default-ai-reasoning-effort" class="setting-control" onchange="markSettingsDirty()"></select>
```

Update `renderSettings()`:

```javascript
const model = settings.default_ai_model || 'gpt-5.5';
const effort = settings.default_ai_reasoning_effort || 'xhigh';
document.getElementById('default-ai-model').innerHTML = modelOptionsHtml(state, model);
document.getElementById('default-ai-model').value = model;
document.getElementById('default-ai-reasoning-effort').innerHTML = reasoningOptionsHtml(state, model, effort);
document.getElementById('default-ai-reasoning-effort').value = effort;
```

Update `onWorkspaceModelChange()` and `onRunModelChange()` so reasoning selects call:

```javascript
reasoningInput.innerHTML = reasoningOptionsHtml(state, model, effort);
reasoningInput.value = effort;
```

Update `aiSettingsHtml()` model/reasoning controls:

```javascript
<select id="${modelId}" ${disabled} ${dirty}>${modelOptionsHtml(state, model)}</select>
<select id="${reasoningId}" ${disabled} ${effortDirty}>${reasoningOptionsHtml(state, model, effort)}</select>
```

- [ ] **Step 5: Run focused tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_dashboard -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_desk/static/dashboard.html agent_desk/static/dashboard.js tests/test_dashboard.py
git commit -m "fix: use select controls for ai settings"
```

---

## Final Verification

Run:

```bash
make test
git diff --check main..HEAD
```

Expected: `make test` passes with all tests OK, and `git diff --check` prints no whitespace errors.
