"use client";
// Compatibility wrapper (plan D11): existing <Card title=…> call sites render the shared Panel chrome.
import { Panel } from "@/components/ui/Panel";

interface CardProps { children: React.ReactNode; title?: string; className?: string; }

export function Card({ children, title, className }: CardProps) {
  return (
    <Panel title={title} className={className} as="h3">
      {children}
    </Panel>
  );
}

export { Panel };
