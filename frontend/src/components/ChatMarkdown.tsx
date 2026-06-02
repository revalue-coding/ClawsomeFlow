import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/cn";

export function ChatMarkdown({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "break-words text-sm leading-6",
        "[&_p]:my-1.5 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-0.5",
        "[&_code]:rounded [&_code]:bg-ink-100 [&_code]:px-1 [&_code]:py-0.5",
        "[&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-ink-900 [&_pre]:p-3 [&_pre]:text-ink-100",
        "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
        "[&_a]:text-brand-600 [&_a]:underline",
        "[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse [&_table]:text-left",
        "[&_thead_th]:border [&_thead_th]:border-ink-200 [&_thead_th]:bg-ink-50 [&_thead_th]:px-2 [&_thead_th]:py-1",
        "[&_tbody_td]:border [&_tbody_td]:border-ink-200 [&_tbody_td]:px-2 [&_tbody_td]:py-1",
        className,
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}

