"use client";

import { type ChatModelAdapter } from "@assistant-ui/react";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// Exported directly (not wrapped in useLocalRuntime here) so the thread-list
// runtime in RuntimeProvider.tsx can spawn a fresh useLocalRuntime(this)
// per active thread instead of one hardcoded single-thread runtime.
export const CyberQwenModelAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal }) {
    const response = await fetch(`${BACKEND_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: messages.map((m) => ({
          role: m.role,
          content: m.content,
        })),
      }),
      signal: abortSignal,
    });

    if (!response.ok) {
      throw new Error(`Backend error: ${response.statusText}`);
    }

    const reader = response.body?.getReader();
    if (!reader) return;

    const decoder = new TextDecoder();
    let text = "";
    let checkedShape = false;
    // server.py sends a single-write JSON blob (not streamed text) when a
    // gated tool call needs operator approval — detected once, on the first
    // bytes, so we never flash raw JSON as visible text before recognizing it.
    let isApprovalRequest = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      text += decoder.decode(value, { stream: true });

      if (!checkedShape && text.length > 0) {
        checkedShape = true;
        isApprovalRequest = text.trimStart().startsWith('{"__approval_request__"');
      }

      if (isApprovalRequest) {
        continue; // buffer fully, don't stream partial JSON as text
      }

      yield { content: [{ type: "text", text }] };
    }

    if (isApprovalRequest) {
      try {
        const req = JSON.parse(text);
        const approvalArgs = { tool: req.tool_name, arguments: req.arguments };
        yield {
          content: [
            {
              type: "tool-call",
              toolCallId: `approval-${Date.now()}`,
              toolName: "requestApproval",
              args: approvalArgs,
              // Newer @assistant-ui/react requires argsText (stringified args)
              // on a tool-call part alongside the object form.
              argsText: JSON.stringify(approvalArgs),
            },
          ],
        };
      } catch {
        // fell through JSON.parse — show as text rather than dropping it
        yield { content: [{ type: "text", text }] };
      }
    }
  },
};
