import type { ChatMessage, ToolRun } from "../types";

export type ActivityStep = "mail" | "mailRead" | "mailSend" | "calendar" | "web" | "files" | "local" | "delegation" | "other";
export type ActivityGroup = { step: ActivityStep; status: ToolRun["status"] };

function toolStep(name: string): ActivityStep {
  const parts = name.toLowerCase().split("__");
  const tool = (parts[0] === "mcp" && parts.length === 3 ? parts[2] : name).toLowerCase().replaceAll(".", "_");
  if (/^(mail|email)_(accounts|search|read)$/.test(tool)) return "mailRead";
  if (/^(mail|email)_send$/.test(tool)) return "mailSend";
  // Hermes can shorten the registered name for this connector before the
  // operation suffix. Only the mail integration is identifiable in that case.
  if (/^mcp_+agent_control_mail_/.test(name.toLowerCase())) return "mail";
  if (/^calendar_(list|search|read|create|update|delete)(_|$)/.test(tool)) return "calendar";
  if (/^(web_(search|extract)|browser_[a-z_]+)$/.test(tool)) return "web";
  if (/^(read_file|write_file|search_files|list_files|patch|apply_patch)$/.test(tool)) return "files";
  if (/^(terminal|execute_code|process)$/.test(tool)) return "local";
  if (/^(delegate_task|delegate|spawn_subagent)$/.test(tool)) return "delegation";
  return "other";
}

const statusPriority = { completed: 0, failed: 1, running: 2 } as const;

export function summarizeAgentActivity(message?: Pick<ChatMessage, "tools" | "activity">) {
  const tools = message?.tools ?? [];
  const reported: ActivityGroup[] = tools.map((tool) => ({ step: toolStep(tool.name), status: tool.status }));
  // Tool records already reconcile progress and completion by ID. Replaying
  // their event log here would count a single invocation multiple times.
  const latestDelegation = message?.activity?.filter((item) => item.kind === "delegation").at(-1);
  if (latestDelegation) {
    reported.push({ step: "delegation", status: latestDelegation.status });
  }
  if (!tools.length) {
    const latestTool = message?.activity?.filter((item) => item.kind === "tool").at(-1);
    if (latestTool) reported.unshift({ step: toolStep(latestTool.label ?? ""), status: latestTool.status });
  }

  const groups = new Map<ActivityStep, ActivityGroup>();
  let current: ActivityStep | undefined;
  for (const entry of reported) {
    const previous = groups.get(entry.step);
    if (!previous || statusPriority[entry.status] > statusPriority[previous.status]) groups.set(entry.step, entry);
    if (entry.status === "running") current = entry.step;
  }
  return { groups: [...groups.values()], current };
}
