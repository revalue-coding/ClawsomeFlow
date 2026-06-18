import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ChatMarkdown } from "@/components/ChatMarkdown";
import { formatChatTime } from "@/lib/chatHistory";

export interface ChatBubbleMessage {
  role: "user" | "assistant" | "system";
  content: string;
  ts?: number;
}

function TypingDots() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((v) => (v + 1) % 3), 350);
    return () => clearInterval(timer);
  }, []);
  return <span className="text-ink-400">{".".repeat(tick + 1)}</span>;
}

/** Small hover action that copies a message's plain text to the clipboard, with
 *  transient "Copied" feedback. Shared by the OpenClaw and Hermes bubbles. */
export function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (insecure context / denied) — ignore */
    }
  };
  return (
    <button
      type="button"
      onClick={onCopy}
      className="text-[11px] text-ink-400 hover:text-ink-600"
      title={t("chat.copy")}
    >
      {copied ? t("chat.copied") : t("chat.copy")}
    </button>
  );
}

/** Light separator marking where messages received since the user last left the
 *  chat begin (typical "unread messages" divider). */
export function NewMessagesDivider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 py-1 text-[11px] font-medium text-ink-400">
      <span className="h-px flex-1 bg-ink-200" />
      <span className="shrink-0">{label}</span>
      <span className="h-px flex-1 bg-ink-200" />
    </div>
  );
}

/** Placeholder shown in the assistant bubble while a turn is still running:
 *  typing dots for the first 10s, then a marquee reassurance line so a slow
 *  turn never looks stuck. Shared by the OpenClaw and Hermes chat bubbles. */
export function PendingReply() {
  const { t } = useTranslation();
  const [waitedLong, setWaitedLong] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setWaitedLong(true), 10000);
    return () => window.clearTimeout(id);
  }, []);
  return waitedLong ? (
    <span className="csflow-thinking-marquee text-ink-400">
      <span className="csflow-thinking-marquee-text">{t("chat.stillThinking")}</span>
    </span>
  ) : (
    <TypingDots />
  );
}

export function ChatBubble({
  msg,
  pending,
  noTextReply,
}: {
  msg: ChatBubbleMessage;
  pending?: boolean;
  noTextReply: string;
}) {
  const isUser = msg.role === "user";
  const time = !pending ? formatChatTime(msg.ts) : "";
  const showFooter = !pending && (!!time || !!msg.content);
  return (
    <div className={`group flex flex-col ${isUser ? "items-end" : "items-start"}`}>
      <div
        className={
          isUser
            ? "max-w-[75%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-brand-500 px-4 py-2 text-sm text-white"
            : "shadow-card max-w-[75%] rounded-2xl rounded-tl-sm border border-ink-200 bg-surface px-4 py-2 text-sm text-ink-800"
        }
      >
        {msg.content ? (
          isUser ? (
            msg.content
          ) : (
            <ChatMarkdown content={msg.content} />
          )
        ) : pending ? (
          <PendingReply />
        ) : (
          <span className="text-ink-400">{noTextReply}</span>
        )}
      </div>
      {showFooter && (
        <div
          className={`mt-1 flex items-center gap-2 px-1 text-[11px] text-ink-400 ${
            isUser ? "flex-row-reverse" : ""
          }`}
        >
          {time && <span>{time}</span>}
          {msg.content && (
            <span className="opacity-0 transition-opacity group-hover:opacity-100">
              <CopyButton text={msg.content} />
            </span>
          )}
        </div>
      )}
    </div>
  );
}
