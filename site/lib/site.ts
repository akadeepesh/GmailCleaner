/**
 * Site-wide constants. SITE_URL comes from NEXT_PUBLIC_SITE_URL so moving to
 * another domain is a one-line change in Vercel. Without it, the site falls
 * back to the Vercel project's production address, then to localhost.
 */
function resolveSiteUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_SITE_URL
  if (explicit) return explicit.replace(/\/+$/, "")
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL
  if (vercel) return `https://${vercel}`
  return "http://localhost:3000"
}

export const site = {
  name: "GmailCleaner",
  tagline: "Clean up Gmail without your email leaving your computer.",
  description:
    "GmailCleaner is a free, open-source desktop app that finds newsletters and bulk clutter in Gmail, then unsubscribes and moves mail to Trash, only after you approve. It runs entirely on your computer.",
  url: resolveSiteUrl(),
  repo: "https://github.com/akadeepesh/GmailCleaner",
  releases: "https://github.com/akadeepesh/GmailCleaner/releases",
  issues: "https://github.com/akadeepesh/GmailCleaner/issues",
} as const
