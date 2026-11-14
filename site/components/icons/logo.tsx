import Image from "next/image"
import { cn } from "@/lib/utils"
import logo from "@/assets/logo.png"

/**
 * The app logo. Same artwork as the desktop app (desktop/assets/logo.png);
 * regenerate site copies from that master, don't edit them by hand.
 * Decorative: it always sits next to the product name, so alt is empty.
 * Pass `eager` for above-the-fold copies (header, hero).
 */
export function Logo({ className, eager }: { className?: string; eager?: boolean }) {
  return (
    <Image
      src={logo}
      alt=""
      sizes="80px"
      loading={eager ? "eager" : "lazy"}
      className={cn("shrink-0 select-none", className)}
    />
  )
}
