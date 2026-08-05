# Design QA — UI v4 refactor

## Comparison inputs

- Selected visual target: `docs/ui-audit/selected-visual-target.png`
- Historical dark baseline: `docs/ui-audit/before-dark-inventory.png`
- Current implementation screenshot: pending

## Static and automated verification

- One light enterprise-console token set is loaded from `frontend/src/styles.css`.
- The obsolete `frontend/src/v3.css` override layer has been removed.
- Configuration, inventory, preflight, difference plan, run controls, queues and logs share the same panel, spacing, status and button system.
- Desktop breakpoints are defined for 1380 px, 1180 px, 820 px and 560 px.
- TypeScript checking, 13 frontend tests, production build and backend test suite pass.
- Run progress and upload-call metrics now share the top task-status container, preserving the existing information and controls while reducing vertical fragmentation.
- Runtime status polling remains active for live feedback; successful fast localhost polls are excluded from operator logs without removing metrics or error evidence.

## Browser visual verification

- Intended viewports: 1920×1080, 1366×768 and 1024×768.
- Required checks: clipping, horizontal overflow, fixed navigation overlap, long-path truncation, form alignment, table density, queue/log scrolling and browser console errors.
- Current blocker: the in-app browser connection failed before a tab could be created (`failed to write kernel assets: 系统找不到指定的路径`). The local demo server itself responds with HTTP 200.
- Historical screenshots are not accepted as proof for the current stylesheet.

## Final result

`blocked` — automated regression is green, but current-version browser screenshots and same-viewport comparison are still required before visual QA can be marked passed.
