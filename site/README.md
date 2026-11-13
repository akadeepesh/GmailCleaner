# GmailCleaner website

The public site for GmailCleaner: home page, privacy policy and guides.
Next.js (App Router), Tailwind CSS v4 and shadcn/ui, deployed on Vercel with
**Root Directory = `site`**.

```bash
pnpm install
pnpm dev        # http://localhost:3000
pnpm build
pnpm lint
pnpm typecheck
```

## Configuration

| Env var | Purpose |
|---|---|
| `NEXT_PUBLIC_SITE_URL` | Canonical address, e.g. `https://gmailcleaner.vercel.app`. Used for `metadataBase`, canonical URLs and the sitemap. |

`vercel.json` skips builds for commits that don't touch `site/`.

No analytics, cookies or trackers. Keep it that way: the privacy policy says so.
