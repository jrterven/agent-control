"""Sign or advance a staged rollout without rebuilding immutable archives."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time


def policy(*, rollout_percent=0, paused=False, previous_sequence=0):
    if type(rollout_percent) is not int or not 0 <= rollout_percent <= 100 or type(paused) is not bool:
        raise ValueError("Invalid update rollout")
    return {"protocol": 1, "sequence": max(int(time.time()), previous_sequence + 1),
            "rolloutPercent": rollout_percent, "paused": paused}


def promote(directory, revision, key, percentage, paused):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/connector"))
    from agent_control_connector.managed_manifest import verify_signature
    if key.is_symlink() or key.stat().st_mode & 0o077:
        raise ValueError("Signing key must be private")
    document = directory / "latest.json"
    verify_signature(document, directory / "latest.json.sig")
    value = json.loads(document.read_bytes())
    if value.get("version") != revision:
        raise ValueError("Publication changed; inspect the current release")
    value["updates"] = policy(rollout_percent=percentage, paused=paused, previous_sequence=value.get("updates", {}).get("sequence", 0))
    with tempfile.TemporaryDirectory(dir=directory, prefix=".policy-") as temporary:
        updated = Path(temporary) / "latest.json"
        signature = Path(temporary) / "latest.json.sig"
        updated.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), "-out", str(signature), str(updated)], check=True, capture_output=True)
        verify_signature(updated, signature)
        signature.replace(directory / signature.name)
        updated.replace(document)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--rollout-percent", type=int, required=True)
    parser.add_argument("--paused", action="store_true")
    args = parser.parse_args()
    promote(args.directory, args.revision, args.private_key, args.rollout_percent, args.paused)
