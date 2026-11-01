import fs from "node:fs/promises";

const [scriptPath, outputPath] = process.argv.slice(2);
if (!scriptPath || !outputPath) {
  process.exitCode = 2;
} else {
  const request = {
    taskSpaceId: 7,
    pageLabel: "p1",
    startUrl: "https://directory.example.test/page-1",
    allowedHosts: ["directory.example.test"],
    allowedPaths: ["/"],
    selection: null,
    pagination: { mode: "next", selector: ".next" },
    limits: { maxInputs: 1, maxPagesPerHost: 1, maxRows: 1, maxPages: 1 },
    generation: 0,
    operation: "advance",
    outputPath,
  };
  let current = "https://directory.example.test/page-1";
  let evaluateCount = 0;
  globalThis.location = { href: current };
  const page = {
    url: async () => current,
    evaluate: async () => {
      evaluateCount += 1;
      if (evaluateCount === 1) return { count: 1, fingerprint: "same" };
      if (evaluateCount === 2) return { kind: "anchor", href: "https://directory.example.test/page-2" };
      return "<html><body>same</body></html>";
    },
    click: async () => { current = "https://directory.example.test/page-2"; globalThis.location.href = current; },
    mouse: { wheel: async () => { throw new Error("wheel is not expected"); } },
    waitForFunction: async (predicate, value) => {
      if (value?.mode !== "next") return;
      if (!predicate(value)) throw new Error("page.waitForFunction timed out after 10000ms on page p1");
    },
  };
  globalThis.taskSpace = async () => ({ page: () => page });
  const template = await fs.readFile(scriptPath, "utf8");
  await import(`data:text/javascript,${encodeURIComponent(template.replace("/*__REQUEST_JSON__*/", JSON.stringify(JSON.stringify(request))))}`);
  const [record] = (await fs.readFile(outputPath, "utf8")).trim().split("\n").map(JSON.parse);
  if (record.status !== "completed" || record.url !== "https://directory.example.test/page-2") process.exitCode = 1;
}
