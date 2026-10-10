"use client";
import toast from "react-hot-toast";

// A params / determinism hash: 8 characters in font-mono, the full value in `title`, and a copy
// button (navigator.clipboard, then a document.execCommand fallback, then a toast). Design §5.4, §7.2.

function execCommandCopy(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the execCommand fallback
  }
  return execCommandCopy(text);
}

export function HashCell({ value, label }: { value: string | null | undefined; label: string }) {
  if (!value) return <span className="font-mono text-text-3" title={`no ${label}`}>—</span>;
  const onCopy = async () => {
    if (await copyText(value)) toast.success(`${label} copied`);
    else toast.error("Copy failed — select the text manually");
  };
  return (
    <span className="inline-flex items-center gap-1">
      <span className="select-all font-mono text-text-2" title={value}>{value.slice(0, 8)}</span>
      <button
        type="button"
        onClick={() => { void onCopy(); }}
        aria-label={`Copy ${label} ${value.slice(0, 8)}`}
        title={`Copy ${label}`}
        className="rounded border border-border-2 px-1 text-micro text-text-3 hover:bg-surface-3 hover:text-text-1"
      >
        copy
      </button>
    </span>
  );
}
