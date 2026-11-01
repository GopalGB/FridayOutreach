const request = JSON.parse(/*__REQUEST_JSON__*/);
const fs = await import("node:fs/promises");
const crypto = await import("node:crypto");
const ACTION_TIMEOUT_MS = 10_000;
let page;

const decodeComponent = (value) => {
  if (/%(?![0-9a-fA-F]{2})/.test(value)) {
    return null;
  }
  try {
    const decoded = decodeURIComponent(value);
    return decoded.includes("%") || decoded.includes("\\") || /[\u0000-\u001f\u007f]/.test(decoded) ? null : decoded;
  } catch {
    return null;
  }
};

const canonicalPath = (value) => {
  if (!value.startsWith("/") || /%(?:2f|5c|2e)/i.test(value)) {
    return null;
  }
  const path = decodeComponent(value);
  return path === null || path.split("/").some((segment) => segment === "." || segment === "..") ? null : path;
};

const isTimeout = (message) => /timed?\s*out/i.test(message);
const isUnavailable = (message) => /(?:task\s*space|page).*(?:not found|not owned|unavailable|missing|does not exist|unknown)|(?:not found|not owned|unavailable|missing|does not exist|unknown).*(?:task\s*space|page)/i.test(message);
const isPublicHost = (host) => host.includes(".")
  && host !== "localhost"
  && !host.endsWith(".localhost")
  && !host.endsWith(".local")
  && !host.endsWith(".localdomain")
  && !host.includes(":")
  && !/^\d+(?:\.\d+){3}$/.test(host);

const inScope = (raw) => {
  try {
    const target = new URL(raw);
    const host = target.hostname.replace(/\.$/, "").toLowerCase();
    const path = canonicalPath(target.pathname);
    const query = decodeComponent(target.search.slice(1));
    const scopedPath = path === null ? null : path.replace(/\/+$/, "") || "/";
    const allowedPaths = request.allowedPaths.map(canonicalPath).map((prefix) => prefix === null ? null : prefix.replace(/\/+$/, "") || "/");
    const defaultPort = target.protocol === "https:" ? "443" : "80";
    return ["http:", "https:"].includes(target.protocol)
      && !target.username && !target.password && !target.hash
      && (!target.port || target.port === defaultPort)
      && query !== null && scopedPath !== null
      && isPublicHost(host)
      && request.allowedHosts.includes(host)
      && allowedPaths.some((prefix) => prefix === "/" || (prefix !== null && (scopedPath === prefix || scopedPath.startsWith(`${prefix}/`))));
  } catch {
    return false;
  }
};

const assertScoped = async () => {
  if (!inScope(await page.url())) {
    throw new Error("TARGET_DENIED");
  }
};

const state = async () => {
  await assertScoped();
  const url = new URL(await page.url()).href;
  const content = await page.evaluate((selection) => {
    const selector = selection?.selector === "table" ? "table tr" : selection?.selector;
    const values = selector ? [...document.querySelectorAll(selector)] : [document.documentElement];
    const text = values.map((node) => node.textContent ?? "").join("\u001f");
    let fingerprint = 2166136261;
    [...text].forEach((character) => { fingerprint = Math.imul(fingerprint ^ character.charCodeAt(0), 16777619); });
    return { count: values.length, fingerprint: String(fingerprint >>> 0) };
  }, request.selection);
  return { ...content, url };
};

const ready = () => page.waitForFunction((selection) => {
  const selector = selection?.selector === "table" ? "table tr" : selection?.selector;
  return selector ? document.querySelectorAll(selector).length > 0 : document.readyState !== "loading";
}, request.selection, { timeout: ACTION_TIMEOUT_MS });

const changed = (before, mode) => page.waitForFunction((value) => {
  if (value.mode === "next" && new URL(location.href).href !== value.before.url) return true;
  const selector = value.selection?.selector === "table" ? "table tr" : value.selection?.selector;
  const nodes = selector ? [...document.querySelectorAll(selector)] : [document.documentElement];
  const text = nodes.map((node) => node.textContent ?? "").join("\u001f");
  let fingerprint = 2166136261;
  [...text].forEach((character) => { fingerprint = Math.imul(fingerprint ^ character.charCodeAt(0), 16777619); });
  return nodes.length !== value.before.count || String(fingerprint >>> 0) !== value.before.fingerprint;
}, { selection: request.selection, before, mode }, { timeout: ACTION_TIMEOUT_MS });

const action = () => page.evaluate(({ mode, selector }) => {
  if (mode === "scroll") {
    return { kind: "scroll" };
  }
  if (!selector) {
    return { kind: "exhausted" };
  }
  const node = document.querySelector(selector);
  if (!node) {
    return { kind: "exhausted" };
  }
  if (node instanceof HTMLAnchorElement && !node.target && !node.hasAttribute("download")) {
    return { kind: "anchor", href: node.href };
  }
  if (node instanceof HTMLButtonElement && !node.form && node.type !== "submit" && !node.hasAttribute("formaction")) {
    return { kind: "button" };
  }
  return { kind: "denied" };
}, { mode: request.pagination.mode, selector: request.pagination.selector });

const emit = async (record) => {
  const body = JSON.stringify(record) + "\n";
  await fs.writeFile(request.outputPath, body, { encoding: "utf8" });
  const receipt = { path: request.outputPath, sha256: crypto.createHash("sha256").update(body).digest("hex"), records: 1 };
  process.stdout.write("EGO_RECEIPT " + JSON.stringify(receipt) + "\n");
};

const outcome = async (status, code, message) => emit({
  url: request.startUrl,
  html: "",
  observedAt: new Date().toISOString(),
  sha256: crypto.createHash("sha256").update("").digest("hex"),
  status,
  error: status === "empty" ? null : { code, message, url: null },
});

try {
  const task = await taskSpace(request.taskSpaceId);
  page = task.page(request.pageLabel);
  let terminal = false;
  if (request.operation === "navigate") {
    await page.goto(request.startUrl, { timeout: ACTION_TIMEOUT_MS, waitUntil: "domcontentloaded" });
    await assertScoped();
    await ready();
  } else {
    await assertScoped();
    if (request.operation === "advance") {
      const before = await state();
      const next = await action();
      if (next.kind === "exhausted") {
        await outcome("empty", "EXHAUSTED", "approved pagination control was absent");
        terminal = true;
      } else if (next.kind === "denied" || (next.kind === "anchor" && !inScope(next.href))) {
        throw new Error("TARGET_DENIED");
      } else {
        if (next.kind === "scroll") {
          await page.mouse.wheel(0, 600, { label: "advance result page" });
        } else {
          await page.click(request.pagination.selector, { label: "advance result page" });
        }
        await assertScoped();
        try {
          await changed(before, request.pagination.mode);
        } catch (error) {
          const message = error instanceof Error ? error.message : "browser action failed";
          if (request.pagination.mode !== "scroll" || !isTimeout(message)) {
            throw error;
          }
        }
        await ready();
      }
    }
  }
  if (!terminal) {
    await assertScoped();
    const html = await page.evaluate(() => document.documentElement.outerHTML);
    const bytes = new TextEncoder().encode(html);
    if (bytes.length > 2 * 1024 * 1024) {
      await outcome("truncated", "CONTENT_TRUNCATED", "browser result exceeded the DOM limit");
    } else {
      await emit({ url: await page.url(), html, observedAt: new Date().toISOString(), sha256: crypto.createHash("sha256").update(bytes).digest("hex"), status: "completed", error: null });
    }
  }
} catch (error) {
  const message = error instanceof Error ? error.message : "browser action failed";
  const timeout = isTimeout(message);
  const denied = message === "TARGET_DENIED";
  const unavailable = isUnavailable(message);
  await outcome(timeout ? "timeout" : denied ? "denied" : unavailable ? "unavailable" : "failed", timeout ? "FETCH_TIMEOUT" : denied ? "TARGET_DENIED" : unavailable ? "CAPABILITY_UNAVAILABLE" : "ACTION_FAILED", timeout ? "browser action timed out and may have late effects" : denied ? "browser target was rejected" : unavailable ? "browser space or page is unavailable" : "browser action failed");
}
