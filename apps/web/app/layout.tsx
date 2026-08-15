import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LEAPS Finder",
  description:
    "A five-filter LEAPS call screener with transparent scoring. Educational tool, not financial advice.",
};

const NAV = [
  { href: "/", label: "Screener" },
  { href: "/compare", label: "Compare" },
  { href: "/about", label: "About" },
];

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col font-sans">
        <header className="border-b border-[var(--border)]">
          <div className="mx-auto flex w-full max-w-5xl items-baseline gap-6 px-6 py-4">
            <Link href="/" className="text-base font-semibold tracking-tight">
              LEAPS Finder
            </Link>
            <nav className="flex gap-4 text-sm text-[var(--muted)]">
              {NAV.map((item) => (
                <Link key={item.href} href={item.href} className="hover:text-[var(--foreground)]">
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>

        <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-10">{children}</main>

        <footer className="border-t border-[var(--border)]">
          <div className="mx-auto w-full max-w-5xl px-6 py-6 text-xs leading-relaxed text-[var(--muted)]">
            <p>
              Educational and personal tooling. Nothing here is financial advice. Data comes from
              Yahoo Finance and is delayed and unofficial — verify every number with your broker
              before trading.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
