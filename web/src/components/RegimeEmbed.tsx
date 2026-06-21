import { useEffect, useRef } from "react";
import DOMPurify from "dompurify";

interface Props {
  html: string;
}

export function RegimeEmbed({ html }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const clean = DOMPurify.sanitize(html, {
      ADD_TAGS: ["style"],
      ADD_ATTR: ["class", "id"],
    });
    ref.current.innerHTML = clean;
  }, [html]);

  return <div className="regime-host content-panel" ref={ref} />;
}
