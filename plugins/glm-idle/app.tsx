import { useEffect, useState } from "react";
import { definePluginApp, useRpc } from "@get-bb/plugin-sdk/app";
import type { Status, rpcContract } from "./contract.js";

const refreshMs = 60_000;

function ticketLabel(status: Status) {
  if (!status.desktop) {
    return status.desktopUnavailable === "not_configured" ? "Desktop is not configured" : "Desktop unavailable";
  }
  if (!status.desktop.support.supported) return "Coding Plan unavailable in ZCode";
  if (status.desktop.quota?.canTakeNumber) return "Accepting tasks";
  const next = status.desktop.quota?.nextTakeAt;
  return next ? `Next ticket ${new Date(next).toLocaleString()}` : "Tickets unavailable";
}

function GlmIdleCard() {
  const rpc = useRpc<typeof rpcContract>();
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    async function refresh() {
      try {
        const result = await rpc.call("glm_status", null);
        if (active) { setStatus(result); setError(false); }
      } catch {
        if (active) setError(true);
      }
    }
    void refresh();
    const interval = setInterval(refresh, refreshMs);
    return () => { active = false; clearInterval(interval); };
  }, [rpc, reload]);

  return (
    <div className="grid gap-2 px-3 py-2 text-xs" aria-label="GLM Idle queue">
      <div className="flex justify-between gap-2">
        <span>GLM Idle</span>
        <button type="button" className="underline" onClick={() => setReload((count) => count + 1)}>Refresh</button>
      </div>
      <p>{error ? "Status unavailable" : status ? ticketLabel(status) : "Loading…"}</p>
      {status?.desktop?.tasks.slice(0, 5).map((task) => (
        <p key={task.id} title={task.id} className="truncate">
          {task.id.slice(8, 16)} · {task.status}{task.queuePosition == null ? "" : ` · #${task.queuePosition}`}
        </p>
      ))}
      {status && (
        <p>OpenCode {status.openCode.offPeak ? "off-peak" : "peak"} · until {new Date(status.openCode.nextChangeAt).toLocaleString()}</p>
      )}
    </div>
  );
}

export default definePluginApp((app) => {
  app.experimental_sidebarFooter.register({
    kind: "disclosure", id: "glm-idle", label: "GLM Idle", icon: "Moon", component: GlmIdleCard,
  });
});
