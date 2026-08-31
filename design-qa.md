# Notification inbox design QA

- Reference source: `design-qa/notification-menu-reference.png` (user-provided notification-list screenshot).
- Implementation screenshot: `design-qa/notification-menu-implementation.png`.
- Scrolled state: `design-qa/notification-menu-scrolled.png`.
- Side-by-side evidence: `design-qa/notification-menu-comparison.png`.
- Verification viewport: 424 × 612 CSS px, mobile breakpoint, dark theme.

## Comparison

The implementation preserves the reference hierarchy: muted day headings,
prominent chat names, secondary workspace rows with folder icons, a rounded
current-chat surface, unread dots aligned to the right, and a vertically
scrollable recent list. It intentionally adds the product's own elevated-sheet
header, close affordance, notification-permission footer, cyan accent and
existing navigation behind the modal scrim so it remains consistent with
Agent Control rather than copying the source application's chrome.

The captured state contains exactly ten chats in recency order; only the first
rows fit, and pointer scrolling reveals the remaining dated groups without
moving the fixed permission footer. The bell badge reports three unread chats,
and the browser console contained no errors after opening and scrolling the
menu.

final result: passed
