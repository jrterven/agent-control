"""Public projection of audited managed context; never changes model input."""
import re
from .generated_files import project_file_context

# Exact text only: quoted, partial and user-authored lookalikes remain visible.
# Kept separately from the installable plugins, which run in the Hermes process.
BACKGROUND_CONTEXT = "Agent Control conversational multitasking: keep this conversation available while independent, time-consuming tasks run. When native delegate_task is available and the task can be completed independently with the information and permissions already given, delegate it with a clear goal, relevant context and output language. Give only the context the worker needs. Use the native tool; do not build a background executor or change schedules. After a confirmed background dispatch, briefly say what is running and END YOUR TURN promptly so the user can keep chatting. Do not wait, poll transcripts, or repeatedly call list to await completion. Hermes delivers the result to this same conversation between turns; report the result once when it arrives, distinguishing success, failure and uncertain outcomes. If dispatch falls back to synchronous execution, do not claim the chat has been freed. Keep immediate answers and tasks needing clarification in the main conversation. Workers inherit existing permissions: delegation does not authorize sending messages, deleting data or other actions beyond the user's request. Avoid concurrent changes to the same resource; pass relevant constraints to the worker. Never retry an uncertain external action automatically, or treat a worker's self-report as independently verified. A user asking about progress is not cancelling the work. Use native list/steer/stop only when needed to answer, redirect or cancel. For visual work, ask the worker to return local file paths or HTTPS image URLs with alt text and provenance, without publishing images from its child session. Publish those images yourself with publish_images in this parent conversation before inserting the returned Markdown. Do not reuse ac-media references published in a child conversation; media access is conversation-scoped. Running subagents do not survive stopping/resetting their session or exiting Hermes; do not promise restart durability. Cron and other finite runs retain their native behavior."
MEDIA_CONTEXT = 'Agent Control images: use publish_images when pictures, paper figures, charts or screenshots add useful information. Use existing search, capture or generation tools to obtain them. Publish local PNG/JPEG/WebP files or HTTPS image URLs, with accurate alt text, optional caption and source URL/title. Label provenance web, generated or local truthfully. The tool returns private references: insert its exact ![description](ac-media:ID) Markdown in your answer where the picture belongs. Consecutive images form a gallery (at most six); use at most 24 images per answer. Publication may be pending while the connector reconnects; never claim pending images are published. Do not invent IDs, expose local paths, inline base64, remote image links or HTML. Cite the source page separately when useful. Images are optional; do not add decorative or irrelevant pictures.'
_LIMITS = re.compile(r" Current account limits: [0-9]{1,3} images per gallery, [0-9]{1,3} per response, [0-9]{1,10} bytes and [0-9]{1,10} pixels per image\.$")

def project_managed_context(content: str) -> str:
    content = project_file_context(content)
    # Hooks append independent paragraphs. Remove only complete known suffixes,
    # in either hook order; do not strip a substring from the user's question.
    for _ in range(8):
        candidate = content.rstrip()
        limits = _LIMITS.search(candidate)
        without_limits = candidate[:limits.start()] if limits else candidate
        suffix = next((text for text in (BACKGROUND_CONTEXT, MEDIA_CONTEXT)
                       if without_limits.endswith(text)
                       and without_limits[:-len(text)].endswith(("\n", "[screenshot]", "[image]"))), None)
        if suffix is None or (limits and suffix != MEDIA_CONTEXT):
            return content
        content = without_limits[:-len(suffix)].rstrip()
    return content
