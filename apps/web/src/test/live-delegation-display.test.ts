import { describe, expect, it } from "vitest";
import { liveDelegationConversation } from "../lib/liveDelegation";

describe("voice delegation display", () => {
  it("recognizes historical instructions without depending on their changing body", () => {
    expect(liveDelegationConversation("This is a live voice request in your current conversation. Earlier internal instructions.\n\nLive conversation:\nUser: Revisa el informe\ny sus anexos.\nVoice assistant: Lo reviso.\nUser: Solo los de hoy.")).toEqual([
      { role: "user", text: "Revisa el informe\ny sus anexos." },
      { role: "assistant", text: "Lo reviso." },
      { role: "user", text: "Solo los de hoy." },
    ]);
  });

  it.each([
    "User: Hola\nVoice assistant: Hola",
    "Explícame este texto:\nThis is a live voice request in your current conversation. Instructions.\n\nLive conversation:\nUser: Hola",
    "This is a live voice request in your current conversation. Sin separador.",
    "This is a live voice request in your current conversation. Instructions.\n\nLive conversation:\nSin transcripción válida.",
  ])("leaves ordinary text, quoted prompts and unrecognized messages intact", (content) => {
    expect(liveDelegationConversation(content)).toBeUndefined();
  });
});
