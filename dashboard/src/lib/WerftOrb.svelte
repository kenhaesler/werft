<script lang="ts">
  import { onMount } from 'svelte';

  let {
    size = 120,
    active = false,
    paused = false,
  }: { size?: number; active?: boolean; paused?: boolean } = $props();

  let canvas: HTMLCanvasElement;
  let stage: HTMLDivElement;
  let useFallback = $state(false);
  let refreshRendering: () => void = () => {};

  $effect(() => {
    void active;
    void paused;
    refreshRendering();
  });

  onMount(() => {
    const context = canvas.getContext('webgl', {
      alpha: true,
      antialias: false,
      depth: false,
      powerPreference: 'low-power',
    });

    if (!context) {
      useFallback = true;
      return;
    }

    const vertexSource = `
      attribute vec2 a_position;
      void main() { gl_Position = vec4(a_position, 0.0, 1.0); }
    `;
    const fragmentSource = `
      precision highp float;
      uniform vec2 u_resolution;
      uniform float u_time;
      uniform float u_active;
      mat2 rotate(float a) { float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }
      void main() {
        vec2 uv=(gl_FragCoord.xy*2.0-u_resolution.xy)/min(u_resolution.x,u_resolution.y);
        float t=u_time*.55;
        vec2 p=rotate(.12*sin(t*.7))*uv;
        p += .045*vec2(sin(t*.83),cos(t*.61));
        float angle=atan(p.y,p.x);
        float radius=length(p);
        float boundary=.55+.035*sin(angle*3.0+t)+.025*cos(angle*2.0-t*.73);
        float body=1.0-smoothstep(boundary-.12,boundary+.19,radius);
        float flow=sin(angle*2.0-t*.75+radius*2.0)*.5+.5;
        vec2 drift=vec2(.17*sin(t*.62),.16*cos(t*.49));
        float blue=exp(-5.0*dot(p-drift,p-drift));
        float cyan=exp(-7.0*dot(p-vec2(.24,.22)-drift*.4,p-vec2(.24,.22)-drift*.4));
        vec3 core=mix(vec3(.045,.28,.95),vec3(.0,.63,.98),clamp(cyan*.85+(1.0-blue)*.3,0.0,1.0));
        core=mix(core,vec3(.20,.83,.90),smoothstep(.10,.67,p.y)*.48);
        float bandRadius=.64+.06*sin(angle*2.0-t*.65)+.025*cos(angle*3.0+t*.43);
        float band=exp(-pow((radius-bandRadius)/.105,2.0));
        float haze=exp(-pow((radius-.62)/.23,2.0))*.28;
        vec3 halo=mix(vec3(.38,.83,.97),vec3(.67,.60,.96),smoothstep(-.25,.6,-p.y));
        halo=mix(halo,vec3(.62,.94,.84),smoothstep(.0,.6,p.y)*flow);
        float haloAlpha=(band*.27+haze)*(.7+.3*flow);
        float alpha=body+haloAlpha*(1.0-body);
        vec3 color=(core*body+halo*haloAlpha*(1.0-body))/max(alpha,.001);
        color=mix(color,vec3(.19,.72,1.0),u_active*.12);
        gl_FragColor=vec4(color,alpha*(1.0-smoothstep(.86,1.0,radius)));
      }
    `;

    const compile = (type: number, source: string) => {
      const shader = context.createShader(type);
      if (!shader) return null;
      context.shaderSource(shader, source);
      context.compileShader(shader);
      if (!context.getShaderParameter(shader, context.COMPILE_STATUS)) {
        context.deleteShader(shader);
        return null;
      }
      return shader;
    };
    const vertex = compile(context.VERTEX_SHADER, vertexSource);
    const fragment = compile(context.FRAGMENT_SHADER, fragmentSource);
    if (!vertex || !fragment) {
      useFallback = true;
      return;
    }
    const program = context.createProgram();
    if (!program) {
      useFallback = true;
      return;
    }
    context.attachShader(program, vertex);
    context.attachShader(program, fragment);
    context.linkProgram(program);
    if (!context.getProgramParameter(program, context.LINK_STATUS)) {
      useFallback = true;
      return;
    }

    const position = context.getAttribLocation(program, 'a_position');
    const resolution = context.getUniformLocation(program, 'u_resolution');
    const time = context.getUniformLocation(program, 'u_time');
    const activity = context.getUniformLocation(program, 'u_active');
    const buffer = context.createBuffer();
    if (position < 0 || !resolution || !time || !activity || !buffer) {
      useFallback = true;
      return;
    }
    context.bindBuffer(context.ARRAY_BUFFER, buffer);
    context.bufferData(
      context.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]),
      context.STATIC_DRAW,
    );
    context.useProgram(program);
    context.enableVertexAttribArray(position);
    context.vertexAttribPointer(position, 2, context.FLOAT, false, 0, 0);
    context.enable(context.BLEND);
    context.blendFunc(context.SRC_ALPHA, context.ONE_MINUS_SRC_ALPHA);

    let frame = 0;
    let intersecting = true;
    let documentVisible = !document.hidden;
    let reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    let lastTime = 0;
    let lastEnergyUpdate = 0;
    let energyLevel = 0;
    let sceneTime = 0;
    let lastSceneTick = 0;

    const resize = () => {
      const box = stage.getBoundingClientRect();
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);
      const width = Math.max(1, Math.round(box.width * pixelRatio));
      const height = Math.max(1, Math.round(box.height * pixelRatio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
    };
    const render = (now: number) => {
      const delta = Math.min(80, Math.max(0, now - lastEnergyUpdate));
      lastEnergyUpdate = now;
      energyLevel += ((active ? 1 : 0) - energyLevel) * Math.min(1, delta * 0.0042);
      resize();
      context.viewport(0, 0, canvas.width, canvas.height);
      context.clearColor(0, 0, 0, 0);
      context.clear(context.COLOR_BUFFER_BIT);
      context.useProgram(program);
      context.uniform2f(resolution, canvas.width, canvas.height);
      context.uniform1f(time, sceneTime * 0.001);
      context.uniform1f(activity, energyLevel);
      context.drawArrays(context.TRIANGLES, 0, 3);
    };
    const shouldAnimate = () =>
      !useFallback && !paused && !reducedMotion && intersecting && documentVisible;
    let previousFrame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = 0;
      lastSceneTick = 0;
      if (useFallback) return;
      if (shouldAnimate()) frame = requestAnimationFrame(tick);
      else render(lastTime);
    };
    const tick = (now: number) => {
      if (now - previousFrame >= 33) {
        previousFrame = now;
        if (lastSceneTick > 0) sceneTime += Math.min(80, now - lastSceneTick);
        lastSceneTick = now;
        lastTime = now;
        render(now);
      }
      if (shouldAnimate()) frame = requestAnimationFrame(tick);
    };
    const onVisibility = () => {
      documentVisible = !document.hidden;
      update();
    };
    const observer = new IntersectionObserver(
      ([entry]) => {
        intersecting = entry.isIntersecting;
        update();
      },
      { threshold: 0.01 },
    );
    const motionQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    const onMotionPreference = () => {
      reducedMotion = motionQuery?.matches ?? false;
      update();
    };
    const onContextLost = (event: Event) => {
      event.preventDefault();
      cancelAnimationFrame(frame);
      useFallback = true;
    };

    refreshRendering = update;

    observer.observe(stage);
    document.addEventListener('visibilitychange', onVisibility);
    motionQuery?.addEventListener('change', onMotionPreference);
    canvas.addEventListener('webglcontextlost', onContextLost, { once: true });
    const resizeObserver = new ResizeObserver(update);
    resizeObserver.observe(stage);
    update();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      resizeObserver.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      motionQuery?.removeEventListener('change', onMotionPreference);
      canvas.removeEventListener('webglcontextlost', onContextLost);
      context.deleteBuffer(buffer);
      context.deleteProgram(program);
      context.deleteShader(vertex);
      context.deleteShader(fragment);
      refreshRendering = () => {};
    };
  });
</script>

<div
  class="werft-orb"
  bind:this={stage}
  style:width={`${size}px`}
  style:height={`${size}px`}
  aria-hidden="true"
>
  <div class="orb-aura"></div>
  <div class="fallback-orb" class:is-hidden={!useFallback}></div>
  <canvas bind:this={canvas} class="orb-canvas" class:is-hidden={useFallback}></canvas>
</div>

<style>
  .werft-orb {
    position: relative;
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    isolation: isolate;
  }
  .orb-aura {
    position: absolute;
    inset: -20%;
    border-radius: 50%;
    background: radial-gradient(circle, #49d7ff28 0 28%, #6244ff1a 44%, transparent 71%);
    filter: blur(8px);
    z-index: -1;
  }
  .orb-canvas,
  .fallback-orb {
    width: 100%;
    height: 100%;
    border-radius: 50%;
  }
  .orb-canvas {
    display: block;
    filter: saturate(1.09) contrast(1.04);
  }
  .is-hidden {
    display: none;
  }
  .fallback-orb {
    background: radial-gradient(
      ellipse at 48% 46%,
      #0764f9 0 23%,
      #128cfa 35%,
      #61deed9c 47%,
      #b3dbef44 59%,
      transparent 70%
    );
    filter: blur(3px);
  }
  @media (prefers-reduced-motion: reduce) {
    .orb-aura {
      filter: none;
    }
  }
</style>
