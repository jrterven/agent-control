# Design QA — Hermes Control mobile option 2

## Evidence

- Source visual truth: `/Users/juanterven/.codex/generated_images/01a0486d-8431-7f90-9bb1-2793844c19ce/exec-6386d030-9c92-464b-a829-4d4808088745.png`
- Source pixels: 853 × 1844, normalized to 393 × 852 for comparison.
- Browser-rendered implementation: `implementation-browser-final-v2.jpg` at 1400 × 1200.
- App viewport crop: `implementation-mobile-screen-final-v2.png` at 392 × 852 (the JPEG 4:2:0 crop rounds the odd width down by one pixel).
- Combined comparison evidence: `design-comparison-final-v2.png`.
- CSS phone-screen size: 393 × 852; deviceScaleFactor 1; in-app Browser viewport 1400 × 1200.
- State: iPhone, dark theme, Newton connected, “Revisión semanal de papers”, tools collapsed, empty composer.

## Full-view comparison

The final comparison preserves the source hierarchy and density: compact agent header, workspace/date row, right-aligned user prompt, author-led assistant answer, four visible research bullets, folded tools, fixed composer and four-item bottom navigation. The template-owned iPhone status bar, bezel and home indicator intentionally add device chrome absent from the source image.

## Focused surfaces

- **Fonts and typography:** system sans-serif matches the source character and optical density. The final pass reduces body copy to 12–12.5px and the result title to 16px so the same four result bullets remain visible above the composer. Weight, wrapping and hierarchy now track the reference.
- **Spacing and layout rhythm:** header, content, composer and bottom navigation align at the same relative anchors after accounting for protected status-bar chrome. Message padding, list gaps and assistant rhythm reproduce the source density. Touch targets remain at least 44px where interactive.
- **Colors and tokens:** near-black blue background, blue accent, slate surfaces, subtle borders, green connected state and white/muted text match the source. Contrast remains readable without introducing source-incompatible gradients.
- **Image and icon fidelity:** the screen contains no raster content assets. All interface symbols use the bundled Radix icon library; device bezel/status/home assets come from the protected mobile runtime. No handcrafted SVG, CSS illustration, emoji or placeholder asset is used.
- **Copy and content:** visible Spanish copy, workspace name, date, prompt, response heading, four findings, tool count, composer label and bottom navigation match the selected design.

Focused region crops were not needed beyond the normalized full-height side-by-side image: all text and controls are legible at 1:1 in `design-comparison-final-v2.png`.

## Interaction and browser verification

- Opened and dismissed the navigation sheet; Newton, Jarvis, `control-dev` and workspaces were visible.
- Opened and dismissed the activity sheet; connection, context, tools, session and stop/configuration actions were visible.
- Expanded the two tool rows.
- Focused the keyboard-aware composer, entered a message, sent it and confirmed the new bubble.
- Confirmed the runtime integrity check passes and inspected browser console errors: none.

## Comparison history

1. **P2 — insufficient contrast and content hidden too early.** The first render inherited muted paragraph colors and placed the composer too high. Fixed by setting explicit body colors, lowering the composer and sizing the bottom navigation within its safe-area box.
2. **P2 — response density diverged from the source.** The second render showed only one or two findings above the composer. Fixed by reducing message padding, author avatar, body/title size and list spacing. The final render shows all four findings and the folded tool row, matching the source information density.
3. Post-fix evidence: `design-comparison-final-v2.png` shows no actionable P0/P1/P2 difference. Template-owned device chrome and the library-specific agent glyph are acceptable P3 deviations.

## Follow-up polish

- P3: replace the generic Radix token glyph only if an official Hermes brand asset becomes available.

final result: passed
