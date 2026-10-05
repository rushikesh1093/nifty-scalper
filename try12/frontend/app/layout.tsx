import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { StatusBar } from "@/components/market/StatusBar";
import { NavBar } from "@/components/market/NavBar";

export const metadata: Metadata = {
  title:       "Nifty Scalper",
  description: "Automated Nifty Options Scalping Dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body>
        <Providers>
          <div className="min-h-screen flex flex-col" style={{ background: "var(--bg)" }}>
            <StatusBar />
            <NavBar />
            <main className="flex-1 overflow-auto">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
