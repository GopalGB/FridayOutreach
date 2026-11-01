import fs from "node:fs/promises";

const [scriptPath, outputPath, mode] = process.argv.slice(2);
if (!scriptPath || !outputPath || !["loadMore", "scroll"].includes(mode)) {
  process.exitCode = 2;
} else {
  const request = {
    taskSpaceId: 7,
    pageLabel: "p1",
    startUrl: "https://quotes.toscrape.com/js/",
    allowedHosts: ["quotes.toscrape.com"],
    allowedPaths: ["/js"],
    selection: null,
    pagination: { mode, selector: mode === "scroll" ? null : ".more" },
    limits: { maxInputs: 1, maxPagesPerHost: 1, maxRows: 1, maxPages: 1 },
    generation: 0,
    operation: "advance",
    outputPath,
  };
  let evaluateCount = 0;
  let waitCount = 0;
  const page = {
    url: async () => "https://quotes.toscrape.com/js/",
    evaluate: async () => {
      evaluateCount += 1;
      if (evaluateCount === 1) return { count: 1, fingerprint: "one" };
      if (evaluateCount === 2) return { kind: mode === "scroll" ? "scroll" : "button" };
      return "<html><body>unchanged</body></html>";
    },
    click: async () => undefined,
    mouse: { wheel: async () => undefined },
    waitForFunction: async () => {
      waitCount += 1;
      if (mode === "scroll" && waitCount === 2) return;
      throw new Error("page.waitForFunction timed out after 10000ms on page p1; last URL was https://quotes.toscrape.com/js/; last title was fixture");
    },
  };
  globalThis.taskSpace = async () => ({ page: () => page });
  const template = await fs.readFile(scriptPath, "utf8");
  const source = template.replace("/*__REQUEST_JSON__*/", JSON.stringify(JSON.stringify(request)));
  await import(`data:text/javascript,${encodeURIComponent(source)}`);
  const [record] = (await fs.readFile(outputPath, "utf8")).trim().split("\n").map(JSON.parse);
  const valid = mode === "scroll"
    ? record.status === "completed" && record.error === null
    : record.status === "timeout" && record.error?.code === "FETCH_TIMEOUT";
  if (!valid) {
    process.exitCode = 1;
  }
}
