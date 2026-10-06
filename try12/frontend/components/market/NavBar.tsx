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
    <nav aria-label="Primary" className="flex h-10 items-stretch gap-0.5 border-b border-border bg-surface px-4">
      {/* Logo */}
      <div className="mr-4 flex items-center gap-2 border-r border-border-2 pr-4">
        <div
          aria-hidden="true"
          className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-[5px] bg-accent text-[11px] font-bold text-white"
        >
          N
        </div>
        <span className="text-[12px] font-semibold tracking-tight text-text-1">Nifty Scalper</span>
      </div>

      <ul className="flex items-stretch gap-0.5">
        {LINKS.map(({ href, label }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <li key={href} className="flex">
              <Link href={href} className="nav-link" aria-current={active ? "page" : undefined}>
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
