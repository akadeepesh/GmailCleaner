import type { SVGProps } from "react"

/** The app icon: an envelope with a sparkle (same artwork as desktop/assets). */
export function Logo(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 256 256" aria-hidden="true" {...props}>
      <rect x="8" y="8" width="240" height="240" rx="52" fill="#5b7fff" />
      <rect x="44" y="74" width="168" height="116" rx="14" fill="#fff" />
      <path
        d="M50 84l78 58 78-58"
        fill="none"
        stroke="#3a4fa6"
        strokeWidth="12"
        strokeLinejoin="round"
      />
      <path d="M198 14l17 29 29 17-29 17-17 29-17-29-29-17 29-17z" fill="#5b7fff" />
      <path d="M198 26l9 25 25 9-25 9-9 25-9-25-25-9 25-9z" fill="#e0a84a" />
    </svg>
  )
}
