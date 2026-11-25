import { definePluginApp, experimental_useRpcMutation, experimental_useRpcQuery } from "@get-bb/plugin-sdk/app";
import { JevActivity } from "./activity-banner.tsx";
import { modeRpc } from "./mode-rpc.ts";

export default definePluginApp(app => {
  app.composer.customize({
    id: "jev-activity", scopes: ["thread"],
    banners: [{ id: "jev-status", chrome: "bare", component: JevActivity }],
  });
  app.slots.experimental_threadAction({
    id: "jev-mode", title: "Jev", icon: "ClipboardCheck", group: "jev", placement: "header",
    useData({ threadIds }) {
      const query = experimental_useRpcQuery({ contract: modeRpc, method: "jevstice.mode.list",
        input: { threadIds: [...threadIds] }, enabled: threadIds.length > 0 });
      const change = experimental_useRpcMutation({ contract: modeRpc, method: "jevstice.mode.set" });
      return { query, change };
    },
    item({ thread, data: { query, change } }) {
      const mode = query.data?.[thread.id];
      if (mode === undefined && !query.error) return null;
      const pending = change.pendingInputs.some(input => input.threadId === thread.id);
      return {
        label: mode === "on" || mode === "inherited" ? "Jev ✓" : "Jev",
        icon: "ClipboardCheck",
        detail: mode === "inherited" ? "Включён в родительском треде"
          : mode === "on" ? "Выключить Jevstice"
          : mode === "off" ? "Включить Jevstice"
          : "Повторить загрузку статуса Jevstice",
        disabled: pending || mode === "inherited",
        async run() {
          if (mode === undefined) await query.refetch();
          else await change.mutate({ threadId: thread.id, active: mode === "off" });
        },
      };
    },
  });
});
