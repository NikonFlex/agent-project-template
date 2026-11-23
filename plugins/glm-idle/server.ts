import { isAbsolute, relative, resolve, sep } from "node:path";
import { z } from "zod";
import {
  cliCommand,
  defineCli,
  PluginCliError,
  type BbPluginApi,
  type PluginCliContext,
} from "@get-bb/plugin-sdk";
import { desktopStatusSchema, resultSchema, rpcContract, type Status } from "./contract.js";
import { readDesktop, submitDesktop, type DesktopEndpoint } from "./desktop.js";
import { offPeakStatus } from "./off-peak.js";

const maxPromptBytes = 64 * 1024;
const opencodeModel = "zai-coding-plan/glm-5.3-flash";
const statusTimeoutMs = 20_000;

function json(value: unknown) {
  return { exitCode: 0, stdout: JSON.stringify(value, null, 2) + "\n" };
}

async function status(endpoint: DesktopEndpoint | undefined): Promise<Status> {
  const openCode = offPeakStatus(Date.now());
  if (!endpoint) return { desktop: null, desktopUnavailable: "not_configured", openCode };
  try {
    return { desktop: desktopStatusSchema.parse(await readDesktop(endpoint)), desktopUnavailable: null, openCode };
  } catch {
    return { desktop: null, desktopUnavailable: "unavailable", openCode };
  }
}

async function worktree(bb: BbPluginApi, ctx: PluginCliContext) {
  if (!ctx.threadId) {
    throw new PluginCliError("Run this command from a BB thread terminal.", { code: "thread_required" });
  }
  const thread = await bb.sdk.threads.get({ threadId: ctx.threadId });
  if (!thread.environmentId) {
    throw new PluginCliError("The BB thread has no environment.", { code: "worktree_required" });
  }
  const env = await bb.sdk.environments.get({ environmentId: thread.environmentId });
  if (!env.path || !env.isWorktree || env.status !== "ready" || env.lifecycle.phase !== "active") {
    throw new PluginCliError("Run this command from a ready BB worktree.", { code: "worktree_required" });
  }
  return { ...env, path: env.path, threadId: ctx.threadId };
}

async function promptFile(bb: BbPluginApi, env: { path: string; hostId: string }, file: string) {
  const path = resolve(env.path, file);
  const inside = relative(env.path, path);
  if (!inside || inside === ".." || inside.startsWith(".." + sep) || isAbsolute(inside)) {
    throw new PluginCliError("Select a prompt file inside the BB worktree.", { code: "invalid_prompt_path" });
  }
  const result = await bb.sdk.files.read({
    hostId: env.hostId, rootPath: env.path, path, signal: AbortSignal.timeout(statusTimeoutMs),
  });
  if (result.contentEncoding !== "utf8" || !result.content.trim()
      || Buffer.byteLength(result.content) > maxPromptBytes) {
    throw new PluginCliError("The prompt must be a non-empty UTF-8 file up to 64 KiB.", { code: "invalid_prompt" });
  }
  return result.content;
}

const promptOption = { type: "string", required: true, description: "UTF-8 prompt file in this worktree" } as const;
const titleOption = { type: "string", default: "GLM task", description: "Task title" } as const;
function scheduledTime(at: string): number {
  const time = Date.parse(at);
  if (!/T.+(?:Z|[+-]\d\d:\d\d)$/i.test(at) || !Number.isFinite(time) || time <= Date.now()) {
    throw new PluginCliError("Use a future ISO 8601 timestamp with a timezone.", { code: "invalid_time" });
  }
  if (!offPeakStatus(time).offPeak) {
    throw new PluginCliError("This time is during Z.ai peak hours.", { code: "peak_hours" });
  }
  return time;
}

const namedWorktreesSchema = z.record(z.string(), z.object({
  path: z.string().refine(isAbsolute),
  readOnly: z.boolean(),
}));

function namedWorktree(setting: string, name: string) {
  const parsed = namedWorktreesSchema.safeParse(JSON.parse(setting));
  const workspace = parsed.success && Object.hasOwn(parsed.data, name) ? parsed.data[name] : undefined;
  if (!workspace) {
    throw new PluginCliError("Unknown approved ZCode worktree.", { code: "unknown_workspace" });
  }
  return workspace;
}

export default function plugin(bb: BbPluginApi): void {
  const settings = bb.settings.define({
    desktopSocket: {
      type: "string",
      label: "ZCode Desktop Unix socket",
      description: "Private socket of the managed ZCode Desktop service.",
    },
    desktopWorktreeId: {
      type: "string",
      label: "Approved BB worktree",
      description: "The BB environment ID mounted for the managed ZCode service.",
    },
    desktopWorktreePath: {
      type: "string",
      label: "ZCode worktree mount",
      description: "The approved worktree path inside the ZCode service.",
    },
    desktopWorktreeReadOnly: {
      type: "boolean",
      label: "Worktree mount is read-only",
      default: true,
    },
    desktopNamedWorktrees: {
      type: "string",
      label: "Named ZCode worktrees",
      description: 'Allowlist as JSON: {"name":{"path":"/absolute/path","readOnly":true}}.',
      default: "{}",
    },
    desktopDebugPort: {
      type: "number",
      label: "ZCode Desktop loopback port",
      description: "Debugging port of the dedicated, local ZCode 4.0 instance.",
    },
    desktopHostId: {
      type: "string",
      label: "ZCode Desktop BB host ID",
      description: "Only worktrees on this host may be submitted to ZCode Desktop.",
    },
  });
  let submitting = false;
  async function getStatus() {
    const { desktopSocket, desktopDebugPort } = await settings.get();
    return status(desktopSocket || desktopDebugPort);
  }

  bb.rpc.register(rpcContract, { glm_status: getStatus });
  bb.cli.register(defineCli({
    name: "glm",
    summary: "ZCode Idle queue and OpenCode off-peak scheduling",
    commands: {
      status: cliCommand({
        summary: "Show ZCode availability, recent tasks and the OpenCode window",
        async run() { return json(await getStatus()); },
      }),
      "idle submit": cliCommand({
        summary: "Submit to the free ZCode queue from this BB worktree",
        options: {
          "prompt-file": promptOption,
          title: titleOption,
          workspace: { type: "string", description: "Choose a named worktree approved by the administrator" },
          "read-only": { type: "boolean", description: "Use ZCode's plan mode" },
        },
        async run({ options }, ctx) {
          if (submitting) throw new PluginCliError("Another GLM submission is in progress.", { code: "busy" });
          submitting = true;
          try {
            const config = await settings.get();
            const endpoint = config.desktopSocket || config.desktopDebugPort;
            if (!endpoint || !config.desktopHostId) {
              throw new PluginCliError("Configure the dedicated ZCode Desktop first.", { code: "desktop_required" });
            }
            const env = await worktree(bb, ctx);
            if (env.hostId !== config.desktopHostId) {
              throw new PluginCliError("ZCode Desktop and the worktree must share a host.", { code: "wrong_host" });
            }
            const readOnly = options["read-only"] === true;
            let workspacePath = env.path;
            let promptPath = env.path;
            if (options.workspace) {
              const workspace = namedWorktree(config.desktopNamedWorktrees, options.workspace);
              if (workspace.readOnly && !readOnly) {
                throw new PluginCliError("This ZCode worktree is mounted read-only. Use --read-only.",
                  { code: "workspace_read_only" });
              }
              workspacePath = workspace.path;
              promptPath = workspace.path;
            } else if (config.desktopSocket) {
              const mount = config.desktopWorktreePath;
              if (!mount || !isAbsolute(mount) || env.id !== config.desktopWorktreeId) {
                throw new PluginCliError("This BB worktree is not mounted for the ZCode service.",
                  { code: "workspace_not_mounted" });
              }
              if (config.desktopWorktreeReadOnly && !readOnly) {
                throw new PluginCliError("This ZCode worktree is mounted read-only. Use --read-only.",
                  { code: "workspace_read_only" });
              }
              workspacePath = mount;
            }
            const prompt = await promptFile(bb, { hostId: env.hostId, path: promptPath }, options["prompt-file"]);
            let raw: unknown;
            try {
              raw = await submitDesktop(endpoint, {
                title: options.title,
                prompt,
                workspacePath,
                permissionMode: readOnly ? "plan" : "build",
              });
            } catch {
              throw new PluginCliError("ZCode did not confirm submission. Check bb glm status before retrying.",
                { code: "desktop_not_confirmed" });
            }
            const result = resultSchema.parse(raw);
            const output = { ...result, thread: `@thread:${env.threadId}` };
            return result.ok ? json(output) : { ...json(output), exitCode: 1 };
          } finally {
            submitting = false;
          }
        },
      }),
      "offpeak schedule": cliCommand({
        summary: "Schedule an OpenCode thread using ordinary Coding Plan quota",
        options: {
          "prompt-file": promptOption,
          title: titleOption,
          at: { type: "string", required: true, description: "Future ISO 8601 time with timezone" },
        },
        async run({ options }, ctx) {
          const sendAt = scheduledTime(options.at);
          const env = await worktree(bb, ctx);
          const prompt = await promptFile(bb, env, options["prompt-file"]);
          const thread = await bb.sdk.threads.spawn({
            projectId: env.projectId, prompt, sendAt,
            environment: { type: "host", hostId: env.hostId,
              workspace: { type: "managed-worktree", baseBranch: { kind: "default" } } },
            parentThreadId: env.threadId, parentNotifications: "off", title: options.title,
            providerId: "acp-opencode", model: opencodeModel, permissionMode: "accept-edits",
          });
          return json({ thread: `@thread:${thread.id}`, sendAt: new Date(sendAt).toISOString() });
        },
      }),
    },
  }));
}
