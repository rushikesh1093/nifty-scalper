"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/",        label: "Market"       },
  { href: "/trading", label: "Trading"      },
  { href: "/study",   label: "Market Study" },
  { href: "/regime",  label: "Regime"       },
];

export function NavBar() {
  const pathname = usePathname();

  return (
    <nav style={{
      display: "flex",
      alignItems: "stretch",
      height: 40,
      padding: "0 16px",
      gap: 2,
      background: "var(--surface)",
      borderBottom: "1px solid var(--border)",
    }}>
      {/* Logo */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginRight: 16, paddingRight: 16, borderRight: "1px solid var(--border-2)" }}>
        <div style={{
          width: 22, height: 22, borderRadius: 5, background: "var(--accent)",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontSize: 11, fontWeight: 700, flexShrink: 0,
        }}>N</div>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-1)", letterSpacing: "-0.01em" }}>
          Nifty Scalper
        </span>
      </div>

      {LINKS.map(({ href, label }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className="nav-link"
            data-active={active ? "true" : "false"}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
