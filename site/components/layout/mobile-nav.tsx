"use client"

import Link from "next/link"
import { Menu } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { GitHubIcon } from "@/components/icons/github"
import { mainNav } from "@/lib/nav"
import { site } from "@/lib/site"

export function MobileNav() {
  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="size-10 md:hidden" aria-label="Open menu">
          <Menu className="size-5" aria-hidden="true" />
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-72">
        <SheetHeader>
          <SheetTitle>{site.name}</SheetTitle>
          <SheetDescription className="sr-only">Site navigation</SheetDescription>
        </SheetHeader>
        <nav aria-label="Mobile" className="flex flex-col gap-1 px-4">
          {[{ href: "/", label: "Home" }, ...mainNav].map((item) => (
            <SheetClose asChild key={item.href}>
              <Link
                href={item.href}
                className="rounded-md px-3 py-2.5 text-base font-medium hover:bg-muted"
              >
                {item.label}
              </Link>
            </SheetClose>
          ))}
          <SheetClose asChild>
            <a
              href={site.repo}
              className="flex items-center gap-2 rounded-md px-3 py-2.5 text-base font-medium hover:bg-muted"
            >
              <GitHubIcon className="size-4" />
              GitHub
            </a>
          </SheetClose>
        </nav>
      </SheetContent>
    </Sheet>
  )
}
