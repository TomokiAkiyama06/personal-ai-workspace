// A readable default name for this device's session ("Chrome · macOS"), so the
// devices list is not full of unnamed entries. The design's sign-in form has no
// device-name field; the Backend takes an optional one (DEVICE_NAME_MAX).

const BROWSERS: readonly [RegExp, string][] = [
  [/Edg\//, "Edge"],
  [/OPR\/|Opera/, "Opera"],
  [/Firefox\//, "Firefox"],
  [/Chrome\/|CriOS\//, "Chrome"],
  [/Safari\//, "Safari"],
];

const SYSTEMS: readonly [RegExp, string][] = [
  [/iPhone/, "iPhone"],
  [/iPad/, "iPad"],
  [/Android/, "Android"],
  [/Windows/, "Windows"],
  [/Mac OS X|Macintosh/, "macOS"],
  [/CrOS/, "ChromeOS"],
  [/Linux/, "Linux"],
];

function first(table: readonly [RegExp, string][], agent: string): string | null {
  return table.find(([pattern]) => pattern.test(agent))?.[1] ?? null;
}

export function describeDevice(agent: string = navigator.userAgent): string {
  const browser = first(BROWSERS, agent);
  const system = first(SYSTEMS, agent);
  return [browser, system].filter(Boolean).join(" · ");
}
