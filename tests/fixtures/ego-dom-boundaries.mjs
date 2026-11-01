import fs from "node:fs/promises";

const [scriptPath, outputPath, scenario] = process.argv.slice(2);
const expected = {
  read: "completed", navigate: "completed", anchor: "completed", button: "completed", scroll: "completed",
  missing: "empty", denied: "denied", oversize: "truncated", timeout: "timeout",
};
if (!scriptPath || !outputPath || !expected[scenario]) {
  process.exitCode = 2;
} else {
  class Node { constructor(text) { this.textContent = text; } }
  class Anchor extends Node {
    constructor(target = "") { super("Next"); this.href = "https://directory.example.test/page-2"; this.target = target; }
    hasAttribute(name) { return name === "download" ? false : false; }
  }
  class Button extends Node {
    constructor() { super("Load more"); this.type = "button"; this.form = null; }
    hasAttribute(name) { return name === "formaction" ? false : false; }
  }
  globalThis.HTMLAnchorElement = Anchor;
  globalThis.HTMLButtonElement = Button;
  globalThis.location = { href: "https://directory.example.test/page-1" };
  let rows = [new Node("first")];
  const control = scenario === "anchor" ? new Anchor() : scenario === "denied" ? new Anchor("_blank") : new Button();
  globalThis.document = {
    readyState: "complete",
    documentElement: { get outerHTML() { return scenario === "oversize" ? "x".repeat(2 * 1024 * 1024 + 1) : `<main>${rows.map((row) => row.textContent).join(",")}</main>`; } },
    querySelectorAll: (selector) => selector === ".card" ? rows : [],
    querySelector: (selector) => selector === ".next" ? (scenario === "missing" ? null : control) : null,
  };
  const advance = () => { rows = [...rows, new Node("second")]; globalThis.location.href = "https://directory.example.test/page-2"; };
  const page = {
    url: async () => globalThis.location.href,
    goto: async () => undefined,
    evaluate: async (callback, argument) => callback(argument),
    click: async () => advance(),
    mouse: { wheel: async () => advance() },
    waitForFunction: async (predicate, value) => {
      if (scenario === "timeout" && value?.mode) throw new Error("page.waitForFunction timed out");
      if (!predicate(value)) throw new Error("page.waitForFunction timed out");
    },
  };
  const mode = scenario === "anchor" ? "next" : scenario === "button" || scenario === "missing" || scenario === "denied" || scenario === "timeout" ? "loadMore" : scenario === "scroll" ? "scroll" : "none";
  const request = { taskSpaceId: 7, pageLabel: "p1", startUrl: globalThis.location.href, allowedHosts: ["directory.example.test"], allowedPaths: ["/"], selection: { selector: ".card" }, pagination: { mode, selector: mode === "scroll" ? null : ".next" }, limits: { maxInputs: 1, maxPagesPerHost: 1, maxRows: 1, maxPages: 1 }, generation: 0, operation: scenario === "navigate" ? "navigate" : scenario === "read" || scenario === "oversize" ? "read" : "advance", outputPath };
  globalThis.taskSpace = async () => ({ page: () => page });
  const source = (await fs.readFile(scriptPath, "utf8")).replace("/*__REQUEST_JSON__*/", JSON.stringify(JSON.stringify(request)));
  await import(`data:text/javascript,${encodeURIComponent(source)}`);
  const [record] = (await fs.readFile(outputPath, "utf8")).trim().split("\n").map(JSON.parse);
  if (record.status !== expected[scenario]) process.exitCode = 1;
}
