import { describe, expect, it } from "vitest";
import { formatBytes, formatEta, formatPercent } from "./utils";

describe("迁移作业台格式化", () => {
  it("以适合运维阅读的单位显示数据量", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(20 * 1024 * 1024)).toBe("20 MB");
  });

  it("把配额和运行进度限制在 0 到 100", () => {
    expect(formatPercent(50, 100)).toBe(50);
    expect(formatPercent(300, 100)).toBe(100);
    expect(formatPercent(1, 0)).toBe(0);
  });

  it("显示跨日迁移的剩余时间", () => {
    expect(formatEta(45)).toBe("45 秒");
    expect(formatEta(90)).toBe("2 分钟");
    expect(formatEta(172_800)).toBe("2.0 天");
  });
});
