import { site } from "@/lib/site"

export type NavLink = { href: string; label: string; external?: boolean }

/** Header links. Download points at GitHub Releases until /download exists. */
export const mainNav: NavLink[] = [
  { href: site.releases, label: "Download", external: true },
  { href: "/privacy", label: "Privacy" },
]

export const footerNav: { title: string; links: NavLink[] }[] = [
  {
    title: "Product",
    links: [
      { href: site.releases, label: "Download", external: true },
      { href: site.repo, label: "Source code", external: true },
    ],
  },
  {
    title: "Legal",
    links: [
      { href: "/privacy", label: "Privacy policy" },
      { href: "/terms", label: "Terms of use" },
      { href: `${site.repo}/blob/main/LICENSE`, label: "MIT license", external: true },
    ],
  },
  {
    title: "Help",
    links: [{ href: site.issues, label: "Report an issue", external: true }],
  },
]
