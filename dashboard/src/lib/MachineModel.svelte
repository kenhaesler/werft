<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';

  let { compact = false }: { compact?: boolean } = $props();
  let stage: HTMLDivElement;
  let paused = $state(false);
  let visible = $state(true);
  let reducedMotion = $state(false);

  onMount(() => {
    if (!window.matchMedia || !window.IntersectionObserver) {
      reducedMotion = true;
      return;
    }
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const syncPreference = () => (reducedMotion = preference.matches);
    let intersecting = true;
    const syncVisibility = () => (visible = intersecting && !document.hidden);
    const observer = new IntersectionObserver(([entry]) => {
      intersecting = entry.isIntersecting;
      syncVisibility();
    });
    syncPreference();
    syncVisibility();
    observer.observe(stage);
    preference.addEventListener('change', syncPreference);
    document.addEventListener('visibilitychange', syncVisibility);
    return () => {
      observer.disconnect();
      preference.removeEventListener('change', syncPreference);
      document.removeEventListener('visibilitychange', syncVisibility);
    };
  });
</script>

<div
  class="engine-stage"
  class:compact
  class:still={paused || !visible || reducedMotion}
  bind:this={stage}
>
  <div class="scene" aria-hidden="true">
    <div class="ground"></div>
    <div class="engine">
      <div class="face lid">
        <span class="brand">W</span><span class="wordmark">WERFT ENGINE</span><span class="lid-seam"
        ></span>
      </div>
      <div class="face base"></div>
      <div class="face front">
        <span class="vents"></span><span class="indicators"><i></i><i></i></span><span class="port"
        ></span>
      </div>
      <div class="face back"><span class="vents"></span><span class="port"></span></div>
      <div class="face left"><span class="side-vents"></span></div>
      <div class="face right"><span class="side-vents"></span></div>
    </div>
  </div>
  {#if !reducedMotion}
    <button
      class="motion-toggle"
      onclick={() => (paused = !paused)}
      aria-label={paused ? 'Play machine animation' : 'Pause machine animation'}
      title={paused ? 'Play animation' : 'Pause animation'}
      ><Icon name={paused ? 'play' : 'pause'} size={14} /></button
    >
  {/if}
</div>

<style>
  .engine-stage {
    position: relative;
    height: 230px;
  }
  .scene {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    perspective: 800px;
    overflow: hidden;
    background: radial-gradient(ellipse at 50% 65%, #bcd6ff35, transparent 65%);
  }
  .engine {
    position: relative;
    width: 220px;
    height: 60px;
    transform-style: preserve-3d;
    transform: translateY(-6px) rotateX(-24deg) rotateY(-28deg);
    animation: engine-turn 12s ease-in-out infinite;
  }
  .face {
    position: absolute;
    box-sizing: border-box;
    backface-visibility: hidden;
    border: 1px solid #8ba8cd;
  }
  .lid,
  .base {
    width: 220px;
    height: 140px;
    top: -40px;
    left: 0;
  }
  .lid {
    transform: translateY(-30px) rotateX(90deg);
    background: linear-gradient(125deg, #f8fbff, #bed2eb 70%, #9cbade);
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 24px;
  }
  .brand {
    font-size: 34px;
    font-weight: 800;
    color: #325986;
    letter-spacing: -4px;
  }
  .wordmark {
    font-size: 9px;
    letter-spacing: 1.5px;
    color: #456990;
    font-weight: 600;
  }
  .lid-seam {
    position: absolute;
    inset: 9px;
    border: 1px solid #ffffff60;
  }
  .base {
    transform: translateY(30px) rotateX(-90deg);
    background: #365477;
  }
  .front,
  .back {
    width: 220px;
    height: 60px;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 18px;
    background: linear-gradient(#55769b, #28496d);
  }
  .front {
    transform: translateZ(70px);
    border-top: 3px solid #e5f0ff;
  }
  .back {
    transform: rotateY(180deg) translateZ(70px);
  }
  .left,
  .right {
    width: 140px;
    height: 60px;
    left: 40px;
    background: linear-gradient(#92aed1, #496b94);
    display: grid;
    place-items: center;
  }
  .left {
    transform: translateX(-110px) rotateY(-90deg);
  }
  .right {
    transform: translateX(110px) rotateY(90deg);
  }
  .vents {
    width: 85px;
    height: 22px;
    background: repeating-linear-gradient(
      90deg,
      #142f4e 0 2px,
      #7190b1 2px 3px,
      transparent 3px 6px
    );
  }
  .side-vents {
    width: 76px;
    height: 20px;
    background: repeating-linear-gradient(0deg, #335477 0 2px, transparent 2px 5px);
  }
  .indicators {
    display: flex;
    gap: 9px;
    margin-left: auto;
  }
  .indicators i {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: #b1e8ff;
    box-shadow: 0 0 8px #67ccff;
  }
  .indicators i + i {
    background: #5fafff;
  }
  .port {
    width: 14px;
    height: 7px;
    border: 1px solid #bbd2e9;
    background: #193550;
  }
  .ground {
    position: absolute;
    width: 240px;
    height: 55px;
    top: calc(50% + 45px);
    border-radius: 50%;
    background: radial-gradient(ellipse, #375c8b35, transparent 70%);
    filter: blur(8px);
  }
  .motion-toggle {
    position: absolute;
    right: 0;
    bottom: 12px;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border: 1px solid var(--border);
    border-radius: 50%;
    color: var(--text-secondary);
    background: var(--surface, #fff);
    cursor: pointer;
  }
  .motion-toggle:hover {
    color: #245eff;
    border-color: #99baff;
  }
  .motion-toggle:focus-visible {
    outline: 2px solid #245eff;
    outline-offset: 3px;
  }
  .still .engine {
    animation-play-state: paused;
  }
  .compact {
    height: 170px;
  }
  .compact .scene {
    scale: 0.78;
    overflow: visible;
  }
  @keyframes engine-turn {
    0%,
    100% {
      transform: translateY(-6px) rotateX(-24deg) rotateY(-28deg);
    }
    50% {
      transform: translateY(-13px) rotateX(-30deg) rotateY(22deg);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .engine {
      animation: none;
    }
  }
</style>
