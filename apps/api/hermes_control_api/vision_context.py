"""Camera evidence travels with a real user turn, never as an autonomous task."""

START = "\n\n[Agent Control camera observations]\n"
END = "\n[End Agent Control camera observations]"


def with_camera_context(prompt: str, context: str) -> str:
    if not context:
        return prompt
    # Escape our delimiters in observed text so the public projection is exact.
    context = context.replace(START.strip(), "[camera evidence]").replace(END.strip(), "[end evidence]")
    return prompt + START + context + END


def project_camera_prompt(prompt: str) -> str:
    start = prompt.rfind(START)
    end = prompt.find(END, start) if start >= 0 else -1
    if end >= 0:
        # Attachment references can be appended after the camera block by the
        # normal router. Keep that suffix for the attachment projection.
        return prompt[:start] + prompt[end + len(END):]
    return prompt
