// frontend/src/components/assistant-ui/thread-list.tsx
"use client";

import { FC, useState, KeyboardEvent } from "react";
import {
  ThreadListPrimitive,
  ThreadListItemPrimitive,
  useAui,
} from "@assistant-ui/react";
import { ArchiveIcon, PlusIcon, TrashIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "./tooltip-icon-button";

export const ThreadList: FC = () => {
  return (
    <ThreadListPrimitive.Root className="flex flex-col gap-2 p-3">
      <ThreadListNew />
      <ThreadListItems />
    </ThreadListPrimitive.Root>
  );
};

const ThreadListNew: FC = () => {
  return (
    <ThreadListPrimitive.New asChild>
      <Button
        variant="ghost"
        className="flex items-center justify-start gap-1 rounded-lg px-2.5 py-2 text-sm font-medium hover:bg-accent"
      >
        <PlusIcon size={16} />
        New Chat
      </Button>
    </ThreadListPrimitive.New>
  );
};

const ThreadListItems: FC = () => {
  return <ThreadListPrimitive.Items components={{ ThreadListItem }} />;
};

const ThreadListItem: FC = () => {
  return (
    <ThreadListItemPrimitive.Root className="flex items-center gap-1 rounded-lg px-2.5 py-2 text-sm hover:bg-accent data-[active]:bg-accent">
      <ThreadListItemTitle />
      <ThreadListItemPrimitive.Archive asChild>
        <TooltipIconButton tooltip="Archive thread">
          <ArchiveIcon size={14} />
        </TooltipIconButton>
      </ThreadListItemPrimitive.Archive>
      <ThreadListItemPrimitive.Delete asChild>
        <TooltipIconButton tooltip="Delete thread">
          <TrashIcon size={14} />
        </TooltipIconButton>
      </ThreadListItemPrimitive.Delete>
    </ThreadListItemPrimitive.Root>
  );
};

const ThreadListItemTitle: FC = () => {
  const aui = useAui();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const startEdit = () => {
    const current = aui.threadListItem().getState().title ?? "New conversation";
    setDraft(current);
    setEditing(true);
  };

  const commit = async () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed) {
      await aui.threadListItem().rename(trimmed);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      setEditing(false);
    }
  };

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={onKeyDown}
        onClick={(e) => e.stopPropagation()}
        className="flex-1 min-w-0 truncate bg-transparent border border-border rounded px-1 py-0.5 text-sm text-foreground outline-none"
      />
    );
  }

  return (
    <ThreadListItemPrimitive.Trigger
      onDoubleClick={(e) => {
        e.stopPropagation();
        startEdit();
      }}
      className="flex-1 min-w-0 truncate text-left"
    >
      <ThreadListItemPrimitive.Title fallback="New conversation" />
    </ThreadListItemPrimitive.Trigger>
  );
};
