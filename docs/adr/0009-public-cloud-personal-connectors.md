# ADR 0009: Public PWA with personal outbound connectors

- Status: accepted
- Date: 2026-09-13

The public beta retains React/PWA and FastAPI. One verified Google identity owns
one personal account, its connectors, gateways and all derived Control resources.
No public account is a platform administrator by default. Google identities bind
to issuer/subject; a matching email never merges an existing local account.

Cloud mode uses PostgreSQL, one API worker and a separate deployment. Private
mode retains SQLite, direct providers, password authentication and Tailscale.
The public deployment now accepts registration without invitations, capped at
20 Google identities including existing accounts. Other deployments default to
`invite_only`; `open` must be configured explicitly. Both policies share the
transactional enrollment limit, and existing active identities can sign in at
capacity. Teams, billing and cloud-hosted runtimes remain outside this beta.

Profile transfer between two computers owned by the same account is supported
when both are connected, both connectors advertise `connector.profileTransferV3`,
and both run audited Hermes 0.21.2 revision `939e45c9…`. Older
connectors must be updated before they can participate. The existing native
lifecycle checks remain mandatory: the `default` profile cannot move, active
work blocks transfer, and the source is removed only after the imported profile
has been verified at the destination. Transfer configuration uses a bounded,
secret-free native YAML snapshot so model routing survives the move;
dashboard-normalized configuration is never written as a replacement document.
Before native export, import or deletion, the connector verifies that the
management server reports `default` as its current base profile. The check is
repeated immediately before deletion; moving the profile that hosts a named
management server could otherwise leave the service bound to deleted data.
Managed launchers explicitly pin `-p default` rather than following the CLI's
saved active profile. External service configuration remains operator-owned.
Confirmed deletions also retire the connector's shared-profile grant, including
during a verified rollback. A surviving shared profile is required to reconcile
deletion even if its response is lost. Identity, configuration, history and
Control route metadata are preserved; inference credentials are not copied.

A native connector runs the existing Hermes adapter on the user's Linux or Mac
host. It keeps Hermes on numeric loopback and establishes the cloud WSS link
outbound. Cloud never obtains a Hermes credential or an arbitrary local URL.
Versioned typed operations carry bounded binary transfers; there is no generic
TCP, HTTP, shell or filesystem proxy. A local profile selection remains
authoritative even if a compromised cloud asks for additional profiles.
Profile archives use typed chunks of at most 1 MiB and a total archive limit of
100 MiB. The transfer channel is restricted to native profile export/import;
it does not expose arbitrary paths on either computer.

Pairing uses an expiring device code, authenticated browser review, a CSRF-bound
confirmation and an individually revocable device credential. Conversations are
processed by the cloud in transit. This is not end-to-end encryption: the host
operator can access processed content and the vault; the privacy UI says so.
Hermes owns the transcript. Existing bounded metadata, voice transcript and email
reference retention remain documented, rather than promising no persistence.

An operation ledger records dispatch uncertainty locally. Losing a reply must
not cause automatic resubmission. Local Hermes connections outlive WAN outages;
bounded event replay and authoritative history recover presentation afterwards.
The frontend remains offline when a user's computer sleeps or loses its network.

Connector releases contain their Python runtime and are signed as four native
archives. Updates stage an immutable version, drain new mutations, verify a
fresh idle inventory and retain the preceding version for rollback. They never
install, update or replace Hermes itself.
