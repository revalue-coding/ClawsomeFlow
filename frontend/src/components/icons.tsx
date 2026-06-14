import { useId, type SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function IconBase({ className, children, ...rest }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function BrandIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M12 3.5 19.5 8v8L12 20.5 4.5 16V8Z" />
      <path d="M9 11.5a3 3 0 0 1 6 0" />
      <path d="M9.5 14.5h5" />
      <circle cx="12" cy="16.5" r="0.9" />
    </IconBase>
  );
}

export function FlowIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="6.5" y="4.5" width="11" height="15" rx="2.2" />
      <path d="M9 4.5h6v2H9z" />
      <path d="M9.5 10h5" />
      <path d="M9.5 13h5" />
    </IconBase>
  );
}

export function RunIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m10 8.7 5 3.3-5 3.3Z" fill="currentColor" stroke="none" />
    </IconBase>
  );
}

export function AssistantIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 3.5v5.4" />
      <path d="M12 15.1v5.4" />
      <path d="M3.5 12h5.4" />
      <path d="M15.1 12h5.4" />
    </IconBase>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="5.5" y="8" width="13" height="9" rx="2.5" />
      <path d="M9 17v2.2l2.2-2.2" />
      <path d="M12 4.8V8" />
      <circle cx="10" cy="12.5" r="0.8" fill="currentColor" stroke="none" />
      <circle cx="14" cy="12.5" r="0.8" fill="currentColor" stroke="none" />
    </IconBase>
  );
}

export function LobsterIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <ellipse cx="12" cy="12.5" rx="2.6" ry="4.1" />
      <circle cx="10.6" cy="7.8" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="13.4" cy="7.8" r="0.6" fill="currentColor" stroke="none" />
      <path d="M11.1 16.8 9.9 19.6" />
      <path d="M12.9 16.8 14.1 19.6" />
      <path d="M9.4 11.2 6.9 10" />
      <path d="M9.2 13.3 6.5 13.3" />
      <path d="M9.4 15.2 6.9 16.4" />
      <path d="M14.6 11.2 17.1 10" />
      <path d="M14.8 13.3 17.5 13.3" />
      <path d="M14.6 15.2 17.1 16.4" />
      <path d="m6.8 8.8-2.3-1.1 1 2.3" />
      <path d="m17.2 8.8 2.3-1.1-1 2.3" />
      <path d="M10.7 5.8 9 4.2" />
      <path d="M13.3 5.8 15 4.2" />
    </IconBase>
  );
}

export function SettingsIcon(props: IconProps) {
  // Sliders/tuning-board glyph — three horizontal rails with knobs at
  // distinct positions. Reads as "configuration profile" much more
  // immediately than a generic cogwheel.
  return (
    <IconBase {...props}>
      <path d="M4.5 7h15" />
      <path d="M4.5 12h15" />
      <path d="M4.5 17h15" />
      <circle cx="9" cy="7" r="2" fill="white" />
      <circle cx="15" cy="12" r="2" fill="white" />
      <circle cx="8" cy="17" r="2" fill="white" />
    </IconBase>
  );
}

export function DocsIcon(props: IconProps) {
  // Open book / manual glyph — reads as "documentation".
  return (
    <IconBase {...props}>
      <path d="M12 6.5C10.5 5.3 8.7 4.7 6.5 4.7c-.9 0-1.7.1-2.5.3v12.4c.8-.2 1.6-.3 2.5-.3 2.2 0 4 .6 5.5 1.8" />
      <path d="M12 6.5c1.5-1.2 3.3-1.8 5.5-1.8.9 0 1.7.1 2.5.3v12.4c-.8-.2-1.6-.3-2.5-.3-2.2 0-4 .6-5.5 1.8" />
      <path d="M12 6.5v12.4" />
    </IconBase>
  );
}

export function ExternalLinkIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M14.5 5.5h4v4" />
      <path d="m10 14 8.5-8.5" />
      <path d="M18.5 13.5v4a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1h4" />
    </IconBase>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M4.5 7h15" />
      <path d="M9.5 7V5.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7" />
      <path d="M7 7l.8 11.2a1 1 0 0 0 1 .9h6.4a1 1 0 0 0 1-.9L17 7" />
      <path d="M10 10.5v5.5" />
      <path d="M14 10.5v5.5" />
    </IconBase>
  );
}

export function EditIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M12.5 6.5 17.5 11.5" />
      <path d="M5.5 18.5 8 12l8.8-8.8a1.8 1.8 0 0 1 2.6 0l1.4 1.4a1.8 1.8 0 0 1 0 2.6L12 16l-6.5 2.5Z" />
      <path d="M14.5 5 19 9.5" />
    </IconBase>
  );
}

export function BackIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path
        d="M10.9 5.1a.9.9 0 0 0-1.27 0l-5.4 5.4a.9.9 0 0 0 0 1.27l5.4 5.4a.9.9 0 1 0 1.27-1.27l-3.86-3.86h7.37a4.78 4.78 0 0 1 4.78 4.78v1.65a.9.9 0 1 0 1.8 0V16.8a6.58 6.58 0 0 0-6.58-6.58H7.04l3.86-3.86a.9.9 0 0 0 0-1.27Z"
        fill="currentColor"
        stroke="none"
      />
    </IconBase>
  );
}

export function StoreIcon(props: IconProps) {
  const gradA = useId();
  const gradB = useId();
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={props.className}
      aria-hidden="true"
      {...props}
    >
      <defs>
        <linearGradient id={gradA} x1="3" y1="3" x2="21" y2="21" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#7C3AED" />
          <stop offset="48%" stopColor="#EC4899" />
          <stop offset="100%" stopColor="#F59E0B" />
        </linearGradient>
        <linearGradient id={gradB} x1="6" y1="10" x2="18" y2="20" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#6366F1" />
          <stop offset="100%" stopColor="#06B6D4" />
        </linearGradient>
      </defs>
      <path d="M4.5 8.2 6.8 4.8h10.4l2.3 3.4" stroke={`url(#${gradA})`} strokeWidth="1.9" />
      <path d="M5 8.2h14v9.9a1.9 1.9 0 0 1-1.9 1.9H6.9A1.9 1.9 0 0 1 5 18.1z" stroke={`url(#${gradA})`} strokeWidth="1.9" />
      <path d="M9 11.8h6" stroke={`url(#${gradB})`} strokeWidth="1.9" />
      <path d="M10 15h4" stroke={`url(#${gradB})`} strokeWidth="1.9" />
    </svg>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M20 12a8 8 0 1 1-2.34-5.66" />
      <path d="M20 4v6h-6" />
    </IconBase>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </IconBase>
  );
}

export function DesktopIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3.5" y="4.5" width="17" height="12" rx="2" />
      <path d="M9 19.5h6" />
      <path d="M12 16.5v3" />
    </IconBase>
  );
}

/** Sun — light-theme glyph for the theme switcher. */
export function SunIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2.5" />
      <path d="M12 19.5V22" />
      <path d="M2 12h2.5" />
      <path d="M19.5 12H22" />
      <path d="m4.6 4.6 1.8 1.8" />
      <path d="m17.6 17.6 1.8 1.8" />
      <path d="m19.4 4.6-1.8 1.8" />
      <path d="m6.4 17.6-1.8 1.8" />
    </IconBase>
  );
}

/** Moon — dark-theme glyph for the theme switcher. */
export function MoonIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M20 13.5A8 8 0 1 1 10.5 4a6.2 6.2 0 0 0 9.5 9.5Z" />
    </IconBase>
  );
}
