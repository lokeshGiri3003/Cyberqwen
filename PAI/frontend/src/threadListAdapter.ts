"use client";

import type { RemoteThreadListAdapter } from "@assistant-ui/react";
import { createAssistantStream } from "assistant-stream";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

type BackendThread = { id: string; title: string | null; archived: boolean };

export const cyberQwenThreadListAdapter: RemoteThreadListAdapter = {
  // Add this block to satisfy TypeScript
  async fetch(remoteId: string): Promise<any> {
    return {};
  },

  async list() {
    const res = await fetch(`${BACKEND_URL}/api/threads`);
    const data = await res.json();
// ... the rest of the file stays exactly the same
    return {
      threads: (data.threads as BackendThread[]).map((t) => ({
        status: t.archived ? "archived" : "regular",
        remoteId: t.id,
        title: t.title ?? undefined,
      })),
    };
  },

  async initialize(threadId) {
    const res = await fetch(`${BACKEND_URL}/api/threads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ threadId }),
    });
    const thread = await res.json();
    return { remoteId: thread.id };
  },

  async rename(remoteId, newTitle) {
    await fetch(`${BACKEND_URL}/api/threads/${remoteId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle }),
    });
  },

  async archive(remoteId) {
    await fetch(`${BACKEND_URL}/api/threads/${remoteId}/archive`, { method: "POST" });
  },

  async unarchive(remoteId) {
    await fetch(`${BACKEND_URL}/api/threads/${remoteId}/unarchive`, { method: "POST" });
  },

  async delete(remoteId) {
    await fetch(`${BACKEND_URL}/api/threads/${remoteId}`, { method: "DELETE" });
  },

  async generateTitle(remoteId, unstable_messages) {
    const res = await fetch(`${BACKEND_URL}/api/threads/${remoteId}/title`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: unstable_messages }),
    });
    const { title } = await res.json();

    // generateTitle must return an AssistantStream so the UI updates.
    return createAssistantStream((controller) => {
      controller.appendText(title);
      controller.close();
    });
  },
};
