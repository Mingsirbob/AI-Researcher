export function pct(value: unknown, digits = 2): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${(number * 100).toFixed(digits)}%`;
}

export function number(value: unknown, digits = 2): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}

export function money(value: unknown): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `¥${parsed.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
}

export function shortId(value: unknown): string {
  const text = String(value ?? "");
  return text ? text.slice(0, 8) : "—";
}
