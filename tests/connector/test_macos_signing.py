"""Publication gates and interrupted Apple submissions, with Apple APIs isolated."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from deploy.connector import macos_signing as signing


@pytest.fixture
def apple(tmp_path, monkeypatch):
    monkeypatch.setattr(signing.sys, "platform", "darwin")
    bundle = tmp_path / "input"
    bundle.mkdir()
    (bundle / "agent-control-connector").write_bytes(bytes.fromhex("cffaedfe") + b"main")
    (bundle / "agent-control-connector").chmod(0o755)
    (bundle / "library.so").write_bytes(bytes.fromhex("cffaedfe") + b"library")
    (bundle / "release.json").write_text('{"revision":"r1"}')
    signer = signing.AppleSigner(tmp_path / "private", "r1", "A" * 40, "ABCDE12345", "test-profile")
    calls, submissions = [], []
    state = {"status": "In Progress", "missing_ticket": False, "bad_team": False}

    def invoke(args):
        calls.append(args)
        if args[0] == "/usr/bin/codesign":
            path = Path(args[-1])
            if "--force" in args:
                assert "--deep" not in args and "--entitlements" not in args
                assert args[args.index("--options") + 1] == "runtime"
                assert "--timestamp" in args
                path.write_bytes(path.read_bytes() + b"signed")
            elif "-d" in args:
                signed = path.parent
                team = "WRONG12345" if state["bad_team"] else signer.team
                return (f"Identifier={signing.identifier_for(signed, path)}\nTeamIdentifier={team}\n"
                        f"CodeDirectory v=20500 flags=0x10000(runtime)\nTimestamp=now\nCDHash={hashlib.sha256(path.read_bytes()).hexdigest()[:40]}\n")
            else:
                assert any(arg.startswith("-R=anchor apple generic") for arg in args)
        elif args[0] == "/usr/bin/ditto":
            Path(args[-1]).write_bytes(b"zip-placeholder")
        elif args[:3] == ["xcrun", "notarytool", "log"]:
            signed = signer.work / signer.revision / "macos-arm64/agent-control-connector"
            entries = [{"cdhash": hashlib.sha256(path.read_bytes()).hexdigest()[:40], "arch": "arm64"}
                       for path in signing.macho_files(signed)]
            Path(args[4]).write_text(json.dumps({"jobId": "submission-id", "status": state["status"],
                "sha256": "wrong" if state.get("wrong_archive") else hashlib.sha256(b"zip-placeholder").hexdigest(),
                "ticketContents": entries[:1] if state["missing_ticket"] else entries}))
        return ""

    def notary(*args):
        if args[0] == "submit":
            submissions.append(args[1])
            return {"id": "submission-id", "name": Path(args[1]).name}
        if args[0] == "history":
            return {"history": [{"id": "submission-id", "name": Path(submissions[0]).name}]}
        return {"id": "submission-id", "status": state["status"]}

    monkeypatch.setattr(signing, "invoke", invoke)
    monkeypatch.setattr(signer, "notary", notary)
    return signer, bundle, state, calls, submissions


def test_pending_resume_reuses_exact_signed_bytes_and_submission(apple):
    signer, bundle, state, calls, submissions = apple
    original = signing.tree_digest(bundle)
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    assert signing.tree_digest(bundle) == original
    signed_calls = [args for args in calls if "--force" in args]
    assert Path(signed_calls[-1][-1]).name == "agent-control-connector"
    assert signed_calls[-1][signed_calls[-1].index("--identifier") + 1] == signing.IDENTIFIER
    state["status"] = "Accepted"
    signer(bundle, "macos-arm64")
    assert len(submissions) == 1
    assert [args for args in calls if "--force" in args] == signed_calls
    assert signing.tree_digest(bundle) != original
    assert len([args for args in calls if "--check-notarization" in args]) == 2


@pytest.mark.parametrize("failure", ["rejected", "missing_ticket", "bad_team", "wrong_archive"])
def test_unapproved_code_never_replaces_input(apple, failure):
    signer, bundle, state, calls, submissions = apple
    original = signing.tree_digest(bundle)
    state["status"] = "Invalid" if failure == "rejected" else "Accepted"
    if failure != "rejected":
        state[failure] = True
    with pytest.raises(ValueError):
        signer(bundle, "macos-arm64")
    assert signing.tree_digest(bundle) == original
    assert not any("--check-notarization" in args for args in calls)


@pytest.mark.parametrize("changed", ["input", "signed", "archive"])
def test_changed_pending_files_fail_closed_without_resubmission(apple, changed):
    signer, bundle, state, calls, submissions = apple
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    stage = signer.work / signer.revision / "macos-arm64"
    path = {"input": bundle / "release.json", "signed": stage / "agent-control-connector/release.json",
            "archive": Path(submissions[0])}[changed]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        signer(bundle, "macos-arm64")
    assert len(submissions) == 1


def test_interrupted_upload_recovers_receipt_without_resubmitting(apple):
    signer, bundle, state, calls, submissions = apple
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    (signer.work / signer.revision / "macos-arm64/submission.json").unlink()
    state["status"] = "Accepted"
    signer(bundle, "macos-arm64")
    assert len(submissions) == 1


def test_uncertain_upload_without_history_requires_operator(apple, monkeypatch):
    signer, bundle, state, calls, submissions = apple
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    (signer.work / signer.revision / "macos-arm64/submission.json").unlink()
    monkeypatch.setattr(signer, "notary", lambda *args: {"history": []})
    with pytest.raises(ValueError, match="uncertain"):
        signer(bundle, "macos-arm64")
    assert len(submissions) == 1


def test_interrupted_zip_is_rebuilt_before_first_submission(apple, monkeypatch):
    signer, bundle, state, calls, submissions = apple
    invoke = signing.invoke
    def interrupted(args):
        if args[0] == "/usr/bin/ditto":
            Path(args[-1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(args, 180)
        return invoke(args)
    monkeypatch.setattr(signing, "invoke", interrupted)
    with pytest.raises(subprocess.TimeoutExpired):
        signer(bundle, "macos-arm64")
    assert not submissions
    monkeypatch.setattr(signing, "invoke", invoke)
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    assert Path(submissions[0]).read_bytes() == b"zip-placeholder"


def test_receipt_without_archive_identity_is_rejected(apple):
    signer, bundle, state, calls, submissions = apple
    with pytest.raises(signing.NotarizationPending):
        signer(bundle, "macos-arm64")
    (signer.work / signer.revision / "macos-arm64/submission-intent.json").unlink()
    with pytest.raises(ValueError, match="no archive identity"):
        signer(bundle, "macos-arm64")
    assert len(submissions) == 1


def test_signing_rejects_links_and_missing_macho_main(tmp_path):
    (tmp_path / "agent-control-connector").write_bytes(b"shell script")
    with pytest.raises(ValueError, match="Mach-O"):
        signing.macho_files(tmp_path)
    (tmp_path / "link").symlink_to("agent-control-connector")
    with pytest.raises(ValueError, match="regular"):
        signing.macho_files(tmp_path)


def test_framework_flattening_requires_identical_library_without_loader_dependencies(tmp_path, monkeypatch):
    (tmp_path / "agent-control-connector").write_bytes(bytes.fromhex("cffaedfe"))
    framework = tmp_path / "_internal/Python.framework"
    python = framework / "Versions/3.12/Python"
    python.parent.mkdir(parents=True)
    python.write_bytes(bytes.fromhex("cffaedfe") + b"python")
    library = tmp_path / "_internal/Python"
    library.write_bytes(python.read_bytes())
    monkeypatch.setattr(signing, "invoke", lambda args: args[-1] + ":\n\t@rpath/Python.framework/Python")
    with pytest.raises(ValueError, match="depends"):
        signing.flatten_python_framework(tmp_path)
    assert framework.exists()
    monkeypatch.setattr(signing, "invoke", lambda args: args[-1] + ":\n\t@rpath/Python")
    signing.flatten_python_framework(tmp_path)
    assert not framework.exists() and library.is_file()
