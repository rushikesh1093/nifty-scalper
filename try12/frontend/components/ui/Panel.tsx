"use client";
import { useId } from "react";
import { cn } from "@/lib/utils";

// The one panel chrome (design §6.3 item 4). A titled panel is a labelled <section> landmark.
export interface PanelProps {
  title?:         React.ReactNode;
  /** Small inline content after the title (counts, chips). */
  meta?:          React.ReactNode;
  /** Right-aligned header controls. */
  actions?:       React.ReactNode;
  footer?:        React.ReactNode;
  children:       React.ReactNode;
  className?:     string;
  bodyClassName?: string;
  /** Remove body padding (tables, charts that run edge to edge). */
  flush?:         boolean;
  /** Dense padding (p-3) instead of standard (p-4). */
  dense?:         boolean;
  /** Heading level for the title (default h2). */
  as?:            "h2" | "h3";
}

export function Panel({
  title, meta, actions, footer, children, className, bodyClassName, flush, dense, as: Heading = "h2",
}: PanelProps) {
  const id = useId();
  const hasHeader = title !== undefined || actions !== undefined;
  return (
    <section aria-labelledby={title !== undefined ? id : undefined} className={cn("panel", className)}>
      {hasHeader && (
        <div className="panel-header">
          {title !== undefined && <Heading id={id} className="panel-title">{title}</Heading>}
          {meta}
          {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cn(!flush && (dense ? "p-3" : "p-4"), bodyClassName)}>{children}</div>
      {footer && <div className="panel-footer">{footer}</div>}
    </section>
  );
}
