# Mail plugins

Users manage mail in **Settings → Plugins → Email** (`/settings#plugins`).
Each account has a label, status and explicit agent assignments. Several accounts
from the same provider are supported. Reconnecting verifies the same provider
identity and retains grants; connecting the same mailbox again updates its credentials.
After OAuth, choose **Name and agents**, save, wait for the computer to connect,
then use **Open conversation**. No active session is reloaded.

The first release supports text search, reading and sending. IMAP searches INBOX
with read-only selection and BODY.PEEK; it does not mark messages read. Drafts
remain in the chat. Sending requires an explicit user instruction and an
unambiguous sender/recipient. Attachments, calendar, inbox organization and
importing credentials from existing Hermes integrations are outside this release.

## Provider activation

Use HTTPS `HERMES_CONTROL_PUBLIC_BASE_URL`, the existing vault key, and the mail
settings in `deploy/cloud/cloud.env.example`. Gmail/Outlook buttons remain
disabled unless their flag, client ID and client secret are configured. These
credentials are independent of platform Google login. Do not reuse or expand
the platform login consent flow. A deployment restart applies configuration.

- Gmail: create a web OAuth client, enable Gmail API, and register
  `https://YOUR_HOST/api/v1/mail/oauth/gmail/callback`. Delegated scopes are
  `openid email gmail.readonly gmail.send` (the latter two use their full Google
  URLs). Configure the consent screen and test users first. Public availability
  must satisfy applicable restricted-scope verification and security assessment
  requirements before setting `MAIL_GMAIL_ENABLED=true` for general access.
  [Google scope classification](https://developers.google.com/workspace/gmail/api/auth/scopes)
  and [verification requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).
- Outlook: register a confidential web application supporting the intended
  Microsoft work/school and personal accounts, with redirect
  `https://YOUR_HOST/api/v1/mail/oauth/outlook/callback`. Delegated scopes are
  `openid email offline_access User.Read Mail.Read Mail.Send`. Tenant policies
  may require administrator consent. Validate consent, refresh and sending for
  the supported tenants before enabling the flag.
  [Microsoft sendMail contract](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0).
- Hostinger Email: `imap.hostinger.com:993`, `smtp.hostinger.com:465`.
  Titan: `imap.titan.email:993`, `smtp.titan.email:465` or STARTTLS on 587.
  [Hostinger settings](https://www.hostinger.com/support/1575756-how-to-get-email-account-configuration-details-for-hostinger-email/),
  [Titan settings](https://support.titan.email/hc/en-us/articles/900000573066-How-to-configure-IMAP-for-Android).
- Other email: public IMAP host on 993 and SMTP on 465 or 587. Credentials are
  submitted only to the configured TLS endpoints. Private, loopback, multicast
  and reserved destinations are rejected, with DNS pinned before connection.
  A password or app password and provider-side IMAP/SMTP access are required.
  **Test connection** authenticates both protocols without sending a message.

An operator can disable all IMAP/SMTP connections with `MAIL_IMAP_ENABLED=false`.
Before enabling OAuth, validate two accounts, renewal, revoked consent,
wrong-account reconnect, cancellation, and a message to an authorized test inbox.
No live provider credentials are included in repository tests.

## Hermes compatibility and deployment

Supported native baseline: Hermes **0.21.2** at
`939e45c91d751fadd94dcd1b873ac3cb44846213`. No Hermes source changes are required.
Agent Control creates one `agent_control_mail_<uuid>` MCP entry per authorized
profile through native create/list/test/delete APIs. Bearer tokens use Hermes's
native profile `.env` indirection; provider credentials stay in Agent Control.
Existing MCP entries, skills and account configuration are preserved. Only the
owned entry and its exact native secret key are removed after its last grant.
Permissions and connector revocation are checked on every MCP operation.
Offline computers retain pending assignments and are retried automatically.

Hermes must have its native `mcp` optional dependency installed. The managed
builder now exports that extra from Hermes's frozen lock and smoke-imports it.
Older managed bundles lacking MCP require a newly signed managed runtime;
never install packages into an immutable signed runtime. Publish that runtime
using `deploy/managed/README.md`. External Hermes installations use Hermes's
normal dependency installation process. A failed native MCP probe keeps the
assignment pending and never claims it is ready.

The opt-in native test uses disposable profiles, actual native MCP APIs,
discovery and tool calls to this API; it verifies separate account lists and
preservation of existing configuration:

```sh
HERMES_MAIL_TEST_RUNTIME=/absolute/agent-control-runtime \
  .venv/bin/python -m pytest tests/backend/test_mail_native.py -q
```

`HERMES_MAIL_TEST_SDK` optionally supplies the exact pinned MCP dependencies for
testing an older bundle without modifying it. The native test was validated
against the pin above with MCP 2.0.0 and httpx2 2.7.0. The JSON MCP transport
negotiates protocol 2025-06-18 with that client.

Deploy migration `0028_mail_plugins` through the cloud release runbook: backup,
restore and migration rehearsal, fresh idle checks, immutable image, readiness
and old/new public PWA assets. HTTP mutations, OAuth callbacks and background
MCP provisioning participate in the release drain. Existing rows are preserved.
The current deployment uses one API worker; account refresh locks are local to
that worker. Distributed worker support needs database/advisory refresh locks.

## Data and failures

Credentials and PKCE verifiers use the existing AES-GCM vault with record-bound
AAD. APIs, audit events and logs do not return credentials. OAuth flows are
single-use, expire in ten minutes and are bound to the original login session
and an HttpOnly browser cookie. Refresh tokens rotate in the encrypted record.
There is a limit of 32 accounts per user and bounded concurrent/rate-limited
provider work. Reads and provider HTTP responses have size limits.

The service does not persist mailbox bodies. Requested text passes through the
agent and can enter existing conversation/cache retention. Audit records contain
actions and IDs only. Send receipts retain operation ID, payload hash and status,
without recipients or message content, until account deletion. Deleted live
records can remain in retained encrypted database backups under the cloud policy.

Each send commits an operation receipt before contacting the provider. Reusing
the same operation ID returns its existing status; changing the payload with
that ID is rejected. A timeout or partial SMTP acceptance is `delivery_unknown`:
the agent must tell the user to inspect sent mail and must not generate a new
operation ID to retry automatically. `accepted` means provider acceptance,
not confirmed recipient delivery. Sending authorization is conveyed to the
model in the tool contract; the service also requires `userRequestedSend=true`.

Disconnect deletes the encrypted account and its grants immediately. Native
configuration cleanup follows when the computer is online. Users can additionally
revoke Google/Microsoft consent in their provider's account settings. Existing
Hermes mail integrations are not imported, modified or revoked by this feature.
