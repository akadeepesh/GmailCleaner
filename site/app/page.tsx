import { ArrowRight, HardDrive, ShieldCheck, Undo2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { GitHubIcon } from "@/components/icons/github"
import { Logo } from "@/components/icons/logo"
import { site } from "@/lib/site"

const points = [
  { icon: HardDrive, text: "Runs on your computer. No servers, no account." },
  { icon: ShieldCheck, text: "Nothing happens until you approve it." },
  { icon: Undo2, text: "Cleanup goes to Trash, recoverable for 30 days." },
]

export default function Home() {
  return (
    <main
      id="main"
      className="relative isolate flex flex-1 items-center justify-center overflow-hidden px-6 py-20"
    >
      {/* Soft accent glow behind the content */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-1/2 -z-10 size-[42rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/10 blur-3xl dark:bg-primary/15"
      />

      <div className="mx-auto flex max-w-2xl flex-col items-center text-center">
        <Logo className="size-16 drop-shadow-sm sm:size-20" />

        <p className="mt-6 text-sm font-medium tracking-wide text-muted-foreground uppercase">
          {site.name}
        </p>

        <h1 className="mt-3 text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
          Clean up Gmail without your email leaving your computer.
        </h1>

        <p className="mt-5 max-w-xl text-lg text-pretty text-muted-foreground">
          A free, open-source desktop app that finds newsletters and bulk
          clutter, then unsubscribes and tidies up, only after you say so.
        </p>

        <ul className="mt-8 grid gap-3 text-left sm:grid-cols-3 sm:gap-4">
          {points.map(({ icon: Icon, text }) => (
            <li
              key={text}
              className="flex items-start gap-3 rounded-xl border bg-card/70 p-4 text-sm text-card-foreground backdrop-blur-sm sm:flex-col"
            >
              <Icon className="size-5 shrink-0 text-link" aria-hidden="true" />
              <span>{text}</span>
            </li>
          ))}
        </ul>

        <div className="mt-10 flex flex-col gap-3 sm:flex-row">
          <Button asChild className="h-11 px-5 text-base">
            <a href={site.releases}>
              Download
              <ArrowRight data-icon="inline-end" />
            </a>
          </Button>
          <Button asChild variant="outline" className="h-11 px-5 text-base">
            <a href={site.repo}>
              <GitHubIcon data-icon="inline-start" className="size-4" />
              View on GitHub
            </a>
          </Button>
        </div>

        <p className="mt-12 text-sm text-muted-foreground">
          The full website is on its way.
        </p>
      </div>
    </main>
  )
}
