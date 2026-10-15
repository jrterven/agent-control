import os
from pathlib import Path
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest

from hermes_client.attachments import compose_attachment_prompt, project_attachment_prompt
from hermes_client.generated_files import (
    MAX_FILE_BYTES, file_directory, file_id, project_file_context, with_file_context,
)
from hermes_client.prompt_display import BACKGROUND_CONTEXT
from hermes_client.types import PromptAttachment, PromptReceipt, SessionRoute
from hermes_control_api.services import SessionService
from agent_control_connector.media import project_media, read_media
from test_runtime import runtime, request


def stage(home, profile="selected", session="session", name="propuesta IA (final).md", content=b"# Proposal"):
    directory = file_directory(home, profile, session)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    history = [{"role": "assistant", "content": f"[Descargar {name}](ac-file:{quote(name, safe='')})"}]
    return path, history, file_id(profile, session, name)


def test_files_keep_bytes_names_and_conversation_scope(tmp_path):
    path, history, identifier = stage(tmp_path)
    projected = project_media(history, tmp_path, "selected", "session")
    assert projected[0]["controlFiles"] == [{"id": identifier, "name": path.name, "size": path.stat().st_size}]
    assert str(tmp_path) not in str(projected)
    assert read_media(history, tmp_path, "selected", "session", identifier) == {
        "content": b"# Proposal", "filename": path.name, "media_type": "application/octet-stream"}
    for profile, session, rows in [("other", "session", history), ("selected", "other", history),
                                   ("selected", "session", []), ("selected", "ac_tmp_test", history)]:
        with pytest.raises(LookupError):
            read_media(rows, tmp_path, profile, session, identifier)
    path.unlink()
    assert "controlFiles" not in project_media(history, tmp_path, "selected", "session")[0]
    with pytest.raises(LookupError):
        read_media(history, tmp_path, "selected", "session", identifier)


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "directory", "fifo", "oversize"])
def test_download_rechecks_files_and_refuses_aliases_and_special_files(tmp_path, replacement):
    path, history, identifier = stage(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    path.unlink()
    if replacement == "symlink":
        path.symlink_to(secret)
    elif replacement == "hardlink":
        os.link(secret, path)
    elif replacement == "directory":
        path.mkdir()
    elif replacement == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as file:
            file.truncate(MAX_FILE_BYTES + 1)
    assert "controlFiles" not in project_media(history, tmp_path, "selected", "session")[0]
    with pytest.raises(LookupError):
        read_media(history, tmp_path, "selected", "session", identifier)


def test_directory_alias_and_arbitrary_paths_are_not_downloadable(tmp_path):
    path, history, identifier = stage(tmp_path)
    alias = path.parent.with_name("private")
    path.parent.rename(alias)
    path.parent.symlink_to(alias, target_is_directory=True)
    with pytest.raises(LookupError):
        read_media(history, tmp_path, "selected", "session", identifier)
    for value in ("%2e%2e%2fsecret.txt", "%2Fetc%2Fpasswd", ".env", "sub%5Csecret.txt", "bad%0Aname", "%ED%A0%80"):
        rows = [{"role": "assistant", "content": f"[file](ac-file:{value})", "controlFiles": [{"id": identifier}]}]
        assert "controlFiles" not in project_media(rows, tmp_path, "selected", "session")[0]


def test_empty_files_and_default_profile(tmp_path):
    path, history, identifier = stage(tmp_path, profile="default", content=b"")
    assert read_media(history, tmp_path, "default", "session", identifier)["content"] == b""
    assert path.is_relative_to(tmp_path / "exports")


def test_context_preserves_user_text_attachments_and_reconciliation(tmp_path):
    prompt = compose_attachment_prompt("Revisa esto", [PromptAttachment("image", "photo.png", "image/png", b"image")], [])
    wrapped = with_file_context(prompt, tmp_path, "selected", "session")
    assert project_file_context(wrapped) == prompt
    assert project_file_context("Quote: " + wrapped) == "Quote: " + wrapped
    assert with_file_context(prompt, tmp_path, "selected", "ac_tmp_test") == prompt
    native = wrapped + "\n\n[Image attached at: /private/photo.png]\n[screenshot]\n" + BACKGROUND_CONTEXT
    public, attachments = project_attachment_prompt(native)
    assert public == "Revisa esto"
    assert attachments[0]["name"] == "photo.png"
    assert SessionService._prompt_digest(native) == SessionService._prompt_digest(prompt)
    # Direct installations add context before the attachment envelope is built.
    inner = compose_attachment_prompt(with_file_context("Revisa esto", tmp_path, "selected", "session"),
                                      [PromptAttachment("image", "photo.png", "image/png", b"image")], [])
    assert project_attachment_prompt(inner)[0] == "Revisa esto"
    assert SessionService._prompt_digest(inner) == SessionService._prompt_digest(prompt)
    empty = compose_attachment_prompt(with_file_context("", tmp_path, "selected", "session"),
                                      [PromptAttachment("image", "photo.png", "image/png", b"image")], [])
    assert project_attachment_prompt(empty + "\n\n[Image attached at: /private/photo.png]\n[screenshot]")[0] == ""


@pytest.mark.asyncio
async def test_connector_adds_delivery_instructions_once_after_deduplication(runtime):
    provider = runtime.providers["selected"]
    provider.submit_prompt = AsyncMock(return_value=PromptReceipt("same-operation"))
    args = (SessionRoute("gateway", "selected", "stored", "runtime"), "Crea un documento")
    await runtime.execute(request("submit_prompt", args, {"operation_id": "same-operation"}))
    await runtime.execute(request("submit_prompt", args, {"operation_id": "same-operation"}))
    assert provider.submit_prompt.await_count == 1
    submitted = provider.submit_prompt.call_args.args[1]
    assert "ac-file:filename" in submitted
    assert str(file_directory(Path(runtime.config["hermesHome"]), "selected", "stored")) in submitted
    assert project_file_context(submitted) == "Crea un documento"
