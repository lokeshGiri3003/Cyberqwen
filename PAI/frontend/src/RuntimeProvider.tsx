"use client";

import { useMemo, type ReactNode } from "react";
import {
  AssistantRuntimeProvider,
  useRemoteThreadListRuntime,
  useAui,
  useLocalRuntime,
  RuntimeAdapterProvider,
  AuiConfig,
  Tools,
  type ThreadHistoryAdapter,
} from "@assistant-ui/react";
import { CyberQwenModelAdapter } from "./useCyberQwenRuntime";
import { cyberQwenThreadListAdapter } from "./threadListAdapter";
import approvalToolkit from "./approvalToolkit";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Runs inside each active thread's context — this is where we attach the
// per-thread message history adapter (load/append), since it needs the
// thread's remoteId, which only exists once a thread is actually active.
function ThreadHistoryProvider({ children }: { children: ReactNode }) {
  const aui = useAui();

  const history = useMemo<ThreadHistoryAdapter>(
    () => ({
      async load() {
        const { remoteId } = aui.threadListItem().getState();
        if (!remoteId) return { messages: [] };
        const res = await fetch(`${BACKEND_URL}/api/threads/${remoteId}/messages`);
        const data = await res.json();
        return { messages: data.messages };
      },
      async append(item) {
        // await initialize() rather than checking remoteId directly — avoids
        // dropping the first message to a race condition (see assistant-ui docs).
        const { remoteId } = await aui.threadListItem().initialize();
        await fetch(`${BACKEND_URL}/api/threads/${remoteId}/messages`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item }),
        });
      },
    }),
    [aui],
  );

  const adapters = useMemo(() => ({ history }), [history]);

  return <RuntimeAdapterProvider adapters={adapters}>{children}</RuntimeAdapterProvider>;
}

export function RuntimeProvider({ children }: { children: ReactNode }) {
  const runtime = useRemoteThreadListRuntime({
    runtimeHook: () =>
      useLocalRuntime(CyberQwenModelAdapter, {
        // Tells LocalRuntime that `requestApproval` is completed by a human via
        // addResult() in approvalToolkit.tsx. WITHOUT this, the runtime renders
        // the approval card, records the decision, but never re-runs the adapter
        // — so the agent stalls after approval (assistant-ui issue #2374).
        // With it, addResult() auto-continues: the adapter POSTs to /api/chat
        // again with the tool-call + result, and server.py resumes the agent.
        unstable_humanToolNames: ["requestApproval"],
      }),
    adapter: {
      ...cyberQwenThreadListAdapter,
      unstable_Provider: ThreadHistoryProvider,
    },
  });

  const config = useMemo(
    () => AuiConfig({ tools: Tools({ toolkit: approvalToolkit }) }),
    [],
  );

  return (
    <AssistantRuntimeProvider runtime={runtime} config={config}>
      {children}
    </AssistantRuntimeProvider>
  );
}
