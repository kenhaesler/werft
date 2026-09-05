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

      mat2 turn(float a) { float c=cos(a), s=sin(a); return mat2(c,-s,s,c); }
      float surface(vec3 p, float t) {
        p.xz = turn(.38 + t * .16) * p.xz;
        p.yz = turn(.52 + sin(t * .31) * .23) * p.yz;
        float angle = atan(p.y, p.x);
        float wave = sin(angle * 3.0 + t * .64);
        float radius = .48 + .048 * wave;
        p.z += .13 * sin(angle * 2.0 - t * .53);
        vec2 section = vec2(length(p.xy) - radius, p.z);
        section = turn(angle * 1.5 + t * .24) * section;
        section /= vec2(1.0, .74);
        return (length(section) - .235 - .028 * sin(angle * 3.0 - t * .7)) * .65;
      }
      vec3 normalAt(vec3 p, float t) {
        vec2 e = vec2(.0015,0.0);
        return normalize(vec3(surface(p+e.xyy,t)-surface(p-e.xyy,t), surface(p+e.yxy,t)-surface(p-e.yxy,t), surface(p+e.yyx,t)-surface(p-e.yyx,t)));
      }
      void main() {
        vec2 uv=(2.0*gl_FragCoord.xy-u_resolution.xy)/min(u_resolution.x,u_resolution.y);
        float t=u_time * .8;
        vec3 ro=vec3(0.0,0.0,2.8);
        vec3 rd=normalize(vec3(uv,-2.8));
        float travel=0.0;
        float distance=1.0;
        for(int i=0;i<80;i++) {
          distance=surface(ro+rd*travel,t);
          travel+=distance;
          if(abs(distance)<.001 || travel>4.0) break;
        }
        if(travel>4.0) { gl_FragColor=vec4(0.0); return; }
        vec3 p=ro+rd*travel;
        vec3 n=normalAt(p,t);
        float facing=clamp(dot(n,-rd),0.0,1.0);
        float rim=pow(1.0-facing,2.0);
        float hue=sin(p.y*2.8+p.x*1.9+t*.25+n.z*2.0)*.5+.5;
        vec3 blue=vec3(.08,.22,.86);
        vec3 violet=vec3(.56,.27,.94);
        vec3 cyan=vec3(.25,.91,.94);
        vec3 color=mix(blue,violet,smoothstep(.25,.9,hue));
        color=mix(color,cyan,smoothstep(.05,.94,n.y*.5+n.x*.3+.48));
        float light=dot(n,normalize(vec3(-.5,.8,1.1)))*.5+.5;
        color*=.55+.55*light;
        vec3 reflection=reflect(rd,n);
        float softbox=pow(max(0.0,dot(reflection,normalize(vec3(-.45,.9,1.5)))),8.0);
        color=mix(color,vec3(.86,.96,1.0),softbox*.72);
        color+=vec3(.15,.4,.48)*rim*.42;
        color+=vec3(.09,.15,.19)*u_active;
        // Subpixel coverage gives the sculpted silhouette a soft, clean edge.
        float alpha=smoothstep(0.0,.075,facing);
        gl_FragColor=vec4(color,alpha);
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
  <div class="fallback-orb" class:is-hidden={!useFallback}>
    <span class="fallback-core"></span><span class="fallback-filament"></span>
  </div>
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
    position: relative;
    overflow: hidden;
    background: radial-gradient(
      circle at 31% 24%,
      #9cf5ff 0 2%,
      #36c8ff 7%,
      #2254f1 28%,
      #16066d 70%
    );
    box-shadow:
      inset -18px -16px 29px #07034c99,
      inset 10px 9px 19px #9ff7ff55,
      0 0 23px #436cff55;
  }
  .fallback-orb::before,
  .fallback-orb::after {
    content: '';
    position: absolute;
    inset: -19%;
    border: 1px solid #b96dff9c;
    border-radius: 47% 53% 49% 51%;
    transform: rotate(-32deg);
    box-shadow: 0 0 10px #58e4ff88;
  }
  .fallback-orb::after {
    inset: 9%;
    border-color: #48ebffb0;
    transform: rotate(38deg) skewX(-17deg);
  }
  .fallback-core {
    position: absolute;
    inset: 18%;
    border-radius: 50%;
    background: radial-gradient(circle at 37% 25%, #aaf6ff, #206cff 31%, #21107c 74%);
    filter: blur(1px);
  }
  .fallback-filament {
    position: absolute;
    width: 136%;
    height: 43%;
    top: 29%;
    left: -18%;
    border: 2px solid #b38aff99;
    border-radius: 50%;
    transform: rotate(-31deg);
    box-shadow: 0 0 12px #52deff;
  }
  @media (prefers-reduced-motion: reduce) {
    .orb-aura {
      filter: none;
    }
  }
</style>
