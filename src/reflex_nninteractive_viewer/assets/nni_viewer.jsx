// React wrapper around the nnInteractive agent's framework-free viewer (viewer.js).
//
// viewer.js owns the canvases, the mouse handling and every GPU resource; this component only
// builds the DOM structure it expects (`els`), forwards props to its methods (one effect per
// prop) and turns its callbacks into Reflex events. Image data never passes through React
// props: props carry URLs and metadata, and the viewer fetches the binaries itself.
import { useEffect, useRef, useState } from "react";
import { getBackendURL } from "$/utils/state";
import { Viewer } from "./viewer.js";

// Layout of the panes, from the upstream app.css (the `.tile-label` class is set by viewer.js).
const CSS = `
.nni-viewer { position: relative; overflow: hidden; background: #000; outline: none; }
.nni-viewer .nni-pane { position: absolute; }
.nni-viewer .nni-pane2d { inset: 0; }
.nni-viewer .nni-pane canvas { display: block; width: 100%; height: 100%; outline: none; }
.nni-viewer .nni-pane3d { border-left: 1px solid #14181f; border-top: 1px solid #14181f; }
.nni-viewer .nni-tile-labels { position: absolute; inset: 0; pointer-events: none; }
.nni-viewer .tile-label {
  position: absolute; margin: 8px; padding: 2px 8px; border-radius: 4px; font: 600 11px/1.4 system-ui, sans-serif;
  letter-spacing: 0.04em; color: #c7ced8; background: rgba(12, 14, 18, 0.72); text-transform: uppercase;
  pointer-events: auto; cursor: pointer;
}
.nni-viewer .tile-label:hover { color: #fff; }
.nni-viewer .nni-status {
  position: absolute; inset: 0; display: grid; place-items: center; background: rgba(5, 6, 8, 0.72);
  color: #a1a9b4; font: 13px/1.45 system-ui, sans-serif; z-index: 4; text-align: center; padding: 16px;
}
.nni-viewer .nni-status.error { color: #f87171; }
.nni-viewer .nni-bar { width: 220px; height: 3px; margin-top: 10px; background: #1d222b; border-radius: 2px; overflow: hidden; }
.nni-viewer .nni-bar > div { height: 100%; background: #5aa8ff; }
`;

const NO_WEBGL =
  "WebGL2 is not available in this browser. Turn on hardware acceleration, or use a current " +
  "Chrome, Edge, Firefox or Safari (not through remote desktop or a VM without a GPU).";

// viewer.js remaps the slice canvas through `url(#wlFilter)` while a window/level drag is in
// progress and looks the filter up by id, so it must exist exactly once in the document.
let sharedRefs = 0;
function acquireSharedDom() {
  if (sharedRefs++ > 0) return;
  const style = document.createElement("style");
  style.id = "nni-viewer-style";
  style.textContent = CSS;
  document.head.appendChild(style);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.id = "nni-viewer-filters";
  svg.setAttribute("width", "0");
  svg.setAttribute("height", "0");
  svg.setAttribute("aria-hidden", "true");
  svg.style.position = "absolute";
  const filter = document.createElementNS(ns, "filter");
  filter.id = "wlFilter";
  filter.setAttribute("color-interpolation-filters", "sRGB");
  const transfer = document.createElementNS(ns, "feComponentTransfer");
  for (const c of ["feFuncR", "feFuncG", "feFuncB"]) {
    const f = document.createElementNS(ns, c);
    f.setAttribute("type", "table");
    transfer.appendChild(f);
  }
  filter.appendChild(transfer);
  svg.appendChild(filter);
  document.body.appendChild(svg);
}
function releaseSharedDom() {
  if (--sharedRefs > 0) return;
  document.getElementById("nni-viewer-style")?.remove();
  document.getElementById("nni-viewer-filters")?.remove();
}

/** Absolute URL for ``url``: relative ones are resolved against ``apiBase`` (default: the Reflex backend). */
function resolveUrl(url, apiBase) {
  if (!url) return url;
  let base = apiBase;
  if (!base) {
    try {
      base = getBackendURL().origin + "/";
    } catch {
      base = window.location.href;
    }
  }
  if (!base.endsWith("/")) base += "/";
  return new URL(url, base).href;
}

/** Split a patch bundle (repeated uint32 LE length + one viewer.applyPatch message) into messages. */
export function splitPatchBundle(buf) {
  const out = [];
  const dv = new DataView(buf);
  let off = 0;
  while (off + 4 <= buf.byteLength) {
    const n = dv.getUint32(off, true);
    if (off + 4 + n > buf.byteLength) throw new Error("Truncated patch bundle");
    out.push(buf.slice(off + 4, off + 4 + n));
    off += 4 + n;
  }
  return out;
}

const objectsSignature = (objects) => JSON.stringify((objects || []).map((o) => [o.id, o.color, o.name]));

export function NniViewer({
  volumeMeta = null,
  objects = [],
  meshes = [],
  patchUrl = null,
  patchWsUrl = null,
  apiBase = null,
  preset = null,
  window: windowIndex = null,
  surfaceOpacity = null,
  overlayMode = null,
  layout = null,
  tool = null,
  focus = null,
  focusObject = null,
  keyboard = true,
  onLocation,
  onReady,
  onError,
  onSliceChange,
  onLayoutRequest,
  onWindowChange,
  className,
  style,
  id,
}) {
  const rootRef = useRef(null);
  const canvas2dRef = useRef(null);
  const canvas3dRef = useRef(null);
  const pane3dRef = useRef(null);
  const labelsRef = useRef(null);
  const viewerRef = useRef(null);
  const initRef = useRef(null); // Promise of viewer.init(); rejects without WebGL2
  const meshState = useRef(new Map()); // key -> { version, opacity } of what the viewer has
  const meshOpacity = useRef(new Map()); // key -> opacity wanted by the props
  const lastLoc = useRef(null);
  const sliceTimer = useRef(null);
  const lastSlice = useRef("");
  const [loadedKey, setLoadedKey] = useState(null); // changes once per loaded image
  const [status, setStatus] = useState(null); // { text, frac, error }

  // Latest props and callbacks for the viewer callbacks, which are created once.
  const props = useRef({});
  props.current = {
    objects, meshes, patchUrl, apiBase, preset, windowIndex, surfaceOpacity, overlayMode, layout, tool,
    onLocation, onReady, onError, onSliceChange, onLayoutRequest, onWindowChange,
  };
  const fail = (message) => {
    setStatus({ text: message, error: true });
    props.current.onError?.(message);
  };

  // ------------------------------------------------------------- mount / unmount
  useEffect(() => {
    acquireSharedDom();
    const els = {
      viewport: rootRef.current,
      canvas2d: canvas2dRef.current,
      canvas3d: canvas3dRef.current,
      pane3d: pane3dRef.current,
      tileLabels: labelsRef.current,
    };
    const viewer = new Viewer(els, {
      onLocation: (loc) => {
        lastLoc.current = loc;
        if (!loc.inside) return;
        clearTimeout(sliceTimer.current);
        sliceTimer.current = setTimeout(() => {
          const key = loc.point.join();
          if (key === lastSlice.current) return;
          lastSlice.current = key;
          props.current.onSliceChange?.(loc.point);
        }, 150);
      },
      onLayoutRequest: (mode) => props.current.onLayoutRequest?.(mode),
      onWindowChange: (w) => {
        if (!w.live) props.current.onWindowChange?.(w.lo, w.hi, w.preset ?? null);
      },
    });
    viewerRef.current = viewer;
    initRef.current = viewer.init();
    initRef.current.catch((e) => {
      console.error("The nnInteractive viewer could not start:", e);
      fail(/webgl2/i.test(String(e?.message)) ? NO_WEBGL : `The image viewer could not start: ${e?.message ?? e}`);
    });
    // Per-mesh opacity: viewer.setMesh always uploads opaque surfaces; apply the wanted opacity
    // when the surface arrives (setMesh fetches asynchronously and may be deferred).
    initRef.current.then(() => {
      const r3 = viewer.r3;
      const setMesh = r3.setMesh.bind(r3);
      r3.setMesh = (key, pos, tris, rgb, opacity, visible) =>
        setMesh(key, pos, tris, rgb, meshOpacity.current.get(key) ?? opacity, visible);
    }, () => {});

    // "Mention location": a click without a drag in the slice views. viewer.js moves the
    // crosshair on mouse-up (directly or through NiiVue), so read it right after.
    const pane2d = canvas2dRef.current.parentElement;
    let down = null;
    const onDown = (e) => { down = e.button === 0 ? [e.clientX, e.clientY] : null; };
    const onUp = (e) => {
      if (!down || e.button !== 0 || Math.hypot(e.clientX - down[0], e.clientY - down[1]) >= 3) return;
      down = null;
      setTimeout(() => {
        const loc = lastLoc.current;
        if (!viewer.ready || !loc?.inside) return;
        const mm = Array.from(viewer.nv2.frac2mm(viewer.nv2.scene.crosshairPos)).slice(0, 3);
        props.current.onLocation?.(loc.point, mm, Number(loc.value));
      }, 30);
    };
    pane2d.addEventListener("mousedown", onDown, { capture: true });
    window.addEventListener("mouseup", onUp);

    return () => {
      pane2d.removeEventListener("mousedown", onDown, { capture: true });
      window.removeEventListener("mouseup", onUp);
      clearTimeout(sliceTimer.current);
      viewer.destroy();
      viewerRef.current = null;
      releaseSharedDom();
    };
  }, []);

  // --------------------------------------------------------------------- image
  const metaKey = volumeMeta ? JSON.stringify([volumeMeta.key, volumeMeta.n, volumeMeta.url, volumeMeta.url3d]) : null;
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !volumeMeta) return;
    let cancelled = false;
    (async () => {
      try {
        await initRef.current;
      } catch {
        return; // reported by the mount effect
      }
      const p = props.current;
      const meta = { ...volumeMeta, url: resolveUrl(volumeMeta.url, p.apiBase), url3d: resolveUrl(volumeMeta.url3d, p.apiBase) };
      if (p.layout) viewer.layout = p.layout; // load() applies it
      meshState.current.clear();
      lastSlice.current = "";
      setLoadedKey(null); // prop effects wait for the new image
      setStatus({ text: "Downloading the image ...", frac: 0 });
      let ok;
      try {
        ok = await viewer.load(meta, (frac, got, total, text) => {
          if (cancelled) return;
          const mb = (x) => (x / 1e6).toFixed(0);
          setStatus({ text: text || (total ? `Downloading the image ... ${mb(got)} of ${mb(total)} MB` : "Downloading the image ..."), frac });
        });
      } catch (e) {
        if (!cancelled) fail(`Could not display the image: ${e?.message ?? e}`);
        return;
      }
      if (!ok || cancelled) return; // superseded by a newer image
      setStatus(null);
      // Re-apply everything load() reset to its defaults.
      viewer.setObjects(p.objects || []);
      for (const o of p.objects || []) if (o.hidden) viewer.setHidden(o.id, true);
      if (p.overlayMode) viewer.setOverlayMode(p.overlayMode);
      if (p.windowIndex !== null && p.windowIndex !== undefined) viewer.setWindow(p.windowIndex);
      if (p.preset) viewer.setVolume3d(p.preset);
      if (p.surfaceOpacity !== null && p.surfaceOpacity !== undefined) viewer.setSurfaceOpacity(p.surfaceOpacity);
      if (p.tool) viewer.setTool(p.tool);
      setLoadedKey(metaKey);
      p.onReady?.({
        windows: viewer.windowNames(),
        presets: viewer.volumePresets || [],
        layout: viewer.layout,
        preset: viewer.volume3d ?? null,
      });
    })();
    return () => { cancelled = true; };
  }, [metaKey]);

  // ------------------------------------------------------------------- objects
  const objSig = objectsSignature(objects);
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !loadedKey) return;
    viewer.setObjects(objects || []);
  }, [objSig, loadedKey]);

  const hiddenSig = JSON.stringify((objects || []).filter((o) => o.hidden).map((o) => o.id).sort());
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !loadedKey) return;
    const want = new Set(JSON.parse(hiddenSig));
    for (const id of [...viewer.hidden]) if (!want.has(id)) viewer.setHidden(id, false);
    for (const id of want) if (!viewer.hidden.has(id)) viewer.setHidden(id, true);
  }, [hiddenSig, loadedKey]);

  // -------------------------------------------------------------------- meshes
  const meshSig = JSON.stringify((meshes || []).map((m) => [m.key, m.version, m.url, m.color, m.opacity]));
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !loadedKey) return;
    const have = meshState.current;
    const want = new Map((meshes || []).map((m) => [m.key, m]));
    for (const key of [...have.keys()]) {
      if (!want.has(key)) {
        viewer.removeMesh(key);
        have.delete(key);
        meshOpacity.current.delete(key);
      }
    }
    for (const [key, m] of want) {
      const opacity = m.opacity ?? 1;
      meshOpacity.current.set(key, opacity);
      const prev = have.get(key);
      if (!prev || prev.version !== m.version || prev.url !== m.url || prev.color !== m.color) {
        viewer.setMesh({ key, url: resolveUrl(m.url, apiBase), v: m.version ?? 0, color: m.color || "#e5e7eb", name: m.name });
      } else if (prev.opacity !== opacity) {
        viewer.r3.setMeshOpacity(key, opacity);
      }
      have.set(key, { version: m.version, url: m.url, color: m.color, opacity });
    }
  }, [meshSig, loadedKey]);

  // ---------------------------------------------------- label layers (simple)
  // The full label layers as one patch bundle, refetched whenever the objects change.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !loadedKey || !patchUrl) return;
    const ctrl = new AbortController();
    (async () => {
      try {
        const resp = await fetch(resolveUrl(patchUrl, apiBase), { signal: ctrl.signal, cache: "no-store" });
        if (resp.status === 204) return;
        if (!resp.ok) throw new Error(`${patchUrl}: HTTP ${resp.status}`);
        for (const msg of splitPatchBundle(await resp.arrayBuffer())) viewer.applyPatch(msg);
      } catch (e) {
        if (e?.name !== "AbortError") props.current.onError?.(`Could not load the label layers: ${e?.message ?? e}`);
      }
    })();
    return () => ctrl.abort();
  }, [patchUrl, objSig, loadedKey]);

  // ------------------------------------------------- label layers (streaming)
  // A WebSocket whose binary messages are single patches (nninteractive_agent's LayerSync).
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !loadedKey || !patchWsUrl) return;
    let sock = null;
    let retry = 0;
    let timer = null;
    let closed = false;
    const connect = () => {
      const url = new URL(resolveUrl(patchWsUrl, apiBase));
      if (url.protocol === "http:") url.protocol = "ws:";
      if (url.protocol === "https:") url.protocol = "wss:";
      sock = new WebSocket(url);
      sock.binaryType = "arraybuffer";
      sock.onopen = () => { retry = 0; };
      sock.onmessage = (m) => { if (typeof m.data !== "string") viewer.applyPatch(m.data); };
      sock.onclose = () => {
        if (!closed) timer = setTimeout(connect, Math.min(10000, 600 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      sock?.close();
    };
  }, [patchWsUrl, loadedKey]);

  // ------------------------------------------------------------ display props
  useEffect(() => {
    if (loadedKey && preset) viewerRef.current?.setVolume3d(preset);
  }, [preset, loadedKey]);
  useEffect(() => {
    if (loadedKey && windowIndex !== null && windowIndex !== undefined) viewerRef.current?.setWindow(windowIndex);
  }, [windowIndex, loadedKey]);
  useEffect(() => {
    if (loadedKey && surfaceOpacity !== null && surfaceOpacity !== undefined) viewerRef.current?.setSurfaceOpacity(surfaceOpacity);
  }, [surfaceOpacity, loadedKey]);
  useEffect(() => {
    if (loadedKey && overlayMode) viewerRef.current?.setOverlayMode(overlayMode);
  }, [overlayMode, loadedKey]);
  useEffect(() => {
    if (loadedKey && layout) viewerRef.current?.setLayout(layout);
  }, [layout, loadedKey]);
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !tool) return;
    initRef.current?.then(() => viewer.setTool(tool), () => {});
  }, [tool]);
  const focusSig = focus ? focus.join() : null;
  useEffect(() => {
    if (loadedKey && focus) viewerRef.current?.focusVoxel(focus);
  }, [focusSig, loadedKey]);
  useEffect(() => {
    if (loadedKey && focusObject !== null && focusObject !== undefined) viewerRef.current?.focusObject(focusObject);
  }, [focusObject, loadedKey]);

  // ------------------------------------------------------------------ keyboard
  // The upstream app's slice keys: arrows / Page Up / Page Down step the slice under the
  // pointer, R resets the view. Tool and layout keys are left to the host (they are props).
  const onKeyDown = (e) => {
    const viewer = viewerRef.current;
    if (!keyboard || !viewer?.ready || e.ctrlKey || e.metaKey || e.altKey) return;
    const step = { ArrowUp: -1, ArrowDown: 1, PageUp: -10, PageDown: 10 }[e.key];
    if (step) {
      e.preventDefault();
      viewer.stepSlice(step);
    } else if (e.key.toLowerCase() === "r") {
      viewer.resetView();
    }
  };

  return (
    <div
      ref={rootRef}
      id={id}
      className={`nni-viewer${className ? ` ${className}` : ""}`}
      style={style}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onMouseDown={() => rootRef.current?.focus({ preventScroll: true })}
      data-nni-ready={loadedKey ? "true" : "false"}
    >
      <div className="nni-pane nni-pane2d"><canvas ref={canvas2dRef} data-nni="canvas2d" /></div>
      <div className="nni-pane nni-pane3d" ref={pane3dRef}><canvas ref={canvas3dRef} data-nni="canvas3d" /></div>
      <div className="nni-tile-labels" ref={labelsRef} />
      {status && (
        <div className={`nni-status${status.error ? " error" : ""}`}>
          <div>
            <div>{status.text}</div>
            {!status.error && (
              <div className="nni-bar"><div style={{ width: `${Math.round((status.frac ?? 0) * 100)}%` }} /></div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
