import fs from "node:fs/promises";

const [scriptPath, outputPath, missing] = process.argv.slice(2);
if (!scriptPath || !outputPath || !["space", "page"].includes(missing)) {
  process.exitCode = 2;
} else {
  const request = {
    taskSpaceId: 7,
    pageLabel: "p1",
    startUrl: "https://directory.example.test/list",
    allowedHosts: ["directory.example.test"],
    allowedPaths: ["/"],
    selection: null,
    pagination: { mode: "none", selector: null },
    limits: { maxInputs: 1, maxPagesPerHost: 1, maxRows: 1, maxPages: 1 },
    generation: 0,
    operation: "read",
    outputPath,
  };
  globalThis.taskSpace = async () => {
    if (missing === "space") throw new Error("TaskSpace 7 was not found or is not owned");
    return { page: () => { throw new Error("Page p1 was not found or is not owned"); } };
  };
  const template = await fs.readFile(scriptPath, "utf8");
  await import(`data:text/javascript,${encodeURIComponent(template.replace("/*__REQUEST_JSON__*/", JSON.stringify(JSON.stringify(request))))}`);
  const [record] = (await fs.readFile(outputPath, "utf8")).trim().split("\n").map(JSON.parse);
  if (record.status !== "unavailable" || record.error?.code !== "CAPABILITY_UNAVAILABLE") process.exitCode = 1;
}
