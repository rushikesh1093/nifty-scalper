"use client";
import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";

// Modal confirmation with a focus trap: focus starts on Cancel (the safe action), Tab cycles inside
// the dialog, Escape cancels, and focus returns to the element that opened it.
export interface ConfirmDialogProps {
  open:          boolean;
  title:         string;
  children:      React.ReactNode;
  confirmLabel:  string;
  cancelLabel?:  string;
  danger?:       boolean;
  pending?:      boolean;
  onConfirm:     () => void;
  onCancel:      () => void;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function ConfirmDialog({
  open, title, children, confirmLabel, cancelLabel = "Cancel", danger, pending, onConfirm, onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const bodyId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancelRef.current();
        return;
      }
      if (e.key !== "Tab" || !dialogRef.current) return;
      const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !dialogRef.current.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !dialogRef.current.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [open]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="w-full max-w-sm rounded-xl border border-border-2 bg-surface p-4 shadow-card"
      >
        <h2 id={titleId} className={cn("text-title font-semibold", danger ? "text-red" : "text-text-1")}>{title}</h2>
        <div id={bodyId} className="mt-2 text-body text-text-2">{children}</div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border-2 bg-surface-3 px-3 py-1.5 text-table font-semibold text-text-1 hover:bg-surface-2"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className={cn(
              "rounded-md px-3 py-1.5 text-table font-semibold disabled:opacity-60",
              danger ? "bg-red text-bg hover:bg-red/90" : "border border-accent/60 bg-accent/30 text-text-1 hover:bg-accent/40",
            )}
          >
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
