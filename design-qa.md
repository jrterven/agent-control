# Desktop workspace and context controls — design QA

- Reference source: `design-qa/workspace-context-reference.png` (user-provided desktop-browser screenshot).
- Implementation screenshot: `design-qa/workspace-context-desktop-closed.png`.
- Open selector state: `design-qa/workspace-switcher-open.png`.
- Collapsed context state: `design-qa/context-panel-collapsed.png`.
- Mobile selector state: `design-qa/workspace-switcher-mobile.png`.
- Side-by-side evidence: `design-qa/workspace-context-comparison.png` (reference left, implementation right).
- Verification viewports: 1643 × 651 desktop, 1000 × 700 tablet, and 424 × 800 mobile.

## Comparison

The implementation preserves the existing Agent Control visual system and the
reference control placement: the workspace control remains in the top-right
toolbar immediately before notifications and the context-panel toggle. The new
workspace menu uses the product's existing dark surfaces, cyan focus treatment,
Phosphor folder/check icons, workspace descriptions, counts, and a constrained
scroll region rather than introducing a new visual language.

The desktop context toggle now removes the full 320 px context column and lets
the conversation expand into the freed space. Activating it again restores the
same panel and width without shifting the left navigation. At tablet width the
same control opens the existing overlay panel instead of changing the saved
desktop preference. On mobile, the workspace menu remains inside the viewport,
keeps practical tap targets, and shows all four QA workspaces without clipping.

## Functional and accessibility checks

- Selecting a workspace updates both the toolbar and the active session.
- Selecting “Sin espacio de trabajo” uses the normalized empty workspace id.
- Arrow keys, Home, End, Escape, outside click, focus restoration, menu roles,
  checked state, and accessible labels are covered by automated tests.
- The hidden desktop panel is removed from assistive navigation with
  `aria-hidden` and `inert`.
- The browser console contained no errors after desktop, tablet, and mobile
  interaction passes.
- No visible overlap, clipping, typography regression, icon mismatch, or
  off-token color was found in the final comparison pass.

final result: passed
