import type { components, paths } from "./openapi.generated";

export type JsonObject = Record<string, unknown>;
export type OpenApiSchemaName = keyof components["schemas"];
export type OpenApiSchema<Name extends OpenApiSchemaName> = components["schemas"][Name];
export type OpenApiPath = keyof paths;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T = JsonObject>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail: unknown = null;
    try { detail = await response.json(); } catch { detail = null; }
    const payload = detail && typeof detail === "object" ? detail as Record<string, unknown> : {};
    const nested = payload.detail && typeof payload.detail === "object" ? payload.detail as Record<string, unknown> : {};
    const message = nested.message ?? payload.detail ?? `请求失败（${response.status}）`;
    throw new ApiError(String(message), response.status, detail);
  }
  return (response.status === 204 ? null : await response.json()) as T;
}

export const jsonBody = (value: unknown): RequestInit => ({ body: JSON.stringify(value) });

export const openApiJsonBody = <Name extends OpenApiSchemaName>(
  value: OpenApiSchema<Name>,
): RequestInit => ({ body: JSON.stringify(value) });
