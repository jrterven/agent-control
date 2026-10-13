/** Format controls can survive trim() while rendering as an empty answer. */
export function hasVisibleText(value: unknown): value is string {
  return typeof value === "string" && value.replace(/[\s\p{Cc}\p{Default_Ignorable_Code_Point}]/gu, "").length > 0;
}
