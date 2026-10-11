# Personal Home and hourly review

Home is the initial authenticated route (`/home`). Layouts, selected sources,
background, transparency and an optional theme belong to the signed-in user.
The revision on `PUT /api/v1/home/layout` prevents one device from overwriting
newer changes from another. Panels can be reordered, resized, removed and pinned
beside desktop chats. Mobile uses Home for the same saved panels.

## Enable review

In Home, open **Hourly review**, choose an agent and explicitly select chats,
projects, automations or connected mail accounts, then enable review. Automatic
analysis is off initially. Quiet hours suppress notices, not checks. The global
pause stops all automatic Home analysis, including custom panel refreshes.
User-requested panel previews remain available while paused.

The cloud checks due users every minute; each user's normal check is scheduled
60 minutes apart. It compares selected source content and known deadline states.
Unchanged inputs reuse saved results without a model call. A disconnected source
keeps the last result and retries availability; reconnecting consolidates the
current state without replaying missed hours. Custom panels support hourly,
daily and weekly cadence. If several selected agents need work, the oldest
result's agent is served first and other work remains queued.

There is at most one scheduled model analysis per user per hour. A durable
owner lease prevents overlapping previews or scheduled work. Each whole check
has an eight-minute bound below the ten-minute lease, and the model admission
time is committed before the connector RPC. Explicit previews are separately
rate limited to one per minute and also postpone scheduled analysis for an hour.
A process restart cannot replay a billed scheduled call within the same hour.

## Restricted analysis and prerequisites

The chosen computer must be connected and advertise `insights.generate` and
`connector.toolFreeInsightsV1`. Older connectors continue normal conversations
and display an update requirement. Publish the signed standalone/managed runtime
with this capability using the existing connector release procedures. Home
never updates another user's computer or changes ordinary conversation models.

Analysis uses only `gpt-6-luna`, low reasoning and the selected profile's OpenAI
API or Codex subscription authentication. Unsupported providers or unavailable
Luna suspend analysis with an error. **Retry** explicitly clears that suspension
but preserves the hourly admission gate. No fallback model is allowed.

The connector runs a fixed subprocess with bounded data and a strict structured
response. It does not construct an AIAgent, load a tool registry or expose tools.
The Responses request has an empty tools list, `tool_choice: none`, disabled
storage and no SDK retry. Tool outputs and unexpected models are rejected.
Credentials remain on the computer. Source reads use read-only history, mail
search and public HTTPS GET; source text cannot invoke tools. Public sources
validate and pin global DNS addresses, reject redirects and are limited to
256 KiB, without credentials, cookies or subresource loads.

A check reads at most 40 selected recent chats, their last 12 text messages,
the latest selected automation run, and 8 messages per selected mail account.
Projects expand to those recent owned chats. Evidence is bounded to 1,800
characters per source and a shared input budget (64 KB on the wire). Large
projects therefore show a recent overview rather than a complete archive.
Custom panel data is rendered with the app's summary, list, metric, table and
chart components; generated code is never executed.

## Suggestions and notices

The first version groups follow-up work per source conversation/mail thread or
automation run and backend-owned action. Wording changes update that same card.
Up to five suggestions are shown, with exact retrieved evidence and proposed
drafts. **Continue in chat** pre-fills an editable draft and does not send it.
External actions require the user's instruction in that chat.

Daily receipts are stored per owner, suggestion, local date and notice kind.
One regular notice is allowed each day, only for newly relevant/changed work.
Changing the calendar day alone does not generate a model call or notification.
One additional urgent notice is allowed after at least an hour when a verified
explicit deadline enters its final two hours. This version verifies exact ISO
date/time evidence (including a supplied timezone or the configured user zone),
and rejects ambiguous daylight-saving times. Unverified natural-language dates
cannot authorize urgency. Discarded/resolved items remain suppressed; **Tomorrow**
snoozes until 09:00 in the user's timezone. Source removal hides its suggestions.

Encrypted snapshots, prepared work and evidence are stored in Home tables;
daily receipts contain IDs, dates and kinds. Notification receipts are committed
before push delivery, favoring no duplicate notifications after crashes. Home's
unread badge still exposes results when browser push is unavailable.

## Operations and validation

Migration `0033_personal_home` adds layouts, snapshots, suggestions and review
history; `0034_home_notices` adds daily receipts and source-check timestamps.
Back up and rehearse both using `deploy/cloud/release.sh`. The worker appears as
`homeReview` in readiness. Deployment drain refuses an in-flight Home analysis.
Never force a restart around that check; follow the existing release runbook.

Focused tests live in `tests/backend/test_home*.py`,
`tests/connector/test_insights.py` and `apps/web/tests/e2e/home.spec.ts`.
They cover ownership, layout conflicts, safe previews, hourly admission,
restarts, unchanged sources, daily and timezone policy, urgency, source failures,
model failure, forbidden tools, and mobile/desktop interactions. Every release
also requires the full test/build and public PWA verification gates.
