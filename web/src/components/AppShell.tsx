import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Activity, AudioLines, MapPinned, Moon, ScanSearch, Sun, Users } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { AppView } from "../lib/route";

const NAV: Array<{ id: AppView; label: string; icon: typeof AudioLines; ready: boolean }> = [
  { id: "recordings", label: "Recordings", icon: AudioLines, ready: true },
  { id: "review", label: "Review", icon: ScanSearch, ready: true },
  { id: "people", label: "People", icon: Users, ready: true },
  { id: "context", label: "Context", icon: MapPinned, ready: true },
  { id: "health", label: "Health", icon: Activity, ready: true },
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

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.classList.add("style-nova");
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  return (
    <TooltipProvider>
      <SidebarProvider className="h-svh overflow-hidden">
        <button type="button" className="skip-link" onClick={() => document.getElementById("viewer-content")?.focus()}>Skip to content</button>
        <Sidebar collapsible="icon" variant="sidebar">
          <SidebarHeader className="gap-1 px-3 py-3">
            <div className="truncate text-sm font-semibold">Life Recorder</div>
            <p className="truncate text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">Private recordings</p>
          </SidebarHeader>
          <SidebarContent>
            <SidebarGroup>
              <SidebarGroupLabel>Library</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {NAV.map((item) => (
                    <SidebarMenuItem key={item.id}>
                      <ViewButton item={item} view={view} onView={onView} />
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>
          <SidebarFooter className="gap-2">
            <p className="px-2 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">{health}</p>
            <a href="/legacy/" className="px-2 text-xs text-muted-foreground underline group-data-[collapsible=icon]:sr-only">Legacy viewer</a>
            <Button type="button" variant="outline" size="sm" className="touch-target" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-pressed={theme === "dark"}>
              {theme === "dark" ? <Sun /> : <Moon />}
              <span className="group-data-[collapsible=icon]:hidden">{theme === "dark" ? "Light" : "Dark"}</span>
            </Button>
          </SidebarFooter>
        </Sidebar>
        <SidebarInset className="min-h-0 overflow-hidden">
          <header className="flex items-center gap-2 border-b px-3 py-2">
            <SidebarTrigger aria-label="Toggle navigation" className="touch-target" />
            <Separator orientation="vertical" className="h-4" />
            <h1 className="text-sm font-medium">{NAV.find((item) => item.id === view)?.label}</h1>
          </header>
          <main id="viewer-content" tabIndex={-1} className="min-h-0 flex-1 overflow-hidden outline-none">{children}</main>
          {player}
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}

function ViewButton({ item, view, onView }: { item: (typeof NAV)[number]; view: AppView; onView: (view: AppView) => void }) {
  const { setOpenMobile } = useSidebar();
  return <SidebarMenuButton isActive={view === item.id} tooltip={item.label} onClick={() => { setOpenMobile(false); onView(item.id); }} aria-current={view === item.id ? "page" : undefined}>
    <item.icon /><span>{item.label}</span>
  </SidebarMenuButton>;
}
