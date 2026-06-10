import type { KeyboardEvent } from "react";

/** Enter sends; Shift+Enter inserts a newline. Skips Enter during IME composition. */
export function handleChatTextareaEnterKey(
  e: KeyboardEvent<HTMLTextAreaElement>,
  onSend: () => void,
): void {
  if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) {
    return;
  }
  e.preventDefault();
  onSend();
}
