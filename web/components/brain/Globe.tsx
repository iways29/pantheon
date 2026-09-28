'use client';

/**
 * The brain as a globe (Step 10, owner's choice; ADR 036), drawn with three.js.
 *
 * Facts are points inside a glass ball, placed by meaning (their stored x, y,
 * z). Drag to turn it, scroll or pinch to zoom, and zoom far enough to fly
 * inside. Each status has its own shape, drawn in the shader: a filled glow
 * (active), a dot in a dashed ring (disputed), a hollow ring (superseded), a
 * diamond (held for the owner). Nothing moves on its own: the scene is drawn
 * again only when the owner moves it or a real event lights a fact.
 */

import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

import { prefersStill } from '@/lib/display';
import type { HeldClaim, MapFact, Neighbourhood } from '@/lib/types';

export interface View {
  /** Around the vertical axis, in radians. */
  azimuth: number;
  /** How far in: 1 at rest, larger closer. */
  zoom: number;
}

export interface GlobeHandle {
  zoom: (factor: number) => void;
  fitAll: () => void;
  /** Light a fact for a while: a real event touched it. */
  flash: (factId: string) => void;
}

interface Props {
  facts: MapFact[];
  held: HeldClaim[];
  neighbourhoods: Neighbourhood[];
  selected: string | null;
  onSelect: (factId: string | null) => void;
  /** The ball's radius on screen at rest, in CSS pixels, told to the dial. */
  onRadius?: (radius: number) => void;
  /** Facts shown bright (a filter); the rest are dimmed. Null: all bright. */
  bright?: Set<string> | null;
  /** Where the owner is looking, for the minimap. */
  onView?: (view: View) => void;
}

const STATUS = { active: 0, disputed: 1, superseded: 2, held: 3 } as const;
/** How long a lit fact glows after its event, in seconds. */
const GLOW_SECONDS = 90;
/** Inside this camera distance, the nearest claims are written out. */
const CLAIMS_WITHIN = 1.35;
const FOV = 45;
/** The ball fills this much of the smaller side at rest. */
const FILL = 0.33;

const VERTEX = /* glsl */ `
  attribute float status;
  attribute float glow;
  attribute float picked;
  attribute float dim;
  uniform float pixelRatio;
  uniform float scale;
  uniform float pointFade;
  uniform float depthNear;
  varying float vAlpha;
  varying float vStatus;
  varying float vGlow;
  varying float vPicked;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    float base = status > 2.5 ? 9.0 : (status > 1.5 ? 6.0 : 7.0);
    float size = (base * scale + glow * 7.0 + picked * 8.0) * 3.0 / max(-mv.z, 0.05);
    // Zoomed out only what needs the owner stays a point; a filter dims the rest.
    vAlpha = (status > 2.5 ? 1.0 : pointFade) * (dim > 0.5 ? 0.14 : 1.0);
    // Depth: facts at the back of the globe read fainter than those in front.
    vAlpha *= clamp(1.35 - (-mv.z - depthNear) * 0.55, 0.4, 1.0);
    gl_PointSize = clamp(size, 2.0, 72.0) * pixelRatio;
    vStatus = status;
    vGlow = glow;
    vPicked = picked;
  }
`;

const FRAGMENT = /* glsl */ `
  uniform vec3 gold;
  uniform vec3 verm;
  uniform vec3 grey;
  uniform vec3 ice;
  uniform float halo;
  varying float vStatus;
  varying float vGlow;
  varying float vPicked;
  varying float vAlpha;
  void main() {
    vec2 c = gl_PointCoord * 2.0 - 1.0;
    float r = length(c);
    vec3 col;
    float a;
    if (vStatus < 0.5) {
      float core = 1.0 - smoothstep(0.34, 0.46, r);
      float glowing = (1.0 - smoothstep(0.0, 1.0, r)) * halo * (0.35 + vGlow);
      a = max(core, glowing);
      col = gold;
    } else if (vStatus < 1.5) {
      float core = 1.0 - smoothstep(0.24, 0.32, r);
      float dash = step(0.0, sin(atan(c.y, c.x) * 8.0));
      float ring = (1.0 - smoothstep(0.05, 0.12, abs(r - 0.78))) * dash;
      a = max(core, ring);
      col = verm;
    } else if (vStatus < 2.5) {
      a = (1.0 - smoothstep(0.05, 0.13, abs(r - 0.5))) * 0.85;
      col = grey;
    } else {
      float d = abs(c.x) + abs(c.y);
      a = 1.0 - smoothstep(0.06, 0.14, abs(d - 0.66));
      col = ice;
    }
    if (vPicked > 0.5) {
      a = max(a, (1.0 - smoothstep(0.03, 0.09, abs(r - 0.95))));
      col = mix(col, ice, step(0.85, r));
    }
    a *= vAlpha;
    if (a < 0.02) discard;
    gl_FragColor = vec4(col, a);
  }
`;

/** A neighbourhood's soft glow: bigger for more facts. */
const CLOUD_VERTEX = /* glsl */ `
  attribute float size;
  uniform float pixelRatio;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(size * 140.0 / max(-mv.z, 0.05), 4.0, 900.0) * pixelRatio;
  }
`;

const CLOUD_FRAGMENT = /* glsl */ `
  uniform vec3 gold;
  uniform float fade;
  void main() {
    float r = length(gl_PointCoord * 2.0 - 1.0);
    float a = exp(-r * r * 4.0) * fade * 0.28;
    if (a < 0.005) discard;
    gl_FragColor = vec4(gold, a);
  }
`;

const GLASS_VERTEX = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const GLASS_FRAGMENT = /* glsl */ `
  uniform vec3 rim;
  uniform vec3 fill;
  uniform float fillAlpha;
  uniform float rimAlpha;
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    float facing = abs(dot(normalize(vNormal), normalize(vView)));
    float fresnel = pow(1.0 - facing, 2.6);
    // A soft light from the upper left, as on the design's lens.
    float light = pow(max(dot(normalize(vNormal), normalize(vec3(-0.5, 0.7, 0.6))), 0.0), 18.0);
    gl_FragColor = vec4(mix(fill, rim, fresnel), fillAlpha + fresnel * rimAlpha + light * 0.18);
  }
`;

function cssColor(name: string, fallback: string): THREE.Color {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  try {
    return new THREE.Color(value.startsWith('#') ? value : fallback);
  } catch {
    return new THREE.Color(fallback);
  }
}

/** Held claims have no place yet: they wait on a ring just inside the glass. */
function heldPosition(i: number, n: number): [number, number, number] {
  const angle = (i / Math.max(n, 1)) * Math.PI * 2;
  return [Math.cos(angle) * 0.9, -0.3, Math.sin(angle) * 0.9];
}

/** The ball's radius on screen at rest: room left for the dial, the agent
 * ring outside it (1.36 × the radius) and the agents' names beside the ring. */
export function restRadius(width: number, height: number): number {
  return Math.max(
    60,
    Math.min(Math.min(width, height) * FILL, (width / 2 - 110) / 1.36, (height / 2 - 40) / 1.36),
  );
}

function restDistance(width: number, height: number): number {
  const half = THREE.MathUtils.degToRad(FOV / 2);
  const radius = restRadius(width, height);
  const ratio = height / 2 / (radius * Math.tan(half));
  return Math.sqrt(1 + ratio * ratio);
}

export const Globe = forwardRef<GlobeHandle, Props>(function Globe(
  { facts, held, neighbourhoods, selected, onSelect, onRadius, bright = null, onView },
  handle,
) {
  const host = useRef<HTMLDivElement>(null);
  const labels = useRef<HTMLDivElement>(null);
  const lens = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  // Everything three.js, kept across renders.
  const world = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    controls: OrbitControls;
    points: THREE.Points;
    clouds: THREE.Points;
    glass: THREE.Mesh;
    ids: string[];
    claims: string[];
    lit: Map<number, number>;
    request: () => void;
    fit: () => void;
  } | null>(null);
  const latest = useRef({ facts, held, neighbourhoods, selected, onSelect, onRadius, onView });
  latest.current = { facts, held, neighbourhoods, selected, onSelect, onRadius, onView };

  // The scene, made once.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);
    el.appendChild(renderer.domElement);
    renderer.domElement.style.display = 'block';
    renderer.domElement.setAttribute('aria-hidden', 'true');

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(FOV, 1, 0.01, 50);
    camera.position.set(0, 0.35, 3.2);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 0.08;
    controls.maxDistance = 8;
    controls.zoomSpeed = 0.9;
    controls.rotateSpeed = 0.6;
    // The ball stays in the dial (ScaleNotes: "the dial and the agent ring
    // stay fixed"): it turns and zooms, never slides. Panning was also what
    // made a pinch drift on a phone.
    controls.enablePan = false;
    // Stop just short of the poles, where the view would flip.
    controls.minPolarAngle = 0.12;
    controls.maxPolarAngle = Math.PI - 0.12;

    const glass = new THREE.Mesh(
      new THREE.SphereGeometry(1, 96, 64),
      new THREE.ShaderMaterial({
        vertexShader: GLASS_VERTEX,
        fragmentShader: GLASS_FRAGMENT,
        transparent: true,
        depthWrite: false,
        side: THREE.DoubleSide,
        uniforms: {
          rim: { value: new THREE.Color() },
          fill: { value: new THREE.Color() },
          fillAlpha: { value: 0.1 },
          rimAlpha: { value: 0.6 },
        },
      }),
    );
    glass.renderOrder = 2;
    scene.add(glass);

    const geometry = new THREE.BufferGeometry();
    const points = new THREE.Points(
      geometry,
      new THREE.ShaderMaterial({
        vertexShader: VERTEX,
        fragmentShader: FRAGMENT,
        transparent: true,
        depthWrite: false,
        uniforms: {
          pixelRatio: { value: renderer.getPixelRatio() },
          scale: { value: 1 },
          pointFade: { value: 1 },
          depthNear: { value: 2 },
          gold: { value: new THREE.Color() },
          verm: { value: new THREE.Color() },
          grey: { value: new THREE.Color() },
          ice: { value: new THREE.Color() },
          halo: { value: 1 },
        },
      }),
    );
    points.renderOrder = 1;
    scene.add(points);

    const clouds = new THREE.Points(
      new THREE.BufferGeometry(),
      new THREE.ShaderMaterial({
        vertexShader: CLOUD_VERTEX,
        fragmentShader: CLOUD_FRAGMENT,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        uniforms: {
          pixelRatio: { value: renderer.getPixelRatio() },
          gold: { value: new THREE.Color() },
          fade: { value: 0.4 },
        },
      }),
    );
    clouds.renderOrder = 0;
    scene.add(clouds);

    let frame = 0;
    let touched = false;
    controls.addEventListener('start', () => {
      touched = true;
    });
    const request = () => {
      if (!frame) frame = requestAnimationFrame(draw);
    };

    function paint() {
      const dark = document.documentElement.dataset.theme !== 'light';
      const pm = points.material as THREE.ShaderMaterial;
      pm.uniforms.gold!.value = cssColor('--gold', '#E8BC62');
      pm.uniforms.verm!.value = cssColor('--verm', '#FF8466');
      pm.uniforms.grey!.value = cssColor('--ink3', '#8189A0');
      pm.uniforms.ice!.value = cssColor('--ice', '#94BBFF');
      pm.uniforms.halo!.value = dark ? 1 : 0.55;
      pm.blending = dark ? THREE.AdditiveBlending : THREE.NormalBlending;
      pm.needsUpdate = true;
      const cm = clouds.material as THREE.ShaderMaterial;
      cm.uniforms.gold!.value = cssColor('--gold', '#E8BC62');
      cm.blending = dark ? THREE.AdditiveBlending : THREE.NormalBlending;
      cm.needsUpdate = true;
      const gm = glass.material as THREE.ShaderMaterial;
      // Dark: blue glass the points glow through. Light: pearl, lit at the rim.
      gm.uniforms.rim!.value = dark ? new THREE.Color('#c9d8ff') : new THREE.Color('#9fb6e8');
      gm.uniforms.fill!.value = dark ? new THREE.Color('#2a3766') : new THREE.Color('#ffffff');
      gm.uniforms.fillAlpha!.value = dark ? 0.07 : 0.34;
      gm.uniforms.rimAlpha!.value = dark ? 0.35 : 0.4;
      // Dark glass is drawn over the points (they glow through it); the pale
      // light glass under them, or it would wash them out.
      glass.renderOrder = dark ? 2 : -1;
      request();
    }

    let last = { w: 0, h: 0 };
    function size() {
      const w = el!.clientWidth;
      const h = el!.clientHeight;
      if (!w || !h) return;
      // A pixel or two of layout jitter must not make the ball twitch.
      if (Math.abs(w - last.w) < 3 && Math.abs(h - last.h) < 3) return;
      last = { w, h };
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      // Until the owner moves it, the ball keeps its place inside the dial.
      if (!touched) {
        camera.position.setLength(restDistance(w, h));
        controls.update();
      }
      latest.current.onRadius?.(restRadius(w, h));
      request();
    }

    function fit() {
      const w = el!.clientWidth || 800;
      const h = el!.clientHeight || 600;
      const distance = restDistance(w, h);
      const start = camera.position.clone();
      const startTarget = controls.target.clone();
      const end = new THREE.Vector3(0, 0.35, 3.2).setLength(distance);
      touched = false;
      if (prefersStill()) {
        camera.position.copy(end);
        controls.target.set(0, 0, 0);
        controls.update();
        request();
        return;
      }
      const t0 = performance.now();
      const step = () => {
        const t = Math.min(1, (performance.now() - t0) / 700);
        const e = 1 - Math.pow(1 - t, 3);
        camera.position.lerpVectors(start, end, e);
        controls.target.lerpVectors(startTarget, new THREE.Vector3(), e);
        controls.update();
        request();
        if (t < 1) requestAnimationFrame(step);
      };
      step();
    }

    const lit = new Map<number, number>();
    const project = new THREE.Vector3();
    const toCamera = new THREE.Vector3();

    function draw() {
      frame = 0;
      // The ball's centre is pinned: whatever the input, it never leaves the dial.
      if (controls.target.lengthSq() > 0) controls.target.set(0, 0, 0);
      const moved = controls.update();
      const w0 = el!.clientWidth || 800;
      const h0 = el!.clientHeight || 600;
      const zoom = restDistance(w0, h0) / Math.max(camera.position.distanceTo(controls.target), 0.01);
      // Levels of detail (ScaleNotes): clouds with counts, points, claims.
      const pointFade = THREE.MathUtils.clamp((zoom - 0.55) / 0.25, 0, 1);
      (points.material as THREE.ShaderMaterial).uniforms.pointFade!.value = pointFade;
      (clouds.material as THREE.ShaderMaterial).uniforms.fade!.value = 1 - pointFade * 0.55;
      latest.current.onView?.({ azimuth: Math.atan2(camera.position.x, camera.position.z), zoom });
      const fromCentre = camera.position.length();
      (points.material as THREE.ShaderMaterial).uniforms.depthNear!.value = Math.max(fromCentre - 1, 0);
      placeLens(w0, h0, fromCentre);
      // Glow fades over GLOW_SECONDS; the loop runs only while something glows.
      const glow = geometry.getAttribute('glow') as THREE.BufferAttribute | undefined;
      let glowing = false;
      if (glow && lit.size) {
        const now = performance.now() / 1000;
        for (const [index, at] of lit) {
          const left = 1 - (now - at) / GLOW_SECONDS;
          if (left <= 0) {
            lit.delete(index);
            glow.setX(index, 0);
          } else {
            glow.setX(index, left);
            glowing = true;
          }
        }
        glow.needsUpdate = true;
      }
      renderer.render(scene, camera);
      overlay();
      if (moved || glowing) request();
    }

    // The lens glass (Field.dc.html): the rim refracts, blurring what lies
    // under it; a lit body and two highlights. Drawn over the canvas, sized
    // to the ball on screen; gone once the owner is inside.
    const centre = new THREE.Vector3();
    function placeLens(w: number, h: number, fromCentre: number) {
      const box = lens.current;
      if (!box) return;
      if (fromCentre < 1.08) {
        box.style.display = 'none';
        return;
      }
      centre.set(0, 0, 0).project(camera);
      const half = THREE.MathUtils.degToRad(FOV / 2);
      const r = (h / 2) / (Math.sqrt(fromCentre * fromCentre - 1) * Math.tan(half));
      const x = (centre.x * 0.5 + 0.5) * w;
      const y = (-centre.y * 0.5 + 0.5) * h;
      box.style.display = 'block';
      box.style.left = `${(x - r).toFixed(1)}px`;
      box.style.top = `${(y - r).toFixed(1)}px`;
      box.style.width = box.style.height = `${(2 * r).toFixed(1)}px`;
    }

    // Names of neighbourhoods, and close up the nearest claims, as HTML.
    function overlay() {
      const box = labels.current;
      if (!box) return;
      const w = el!.clientWidth;
      const h = el!.clientHeight;
      const distance = camera.position.distanceTo(controls.target);
      const zoom = restDistance(w, h) / Math.max(distance, 0.01);
      const { neighbourhoods: hoods, facts: all } = latest.current;
      const nodes: string[] = [];
      toCamera.copy(camera.position).normalize();
      for (const n of hoods) {
        project.set(n.x, n.y, n.z ?? 0);
        const behind = project.dot(toCamera) < -0.2;
        project.project(camera);
        if (project.z > 1) continue;
        const x = (project.x * 0.5 + 0.5) * w;
        const y = (-project.y * 0.5 + 0.5) * h;
        const counted = zoom < 0.8 ? `<small>${n.size} ${n.size === 1 ? 'fact' : 'facts'}</small>` : '';
        nodes.push(
          `<span class="hood${behind ? ' behind' : ''}" style="transform:translate(${x.toFixed(1)}px,${y.toFixed(1)}px)">${escape(n.label)}${counted}</span>`,
        );
      }
      if (distance < CLAIMS_WITHIN) {
        const near: { d: number; x: number; y: number; claim: string }[] = [];
        for (const f of all) {
          if (f.x === null || f.y === null) continue;
          project.set(f.x, f.y, f.z ?? 0);
          const d = project.distanceTo(camera.position);
          project.project(camera);
          if (project.z > 1 || Math.abs(project.x) > 0.8 || Math.abs(project.y) > 0.8) continue;
          near.push({
            d,
            x: (project.x * 0.5 + 0.5) * w,
            y: (-project.y * 0.5 + 0.5) * h,
            claim: f.claim,
          });
        }
        near.sort((a, b) => a.d - b.d);
        for (const c of near.slice(0, 5)) {
          nodes.push(
            `<span class="claim-tag" style="transform:translate(${(c.x + 10).toFixed(1)}px,${(c.y - 10).toFixed(1)}px)">${escape(c.claim)}</span>`,
          );
        }
      }
      box.innerHTML = nodes.join('');
    }

    // Picking by screen distance: exact for points, cheap at 10,000.
    function pick(event: PointerEvent): number | null {
      const rect = renderer.domElement.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      const pos = geometry.getAttribute('position') as THREE.BufferAttribute | undefined;
      if (!pos) return null;
      let best: number | null = null;
      let bestD = 14;
      for (let i = 0; i < pos.count; i++) {
        project.fromBufferAttribute(pos, i).project(camera);
        if (project.z > 1) continue;
        const x = (project.x * 0.5 + 0.5) * rect.width;
        const y = (-project.y * 0.5 + 0.5) * rect.height;
        const d = Math.hypot(x - mx, y - my);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      return best;
    }

    let down: { x: number; y: number } | null = null;
    const onDown = (e: PointerEvent) => {
      down = { x: e.clientX, y: e.clientY };
    };
    const onUp = (e: PointerEvent) => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const index = pick(e);
      const w = world.current;
      latest.current.onSelect(index === null || !w ? null : (w.ids[index] ?? null));
    };
    const onMove = (e: PointerEvent) => {
      const t = tip.current;
      const w = world.current;
      if (!t || !w || e.buttons) return;
      const index = pick(e);
      if (index === null) {
        t.hidden = true;
        renderer.domElement.style.cursor = 'grab';
        return;
      }
      const rect = renderer.domElement.getBoundingClientRect();
      t.hidden = false;
      t.textContent = w.claims[index] ?? '';
      t.style.transform = `translate(${e.clientX - rect.left + 14}px, ${e.clientY - rect.top + 14}px)`;
      renderer.domElement.style.cursor = 'pointer';
    };
    const onLeave = () => {
      if (tip.current) tip.current.hidden = true;
    };
    renderer.domElement.addEventListener('pointerdown', onDown);
    renderer.domElement.addEventListener('pointerup', onUp);
    renderer.domElement.addEventListener('pointermove', onMove);
    renderer.domElement.addEventListener('pointerleave', onLeave);
    controls.addEventListener('change', request);

    const resize = new ResizeObserver(size);
    resize.observe(el);
    const theme = new MutationObserver(paint);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    world.current = {
      renderer,
      scene,
      camera,
      controls,
      points,
      clouds,
      glass,
      ids: [],
      claims: [],
      lit,
      request,
      fit,
    };
    size();
    paint();
    camera.position.setLength(restDistance(el.clientWidth || 800, el.clientHeight || 600));
    controls.update();
    request();

    return () => {
      cancelAnimationFrame(frame);
      resize.disconnect();
      theme.disconnect();
      controls.dispose();
      geometry.dispose();
      (points.material as THREE.Material).dispose();
      glass.geometry.dispose();
      clouds.geometry.dispose();
      (clouds.material as THREE.Material).dispose();
      (glass.material as THREE.Material).dispose();
      renderer.dispose();
      renderer.domElement.remove();
      world.current = null;
    };
  }, []);

  // The points, rebuilt when the facts change.
  useEffect(() => {
    const w = world.current;
    if (!w) return;
    const placed = facts.filter((f) => f.x !== null && f.y !== null);
    const n = placed.length + held.length;
    const position = new Float32Array(n * 3);
    const status = new Float32Array(n);
    const glow = new Float32Array(n);
    const picked = new Float32Array(n);
    const dim = new Float32Array(n);
    const ids: string[] = [];
    const claims: string[] = [];
    placed.forEach((f, i) => {
      position.set([f.x!, f.y!, f.z ?? 0], i * 3);
      status[i] = STATUS[f.status] ?? 0;
      dim[i] = bright && !bright.has(f.id) ? 1 : 0;
      ids.push(f.id);
      claims.push(f.claim);
    });
    held.forEach((h, j) => {
      const i = placed.length + j;
      position.set(heldPosition(j, held.length), i * 3);
      status[i] = STATUS.held;
      ids.push(`held:${h.approval_id}`);
      claims.push(`Held for you: ${h.claim}`);
    });
    const g = w.points.geometry;
    g.setAttribute('position', new THREE.BufferAttribute(position, 3));
    g.setAttribute('status', new THREE.BufferAttribute(status, 1));
    g.setAttribute('glow', new THREE.BufferAttribute(glow, 1));
    g.setAttribute('picked', new THREE.BufferAttribute(picked, 1));
    g.setAttribute('dim', new THREE.BufferAttribute(dim, 1));
    // Fewer facts, bigger points: a young brain still reads; 10,000 stay fine.
    (w.points.material as THREE.ShaderMaterial).uniforms.scale!.value = THREE.MathUtils.clamp(
      2.3 - Math.log10(Math.max(placed.length, 1)) * 0.38,
      0.75,
      2,
    );
    const cg = w.clouds.geometry;
    cg.setAttribute(
      'position',
      new THREE.BufferAttribute(new Float32Array(neighbourhoods.flatMap((h) => [h.x, h.y, h.z ?? 0])), 3),
    );
    cg.setAttribute(
      'size',
      new THREE.BufferAttribute(
        new Float32Array(neighbourhoods.map((h) => 0.12 + Math.sqrt(Math.max(h.size, 1)) * 0.035)),
        1,
      ),
    );
    g.computeBoundingSphere();
    w.ids = ids;
    w.claims = claims;
    w.lit.clear();
    w.request();
  }, [facts, held, neighbourhoods, bright]);

  // The selected fact wears a ring.
  useEffect(() => {
    const w = world.current;
    if (!w) return;
    const attr = w.points.geometry.getAttribute('picked') as THREE.BufferAttribute | undefined;
    if (!attr) return;
    for (let i = 0; i < attr.count; i++) attr.setX(i, w.ids[i] === selected ? 1 : 0);
    attr.needsUpdate = true;
    w.request();
  }, [selected, facts, held]);

  useImperativeHandle(
    handle,
    () => ({
      zoom(factor: number) {
        const w = world.current;
        if (!w) return;
        const offset = w.camera.position.clone().sub(w.controls.target);
        offset.setLength(
          THREE.MathUtils.clamp(offset.length() * factor, w.controls.minDistance, w.controls.maxDistance),
        );
        w.camera.position.copy(w.controls.target).add(offset);
        w.controls.update();
        w.request();
      },
      fitAll() {
        world.current?.fit();
      },
      flash(factId: string) {
        const w = world.current;
        if (!w) return;
        const index = w.ids.indexOf(factId);
        if (index < 0) return;
        w.lit.set(index, performance.now() / 1000);
        w.request();
      },
    }),
    [],
  );

  return (
    <div className="globe" ref={host}>
      <div className="lens" ref={lens} aria-hidden="true">
        <div className="lg-rim" />
        <div className="lg-body" />
        <div className="lg-spec" />
        <div className="lg-spec b" />
      </div>
      <div className="globe-labels" ref={labels} aria-hidden="true" />
      <div className="globe-tip" ref={tip} hidden role="tooltip" />
    </div>
  );
});

function escape(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
