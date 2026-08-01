"use client";

import { useEffect, useRef } from "react";

/**
 * Lightweight 2D canvas starfield, not a full Three.js scene. Chosen over
 * a 3D dependency deliberately: this needs to build and run correctly with
 * no browser available in the environment that wrote it, and a plain
 * canvas particle field gets most of the same visual mood (a dark sky with
 * drifting light) at a fraction of the risk and bundle size.
 */
export default function CosmicBackground() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let width = window.innerWidth;
    let height = window.innerHeight;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    type Star = { x: number; y: number; r: number; speed: number; hue: number; twinkle: number };
    let stars: Star[] = [];

    const colors = [
      "91,157,255",   // twin-accent blue
      "124,108,240",  // twin-accent-2 purple
      "242,169,59",   // twin-accent-warm amber
      "238,241,246",  // twin-text white
    ];

    function resize() {
      width = window.innerWidth;
      height = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas!.width = width * dpr;
      canvas!.height = height * dpr;
      canvas!.style.width = `${width}px`;
      canvas!.style.height = `${height}px`;
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
    let t = 0;

    function frame() {
      t += 0.01;
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
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 h-full w-full"
      aria-hidden="true"
    />
  );
}
