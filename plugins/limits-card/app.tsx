// bb-plugin-limits-card — frontend entry.
//
// A compact disclosure card in the sidebar footer. The data comes from this
// plugin's `get_limits` RPC, which asks BB's Provider usage plugin for the
// aggregated snapshot.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  definePluginApp,
  experimental_Icon as Icon,
  useRpc,
  type ExperimentalSidebarFooterDisclosureProps,
} from "@get-bb/plugin-sdk/app";
import type { LimitRow, LimitsResponse, rpcContract } from "./server";
import { cn } from "@/lib/utils";

function barTone(usedPercent: number): string {
  if (usedPercent >= 95) return "bg-destructive";
  if (usedPercent >= 80) return "bg-warning";
  return "bg-primary";
}

function textTone(usedPercent: number): string {
  if (usedPercent >= 95) return "text-destructive";
  if (usedPercent >= 80) return "text-warning";
  return "text-sidebar-foreground";
}

function formatPercent(usedPercent: number): string {
  if (Number.isInteger(usedPercent)) return `${usedPercent}%`;
  return `${usedPercent.toFixed(1)}%`;
}

function formatCountdown(resetsAt: string | null, now: number): string | null {
  if (resetsAt === null) return null;
  const target = new Date(resetsAt).getTime();
  if (!Number.isFinite(target)) return null;
  const remaining = target - now;
  if (remaining <= 0) return "now";
  const minutes = Math.ceil(remaining / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
  }
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  return rest === 0 ? `${days}d` : `${days}d ${rest}h`;
}

function exactReset(resetsAt: string | null): string | null {
  if (resetsAt === null) return null;
  const date = new Date(resetsAt);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function quotaTitle(row: LimitRow): string {
  if (row.usedPercent === null) {
    return `${row.label}: ${row.detail ?? "no data"}`;
  }
  const reset = exactReset(row.resetsAt);
  return (
    `${row.label}: ${formatPercent(row.usedPercent)} used` +
    (reset === null ? " · reset time not reported" : ` · resets ${reset}`)
  );
}

function QuotaLine({ row, now }: { row: LimitRow; now: number }) {
  const countdown = formatCountdown(row.resetsAt, now);
  return (
    <div
      className="grid h-6 grid-cols-[minmax(0,1fr)_minmax(0,0.85fr)_auto_auto] items-center gap-x-2 text-2xs"
      title={quotaTitle(row)}
    >
      <span className="truncate text-subtle-foreground">{row.label}</span>
      <span className="h-1 min-w-0 overflow-hidden rounded-full bg-sidebar-border">
        {row.usedPercent === null ? null : (
          <span
            className={cn(
              "block h-full rounded-full",
              barTone(row.usedPercent),
            )}
            style={{
              width: Math.min(100, Math.max(0, row.usedPercent)) + "%",
            }}
          />
        )}
      </span>
      <span
        className={cn(
          "w-9 text-right tabular-nums",
          row.usedPercent === null
            ? "text-subtle-foreground"
            : textTone(row.usedPercent),
        )}
      >
        {row.usedPercent === null ? "—" : formatPercent(row.usedPercent)}
      </span>
      <span className="w-10 text-right tabular-nums text-subtle-foreground">
        {row.usedPercent === null ? "—" : (countdown ?? "—")}
      </span>
    </div>
  );
}

function BalanceLine({ row }: { row: LimitRow }) {
  return (
    <div
      className="flex h-6 min-w-0 items-center gap-2 text-2xs"
      title={`${row.label}: ${row.value ?? row.detail ?? "no data"}`}
    >
      <span className="min-w-0 flex-1 truncate text-subtle-foreground">
        {row.label}
      </span>
      <span
        className={cn(
          "shrink-0 tabular-nums",
          row.value === null
            ? "text-subtle-foreground"
            : "text-sidebar-foreground",
        )}
      >
        {row.value ?? "—"}
      </span>
    </div>
  );
}

function ErrorNotice({ message, onRetry }: { message: string; onRetry(): void }) {
  return (
    <div
      role="status"
      className="mb-1.5 flex min-w-0 items-center gap-2 rounded-md bg-destructive/10 px-2 py-1 text-2xs text-destructive"
    >
      <span className="min-w-0 flex-1 truncate" title={message}>
        {message}
      </span>
      <button
        type="button"
        className="shrink-0 underline underline-offset-2"
        onClick={onRetry}
      >
        Retry
      </button>
    </div>
  );
}

export function LimitsCard({
  dismiss,
}: ExperimentalSidebarFooterDisclosureProps) {
  const rpc = useRpc<typeof rpcContract>();
  const [data, setData] = useState<LimitsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const alive = useRef(true);
  const inFlight = useRef(false);
  const dataRef = useRef<LimitsResponse | null>(null);

  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  const refresh = useCallback(
    async (force: boolean) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setBusy(true);
      try {
        const result = await rpc.call("get_limits", { refresh: force });
        if (!alive.current) return;
        if (result.error !== null && dataRef.current !== null) {
          setError(result.error);
          return;
        }
        dataRef.current = result;
        setData(result);
        setError(result.error);
      } catch (cause) {
        if (!alive.current) return;
        setError(
          cause instanceof Error && cause.message.trim() !== ""
            ? cause.message
            : "Couldn’t load limits.",
        );
      } finally {
        inFlight.current = false;
        if (alive.current) setBusy(false);
      }
    },
    [rpc],
  );

  useEffect(() => {
    alive.current = true;
    void refresh(false);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh(false);
    }, 60_000);
    const onFocus = () => void refresh(false);
    window.addEventListener("focus", onFocus);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const reload = () => void refresh(true);
  const quotas = data?.rows.filter((row) => row.kind === "quota") ?? [];
  const balances = data?.rows.filter((row) => row.kind === "balance") ?? [];

  return (
    <div
      className="flex min-h-0 flex-col"
      role="group"
      aria-label="Provider limits"
    >
      <div className="flex h-9 min-w-0 shrink-0 items-center gap-1 border-b border-sidebar-border px-1.5">
        <span className="px-1.5 text-2xs font-medium text-sidebar-foreground">
          Limits
        </span>
        <span
          className="min-w-0 flex-1 truncate px-1 text-right text-2xs text-subtle-foreground"
          title={data?.machine ?? undefined}
        >
          {data?.machine ?? ""}
        </span>
        <button
          type="button"
          aria-label="Reload limits"
          disabled={busy}
          className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring disabled:opacity-50"
          onClick={reload}
        >
          <Icon
            name="RotateCcw"
            aria-hidden="true"
            className={cn("size-3.5", busy && "animate-spin")}
          />
        </button>
        <button
          type="button"
          aria-label="Collapse limits"
          className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
          onClick={dismiss}
        >
          <Icon name="ChevronDown" aria-hidden="true" className="size-4" />
        </button>
      </div>
      <div className="min-h-0 overflow-y-auto px-2.5 py-2">
        {error === null ? null : (
          <ErrorNotice message={error} onRetry={reload} />
        )}
        {data === null ? (
          <p className="py-1 text-2xs text-subtle-foreground">
            {busy ? "Loading limits…" : "Limits are unavailable."}
          </p>
        ) : (
          <div className="grid gap-y-0.5">
            {quotas.map((row) => (
              <QuotaLine key={row.id} row={row} now={now} />
            ))}
            {balances.length === 0 ? null : (
              <div className="mt-1 grid gap-y-0.5 border-t border-sidebar-border pt-1">
                {balances.map((row) => (
                  <BalanceLine key={row.id} row={row} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default definePluginApp((app) => {
  app.experimental_sidebarFooter.register({
    kind: "disclosure",
    id: "limits",
    label: "Limits",
    icon: "ChartColumn",
    component: LimitsCard,
  });
});
