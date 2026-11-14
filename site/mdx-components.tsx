import type { MDXComponents } from "mdx/types"

const components: MDXComponents = {
  // Wide tables (the privacy policy's permissions table) scroll sideways on
  // small screens instead of stretching the page.
  table: (props) => (
    <div className="not-prose my-6 overflow-x-auto rounded-lg border">
      <table
        className="w-full border-collapse text-left text-sm [&_td]:border-t [&_td]:px-4 [&_td]:py-3 [&_td]:align-top [&_th]:bg-muted [&_th]:px-4 [&_th]:py-2.5 [&_th]:font-semibold [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.85em]"
        {...props}
      />
    </div>
  ),
  a: ({ href = "", ...props }) => {
    const external = /^https?:\/\//.test(href)
    return (
      <a
        href={href}
        {...(external ? { rel: "noopener noreferrer" } : {})}
        {...props}
      />
    )
  },
}

export function useMDXComponents(): MDXComponents {
  return components
}
