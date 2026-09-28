import { useMemo } from "react";
import { encode } from "uqr";

/** A QR code drawn as one SVG path (no innerHTML, no canvas). */
export function QrCode({
  value,
  label,
  size = 200,
}: {
  value: string;
  label: string;
  size?: number;
}) {
  const { path, dimension } = useMemo(() => {
    const qr = encode(value, { ecc: "M", border: 2 });
    let d = "";
    qr.data.forEach((row, y) => {
      row.forEach((dark, x) => {
        if (dark) d += `M${x} ${y}h1v1h-1z`;
      });
    });
    return { path: d, dimension: qr.data.length };
  }, [value]);
  return (
    <svg
      className="qr"
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox={`0 0 ${dimension} ${dimension}`}
      shapeRendering="crispEdges"
    >
      <rect width={dimension} height={dimension} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  );
}
