import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, openApiJsonBody } from "@/api/client";

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("returns typed JSON responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ok" }), { status: 200 })));
    await expect(api<{ status: string }>("/api/health")).resolves.toEqual({ status: "ok" });
  });

  it("normalizes FastAPI errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { message: "合同不匹配" } }), { status: 409 })));
    await expect(api("/api/probe")).rejects.toEqual(expect.objectContaining<ApiError>({ message: "合同不匹配", status: 409 }));
  });

  it("serializes generated OpenAPI request schemas", () => {
    const init = openApiJsonBody<"PaperOrderReview">({ reviewer: "human", note: "已复核" });
    expect(init.body).toBe(JSON.stringify({ reviewer: "human", note: "已复核" }));
  });
});
