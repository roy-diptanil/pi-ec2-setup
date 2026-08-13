import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const STATE_FILE = join(process.env.HOME ?? "", ".pi", "agent", "google-workspace-setup");
const HOOKS = ["block-destructive.sh", "check-domain.sh", "check-setup.sh"];
const OAUTH_DIR = join(process.env.HOME ?? "", ".pi", "agent", "google-workspace-oauth");
const OAUTH_LOG = join(OAUTH_DIR, "oauth.log");
const OAUTH_STATE = join(OAUTH_DIR, "state.json");

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function runHook(name: string, command: string): string | undefined {
  const result = spawnSync(join(ROOT, "hooks", name), [], {
    input: JSON.stringify({ tool_input: { command } }),
    encoding: "utf8",
    timeout: 5_000,
    env: { ...process.env, CLAUDE_PLUGIN_ROOT: ROOT },
  });

  if (result.error) return `Google Workspace policy check failed closed: ${result.error.message}`;
  if (result.status === 0) return undefined;
  return (result.stderr || result.stdout || `${name} exited with status ${result.status}`).trim();
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const command = (event.input as { command?: unknown }).command;
    if (typeof command !== "string") {
      return { block: true, reason: "Google Workspace policy could not inspect a malformed Bash command." };
    }

    for (const hook of HOOKS) {
      const reason = runHook(hook, command);
      if (reason) return { block: true, reason };
    }
  });

  pi.registerCommand("google-workspace-setup", {
    description: "Install and prepare the Upstart Google Workspace integration",
    handler: async (_args, ctx) => {
      writeFileSync(STATE_FILE, "in_progress\n", { mode: 0o600 });
      const install = await pi.exec("bash", [join(ROOT, "scripts", "install-gws.sh")], { timeout: 120_000 });
      if (install.code !== 0) {
        ctx.ui.notify((install.stderr || install.stdout).trim() || "gws installation failed", "error");
        return;
      }
      const secret = await pi.exec("bash", [join(ROOT, "scripts", "install-secret.sh")], { timeout: 10_000 });
      if (secret.code !== 0) {
        ctx.ui.notify((secret.stderr || secret.stdout).trim() || "OAuth client installation failed", "error");
        return;
      }
      ctx.ui.setEditorText(`/google-workspace-auth`);
      ctx.ui.notify("gws and the Upstart OAuth client are ready. Press Enter to start authentication.", "info");
    },
  });

  pi.registerCommand("google-workspace-auth", {
    description: "Start Google Workspace OAuth without SSH port forwarding",
    handler: async (_args, ctx) => {
      writeFileSync(STATE_FILE, "in_progress\n", { mode: 0o600 });
      mkdirSync(OAUTH_DIR, { recursive: true, mode: 0o700 });
      writeFileSync(OAUTH_LOG, "", { mode: 0o600 });
      const fd = openSync(OAUTH_LOG, "a", 0o600);
      const child = spawn("bash", [join(ROOT, "scripts", "setup-oauth.sh")], {
        detached: true,
        stdio: ["ignore", fd, fd],
        env: process.env,
      });
      child.unref();
      closeSync(fd);

      let authUrl: string | undefined;
      for (let attempt = 0; attempt < 100; attempt++) {
        await sleep(200);
        const log = readFileSync(OAUTH_LOG, "utf8");
        authUrl = log.match(/GWS_AUTH_URL:\s*(https:\/\/\S+)/)?.[1];
        if (authUrl) break;
        if (child.exitCode !== null) {
          ctx.ui.notify(log.trim() || "Google OAuth exited before producing a URL", "error");
          return;
        }
      }
      if (!authUrl) {
        ctx.ui.notify(`Timed out waiting for OAuth URL. See ${OAUTH_LOG}`, "error");
        return;
      }

      const redirectUri = new URL(authUrl).searchParams.get("redirect_uri");
      writeFileSync(OAUTH_STATE, JSON.stringify({ pid: child.pid, redirectUri }), { mode: 0o600 });
      if (ctx.hasUI) await ctx.ui.editor("Open this Google sign-in URL locally", authUrl);
      else console.log(authUrl);
      ctx.ui.notify("After the local callback page fails, copy its full URL and run /google-workspace-auth-complete <URL>.", "info");
    },
  });

  pi.registerCommand("google-workspace-auth-complete", {
    description: "Relay a failed localhost Google OAuth callback on the remote host",
    handler: async (args, ctx) => {
      let callback: URL;
      try {
        callback = new URL(args.trim());
      } catch {
        ctx.ui.notify("Usage: /google-workspace-auth-complete http://localhost:PORT/?code=...&state=...", "error");
        return;
      }
      const expected = JSON.parse(readFileSync(OAUTH_STATE, "utf8")) as { redirectUri?: string };
      const expectedUrl = expected.redirectUri ? new URL(expected.redirectUri) : undefined;
      if (callback.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(callback.hostname)
          || !expectedUrl || callback.port !== expectedUrl.port) {
        ctx.ui.notify("Callback host/port does not match the active local OAuth listener.", "error");
        return;
      }
      try {
        await fetch(callback, { signal: AbortSignal.timeout(15_000) });
      } catch (error) {
        ctx.ui.notify(`Callback relay failed: ${error instanceof Error ? error.message : String(error)}`, "error");
        return;
      }
      await sleep(1_000);
      const verify = await pi.exec("gws", ["drive", "files", "list", "--params", '{"pageSize":1}'], { timeout: 30_000 });
      if (verify.code !== 0) {
        ctx.ui.notify((verify.stderr || verify.stdout).trim() || `OAuth callback relayed; verification failed. See ${OAUTH_LOG}`, "error");
        return;
      }
      writeFileSync(STATE_FILE, "completed\n", { mode: 0o600 });
      ctx.ui.notify("Google Workspace authenticated, verified, and enabled.", "info");
    },
  });

  pi.registerCommand("google-workspace-doctor", {
    description: "Check the Upstart Google Workspace integration",
    handler: async (_args, ctx) => {
      const result = await pi.exec("bash", [join(ROOT, "scripts", "doctor.sh")], { timeout: 30_000 });
      const output = `${result.stdout}${result.stderr}`.trim();
      if (ctx.hasUI) await ctx.ui.editor("Google Workspace diagnostics", output || "No output");
      else console.log(output);
    },
  });

  pi.registerCommand("google-workspace-complete", {
    description: "Verify Google Workspace OAuth and mark setup complete",
    handler: async (_args, ctx) => {
      const result = await pi.exec("gws", ["drive", "files", "list", "--params", '{"pageSize":1}'], { timeout: 30_000 });
      if (result.code !== 0) {
        ctx.ui.notify((result.stderr || result.stdout).trim() || "Drive verification failed", "error");
        return;
      }
      writeFileSync(STATE_FILE, "completed\n", { mode: 0o600 });
      ctx.ui.notify("Google Workspace authenticated and enabled.", "info");
    },
  });
}
