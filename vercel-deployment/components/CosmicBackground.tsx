"use client";

import { useEffect, useRef } from "react";
import * as THREE from "three";

/**
 * Real Three.js scene when WebGL is available: camera starts deep in a
 * layered star field and eases forward on load, like flying into the
 * universe as the page opens, then settles into a slow ambient drift.
 *
 * WebGLRenderer throws synchronously if the browser cannot create a WebGL
 * context (disabled hardware acceleration, some sandboxed/headless
 * environments, certain locked-down corporate machines) -- confirmed this
 * the hard way rendering the deployed page headless, where it took the
 * entire page down with a client-side exception. A portfolio site cannot
 * gamble a total blank-page crash on every visitor's GPU state, so 3D
 * construction is wrapped end to end and falls back to the same 2D canvas
 * starfield the AWS copy always used -- still on-brand, just flat.
 */
export default function CosmicBackground() {
  const mountRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let cleanup = () => {};

    try {
      cleanup = mount3D(mount, prefersReducedMotion);
    } catch (err) {
      console.warn("CosmicBackground: 3D scene unavailable, falling back to 2D starfield.", err);
      cleanup = mount2D(mount, prefersReducedMotion);
    }

    return () => cleanup();
  }, []);

  return <div ref={mountRef} className="absolute inset-0 h-full w-full overflow-hidden" aria-hidden="true" />;
}

function mount3D(mount: HTMLDivElement, prefersReducedMotion: boolean): () => void {
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x020617, 0.00055);

  const camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 1, 6000);
  const restZ = 500;
  const startZ = prefersReducedMotion ? restZ : 3200;
  camera.position.z = startZ;

  // Throws synchronously if a WebGL context genuinely cannot be created --
  // the caller's try/catch is what makes this safe to call at all.
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  mount.appendChild(renderer.domElement);

  const palette = [0x5b9dff, 0x7c6cf0, 0xf2a93b, 0xeef1f6];
  const layers: THREE.Points[] = [];
  const layerCount = 4;
  for (let layer = 0; layer < layerCount; layer++) {
    const count = 1100;
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const base = new THREE.Color(palette[layer % palette.length]);
    for (let i = 0; i < count; i++) {
      const radius = 400 + Math.random() * 2400;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(Math.random() * 2 - 1);
      positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
      positions[i * 3 + 2] = radius * Math.cos(phi) - 900;
      const flicker = 0.75 + Math.random() * 0.25;
      colors[i * 3] = base.r * flicker;
      colors[i * 3 + 1] = base.g * flicker;
      colors[i * 3 + 2] = base.b * flicker;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const material = new THREE.PointsMaterial({
      size: 2.1 + layer * 0.7,
      vertexColors: true,
      transparent: true,
      opacity: 0.9,
      sizeAttenuation: true,
      depthWrite: false,
    });
    const points = new THREE.Points(geometry, material);
    scene.add(points);
    layers.push(points);
  }

  let raf = 0;
  const start = performance.now();
  const introDuration = prefersReducedMotion ? 0 : 2400;

  function frame(now: number) {
    const elapsed = now - start;
    if (elapsed < introDuration) {
      const p = Math.min(elapsed / introDuration, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      camera.position.z = startZ + (restZ - startZ) * eased;
    } else if (!prefersReducedMotion) {
      camera.position.z = restZ + Math.sin(now * 0.00018) * 5;
    }

    if (!prefersReducedMotion) {
      layers.forEach((layer, i) => {
        layer.rotation.y += 0.00007 * (i + 1);
        layer.rotation.x += 0.00002 * (i + 1);
      });
    }

    renderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);

  function onResize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  }
  window.addEventListener("resize", onResize);

  return () => {
    cancelAnimationFrame(raf);
    window.removeEventListener("resize", onResize);
    layers.forEach((layer) => {
      layer.geometry.dispose();
      (layer.material as THREE.Material).dispose();
    });
    renderer.dispose();
    if (mount.contains(renderer.domElement)) mount.removeChild(renderer.domElement);
  };
}

function mount2D(mount: HTMLDivElement, prefersReducedMotion: boolean): () => void {
  const canvas = document.createElement("canvas");
  canvas.className = "absolute inset-0 h-full w-full";
  mount.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  if (!ctx) return () => mount.removeChild(canvas);

  let width = window.innerWidth;
  let height = window.innerHeight;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  type Star = { x: number; y: number; r: number; speed: number; hue: number; twinkle: number };
  let stars: Star[] = [];
  const colors = ["91,157,255", "124,108,240", "242,169,59", "238,241,246"];

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.floor((width * height) / 9000);
    stars = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      r: Math.random() * 1.4 + 0.3,
      speed: Math.random() * 0.15 + 0.02,
      hue: Math.floor(Math.random() * colors.length),
      twinkle: Math.random() * Math.PI * 2,
    }));
  }

  let raf = 0;
  function frame() {
    ctx!.clearRect(0, 0, width, height);
    for (const s of stars) {
      s.twinkle += 0.02;
      const alpha = 0.35 + Math.sin(s.twinkle) * 0.35;
      ctx!.beginPath();
      ctx!.fillStyle = `rgba(${colors[s.hue]},${Math.max(0.08, alpha)})`;
      ctx!.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx!.fill();
      if (!prefersReducedMotion) {
        s.y += s.speed;
        if (s.y > height) {
          s.y = -2;
          s.x = Math.random() * width;
        }
      }
    }
    raf = requestAnimationFrame(frame);
  }

  resize();
  frame();
  window.addEventListener("resize", resize);

  return () => {
    cancelAnimationFrame(raf);
    window.removeEventListener("resize", resize);
    if (mount.contains(canvas)) mount.removeChild(canvas);
  };
}
