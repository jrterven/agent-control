"""Exact, reviewed Hermes contracts shared by the adapter and Control API.

An operator-supplied SHA is required; upstream version strings alone never
authorize writes. Archive compatibility is directional and narrower than RPC.
"""

from dataclasses import dataclass


HERMES_0212_SHA = "939e45c91d751fadd94dcd1b873ac3cb44846213"
HERMES_0206_SHA = "4209d371aa1bb8840ce8447555bdd863a1a96c38"
_SESSION_METHODS = frozenset({
    "session.create", "session.resume", "session.status", "session.history",
    "prompt.submit", "session.interrupt", "approval.respond", "clarify.respond",
    "session.events.since", "session.delete",
})
_CRON_METHODS = frozenset({"cron.create", "cron.update", "cron.delete", "cron.trigger"})
_PROFILE_ARCHIVES = frozenset({
    "profiles.create", "profiles.delete", "profiles.export", "profiles.import",
})


@dataclass(frozen=True)
class HermesContract:
    version: str
    profile_methods: frozenset[str]
    atomic_paused_cron: bool = False


CONTRACTS = {
    # 0.20.5's stale scheduler can recreate deleted profiles.
    "791e2ae3257e211d14ca77e654dfe10ee1976a1c": HermesContract(
        "0.20.5", frozenset({"profiles.create"}),
    ),
    "9978706e9303dbf990d90e744b131361449d73b9": HermesContract(
        "0.20.6", _PROFILE_ARCHIVES,
    ),
    HERMES_0206_SHA: HermesContract(
        "0.20.6", _PROFILE_ARCHIVES | {"profiles.transfer"},
    ),
    HERMES_0212_SHA: HermesContract(
        "0.21.2", _PROFILE_ARCHIVES | {"profiles.transfer"}, atomic_paused_cron=True,
    ),
}
AUDITED_REVISIONS = {
    sha: (contract.version, _SESSION_METHODS, _CRON_METHODS)
    for sha, contract in CONTRACTS.items()
}
PROFILE_MANAGEMENT_METHODS = {
    sha: contract.profile_methods for sha, contract in CONTRACTS.items()
}
PROFILE_TRANSFER_REVISIONS = {
    sha: contract.version for sha, contract in CONTRACTS.items()
}
# Do not permit mixed-version transfers: 0.21.2 advances Hermes' SQLite schema.
PROFILE_TRANSFER_PAIRS = frozenset({
    (HERMES_0206_SHA, HERMES_0206_SHA),
    (HERMES_0212_SHA, HERMES_0212_SHA),
})


def profile_contract_supports(sha: str | None, version: str | None, method: str) -> bool:
    contract = CONTRACTS.get((sha or "").casefold())
    return bool(contract and contract.version == version and method in contract.profile_methods)
