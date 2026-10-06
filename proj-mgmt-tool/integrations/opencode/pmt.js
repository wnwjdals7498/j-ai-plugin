import { spawn } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const EVENTS = new Set(["session.created", "session.idle", "session.error"])
const MAX_INPUT = 64 * 1024
const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..")
const BRIDGE = resolve(PLUGIN_ROOT, "integrations/opencode/bridge.py")

function minimalInput(event) {
  const properties = event?.properties && typeof event.properties === "object" ? event.properties : {}
  const info = properties.info && typeof properties.info === "object" ? properties.info : {}
  const safe = {}
  for (const key of ["sessionID", "eventID", "status", "tool", "time"]) {
    const value = properties[key]
    if (typeof value === "string" && value.length <= 256) safe[key] = value
  }
  if (typeof info.id === "string" && info.id.length <= 256) safe.info = { id: info.id }
  return { properties: safe }
}

function sendToBridge(eventType, input, timeoutMs = 1500) {
  return new Promise((resolve, reject) => {
    const python = process.env.PMT_PYTHON || "python"
    const child = spawn(python, [BRIDGE, "--product", "opencode", "--event", eventType], {
      shell: false,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    })
    let stderr = ""
    let stdoutBytes = 0
    const timer = setTimeout(() => child.kill(), timeoutMs)
    child.stdout.on("data", (chunk) => { stdoutBytes += chunk.length })
    child.stderr.on("data", (chunk) => { if (stderr.length < 4096) stderr += chunk.toString("utf8") })
    child.on("error", reject)
    child.on("close", (code) => {
      clearTimeout(timer)
      if (code !== 0) return reject(new Error("bridge failed"))
      if (stdoutBytes) return reject(new Error("bridge emitted unexpected stdout"))
      resolve(stderr)
    })
    child.stdin.end(JSON.stringify(input))
  })
}

function readContextFromBridge(sessionID, timeoutMs = 1500) {
  return new Promise((resolve, reject) => {
    const python = process.env.PMT_PYTHON || "python"
    const child = spawn(python, [BRIDGE, "--product", "opencode", "--read-context"], {
      shell: false,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    })
    let stdout = ""
    let stderrBytes = 0
    const timer = setTimeout(() => child.kill(), timeoutMs)
    child.stdout.on("data", (chunk) => {
      if (stdout.length + chunk.length <= 32 * 1024) stdout += chunk.toString("utf8")
      else child.kill()
    })
    child.stderr.on("data", (chunk) => { stderrBytes += chunk.length })
    child.on("error", reject)
    child.on("close", (code) => {
      clearTimeout(timer)
      if (code !== 0 || stderrBytes) return reject(new Error("context bridge failed"))
      try { resolve(JSON.parse(stdout)) } catch { reject(new Error("context bridge returned invalid JSON")) }
    })
    child.stdin.end(JSON.stringify({ session_id: sessionID, native_event: "session.created" }))
  })
}

async function record(eventType, input, client) {
    if (Buffer.byteLength(JSON.stringify(input), "utf8") > MAX_INPUT) return
    try {
      const warning = await sendToBridge(eventType, input)
      if (warning.trim()) {
        await client.app.log({ body: { service: "pmt", level: "warn", message: "PMT could not confirm event storage; pending event retained." } })
      }
    } catch {
      await client.app.log({ body: { service: "pmt", level: "warn", message: "PMT event bridge unavailable; event may not be stored." } }).catch(() => {})
    }
}

async function logContextFailure(client, errorCode) {
  try {
    await client.app.log({ body: {
      service: "pmt", level: "warn",
      message: `PMT startup context unavailable (${errorCode || "context_read_failed"}).`,
    } })
  } catch { /* logging failure must not block the session */ }
}

export const PmtPlugin = async ({ client }) => {
  const sessionContext = new Map()
  const contextFailed = new Set()
  const contextPending = new Map()
  const loadSessionContext = async (sessionID) => {
    if (!process.env.PMT_SCOPE_ID?.trim()) return { status: "not_configured" }
    if (sessionContext.has(sessionID)) return { status: "ok", context_markdown: sessionContext.get(sessionID) }
    if (contextFailed.has(sessionID)) return { status: "unavailable" }
    if (contextPending.has(sessionID)) return contextPending.get(sessionID)
    const pending = readContextFromBridge(sessionID).then(async (result) => {
      if (result?.status === "ok" && typeof result.context_markdown === "string") {
        sessionContext.set(sessionID, result.context_markdown)
      } else if (result?.status !== "not_configured") {
        contextFailed.add(sessionID)
        await logContextFailure(client, result?.error_code)
      }
      return result
    }).catch(async () => {
      contextFailed.add(sessionID)
      await logContextFailure(client, "context_read_failed")
      return { status: "unavailable", error_code: "context_read_failed" }
    }).finally(() => contextPending.delete(sessionID))
    contextPending.set(sessionID, pending)
    return pending
  }

  return {
  event: async ({ event }) => {
    const eventType = event?.type
    const properties = event?.properties && typeof event.properties === "object" ? event.properties : {}
    const info = properties.info && typeof properties.info === "object" ? properties.info : {}
    const sessionID = typeof properties.sessionID === "string" ? properties.sessionID : info.id
    if (eventType === "session.deleted" && typeof sessionID === "string") {
      sessionContext.delete(sessionID)
      contextFailed.delete(sessionID)
      contextPending.delete(sessionID)
      return
    }
    if (!EVENTS.has(eventType)) return
    const tasks = [record(eventType, minimalInput(event), client)]
    if (eventType === "session.created" && typeof sessionID === "string") {
      tasks.push(loadSessionContext(sessionID))
    }
    await Promise.all(tasks)
  },
  // OpenCode 1.18 exposes this as a tool hook with input/output arguments,
  // not as a server event whose event.type is tool.execute.after.
  "tool.execute.after": async (input) => {
    if (!input || typeof input.sessionID !== "string" || typeof input.callID !== "string") return
    await record("tool.execute.after", { properties: {
      sessionID: input.sessionID,
      eventID: input.callID,
      tool: typeof input.tool === "string" ? input.tool : undefined,
      status: "completed",
    } }, client)
  },
  "experimental.chat.system.transform": async (input, output) => {
    if (typeof input?.sessionID !== "string" || !process.env.PMT_SCOPE_ID?.trim()) return
    await loadSessionContext(input.sessionID)
    const context = sessionContext.get(input.sessionID)
    if (context && !output.system.includes(context)) output.system.push(context)
  },
  }
}
