"use client";

import { defineToolkit } from "@assistant-ui/react";
import { z } from "zod";

// A "human" toolkit tool: the model doesn't decide the outcome, the operator
// does. render() shows an approve/deny card; clicking a button calls
// addResult(), which assistant-ui feeds back as this tool's result and
// automatically resends the conversation so agent.py can resume.
export default defineToolkit({
  requestApproval: {
    type: "human",
    description:
      "Ask the operator to approve or deny a sensitive tool call before it runs.",
    parameters: z.object({
      tool: z.string(),
      arguments: z.record(z.string(), z.any()),
    }),
    render: ({ args, result, addResult }) => {
      if (result) {
        return (
          <div
            className={
              result.approved
                ? "my-2 rounded border border-green-600 bg-green-50 p-3 text-green-700 dark:bg-green-950 dark:text-green-400"
                : "my-2 rounded border border-red-600 bg-red-50 p-3 text-red-700 dark:bg-red-950 dark:text-red-400"
            }
          >
            {result.approved ? `✅ Approved: ${args.tool}` : `❌ Denied: ${args.tool}`}
          </div>
        );
      }

      return (
        <div className="my-2 rounded border-2 border-yellow-500 bg-card p-4">
          <div className="mb-2 font-bold">Approval required</div>
          <div className="mb-2 text-sm">
            Tool: <code className="rounded bg-muted px-1">{args.tool}</code>
          </div>
          <pre className="mb-3 max-h-40 overflow-x-auto rounded bg-muted p-2 text-xs">
            {JSON.stringify(args.arguments, null, 2)}
          </pre>
          <div className="flex gap-2">
            <button
              onClick={() => addResult({ approved: true })}
              className="rounded bg-green-600 px-3 py-1.5 text-sm text-white hover:bg-green-700"
            >
              Approve
            </button>
            <button
              onClick={() => addResult({ approved: false })}
              className="rounded bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700"
            >
              Deny
            </button>
          </div>
        </div>
      );
    },
  },
});
