/** A boxed message for the empty, unconfigured and error states. */
export function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6">
      <h2 className="text-sm font-semibold">{title}</h2>
      <div className="mt-2 text-sm leading-relaxed text-[var(--muted)]">{children}</div>
    </div>
  );
}
