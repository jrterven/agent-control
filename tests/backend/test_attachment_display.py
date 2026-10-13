"""Regressions for native-vision history and sent image previews."""
import pytest
from hermes_client.attachments import compose_attachment_prompt, project_attachment_prompt, attachment_image_paths
from hermes_client.prompt_display import BACKGROUND_CONTEXT, MEDIA_CONTEXT, project_managed_context
from hermes_client.types import PromptAttachment, PromptAttachmentReceipt


def native_prompt(path='/private/profile/images/upload.png', separator='\n'):
    value = compose_attachment_prompt('¿Qué hacen A0 y D0?', [PromptAttachment('image', 'sensor.png', 'image/png', b'image')], [])
    return value + f'\n\n[Image attached at: {path}]\n[screenshot]' + separator + BACKGROUND_CONTEXT + '\n\n' + MEDIA_CONTEXT + ' Current account limits: 6 images per gallery, 24 per response, 10485760 bytes and 25000000 pixels per image.'


@pytest.mark.parametrize('separator', ['\n\n', '\n', ''])
def test_native_vision_hides_paths_placeholders_and_complete_plugin_context(separator):
    raw = native_prompt(separator=separator)
    text, attachments = project_attachment_prompt(raw)
    assert text == '¿Qué hacen A0 y D0?'
    assert attachments == [{'kind':'image','name':'sensor.png','mediaType':'image/png','size':5}]
    assert attachment_image_paths(raw) == [(0, '/private/profile/images/upload.png')]
    assert BACKGROUND_CONTEXT in raw  # Projection leaves model/history data untouched.


def test_mixed_attachments_keep_names_and_scoped_ids_but_remove_private_references():
    raw = compose_attachment_prompt('Revisa ambos', [PromptAttachment('file','notes.txt','text/plain',b'notes'), PromptAttachment('image','image.png','image/png',b'image')], [PromptAttachmentReceipt(reference='@file:/private/notes.txt')], [None,'a'*32])
    raw += '\n@image:"/private/images/a b.png"\n[image]\n' + BACKGROUND_CONTEXT
    text, attachments = project_attachment_prompt(raw)
    assert text == 'Revisa ambos'
    assert attachments[1]['mediaId'] == 'a'*32
    assert attachment_image_paths(raw) == [(1, '/private/images/a b.png')]


def test_partial_context_and_user_quotes_are_preserved():
    for text in ['Explain Agent Control conversational multitasking: please.', 'Example:\n> '+BACKGROUND_CONTEXT, 'Before\n\n'+BACKGROUND_CONTEXT+'\nWhat does this mean?', 'A path [Image attached at: /private/photo.png]']:
        assert project_managed_context(text) == text
        assert project_attachment_prompt(text)[0] == text


def test_projection_matches_the_installed_managed_plugin_instructions():
    from agent_control_connector.hermes_background_plugin import INSTRUCTIONS as background
    from agent_control_connector.hermes_media_plugin import INSTRUCTIONS as media
    assert (BACKGROUND_CONTEXT, MEDIA_CONTEXT) == (background, media)


def test_prompt_recovery_ignores_native_scaffolding_but_keeps_attachment_identity():
    from hermes_control_api.services import SessionService
    from hermes_client.history import project_history_message
    submitted = compose_attachment_prompt('¿Qué hacen A0 y D0?', [PromptAttachment('image','sensor.png','image/png',b'image')], [])
    assert SessionService._prompt_digest(submitted) == SessionService._prompt_digest(native_prompt())
    other = compose_attachment_prompt('¿Qué hacen A0 y D0?', [PromptAttachment('image','other.png','image/png',b'image')], [])
    assert SessionService._prompt_digest(other) != SessionService._prompt_digest(native_prompt())
    assert 'api_content' not in project_history_message({'role':'user','content':'Question','api_content':BACKGROUND_CONTEXT})
