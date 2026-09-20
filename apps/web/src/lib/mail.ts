import { request } from "./api";

export type MailProvider = "gmail" | "outlook" | "hostinger" | "imap";
export type MailAccount = {
  id: string; provider: MailProvider; address: string; label: string; status: string;
  config: { service?: "hostinger" | "titan" | "custom"; username?: string; imapHost?: string; smtpHost?: string; smtpPort?: 465 | 587 };
  agents: { profileId: string; state: string }[];
};
export type MailInput = {
  provider: "hostinger" | "imap"; address: string; label: string; service: "hostinger" | "titan" | "custom";
  username: string; password: string; imapHost: string; smtpHost: string; smtpPort: 465 | 587; accountId?: string;
};
const mutation = (method: string, body: unknown, csrfToken: string | undefined, signal?: AbortSignal): RequestInit => ({
  method, signal, headers: { ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const mailApi = {
  providers: (signal?: AbortSignal) => request<{ id: MailProvider; enabled: boolean }[]>("/mail/providers", { signal, cache: "no-store" }),
  accounts: (signal?: AbortSignal) => request<MailAccount[]>("/mail/accounts", { signal, cache: "no-store" }),
  connect: (input: MailInput, csrf?: string, signal?: AbortSignal) => request<MailAccount>("/mail/accounts", mutation("POST", input, csrf, signal)),
  update: (id: string, label: string, profileIds: string[], csrf?: string, signal?: AbortSignal) => request<MailAccount>(`/mail/accounts/${encodeURIComponent(id)}`, mutation("PATCH", { label, profileIds }, csrf, signal)),
  test: (id: string, csrf?: string, signal?: AbortSignal) => request<MailAccount>(`/mail/accounts/${encodeURIComponent(id)}/test`, mutation("POST", undefined, csrf, signal)),
  disconnect: (id: string, csrf?: string, signal?: AbortSignal) => request<void>(`/mail/accounts/${encodeURIComponent(id)}`, mutation("DELETE", undefined, csrf, signal)),
  oauth: (provider: MailProvider, accountId?: string, csrf?: string, signal?: AbortSignal) => request<{ authorizationUrl: string }>(`/mail/oauth/${provider}/start`, mutation("POST", { accountId }, csrf, signal)),
};
