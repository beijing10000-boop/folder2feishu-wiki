import { afterAll, describe, expect, it, vi } from "vitest";
import { api } from "./client";

describe("API 安全会话", () => {
  it("所有写请求先取得同源 CSRF 令牌并随请求发送", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url === "/api/v2/session") {
          return new Response(JSON.stringify({ csrf_token: "csrf-for-test" }), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          });
        }
        if (url === "/api/v2/settings") {
          return new Response(
            JSON.stringify({
              app_id: "cli_test",
              redirect_uri: "http://127.0.0.1:8000/oauth/callback",
              scopes: ["drive:drive"],
              secret_configured: true
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        }
        return new Response(null, { status: 404 });
      })
    );

    const settings = await api.saveSettings({
      app_id: "cli_test",
      app_secret: "temporary-input-only",
      redirect_uri: "http://127.0.0.1:8000/oauth/callback",
      scopes: ["drive:drive"]
    });

    expect(settings.app_secret_configured).toBe(true);
    expect(calls.map((call) => call.url)).toEqual(["/api/v2/session", "/api/v2/settings"]);
    expect(new Headers(calls[1].init?.headers).get("X-F2F-CSRF")).toBe("csrf-for-test");
    expect(calls[0].init?.cache).toBe("no-store");
    expect(calls[1].init?.credentials).toBe("same-origin");
  });

  it("四个配置验证请求都是受保护的独立 POST 端点", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url === "/api/v2/session") {
          return new Response(JSON.stringify({ csrf_token: "csrf-for-verification" }), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          });
        }
        const kind = url.split("/").at(-1);
        return new Response(
          JSON.stringify({
            ok: true,
            kind,
            message: `${kind} verified`,
            details: {}
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      })
    );

    await api.verifyApp();
    await api.verifyOauth();
    await api.verifySource("D:\\OneDrive");
    await api.verifyTarget("https://example.feishu.cn/drive/folder/DriveFolderToken99");

    const verificationCalls = calls.filter((call) => call.url.includes("/verify/"));
    expect(verificationCalls.map((call) => call.url)).toEqual([
      "/api/v2/verify/app",
      "/api/v2/verify/oauth",
      "/api/v2/verify/source",
      "/api/v2/verify/target"
    ]);
    verificationCalls.forEach((call) => {
      expect(call.init?.method).toBe("POST");
      expect(new Headers(call.init?.headers).get("X-F2F-CSRF")).toBeTruthy();
    });
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });
});
