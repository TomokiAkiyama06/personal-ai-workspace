// Line icons of the PAW-060 design canvas (same paths), drawn with currentColor
// so the theme tokens colour them. Decorative: always aria-hidden.
import type { ReactNode } from "react";

export type IconName =
  | "logo"
  | "search"
  | "moon"
  | "sun"
  | "bell"
  | "chevronDown"
  | "chevronRight"
  | "back"
  | "plus"
  | "chat"
  | "projects"
  | "agents"
  | "memory"
  | "pulls"
  | "admin"
  | "settings"
  | "profile"
  | "devices"
  | "usage"
  | "keyboard"
  | "help"
  | "signOut"
  | "menu"
  | "info"
  | "eye"
  | "laptop"
  | "phone"
  | "key";

const PATHS: Record<Exclude<IconName, "logo">, ReactNode> = {
  search: (
    <>
      <circle cx="11" cy="11" r="6" />
      <path d="M15.5 15.5 20 20" />
    </>
  ),
  moon: <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2" />
      <path d="M12 19v2" />
      <path d="M4.5 4.5 6 6" />
      <path d="M18 18l1.5 1.5" />
      <path d="M3 12h2" />
      <path d="M19 12h2" />
      <path d="M4.5 19.5 6 18" />
      <path d="M18 6l1.5-1.5" />
    </>
  ),
  bell: (
    <>
      <path d="M6 9.5a6 6 0 0 1 12 0c0 3.8 1.4 5.2 1.4 5.2H4.6S6 13.3 6 9.5Z" />
      <path d="M10 18a2 2 0 0 0 4 0" />
    </>
  ),
  chevronDown: <path d="m6 9.5 6 5.5 6-5.5" />,
  chevronRight: <path d="m9.5 6 6 6-6 6" />,
  back: <path d="M14.5 6 8.5 12l6 6" />,
  plus: (
    <>
      <path d="M12 5.5v13" />
      <path d="M5.5 12h13" />
    </>
  ),
  chat: (
    <path d="M20 6.5a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2v4l4.5-4H18a2 2 0 0 0 2-2Z" />
  ),
  projects: (
    <path d="M3 7.5a2 2 0 0 1 2-2h3.6l2 2.4H19a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
  ),
  agents: (
    <>
      <circle cx="6" cy="7" r="2.4" />
      <circle cx="18" cy="6" r="2.4" />
      <circle cx="12" cy="18" r="2.4" />
      <path d="M7.4 9 L11 15.8" />
      <path d="M16.6 8 L13.2 16" />
      <path d="M8.4 6.7 L15.6 6.2" />
    </>
  ),
  memory: (
    <>
      <path d="M12 3.5 3 8l9 4.5L21 8Z" />
      <path d="M3 12.6 12 17.1 21 12.6" />
      <path d="M3 16.9 12 21.4 21 16.9" />
    </>
  ),
  pulls: (
    <>
      <circle cx="7" cy="6" r="2.2" />
      <circle cx="7" cy="18" r="2.2" />
      <circle cx="17" cy="18" r="2.2" />
      <path d="M7 8.2v7.6" />
      <path d="M17 15.8V11a3 3 0 0 0-3-3h-2.6" />
    </>
  ),
  admin: <path d="M12 3.5 19 6v6c0 4-3 6.6-7 8.5-4-1.9-7-4.5-7-8.5V6Z" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.2" />
      <path d="M12 18.8V21" />
      <path d="m4.6 7.5 1.9 1.1" />
      <path d="m17.5 15.4 1.9 1.1" />
      <path d="m4.6 16.5 1.9-1.1" />
      <path d="m17.5 8.6 1.9-1.1" />
    </>
  ),
  profile: (
    <>
      <circle cx="12" cy="8" r="3.4" />
      <path d="M5.5 20v-1a4 4 0 0 1 4-4h5a4 4 0 0 1 4 4v1" />
    </>
  ),
  devices: (
    <>
      <rect x="3" y="5" width="18" height="12" rx="2" />
      <path d="M2 20h20" />
    </>
  ),
  usage: (
    <>
      <path d="M5 19V11" />
      <path d="M12 19V5" />
      <path d="M19 19v-5" />
    </>
  ),
  keyboard: (
    <>
      <rect x="3" y="6" width="18" height="12" rx="2" />
      <path d="M8 14h8" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.6 9.5a2.4 2.4 0 1 1 3.3 2.2c-.6.3-.9.8-.9 1.4v.4" />
      <path d="M12 16.6v.4" />
    </>
  ),
  signOut: (
    <>
      <path d="M15 5.5H7.5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2H15" />
      <path d="M18.5 12H11" />
      <path d="m15.5 8.5 3.5 3.5-3.5 3.5" />
    </>
  ),
  menu: (
    <>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5" />
      <path d="M12 7.6v.5" />
    </>
  ),
  eye: (
    <>
      <path d="M2 12s3.8-6.5 10-6.5S22 12 22 12s-3.8 6.5-10 6.5S2 12 2 12Z" />
      <circle cx="12" cy="12" r="2.6" />
    </>
  ),
  laptop: (
    <>
      <rect x="3" y="5" width="18" height="12" rx="2" />
      <path d="M2 20h20" />
    </>
  ),
  phone: (
    <>
      <rect x="6" y="2.5" width="12" height="19" rx="3" />
      <path d="M10.5 18.5h3" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="12" r="4" />
      <path d="M12 12h9" />
      <path d="M17 12v4" />
      <path d="M20.5 12v3" />
    </>
  ),
};

export function Icon({ name, size = 17 }: { name: IconName; size?: number }) {
  if (name === "logo") {
    return (
      <svg
        className="icon logo-icon"
        width={size}
        height={size}
        viewBox="0 0 28 28"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
        focusable="false"
      >
        <circle cx="6" cy="8" r="3" />
        <circle cx="22" cy="6" r="3" />
        <circle cx="14" cy="21" r="3" />
        <path d="M8.6 9.8 L12 18" />
        <path d="M19.6 8 L15.4 18.6" />
        <path d="M9 7.4 L19 6.3" />
      </svg>
    );
  }
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
