/**
 * SilentLink — a link that never reveals its destination in the browser status
 * bar on hover. Native `<a href>` (and react-router's `<Link>`, which renders
 * one) make the browser show the target URL in a corner tooltip on hover; this
 * component avoids that by NOT using `href` at all. The destination lives in a
 * `data-href` attribute and navigation is driven by JS (click + Enter/Space),
 * keeping it keyboard-accessible (role="link", tabindex).
 *
 * This mirrors the same approach used on the marketing site (clawsomeflow.com),
 * where nav/CTA links move their URL to `data-href` for the same reason.
 *
 * Use `as="div"` for block/card links that wrap other content (cards may
 * contain their own buttons — those should call `e.stopPropagation()` so a
 * click on them does not also trigger navigation). Default is an inline span.
 * Pass `external` to open the target in a new tab instead of in-app routing.
 */
import {
  type CSSProperties,
  type HTMLAttributes,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";

interface SilentLinkProps extends Omit<HTMLAttributes<HTMLElement>, "onClick"> {
  to: string;
  children: ReactNode;
  /** Open in a new tab (external URL) instead of client-side routing. */
  external?: boolean;
  /** Element tag: "div" for block/card links, "span" (default) for inline. */
  as?: "span" | "div";
  className?: string;
  style?: CSSProperties;
}

export function SilentLink({
  to,
  children,
  external = false,
  as = "span",
  className,
  style,
  ...rest
}: SilentLinkProps) {
  const navigate = useNavigate();
  const go = () => {
    if (external) window.open(to, "_blank", "noopener,noreferrer");
    else navigate(to);
  };
  const Tag = as;
  return (
    <Tag
      {...rest}
      role="link"
      tabIndex={0}
      data-href={to}
      className={className}
      style={{ cursor: "pointer", ...style }}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        go();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go();
        }
      }}
    >
      {children}
    </Tag>
  );
}
