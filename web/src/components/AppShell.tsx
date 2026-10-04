import { Button } from "@/components/ui/button";
import { Moon, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import type { AppView } from "../lib/route";

const PRIMARY: Array<{ id: AppView; label: string }> = [
  { id: "recordings", label: "Recordings" },
  { id: "people", label: "People" },
];

const MORE: Array<{ id: AppView; label: string }> = [
  { id: "review", label: "Review" },
  { id: "context", label: "Context" },
  { id: "health", label: "Health" },
];

const THEME_KEY = "life-recorder-theme";

function initialTheme(): "dark" | "light" {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return "dark";
}

export function AppShell({
  view,
  onView,
  health,
  children,
  player,
}: {
  view: AppView;
  onView: (view: AppView) => void;
  health: string;
  children: ReactNode;
  player: ReactNode;
}) {
  const [theme, setTheme] = useState<"dark" | "light">(initialTheme);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const moreActive = MORE.some((item) => item.id === view);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.classList.add("style-nova");
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    if (!moreOpen) return;
    moreRef.current?.querySelector<HTMLElement>("[role=menuitem]")?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!moreRef.current?.contains(event.target as Node)) setMoreOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [moreOpen]);

  function openView(next: AppView) {
    setMoreOpen(false);
    onView(next);
  }

  return (
    <div className="flex h-svh min-w-0 flex-col overflow-hidden">
      <button type="button" className="skip-link" onClick={() => document.getElementById("viewer-content")?.focus()}>Skip to content</button>
      <header className="flex min-w-0 flex-wrap items-center gap-1 border-b px-2 py-1">
        <h1 className="sr-only">Life Recorder</h1>
        <nav aria-label="Primary" className="flex min-w-0 flex-wrap items-center gap-1">
          {PRIMARY.map((item) => (
            <Button key={item.id} type="button" size="sm" variant={view === item.id ? "secondary" : "ghost"} className="touch-target" aria-current={view === item.id ? "page" : undefined} onClick={() => openView(item.id)}>
              {item.label}
            </Button>
          ))}
          <div ref={moreRef} className="relative" onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setMoreOpen(false);
              moreRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
            }
            if (!moreOpen && ["ArrowDown", "ArrowUp"].includes(event.key)) {
              event.preventDefault();
              setMoreOpen(true);
            } else if (moreOpen && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
              event.preventDefault();
              const items = [...(moreRef.current?.querySelectorAll<HTMLElement>("[role=menuitem]") || [])];
              const index = items.indexOf(document.activeElement as HTMLElement);
              const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
              items[next]?.focus();
            }
          }} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setMoreOpen(false); }}>
            <Button type="button" size="sm" variant={moreActive ? "secondary" : "ghost"} className="touch-target" aria-expanded={moreOpen} aria-controls="more-nav" aria-current={moreActive ? "page" : undefined} onClick={() => setMoreOpen((open) => !open)}>
              More
            </Button>
            {moreOpen ? (
              <div id="more-nav" role="menu" aria-label="More" className="absolute left-0 z-30 mt-1 grid min-w-36 gap-1 rounded-lg border bg-popover p-1 text-popover-foreground shadow-md">
                {MORE.map((item) => (
                  <Button key={item.id} type="button" role="menuitem" size="sm" variant={view === item.id ? "secondary" : "ghost"} className="touch-target justify-start" aria-current={view === item.id ? "page" : undefined} onClick={() => openView(item.id)}>
                    {item.label}
                  </Button>
                ))}
                <a role="menuitem" href="/legacy/" className="touch-target inline-flex items-center px-2 text-sm underline md:hidden">Legacy viewer</a>
              </div>
            ) : null}
          </div>
        </nav>
        <p className="ml-auto hidden max-w-full truncate text-xs text-muted-foreground md:block">{health}</p>
        <a href="/legacy/" className="touch-target hidden items-center px-1 text-xs text-muted-foreground underline md:inline-flex">Legacy viewer</a>
        <Button type="button" variant="outline" size="sm" className="touch-target ml-auto md:ml-0" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-pressed={theme === "dark"} aria-label={theme === "dark" ? "Light theme" : "Dark theme"}>
          {theme === "dark" ? <Sun /> : <Moon />}
          <span className="max-[380px]:sr-only">{theme === "dark" ? "Light" : "Dark"}</span>
        </Button>
      </header>
      <main id="viewer-content" tabIndex={-1} className="min-h-0 min-w-0 flex-1 overflow-hidden outline-none">{children}</main>
      {player}
    </div>
  );
}
