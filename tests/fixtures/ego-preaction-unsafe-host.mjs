import fs from "node:fs/promises";

const [scriptPath, outputPath, hostname] = process.argv.slice(2);
if (!scriptPath || !outputPath || !hostname) {
  process.exitCode = 2;
} else {
  const request = {
    taskSpaceId: 7,
    pageLabel: "p1",
    startUrl: `https://${hostname}/list`,
    allowedHosts: [hostname],
    allowedPaths: ["/"],
    selection: null,
    pagination: { mode: "none", selector: null },
    limits: { maxInputs: 1, maxPagesPerHost: 1, maxRows: 1, maxPages: 1 },
    generation: 0,
    operation: "read",
    outputPath,
  };
  const page = {
    url: async () => `https://${hostname}/list`,
    evaluate: async () => { throw new Error("fixture DOM access is forbidden"); },
    goto: async () => { throw new Error("fixture navigation is forbidden"); },
    click: async () => { throw new Error("fixture click is forbidden"); },
    mouse: { wheel: async () => { throw new Error("fixture wheel is forbidden"); } },
  };
  globalThis.taskSpace = async () => ({ page: () => page });
  const template = await fs.readFile(scriptPath, "utf8");
  await import(`data:text/javascript,${encodeURIComponent(template.replace("/*__REQUEST_JSON__*/", JSON.stringify(JSON.stringify(request))))}`);
  const [record] = (await fs.readFile(outputPath, "utf8")).trim().split("\n").map(JSON.parse);
  if (record.status !== "denied" || record.error?.code !== "TARGET_DENIED") process.exitCode = 1;
}
