"use client";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Admin key prompt (design §5.5), on the ConfirmDialog chrome: focus starts in the labelled password
// input, Tab cycles inside the dialog, Escape cancels, and focus returns to the opener. The value is
// handed to onSave and never logged.
export interface AdminKeyDialogProps {
  open:      boolean;
  onSave:    (key: string) => void;
  onCancel:  () => void;
  /** Shown above the input, e.g. "Admin key rejected — enter it again". */
  message?:  string;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function AdminKeyDialog({ open, onSave, onCancel, message }: AdminKeyDialogProps) {
  const titleId = useId();
  const bodyId = useId();
  const inputId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;
  const [value, setValue] = useState("");

  useEffect(() => {
    if (!open) return;
    setValue("");
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    inputRef.current?.focus();

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

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (value === "") return;
    onSave(value);
    setValue("");
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="w-full max-w-md rounded-xl border border-border-2 bg-surface p-4 shadow-card"
      >
        <h2 id={titleId} className="text-title font-semibold text-text-1">Admin key</h2>
        <form onSubmit={submit} className="mt-2 space-y-3">
          {message && <p role="alert" className="text-table font-semibold text-red">{message}</p>}
          <div className="space-y-1">
            <label htmlFor={inputId} className="block text-table font-semibold text-text-1">
              X-API-Key (the API&apos;s ADMIN_API_KEY)
            </label>
            <input
              ref={inputRef}
              id={inputId}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="w-full rounded-md border border-border-2 bg-surface-2 px-2.5 py-1.5 font-mono text-table text-text-1"
            />
          </div>
          <p id={bodyId} className="text-micro text-text-3">
            Kept in this tab&apos;s sessionStorage only and sent only to the backtest launcher endpoints. It is cleared
            when the tab closes. Any script on this origin can read sessionStorage, so this protects against the key
            being persisted or bundled, not against XSS.
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border-2 bg-surface-3 px-3 py-1.5 text-table font-semibold text-text-1 hover:bg-surface-2"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={value === ""}
              className="rounded-md border border-accent/60 bg-accent/30 px-3 py-1.5 text-table font-semibold text-text-1 hover:bg-accent/40 disabled:opacity-60"
            >
              Save
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
