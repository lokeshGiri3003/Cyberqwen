// src/app/page.tsx
"use client";

import { useEffect, useState } from "react";
import { RuntimeProvider } from "../RuntimeProvider";
import { Thread } from "../components/assistant-ui/thread";
import { ThreadList } from "../components/assistant-ui/thread-list";
import Image from "next/image";

function DarkModeToggle() {
  const [isDark, setIsDark] = useState(true);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains("dark"));
  }, []);

  const toggle = () => {
    const next = !isDark;
    setIsDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("cyberqwen-theme", next ? "dark" : "light");
  };

  return (
    <button
      onClick={toggle}
      className="fixed top-3 right-3 z-50 rounded-md border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-accent cursor-pointer"
    >
      {isDark ? "Light mode" : "Dark mode"}
    </button>
  );
}

export default function Home() {
  return (
    <RuntimeProvider>
      <main className="h-screen w-full flex bg-background text-foreground">
        <DarkModeToggle />

        {/* Left: sidebar, visibly distinct from main pane */}
        <div className="w-72 shrink-0 flex flex-col bg-sidebar border-r border-sidebar-border">
          <div className="flex items-center gap-2 px-4 py-4 border-b border-sidebar-border">
            <Image
              src="/bot-avatar.png"
              alt="CyberQwen logo"
              width={28}
              height={28}
              className="rounded-md"
            />
            <span className="text-base font-semibold text-sidebar-foreground tracking-tight">
              CyberQwen
            </span>
          </div>
          <div className="flex-1 overflow-y-auto">
            <ThreadList />
          </div>
        </div>

        {/* Right: active conversation */}
        <div className="flex-1 flex flex-col overflow-hidden relative max-w-4xl mx-auto">
          <Thread />
        </div>
      </main>
    </RuntimeProvider>
  );
}
