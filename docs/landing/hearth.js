/** HESTIA hero — a diagram of what the control plane actually does.
 *
 *  host plane          the operator's machine
 *  control plane       the Hestia process, :9480, holding the tenant ledger
 *  isolation boundary  what a tenant cannot cross
 *  tenant cells        one isolated process per deployed capability provider
 *  edge                /t/{slug} (invoke/health) and /ai-market/v2/invoke
 *                      (same tenant by capability_id; host SKUs token-gated).
 *                      Authorization never forwarded
 *  receipt             Ed25519 over result + capability + input digest
 *  hub                 outside the boundary; reached only by an explicit announce
 *
 * The scene is a model (SIM) unless /v1/hearth returns a roster with tenants in
 * it. An empty roster never invents one: the rack stays dark and the scene falls
 * back to narrating the deploy path, badged SIM and captioned "0 hosted", because
 * a model of a deploy is not a claim that a deploy happened. A failed probe is
 * UNREACHABLE, never fake LIVE.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";

export const HEARTH_ROSTER_URL = "https://hestia.modelmarket.dev/v1/hearth";

const CYCLE = 18;
const PHASES = [
  { id: "IDLE", at: 0 },
  { id: "DEPLOY", at: 2.2 },
  { id: "ADMIT", at: 4.6 },
  { id: "START", at: 6.0 },
  { id: "INVOKE", at: 8.0 },
  { id: "RECEIPT", at: 10.2 },
  { id: "ANNOUNCE", at: 12.4 },
  { id: "STEADY", at: 14.6 },
];

const EMBER = 0xff9a3c;
const TEAL = 0x66f7c5;
const GOLD = 0xffcc66;
const HUB = 0x8ecbff;
const SLOTS = 8;

export async function probeHearth(url = HEARTH_ROSTER_URL) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      headers: { accept: "application/json" },
    });
    if (!res.ok) {
      return { mode: "UNREACHABLE", status: res.status, tenants: [] };
    }
    const data = await res.json();
    if (!data || data.ok !== true || !Array.isArray(data.tenants)) {
      return { mode: "UNREACHABLE", status: res.status, tenants: [] };
    }
    return {
      mode: "LIVE",
      hearth: typeof data.hearth === "string" ? data.hearth : url,
      tenants: data.tenants,
    };
  } catch {
    return { mode: "UNREACHABLE", tenants: [] };
  } finally {
    clearTimeout(timer);
  }
}

function phaseAt(t) {
  let cur = PHASES[0].id;
  for (const p of PHASES) {
    if (t >= p.at) cur = p.id;
  }
  return cur;
}

function span(from, to, t) {
  return THREE.MathUtils.clamp((t - from) / (to - from), 0, 1);
}

// The host plane. A grid drawn as line segments has hard edges where it stops;
// this fades out radially so the diagram sits on a surface rather than a rug.
const floorVert = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const floorFrag = /* glsl */ `
  varying vec2 vUv;
  uniform vec3 uColor;
  uniform vec3 uGlow;
  uniform float uTime;
  uniform float uCells;
  float line(float v, float w) {
    float d = abs(fract(v) - 0.5) / fwidth(v);
    return 1.0 - smoothstep(0.0, w, d);
  }
  void main() {
    vec2 p = (vUv - 0.5) * 2.0;
    float r = length(p);
    float fade = 1.0 - smoothstep(0.25, 1.0, r);
    vec2 g = vUv * 26.0;
    float minor = max(line(g.x, 1.2), line(g.y, 1.2)) * 0.22;
    float major = max(line(g.x / 4.0, 1.0), line(g.y / 4.0, 1.0)) * 0.40;
    vec3 c = uColor * (minor + major);
    // A pool of light under the rack, brightening with the number of tenants.
    float pool = (1.0 - smoothstep(0.0, 0.42, r)) * (0.16 + uCells * 0.12);
    c += uGlow * pool;
    float a = (minor + major + pool) * fade;
    if (a < 0.004) discard;
    gl_FragColor = vec4(c, a);
  }
`;

export function mountHearth(canvas, opts = {}) {
  const host = canvas.parentElement || canvas;
  const mobile = matchMedia("(max-width: 820px), (pointer: coarse)").matches;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const onHud = typeof opts.onHud === "function" ? opts.onHud : () => {};
  const onFail = typeof opts.onFail === "function" ? opts.onFail : () => {};
  const labels = typeof opts.labels === "function" ? opts.labels : () => ({});
  const roster = opts.roster && opts.roster.mode === "LIVE" ? opts.roster : null;
  const liveTenants = roster ? roster.tenants.slice(0, SLOTS) : [];
  const live = Boolean(roster);
  const liveEmpty = live && liveTenants.length === 0;

  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: !mobile,
    powerPreference: "high-performance",
  });
  if (!renderer.getContext()) {
    onFail();
    return { destroy() {}, ok: false };
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, mobile ? 1.15 : 1.75));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(mobile ? 46 : 41, 1, 0.1, 90);
  if (mobile) camera.position.set(2.2, 3.7, 8.8);
  else camera.position.set(2.9, 3.2, 7.6);

  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = !reduced;
  controls.dampingFactor = 0.055;
  controls.enablePan = false;
  controls.autoRotate = false;
  controls.target.set(0, 0.62, 0);
  controls.minDistance = 5.6;
  controls.maxDistance = 13;
  controls.minPolarAngle = 0.5;
  controls.maxPolarAngle = 1.36;

  // Physically-correct light units: point lights here are ~1, not ~10. A point
  // light of intensity 5 a few tenths of a unit from a surface saturates it to
  // white, which is what used to wash this hero out.
  scene.add(
    new THREE.AmbientLight(0x2a2230, 1.1),
    new THREE.HemisphereLight(0x5c6b82, 0x0a0806, 0.55),
  );
  const key = new THREE.DirectionalLight(0xffe2bd, 2.1);
  key.position.set(4.2, 6.0, 4.6);
  const fill = new THREE.DirectionalLight(0x9fc8ff, 0.5);
  fill.position.set(-5.0, 2.6, -2.4);
  // Back rim so each blade in the rack keeps a lit edge.
  const rim = new THREE.DirectionalLight(0xbfe4ff, 1.1);
  rim.position.set(-2.0, 2.2, -5.0);
  scene.add(key, fill, rim);

  const root = new THREE.Group();
  scene.add(root);
  const trash = [];
  const track = (obj) => {
    trash.push(obj);
    return obj;
  };

  // ---------------------------------------------------------------- host plane
  const floorUniforms = {
    uColor: { value: new THREE.Color(0xb0774e) },
    uGlow: { value: new THREE.Color(EMBER) },
    uTime: { value: 0 },
    uCells: { value: 0 },
  };
  const floor = new THREE.Mesh(
    track(new THREE.PlaneGeometry(9, 9)),
    track(new THREE.ShaderMaterial({
      uniforms: floorUniforms,
      vertexShader: floorVert,
      fragmentShader: floorFrag,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    })),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.002;
  root.add(floor);

  // ------------------------------------------------------- control plane slab
  const slabW = 3.4;
  const slabD = 1.9;
  const slab = new THREE.Mesh(
    track(new RoundedBoxGeometry(slabW, 0.24, slabD, 3, 0.035)),
    track(new THREE.MeshStandardMaterial({
      color: 0x4a3526,
      roughness: 0.42,
      metalness: 0.62,
    })),
  );
  slab.position.y = 0.12;
  root.add(slab);

  // Port strip: the lit front face of the box the tenants are racked into.
  const portStrip = new THREE.Mesh(
    track(new THREE.PlaneGeometry(slabW - 0.3, 0.055)),
    track(new THREE.MeshBasicMaterial({
      color: EMBER, transparent: true, opacity: 0.5,
    })),
  );
  portStrip.position.set(0, 0.06, slabD / 2 + 0.002);
  root.add(portStrip);

  const slabEdge = new THREE.LineSegments(
    track(new THREE.EdgesGeometry(slab.geometry)),
    track(new THREE.LineBasicMaterial({ color: EMBER, transparent: true, opacity: 0.55 })),
  );
  slabEdge.position.copy(slab.position);
  root.add(slabEdge);

  // The edge: the single door into the boundary, on the front face of the slab.
  const gate = new THREE.Mesh(
    track(new THREE.BoxGeometry(0.46, 0.2, 0.16)),
    track(new THREE.MeshStandardMaterial({
      color: 0x2c1d10,
      emissive: EMBER,
      emissiveIntensity: 0.5,
      roughness: 0.4,
    })),
  );
  gate.position.set(0, 0.22, slabD / 2 + 0.02);
  root.add(gate);

  // ----------------------------------------------------------- isolation shell
  const shellW = 3.0;
  const shellH = 1.05;
  const shellD = 1.5;
  const shellGeo = track(new THREE.BoxGeometry(shellW, shellH, shellD));
  const shellEdge = new THREE.LineSegments(
    track(new THREE.EdgesGeometry(shellGeo)),
    track(new THREE.LineBasicMaterial({ color: TEAL, transparent: true, opacity: 0.55 })),
  );
  shellEdge.position.set(0, 0.24 + shellH / 2, 0);
  root.add(shellEdge);

  // Corner brackets. Twelve short L-segments at the corners say "enclosure";
  // a plain box outline just says "a cube is here".
  const bracketPts = [];
  const bx = shellW / 2;
  const by = shellH / 2;
  const bz = shellD / 2;
  const armX = shellW * 0.17;
  const armY = shellH * 0.22;
  const armZ = shellD * 0.2;
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const cx = sx * bx;
        const cy = sy * by;
        const cz = sz * bz;
        bracketPts.push(cx, cy, cz, cx - sx * armX, cy, cz);
        bracketPts.push(cx, cy, cz, cx, cy - sy * armY, cz);
        bracketPts.push(cx, cy, cz, cx, cy, cz - sz * armZ);
      }
    }
  }
  const bracketGeo = track(new THREE.BufferGeometry());
  bracketGeo.setAttribute("position", new THREE.Float32BufferAttribute(bracketPts, 3));
  const brackets = new THREE.LineSegments(
    bracketGeo,
    track(new THREE.LineBasicMaterial({ color: TEAL, transparent: true, opacity: 0.95 })),
  );
  brackets.position.set(0, 0.24 + shellH / 2, 0);
  root.add(brackets);

  // A plane sweeping the enclosure: the boundary is checked, not assumed.
  const scanPlane = new THREE.Mesh(
    track(new THREE.PlaneGeometry(shellW, shellD)),
    track(new THREE.MeshBasicMaterial({
      color: TEAL, transparent: true, opacity: 0.07,
      side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending,
    })),
  );
  scanPlane.rotation.x = -Math.PI / 2;
  root.add(scanPlane);

  // Footprint: where the boundary meets the control plane.
  const footprint = new THREE.LineLoop(
    track(new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-shellW / 2, 0.245, -shellD / 2),
      new THREE.Vector3(shellW / 2, 0.245, -shellD / 2),
      new THREE.Vector3(shellW / 2, 0.245, shellD / 2),
      new THREE.Vector3(-shellW / 2, 0.245, shellD / 2),
    ])),
    track(new THREE.LineBasicMaterial({ color: TEAL, transparent: true, opacity: 0.5 })),
  );
  root.add(footprint);

  // ------------------------------------------------------------- tenant cells
  const cellMats = [];
  const cells = [];
  for (let i = 0; i < SLOTS; i += 1) {
    const col = i % 4;
    const row = Math.floor(i / 4);
    const x = -1.05 + col * 0.7;
    const z = -0.32 + row * 0.64;
    const body = new THREE.Mesh(
      track(new RoundedBoxGeometry(0.46, 0.42, 0.4, 2, 0.035)),
      track(new THREE.MeshStandardMaterial({
        color: 0x3b3d4d,
        roughness: 0.36,
        metalness: 0.55,
      })),
    );
    body.position.set(x, 0.24 + 0.21, z);
    const lampMat = track(new THREE.MeshStandardMaterial({
      color: 0x10201c,
      emissive: TEAL,
      emissiveIntensity: 0,
      roughness: 0.35,
    }));
    cellMats.push(lampMat);
    const lamp = new THREE.Mesh(track(new THREE.BoxGeometry(0.34, 0.035, 0.28)), lampMat);
    lamp.position.set(x, 0.24 + 0.43, z);
    const outline = new THREE.LineSegments(
      track(new THREE.EdgesGeometry(new THREE.BoxGeometry(0.46, 0.42, 0.4))),
      track(new THREE.LineBasicMaterial({ color: 0xaebdd4, transparent: true, opacity: 0.35 })),
    );
    outline.position.copy(body.position);
    // Front-facing activity bar: what makes a running process look like one.
    const barMat = track(new THREE.MeshBasicMaterial({
      color: TEAL, transparent: true, opacity: 0,
    }));
    const bar = new THREE.Mesh(track(new THREE.PlaneGeometry(0.3, 0.028)), barMat);
    bar.position.set(x, 0.24 + 0.12, z + 0.201);
    root.add(body, lamp, outline, bar);
    cells.push({ body, lamp, lampMat, outline, bar, barMat, x, z, running: false });
  }

  // --------------------------------------------------------- external actors
  function node(color, size, pos) {
    const mesh = new THREE.Mesh(
      track(new THREE.OctahedronGeometry(size, 0)),
      track(new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: 0.55,
        roughness: 0.3,
        metalness: 0.2,
      })),
    );
    mesh.position.copy(pos);
    root.add(mesh);
    return mesh;
  }

  const clientPos = new THREE.Vector3(-2.35, 0.62, 1.65);
  const hubPos = new THREE.Vector3(2.3, 1.75, -1.35);
  const client = node(GOLD, 0.19, clientPos);
  const hub = node(HUB, 0.21, hubPos);

  function path(points, color, opacity) {
    const geo = track(new THREE.BufferGeometry().setFromPoints(points));
    const line = new THREE.Line(
      geo,
      track(new THREE.LineBasicMaterial({ color, transparent: true, opacity })),
    );
    root.add(line);
    return line;
  }

  const gateIn = new THREE.Vector3(gate.position.x, gate.position.y, gate.position.z + 0.1);

  // Offset the two lanes perpendicular to the run so they read as separate.
  const lane = new THREE.Vector3().subVectors(gateIn, clientPos).normalize();
  const side = new THREE.Vector3(-lane.z, 0, lane.x).multiplyScalar(0.075);
  const reqA = new THREE.Vector3().addVectors(clientPos, side);
  const reqB = new THREE.Vector3().addVectors(gateIn, side);
  const resA = new THREE.Vector3().subVectors(gateIn, side);
  const resB = new THREE.Vector3().subVectors(clientPos, side);
  path([reqA, reqB], GOLD, 0.24);
  path([resA, resB], TEAL, 0.24);
  const announceLine = path([new THREE.Vector3(0, 0.9, 0), hubPos], HUB, 0.16);

  // Dots moving along a lane. Cheap, and it shows direction the way an arrow
  // head never quite does at this scale.
  function flow(from, to, color, count, size) {
    const geo = track(new THREE.BufferGeometry());
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(count * 3), 3));
    const points = new THREE.Points(
      geo,
      track(new THREE.PointsMaterial({
        color,
        size,
        transparent: true,
        opacity: 0.9,
        depthWrite: false,
        sizeAttenuation: true,
        blending: THREE.AdditiveBlending,
      })),
    );
    root.add(points);
    const arr = geo.attributes.position.array;
    return {
      points,
      update(time, speed, active) {
        points.visible = active;
        if (!active) return;
        for (let i = 0; i < count; i += 1) {
          const u = ((time * speed + i / count) % 1);
          arr[i * 3] = from.x + (to.x - from.x) * u;
          arr[i * 3 + 1] = from.y + (to.y - from.y) * u;
          arr[i * 3 + 2] = from.z + (to.z - from.z) * u;
        }
        geo.attributes.position.needsUpdate = true;
      },
    };
  }

  const reqFlow = flow(reqA, reqB, GOLD, mobile ? 4 : 7, 0.055);
  const resFlow = flow(resA, resB, TEAL, mobile ? 4 : 7, 0.055);

  function packet(color, size) {
    const mesh = new THREE.Mesh(
      track(new THREE.SphereGeometry(size, 12, 12)),
      track(new THREE.MeshBasicMaterial({ color })),
    );
    mesh.visible = false;
    root.add(mesh);
    return mesh;
  }

  const reqDot = packet(GOLD, 0.062);
  const resDot = packet(TEAL, 0.062);
  const annDot = packet(HUB, 0.055);

  // The signed receipt leaving the edge.
  const seal = new THREE.Mesh(
    track(new THREE.TorusGeometry(0.1, 0.022, 8, 24)),
    track(new THREE.MeshStandardMaterial({
      color: GOLD,
      emissive: GOLD,
      emissiveIntensity: 0.9,
      roughness: 0.3,
    })),
  );
  seal.visible = false;
  root.add(seal);

  // The deploy request descending onto a free slot.
  const bundle = new THREE.Mesh(
    track(new RoundedBoxGeometry(0.34, 0.34, 0.34, 2, 0.03)),
    track(new THREE.MeshStandardMaterial({
      color: 0x3a3020,
      metalness: 0.55,
      roughness: 0.35,
      emissive: GOLD,
      emissiveIntensity: 0.3,
    })),
  );
  bundle.visible = false;
  root.add(bundle);

  const bundleEdge = new THREE.LineSegments(
    track(new THREE.EdgesGeometry(new THREE.BoxGeometry(0.34, 0.34, 0.34))),
    track(new THREE.LineBasicMaterial({ color: GOLD, transparent: true, opacity: 0.9 })),
  );
  bundleEdge.visible = false;
  root.add(bundleEdge);

  // The slot it is heading for, so the fall reads as a placement.
  const slotMark = new THREE.Mesh(
    track(new THREE.PlaneGeometry(0.5, 0.44)),
    track(new THREE.MeshBasicMaterial({
      color: GOLD, transparent: true, opacity: 0, side: THREE.DoubleSide,
      depthWrite: false, blending: THREE.AdditiveBlending,
    })),
  );
  slotMark.rotation.x = -Math.PI / 2;
  slotMark.visible = false;
  root.add(slotMark);

  // ------------------------------------------------------------------ tenants
  const simSlot = 6;  // front row, right of centre — clear of every label
  if (live) {
    liveTenants.forEach((row, i) => {
      if (i < cells.length) {
        cells[i].running = true;
        cells[i].slug = row.slug || "";
      }
    });
  }

  // Empty anchors so each label sits where it points, not at a mesh centroid.
  const anchorTenants = new THREE.Object3D();
  anchorTenants.position.set(-shellW / 2 + 0.25, 0.24 + 0.56, shellD / 2 + 0.1);
  const anchorIso = new THREE.Object3D();
  anchorIso.position.set(-shellW / 2 + 0.2, 0.24 + shellH + 0.16, -shellD / 2);
  const anchorHub = new THREE.Object3D();
  anchorHub.position.set(hubPos.x, hubPos.y + 0.3, hubPos.z);
  root.add(anchorTenants, anchorIso, anchorHub);

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  if (!reduced) {
    composer.addPass(
      new UnrealBloomPass(new THREE.Vector2(1, 1), mobile ? 0.3 : 0.42, 0.55, 0.68),
    );
  }
  composer.addPass(new OutputPass());

  function resize() {
    const w = Math.max(1, host.clientWidth || 640);
    const h = Math.max(1, host.clientHeight || 520);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(host);

  // Pointer parallax. Small enough that the diagram stays legible, enough that
  // it reads as a solid in space.
  let parX = 0;
  let parY = 0;
  let parTX = 0;
  let parTY = 0;
  const onPointer = (event) => {
    const rect = host.getBoundingClientRect();
    parTX = ((event.clientX - rect.left) / Math.max(1, rect.width) - 0.5) * 2;
    parTY = ((event.clientY - rect.top) / Math.max(1, rect.height) - 0.5) * 2;
  };
  if (!reduced && !mobile) host.addEventListener("pointermove", onPointer);

  const projector = new THREE.Vector3();
  function project(obj, el) {
    if (!el) return;
    obj.getWorldPosition(projector);
    projector.project(camera);
    const w = host.clientWidth || 1;
    const h = host.clientHeight || 1;
    const x = (projector.x * 0.5 + 0.5) * w;
    const y = (-projector.y * 0.5 + 0.5) * h;
    const on = projector.z < 1 && x > 0 && x < w && y > 0 && y < h;
    el.style.opacity = on ? "1" : "0";
    if (!on) return;
    // A label pushed off the stage points at nothing; keep it inside the frame.
    const halfW = (el.offsetWidth || 120) / 2;
    const clampedX = Math.min(Math.max(x, halfW + 10), w - halfW - 10);
    const clampedY = Math.min(Math.max(y, (el.offsetHeight || 34) + 12), h - 10);
    // The inline transform replaces the stylesheet's translate(-50%,-120%), so
    // the centring has to be restated here or every label hangs down-right of
    // the thing it labels instead of floating above it.
    el.style.transform = `translate(calc(${clampedX}px - 50%), calc(${clampedY}px - 120%))`;
  }

  let t = live && !liveEmpty ? 14.8 : 0;
  let raf = 0;
  let lastHud = "";
  let lastPhase = "";
  let admitFlare = 0;
  const clock = new THREE.Clock();
  const tmpA = new THREE.Vector3();
  const tmpB = new THREE.Vector3();

  function cellTop(i) {
    const c = cells[i] || cells[0];
    return tmpB.set(c.x, 0.24 + 0.46, c.z);
  }

  function frame() {
    raf = requestAnimationFrame(frame);
    const dt = Math.min(clock.getDelta(), 0.05);
    t += reduced ? dt * 0.3 : dt;
    const cycleT = live ? (t % CYCLE) : t % CYCLE;
    const phase = phaseAt(cycleT);
    // With nothing hosted there is nothing live to depict, so the cycle narrates
    // the deploy path instead of freezing on IDLE. It never lights a cell that a
    // real roster did not report.
    const narrating = !live || liveEmpty;
  

    // In SIM the first slot is the one being deployed this cycle; in LIVE the
    // roster decides and the cycle only drives the request/receipt animation.
    const started = narrating
      && ["START", "INVOKE", "RECEIPT", "ANNOUNCE", "STEADY"].includes(phase);
    let runningCount = 0;
    cells.forEach((cell, i) => {
      const on = narrating ? (i === simSlot && started) : cell.running;
      if (on) runningCount += 1;
      const target = on ? 0.85 : 0.0;
      cell.lampMat.emissiveIntensity += (target - cell.lampMat.emissiveIntensity) * 0.1;
      cell.outline.material.opacity = on ? 0.75 : 0.3;
      // Activity bar ticks per cell, out of phase, so the rack looks busy
      // rather than synchronised.
      cell.barMat.opacity = on
        ? 0.35 + (reduced ? 0.2 : (Math.sin(t * 2.4 + i * 2.1) * 0.5 + 0.5) * 0.5)
        : 0;
      const bob = on && !reduced ? Math.sin(t * 1.6 + i * 1.3) * 0.004 : 0;
      cell.body.position.y = 0.24 + 0.21 + bob;
      cell.lamp.position.y = 0.24 + 0.43 + bob;
      cell.outline.position.y = cell.body.position.y;
    });

    floorUniforms.uTime.value = t;
    floorUniforms.uCells.value = Math.min(runningCount, 6) / 6;

    // ADMIT is the one moment the boundary is asserted rather than assumed.
    const flare = phase === "ADMIT" ? 1 : 0;
    admitFlare += (flare - admitFlare) * (reduced ? 1 : 0.07);
    shellEdge.material.opacity = 0.2 + admitFlare * 0.4;
    brackets.material.opacity = 0.75 + admitFlare * 0.25;
    footprint.material.opacity = 0.42 + admitFlare * 0.45;
    // The sweep parks at the top unless something is crossing the boundary.
    const sweeping = (phase === "ADMIT" || phase === "DEPLOY") && !reduced;
    scanPlane.visible = sweeping;
    if (sweeping) {
      scanPlane.position.set(0, 0.25 + ((t * 0.45) % 1) * (shellH - 0.02), 0);
      scanPlane.material.opacity = 0.06 + admitFlare * 0.07;
    }

    // deploy request descending onto the slot
    if (narrating && (phase === "DEPLOY" || phase === "ADMIT")) {
      bundle.visible = true;
      const u = phase === "DEPLOY" ? span(2.2, 4.6, cycleT) : 1;
      const dest = cellTop(simSlot);
      // Ease in, so it lands rather than drifting down at constant speed.
      const drop = u * u;
      bundle.position.set(dest.x, THREE.MathUtils.lerp(1.85, dest.y + 0.22, drop), dest.z);
      bundle.rotation.y = t * 1.2;
      bundle.material.emissiveIntensity = 0.25 + u * 0.55;
      bundleEdge.visible = true;
      bundleEdge.position.copy(bundle.position);
      bundleEdge.rotation.copy(bundle.rotation);
      slotMark.visible = true;
      slotMark.position.set(dest.x, dest.y + 0.005, dest.z);
      slotMark.material.opacity = 0.1 + (1 - u) * 0.3;
    } else {
      bundle.visible = false;
      bundleEdge.visible = false;
      slotMark.visible = false;
    }

    // request: client -> edge -> tenant
    const invoking = phase === "INVOKE";
    reqDot.visible = invoking;
    if (invoking) {
      const u = span(8.0, 10.2, cycleT);
      const dest = cellTop(narrating ? simSlot : 0);
      if (u < 0.55) reqDot.position.lerpVectors(clientPos, gateIn, u / 0.55);
      else reqDot.position.lerpVectors(gateIn, tmpA.copy(dest), (u - 0.55) / 0.45);
      gate.material.emissiveIntensity = 0.5 + Math.sin(u * Math.PI) * 0.9;
    } else {
      gate.material.emissiveIntensity = 0.45;
    }

    // receipt: tenant -> edge -> client, with the Ed25519 seal at the door
    const receipting = phase === "RECEIPT";
    resDot.visible = receipting;
    seal.visible = receipting;
    if (receipting) {
      const u = span(10.2, 12.4, cycleT);
      const from = cellTop(narrating ? simSlot : 0);
      if (u < 0.45) resDot.position.lerpVectors(tmpA.copy(from), gateIn, u / 0.45);
      else resDot.position.lerpVectors(gateIn, clientPos, (u - 0.45) / 0.55);
      seal.position.copy(gateIn).setY(gateIn.y + 0.34);
      seal.rotation.set(Math.PI / 2, 0, t * 1.6);
      seal.scale.setScalar(0.7 + Math.sin(u * Math.PI) * 0.5);
    }

    // announce: explicit, and only to the hub outside the boundary
    const announcing = phase === "ANNOUNCE";
    annDot.visible = announcing;
    announceLine.material.opacity = announcing ? 0.5 : 0.14;
    if (announcing) {
      const u = span(12.4, 14.6, cycleT);
      const from = cellTop(narrating ? simSlot : 0);
      annDot.position.lerpVectors(tmpA.copy(from).setY(from.y + 0.4), hubPos, u);
    }

    // Lanes run whenever the edge is live; the single bright packet marks the
    // one call the cycle is narrating.
    const laneOn = !reduced && (narrating ? started : true);
    reqFlow.update(t, 0.22, laneOn);
    resFlow.update(t, 0.22, laneOn);

    hub.rotation.y = reduced ? 0.4 : t * 0.5;
    client.rotation.y = reduced ? 0.2 : -t * 0.4;
    hub.position.y = hubPos.y + (reduced ? 0 : Math.sin(t * 0.8) * 0.05);

    if (!reduced && !mobile) {
      parX += (parTX - parX) * 0.05;
      parY += (parTY - parY) * 0.05;
      root.rotation.y = parX * 0.09;
      root.rotation.x = parY * 0.035;
    }

    controls.update();
    composer.render();

    project(anchorTenants, opts.tagInside);
    project(anchorIso, opts.tagMembrane);
    project(anchorHub, opts.tagHub);

    if (phase !== lastPhase || t < 0.05) {
      lastPhase = phase;
      const pack = labels();
      // Badge what is actually on screen: an empty roster means the picture is
      // a model, even though the probe did reach the host.
      const mode = live && !liveEmpty ? "LIVE" : "SIM";
      const note = live
        ? (liveEmpty ? pack.hud_live_empty : (pack.hud_live_n || "").replace("{n}", String(liveTenants.length)))
        : (opts.roster && opts.roster.mode === "UNREACHABLE" ? pack.hud_off : pack.hud_sim);
      const key2 = `${mode}|${phase}|${note}`;
      if (key2 !== lastHud) {
        lastHud = key2;
        onHud({
          mode,
          phase,
          tenants: liveTenants.length,
          note: note || "",
          phaseLabel: pack[`phase_${phase}`] || phase,
        });
      }
    }
  }

  frame();

  const onVis = () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else raf = requestAnimationFrame(frame);
  };
  document.addEventListener("visibilitychange", onVis);

  return {
    ok: true,
    live,
    destroy() {
      cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", onVis);
      host.removeEventListener("pointermove", onPointer);
      ro.disconnect();
      controls.dispose();
      composer.dispose();
      renderer.dispose();
      trash.forEach((item) => {
        if (item.dispose) item.dispose();
      });
    },
  };
}
