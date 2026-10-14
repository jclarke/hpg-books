import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { apiPost } from "../api/client";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { EmptyState } from "./EmptyState";
import { LoginScreen } from "./LoginScreen";
import { Modal } from "./Modal";
import { Sidebar } from "./Sidebar";
import { Skeleton } from "./Skeleton";
import { Topbar } from "./Topbar";
import { configKey, useConfigQuery } from "../hooks/useConfig";
import { useHotkeys } from "../hooks/useHotkeys";
import { useMode } from "../hooks/useMode";
import { sessionKey, sessionReady, useSession } from "../hooks/useSession";
import { useTheme } from "../hooks/useTheme";
import { cx } from "../lib/cx";
import { applySiteConfig, hasDetail, normalizeSiteConfig } from "../lib/siteConfig";
import type { Session } from "../api/types";

const SHORTCUTS: [string, string][] = [
  ["Ctrl/⌘ K", "Search and jump anywhere"],
  ["g then d / t / r / v", "Go to Dashboard / Transactions / Review / Reports"],
  ["j / k", "Next / previous row"],
  ["Enter", "Open the highlighted row"],
  ["x", "Select the highlighted row"],
  ["?", "Show these shortcuts"],
];

export function AppShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const config = useConfigQuery();
  const queryClient = useQueryClient();
  const ready = sessionReady(session.data);
  // Businesses, accounts, and brands are only in the payload once signed in.
  const needsDetail = ready && config.isSuccess && !hasDetail(config.data);
  const site = useMemo(() => normalizeSiteConfig(config.data, session.data), [config.data, session.data]);
  // Label helpers read the registry during render, so it is set before the children render.
  applySiteConfig(site);

  useEffect(() => {
    if (needsDetail) void queryClient.invalidateQueries({ queryKey: configKey });
  }, [needsDetail, queryClient]);

  useEffect(() => {
    // Pages set their own titles; this covers the boot and sign-in screens.
    if (!ready || document.title === "Books") document.title = site.product;
  }, [ready, site.product]);

  if (session.isPending || config.isPending || (needsDetail && config.isFetching)) {
    return (
      <div className="boot" role="status" aria-label={`Loading ${site.product}`}>
        <Skeleton width={220} height={18} />
        <Skeleton width={320} height={12} />
      </div>
    );
  }
  if (session.isError || !session.data) {
    return (
      <div className="boot">
        <EmptyState icon="alert" tone="error" title="Can't reach the HPG Books server" action={<button type="button" className="btn btn--primary" onClick={() => session.refetch()}>Retry</button>}>
          Start it with <code>bin/hpgbooks web</code> and reload. {session.error instanceof Error ? session.error.message : ""}
        </EmptyState>
      </div>
    );
  }
  if (!sessionReady(session.data)) {
    return <LoginScreen />;
  }
  return <SignedInShell session={session.data}>{children}</SignedInShell>;
}

function SignedInShell({ session, children }: { session: Session; children: ReactNode }) {
  const theme = useTheme();
  const queryClient = useQueryClient();
  const [signingOut, setSigningOut] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [pendingG, setPendingG] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const { mode } = useMode();
  const personal = mode === "personal";

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.querySelector<HTMLElement>(".shell__sidebar a")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  useEffect(() => {
    if (!pendingG) return undefined;
    const id = window.setTimeout(() => setPendingG(false), 1200);
    return () => window.clearTimeout(id);
  }, [pendingG]);

  const jump = (path: string) => {
    setPendingG(false);
    const keep = new URLSearchParams(location.search);
    const next = new URLSearchParams();
    for (const key of ["business", "start", "end", "mode"]) {
      const value = keep.get(key);
      if (value) next.set(key, value);
    }
    navigate(`${path}${next.toString() ? `?${next}` : ""}`);
  };

  useHotkeys({
    "mod+k": () => setPaletteOpen((value) => !value),
    "/": () => setPaletteOpen(true),
    "?": () => setHelpOpen(true),
    g: () => setPendingG(true),
  });
  useHotkeys(
    {
      d: () => jump(personal ? "/personal" : "/"),
      t: () => jump(personal ? "/personal/transactions" : "/transactions"),
      r: () => jump(personal ? "/personal/review" : "/review"),
      v: () => jump(personal ? "/personal/spending" : "/reports"),
    },
    pendingG,
  );

  async function signOut() {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await apiPost("/logout", {});
      await queryClient.invalidateQueries({ queryKey: sessionKey });
    } finally {
      setSigningOut(false);
    }
  }

  const onSignOut = session.requires_login && session.authenticated ? signOut : undefined;
  return (
    <div className={cx("shell", menuOpen && "shell--menu-open", personal && "shell--personal")} data-mode={mode}>
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="shell__sidebar">
        <Sidebar
          reviewCount={session.review_count}
          company={session.company}
          onNavigate={() => setMenuOpen(false)}
          onSignOut={onSignOut}
          signingOut={signingOut}
        />
      </aside>
      {menuOpen ? <div className="shell__scrim" onClick={() => setMenuOpen(false)} aria-hidden="true" /> : null}
      <div className="shell__main">
        <Topbar
          session={session}
          onOpenPalette={() => setPaletteOpen(true)}
          onOpenMenu={() => setMenuOpen(true)}
          onSignOut={onSignOut}
          signingOut={signingOut}
        />
        <main id="main" className="content" tabIndex={-1}>
          <ErrorBoundary resetKey={location.pathname}>{children}</ErrorBoundary>
        </main>
      </div>
      <BottomNav reviewCount={session.review_count} onMore={() => setMenuOpen((value) => !value)} moreOpen={menuOpen} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onToggleTheme={theme.toggle} />
      <Modal open={helpOpen} onClose={() => setHelpOpen(false)} title="Keyboard shortcuts" size="sm">
        <dl className="shortcuts">
          {SHORTCUTS.map(([keys, text]) => (
            <div key={keys} className="shortcuts__row">
              <dt>
                <kbd>{keys}</kbd>
              </dt>
              <dd>{text}</dd>
            </div>
          ))}
        </dl>
      </Modal>
    </div>
  );
}
