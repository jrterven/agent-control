export const TIME_ZONE_PREFERENCE_KEY = "display-time-zone";

const FALLBACK_TIME_ZONES = [
  "UTC",
  "America/Mexico_City",
  "America/Cancun",
  "America/Tijuana",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Bogota",
  "America/Lima",
  "America/Santiago",
  "America/Argentina/Buenos_Aires",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Madrid",
  "Europe/Paris",
  "Europe/Berlin",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
] as const;

type IntlWithSupportedValues = typeof Intl & {
  supportedValuesOf?: (key: "timeZone") => string[];
};

const formatterCache = new Map<string, Intl.DateTimeFormat>();

function cachedFormatter(locale: string, timeZone: string, options: Intl.DateTimeFormatOptions) {
  const key = `${locale}|${timeZone}|${JSON.stringify(options)}`;
  let formatter = formatterCache.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, { ...options, timeZone });
    formatterCache.set(key, formatter);
  }
  return formatter;
}

/**
 * SQLAlchemy's SQLite datetime serializer returns UTC timestamps without a
 * trailing zone marker. Treat those values as UTC instead of allowing the
 * browser to reinterpret them as device-local wall time.
 */
export function parseControlTimestamp(value: string) {
  const trimmed = value.trim();
  const hasTime = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(trimmed);
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed);
  const date = new Date(hasTime && !hasZone ? `${trimmed}Z` : trimmed);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

export function detectedTimeZone() {
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return isValidTimeZone(detected) ? detected : "UTC";
}

export function isValidTimeZone(value: string | undefined): value is string {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat("en", { timeZone: value }).format(0);
    return true;
  } catch {
    return false;
  }
}

export function availableTimeZones(current?: string) {
  let supported: string[] = [];
  try {
    supported = (Intl as IntlWithSupportedValues).supportedValuesOf?.("timeZone") ?? [];
  } catch {
    supported = [];
  }
  return [...new Set([
    ...(current && isValidTimeZone(current) ? [current] : []),
    detectedTimeZone(),
    ...FALLBACK_TIME_ZONES,
    ...supported,
  ])].sort((left, right) => left.localeCompare(right));
}

export function formatConversationTimestamp(value: string, locale: string, timeZone: string) {
  const date = parseControlTimestamp(value);
  if (!date) return value;
  const safeTimeZone = isValidTimeZone(timeZone) ? timeZone : detectedTimeZone();
  const weekday = cachedFormatter(locale, safeTimeZone, { weekday: "short" })
    .format(date)
    .replace(/[.,]$/, "");
  const localizedWeekday = weekday
    ? weekday[0].toLocaleUpperCase(locale) + weekday.slice(1)
    : weekday;
  const dateAndTime = cachedFormatter(locale, safeTimeZone, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
  return `${localizedWeekday} ${dateAndTime}`;
}

export function formatConversationTimestampLong(value: string, locale: string, timeZone: string) {
  const date = parseControlTimestamp(value);
  if (!date) return value;
  const safeTimeZone = isValidTimeZone(timeZone) ? timeZone : detectedTimeZone();
  return cachedFormatter(locale, safeTimeZone, {
    dateStyle: "full",
    timeStyle: "short",
    timeZoneName: undefined,
  }).format(date) + ` (${safeTimeZone})`;
}

function zonedDateParts(value: Date, timeZone: string) {
  const parts = cachedFormatter("en-CA", timeZone, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((item) => item.type === type)?.value ?? 0);
  return { year: part("year"), month: part("month"), day: part("day") };
}

export function zonedDayKey(value: string | Date, timeZone: string) {
  const date = typeof value === "string" ? parseControlTimestamp(value) : value;
  if (!date || Number.isNaN(date.getTime())) return "unknown";
  const safeTimeZone = isValidTimeZone(timeZone) ? timeZone : detectedTimeZone();
  const { year, month, day } = zonedDateParts(date, safeTimeZone);
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

export function zonedDayDistance(value: string, timeZone: string, from = new Date()) {
  const date = parseControlTimestamp(value);
  if (!date) return undefined;
  const safeTimeZone = isValidTimeZone(timeZone) ? timeZone : detectedTimeZone();
  const target = zonedDateParts(date, safeTimeZone);
  const origin = zonedDateParts(from, safeTimeZone);
  const targetDay = Date.UTC(target.year, target.month - 1, target.day) / 86_400_000;
  const originDay = Date.UTC(origin.year, origin.month - 1, origin.day) / 86_400_000;
  return originDay - targetDay;
}

export function formatConversationDay(value: string, locale: string, timeZone: string) {
  const date = parseControlTimestamp(value);
  if (!date) return value;
  const safeTimeZone = isValidTimeZone(timeZone) ? timeZone : detectedTimeZone();
  return cachedFormatter(locale, safeTimeZone, {
    weekday: "long",
    day: "numeric",
    month: "short",
  }).format(date);
}
