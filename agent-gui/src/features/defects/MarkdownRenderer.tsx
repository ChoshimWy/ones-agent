import type { ReactNode } from "react";
import { Fragment } from "react";
import { cn } from "@/lib/utils";

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const segments = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*\s][^*]*\*)/g);

  return segments.map((segment, index) => {
    const key = `${keyPrefix}-${index}`;

    if (segment.startsWith("**") && segment.endsWith("**")) {
      return <strong key={key}>{segment.slice(2, -2)}</strong>;
    }

    if (segment.startsWith("`") && segment.endsWith("`")) {
      return (
        <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
          {segment.slice(1, -1)}
        </code>
      );
    }

    const linkMatch = segment.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a key={key} href={linkMatch[2]} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-4">
          {linkMatch[1]}
        </a>
      );
    }

    if (segment.startsWith("*") && segment.endsWith("*") && segment.length > 2) {
      return <em key={key}>{segment.slice(1, -1)}</em>;
    }

    return <Fragment key={key}>{segment}</Fragment>;
  });
}

function flushParagraph(lines: string[], blocks: ReactNode[], keyPrefix: string) {
  if (lines.length === 0) return;
  const text = lines.join(" ").trim();
  if (!text) return;
  blocks.push(
    <p key={`${keyPrefix}-p-${blocks.length}`} className="leading-7 text-muted-foreground">
      {renderInline(text, `${keyPrefix}-inline-${blocks.length}`)}
    </p>
  );
  lines.length = 0;
}

export default function MarkdownRenderer({
  markdown,
  className,
}: {
  markdown?: string;
  className?: string;
}) {
  const source = (markdown || "").replace(/\r\n/g, "\n").trim();

  if (!source) {
    return <p className="text-sm text-muted-foreground">No markdown available.</p>;
  }

  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  const paragraph: string[] = [];
  const listItems: string[] = [];
  const codeLines: string[] = [];
  let inCode = false;

  const flushList = () => {
    if (listItems.length === 0) return;
    blocks.push(
      <ul key={`list-${blocks.length}`} className="list-disc space-y-2 pl-5 text-muted-foreground">
        {listItems.map((item, index) => (
          <li key={`list-${blocks.length}-${index}`}>{renderInline(item, `list-${blocks.length}-${index}`)}</li>
        ))}
      </ul>
    );
    listItems.length = 0;
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();

    if (line.startsWith("```")) {
      if (inCode) {
        blocks.push(
          <pre key={`code-${blocks.length}`} className="overflow-x-auto rounded-xl border bg-muted/50 p-4 text-xs leading-6 text-foreground">
            <code>{codeLines.join("\n")}</code>
          </pre>
        );
        codeLines.length = 0;
      } else {
        flushParagraph(paragraph, blocks, `block-${blocks.length}`);
        flushList();
      }
      inCode = !inCode;
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph(paragraph, blocks, `block-${blocks.length}`);
      flushList();
      continue;
    }

    if (/^#{1,3}\s+/.test(line)) {
      flushParagraph(paragraph, blocks, `block-${blocks.length}`);
      flushList();
      const level = line.match(/^#+/)?.[0].length ?? 1;
      const text = line.replace(/^#{1,3}\s+/, "");
      const headingClass = level === 1 ? "text-lg" : level === 2 ? "text-base" : "text-sm";
      const headingKey = `heading-${blocks.length}`;
      if (level === 1) {
        blocks.push(
          <h3 key={headingKey} className={cn("font-heading font-semibold tracking-tight text-foreground", headingClass)}>
            {renderInline(text, headingKey)}
          </h3>
        );
      } else if (level === 2) {
        blocks.push(
          <h4 key={headingKey} className={cn("font-heading font-semibold tracking-tight text-foreground", headingClass)}>
            {renderInline(text, headingKey)}
          </h4>
        );
      } else {
        blocks.push(
          <h5 key={headingKey} className={cn("font-heading font-semibold tracking-tight text-foreground", headingClass)}>
            {renderInline(text, headingKey)}
          </h5>
        );
      }
      continue;
    }

    if (/^>\s+/.test(line)) {
      flushParagraph(paragraph, blocks, `block-${blocks.length}`);
      flushList();
      blocks.push(
        <blockquote key={`quote-${blocks.length}`} className="border-l-2 border-border pl-4 text-muted-foreground">
          {renderInline(line.replace(/^>\s+/, ""), `quote-${blocks.length}`)}
        </blockquote>
      );
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      flushParagraph(paragraph, blocks, `block-${blocks.length}`);
      listItems.push(line.replace(/^[-*]\s+/, ""));
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph(paragraph, blocks, `block-${blocks.length}`);
  flushList();

  if (inCode && codeLines.length > 0) {
    blocks.push(
      <pre key={`code-${blocks.length}`} className="overflow-x-auto rounded-xl border bg-muted/50 p-4 text-xs leading-6 text-foreground">
        <code>{codeLines.join("\n")}</code>
      </pre>
    );
  }

  return <div className={cn("space-y-4", className)}>{blocks}</div>;
}
