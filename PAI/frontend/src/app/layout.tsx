// src/app/layout.tsx
import React from "react";
import { Inter } from "next/font/google";
import "./globals.css";

// Use the universally supported Inter font
const inter = Inter({ subsets: ["latin"] });

export const metadata = {
  title: "CyberQwen Agent",
  description: "Chat interface for CyberQwen",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className={inter.className}>
        {children}
      </body>
    </html>
  );
}
