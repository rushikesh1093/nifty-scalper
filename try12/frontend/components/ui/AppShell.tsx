// Server-compatible shell (no hooks): renders the client StatusBar/NavBar and the <main> landmark
// that the layout's skip link targets.
import { StatusBar } from "@/components/market/StatusBar";
import { NavBar } from "@/components/market/NavBar";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col bg-bg">
      <header>
        <StatusBar />
        <NavBar />
      </header>
      <main id="main" tabIndex={-1} className="flex-1 overflow-auto focus:outline-none">
        {children}
      </main>
    </div>
  );
}
