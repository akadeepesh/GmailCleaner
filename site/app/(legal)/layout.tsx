export default function LegalLayout({ children }: { children: React.ReactNode }) {
  return (
    <main id="main" className="flex-1 px-4 py-12 sm:px-6 sm:py-16">
      <div className="mx-auto max-w-3xl">
        <article className="prose prose-site max-w-none sm:prose-lg prose-headings:scroll-mt-24 prose-headings:font-semibold prose-headings:tracking-tight prose-h1:mb-2 prose-h2:mt-12 prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:font-mono prose-code:text-[0.85em] prose-code:font-normal prose-code:before:content-none prose-code:after:content-none">
          {children}
        </article>
      </div>
    </main>
  )
}
