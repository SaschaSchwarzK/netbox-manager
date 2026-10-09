import { useEffect, useState } from "react";
import { ImagePreview } from "../api/client";

export type ActiveImagePreview = (ImagePreview & { title: string }) | "loading" | "error" | null;

export default function ImagePreviewModal({ image, onClose }: { image: ActiveImagePreview; onClose: () => void }) {
  const [zoom, setZoom] = useState(1);
  useEffect(() => { setZoom(1); }, [image]);
  if (!image) return null;
  return <div className="card" style={{ position: "fixed", inset: "5% 8%", zIndex: 20, overflow: "auto", boxShadow: "0 8px 40px rgba(0,0,0,.5)" }}>
    <div className="toolbar" style={{ justifyContent: "space-between" }}><h2>Image preview</h2><div>
      <button disabled={typeof image === "string"} onClick={() => setZoom((value) => Math.max(.25, value - .25))}>−</button>{" "}
      <button disabled={typeof image === "string"} onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>{" "}
      <button disabled={typeof image === "string"} onClick={() => setZoom((value) => Math.min(5, value + .25))}>+</button>{" "}
      <button onClick={onClose}>Close</button>
    </div></div>
    {image === "loading" && <p>Loading image…</p>}
    {image === "error" && <p style={{ color: "var(--danger)" }}>Could not load this image.</p>}
    {image !== "loading" && image !== "error" && <div style={{ overflow: "auto", textAlign: "center" }}>
      <p>{image.title} <span className="mono">{image.filename}</span></p>
      <img src={`data:${image.content_type};base64,${image.content_base64}`} alt={image.title}
        style={{ display: "inline-block", width: `${zoom * 100}%`, maxWidth: "none", transformOrigin: "top center" }} />
    </div>}
  </div>;
}
