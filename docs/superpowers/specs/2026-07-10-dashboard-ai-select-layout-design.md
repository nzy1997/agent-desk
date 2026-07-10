# Dashboard AI Settings Layout And Select Controls

## Context

The Workspace Settings card in the left sidebar can overflow horizontally when labels
and AI setting inputs compete for the narrow first column. AI model controls also use
text inputs with datalists, which makes the supported model list feel editable by
typing instead of selectable.

## Goals

- Keep the Workspace Settings card aligned inside the sidebar at desktop widths.
- Make supported AI model choices visible as option-style controls.
- Keep existing saved values readable, including legacy or unknown model IDs.
- Preserve the current backend mapping and default behavior: `gpt-5.5` plus `xhigh`.

## Design

Use approach A from the design discussion:

- Increase the dashboard sidebar's desktop max width from about 280px to about 340px.
- Change Workspace Settings rows from flex `space-between` to a two-column grid.
- Let long labels wrap while keeping controls inside the card.
- Give model and reasoning controls stable widths with `minmax(0, 1fr)` behavior so
  they shrink within their grid column instead of overflowing.

For AI model and reasoning controls:

- Replace model text inputs with `<select>` controls in Workspace Settings and task
  cards.
- Populate model choices from the existing `ai_models` catalog sent by `/api/state`.
- Populate reasoning choices from the selected model's supported efforts.
- When the current model or reasoning value is not present in the catalog, include a
  temporary current-value option so existing settings are not hidden or overwritten.
- Keep Save behavior and payloads unchanged: selected strings still post as
  `default_ai_model`, `default_ai_reasoning_effort`, `ai_model`, and
  `ai_reasoning_effort`.

## Testing

- HTML tests should assert Workspace Settings uses the non-overflowing layout hooks and
  that model controls are rendered as selects, not text inputs with datalists.
- JavaScript tests should assert supported model options render in the select.
- JavaScript tests should assert unknown current model and reasoning values remain
  visible as temporary options.
- Existing dirty-refresh tests should continue to pass because the DOM element IDs and
  `.value` reads remain stable.
