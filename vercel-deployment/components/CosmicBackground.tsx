"use client";

import { useEffect, useRef } from "react";
import * as THREE from "three";

/**
 * Real Three.js scene, not a 2D canvas approximation -- this deployment
 * runs in a real browser (Vercel, not a build-time static export target),
 * so the risk tradeoff that kept app/frontend-next's CosmicBackground flat
 * doesn't apply here. On mount the camera starts deep in the field and
 * eases forward, like flying into the universe as the page opens, then
 * settles into a slow ambient drift. Four layered point clouds in the
 * site's own accent colors give it depth instead of one flat shell of dots.
 */
export default function CosmicBackground() {
  const mountRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020617, 0.00055);

    const camera = new THREE.PerspectiveCamera(
      70,
      window.innerWidth / window.innerHeight,
      1,
      6000,
    );
    const restZ = 500;
    const startZ = prefersReducedMotion ? restZ : 3200;
    camera.position.z = startZ;

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
  }, []);

  return <div ref={mountRef} className="absolute inset-0 h-full w-full" aria-hidden="true" />;
}
