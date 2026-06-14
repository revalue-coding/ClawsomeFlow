import { useEffect, useState } from "react";

import { ChatMarkdown } from "@/components/ChatMarkdown";

export interface ChatBubbleMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

function TypingDots() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((v) => (v + 1) % 3), 350);
    return () => clearInterval(timer);
  }, []);
  return <span className="text-ink-400">{".".repeat(tick + 1)}</span>;
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
  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
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
          <TypingDots />
        ) : (
          <span className="text-ink-400">{noTextReply}</span>
        )}
      </div>
    </div>
  );
}
