/** HESTIA hero: a hearth bowl, a contained fire, tenant orbs that only exist because they were placed. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

export function mountHearth(canvas) {
  const host = canvas.parentElement || canvas;
  const mobile = matchMedia("(max-width: 820px), (pointer: coarse)").matches;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(1);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(mobile ? 42 : 36, 1, 0.1, 80);
  camera.position.set(0.2, 1.35, 5.2);

  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.enablePan = false;
  controls.autoRotate = !reduced;
  controls.autoRotateSpeed = 0.42;
  controls.target.set(0, 0.35, 0);
  controls.minDistance = 3.2;
  controls.maxDistance = 8;

  const ember = new THREE.PointLight(0xff9a3c, 22, 18);
  ember.position.set(0, 0.9, 0);
  scene.add(
    new THREE.AmbientLight(0x3a2414, 1.05),
    new THREE.HemisphereLight(0xffc48a, 0x1a0c06, 0.55),
    ember,
  );
  const rim = new THREE.DirectionalLight(0x8ecbff, 0.55);
  rim.position.set(-4, 2.4, 3);
  scene.add(rim);

  const system = new THREE.Group();
  scene.add(system);

  const bowl = new THREE.Mesh(
    new THREE.SphereGeometry(1.45, 48, 32, 0, Math.PI * 2, 0, Math.PI * 0.52),
    new THREE.MeshPhysicalMaterial({
      color: 0x3b2a1c,
      metalness: 0.55,
      roughness: 0.38,
      clearcoat: 0.7,
      clearcoatRoughness: 0.2,
      side: THREE.DoubleSide,
    }),
  );
  bowl.rotation.x = Math.PI;
  bowl.position.y = 0.15;
  system.add(bowl);

  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(1.52, 0.05, 12, 80),
    new THREE.MeshPhysicalMaterial({ color: 0xc47a32, metalness: 0.8, roughness: 0.22 }),
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 0.18;
  system.add(ring);

  const coal = new THREE.Mesh(
    new THREE.CircleGeometry(1.05, 40),
    new THREE.MeshStandardMaterial({ color: 0x1a0c08, emissive: 0x4a1808, emissiveIntensity: 0.6 }),
  );
  coal.rotation.x = -Math.PI / 2;
  coal.position.y = 0.2;
  system.add(coal);

  const flameMat = new THREE.MeshPhysicalMaterial({
    color: 0xff7a1a,
    emissive: 0xff5a00,
    emissiveIntensity: 1.8,
    transparent: true,
    opacity: 0.78,
    roughness: 0.2,
  });
  const flames = [0.55, 0.38, 0.26].map((h, i) => {
    const mesh = new THREE.Mesh(new THREE.ConeGeometry(0.22 - i * 0.04, h, 7), flameMat.clone());
    mesh.position.set((i - 1) * 0.18, 0.42 + h / 2, (i % 2) * 0.08);
    system.add(mesh);
    return mesh;
  });

  const tenants = [];
  const palette = [0x66f7c5, 0xffcc66, 0x8ecbff, 0xff6b8a, 0xe8c36a];
  for (let i = 0; i < 5; i += 1) {
    const orb = new THREE.Mesh(
      new THREE.SphereGeometry(0.09, 16, 16),
      new THREE.MeshPhysicalMaterial({
        color: palette[i],
        emissive: palette[i],
        emissiveIntensity: 0.7,
        roughness: 0.25,
      }),
    );
    tenants.push({ orb, phase: i * 1.15, radius: 1.95 + (i % 2) * 0.18, y: 0.7 + (i % 3) * 0.12 });
    system.add(orb);
  }

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), reduced ? 0.35 : 0.72, 0.7, 0.2);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  function resize() {
    const w = host.clientWidth || 640;
    const h = host.clientHeight || 520;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    bloom.resolution.set(w, h);
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(host);

  let t = 0;
  let raf = 0;
  function frame() {
    raf = requestAnimationFrame(frame);
    t += reduced ? 0.004 : 0.012;
    flames.forEach((mesh, i) => {
      mesh.scale.y = 1 + Math.sin(t * 6 + i) * 0.08;
      mesh.rotation.y = t * 0.4;
    });
    ember.intensity = 18 + Math.sin(t * 5) * 4;
    tenants.forEach((item) => {
      const a = item.phase + t * 0.55;
      item.orb.position.set(Math.cos(a) * item.radius, item.y + Math.sin(a * 2) * 0.08, Math.sin(a) * item.radius);
    });
    controls.update();
    composer.render();
  }
  frame();

  return {
    destroy() {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
    },
  };
}
