/**
 * Site-wide constants. The canonical address comes from the SITE_URL env var
 * (set in Vercel), so moving to another domain is a one-line change. It is
 * only read on the server at build time, so it needs no NEXT_PUBLIC_ prefix.
 * Without it, the site falls back to the Vercel project's production address,
 * then to localhost.
 */
function resolveSiteUrl(): string {
  const explicit = process.env.SITE_URL
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
