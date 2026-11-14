import Link from "next/link"
import { Logo } from "@/components/icons/logo"
import { footerNav } from "@/lib/nav"
import { site } from "@/lib/site"

export function SiteFooter() {
  return (
    <footer className="border-t bg-card/40">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[1.5fr_repeat(3,1fr)]">
        <div className="max-w-xs">
          <Link href="/" className="inline-flex items-center gap-2.5 rounded-md font-semibold">
            <Logo className="size-7" />
            {site.name}
          </Link>
          <p className="mt-3 text-sm text-muted-foreground">
            Free and open source. No account, no cookies, no analytics.
          </p>
        </div>

        {footerNav.map((group) => (
          <nav key={group.title} aria-label={group.title}>
            <h2 className="text-sm font-semibold">{group.title}</h2>
            <ul className="mt-3 space-y-2">
              {group.links.map((link) => (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                  >
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ))}
      </div>
      <div className="border-t">
        <p className="mx-auto max-w-6xl px-4 py-6 text-xs text-muted-foreground sm:px-6">
          {site.name} is not affiliated with or endorsed by Google. Gmail is a
          trademark of Google LLC.
        </p>
      </div>
    </footer>
  )
}
