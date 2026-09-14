export const liveDelegationPrefix = "This is a live voice request in your current conversation. ";
export const liveConversationSeparator = "\n\nLive conversation:\n";

export type LiveConversationRow = { id?: string; role: "user" | "assistant"; text: string };

/** Presentation only: preserve the original prompt for delivery and reconciliation. */
export function liveDelegationConversation(content: string): LiveConversationRow[] | undefined {
  if (!content.startsWith(liveDelegationPrefix)) return undefined;
  const separator = content.indexOf(liveConversationSeparator, liveDelegationPrefix.length);
  if (separator < 0) return undefined;
  const conversation = content.slice(separator + liveConversationSeparator.length).trim();
  if (!/^(User|Voice assistant): /.test(conversation)) return undefined;

  const rows: LiveConversationRow[] = [];
  for (const line of conversation.split("\n")) {
    const speaker = /^(User|Voice assistant): /.exec(line);
    if (speaker) rows.push({ role: speaker[1] === "User" ? "user" : "assistant", text: line.slice(speaker[0].length) });
    else rows[rows.length - 1].text += `\n${line}`;
  }
  return rows;
}
