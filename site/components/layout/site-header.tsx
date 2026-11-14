import Link from "next/link"
import { Button } from "@/components/ui/button"
import { GitHubIcon } from "@/components/icons/github"
import { Logo } from "@/components/icons/logo"
import { MobileNav } from "@/components/layout/mobile-nav"
import { ThemeToggle } from "@/components/layout/theme-toggle"
import { mainNav } from "@/lib/nav"
import { site } from "@/lib/site"

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur-md supports-[backdrop-filter]:bg-background/70">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:px-6">
        <Link
          href="/"
          className="flex items-center gap-2.5 rounded-md font-semibold tracking-tight"
        >
          <Logo className="size-8" />
          <span>{site.name}</span>
        </Link>

        <nav aria-label="Main" className="ml-auto hidden items-center gap-1 md:flex">
          {mainNav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-1 md:ml-2">
          <Button asChild variant="ghost" size="icon" className="hidden size-10 md:inline-flex">
            <a href={site.repo} aria-label="GmailCleaner on GitHub">
              <GitHubIcon className="size-5" />
            </a>
          </Button>
          <ThemeToggle />
          <MobileNav />
        </div>
      </div>
    </header>
  )
}
