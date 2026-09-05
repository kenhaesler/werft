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

      #define PI 3.14159265359

      mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
      vec3 rotateX(vec3 p, float a) { p.yz = rot(a) * p.yz; return p; }
      vec3 rotateY(vec3 p, float a) { p.xz = rot(a) * p.xz; return p; }
      float smin(float a, float b, float k) { float h = max(k - abs(a - b), 0.0) / k; return min(a, b) - h * h * h * k / 6.0; }
      float torus(vec3 p, vec2 t) { vec2 q = vec2(length(p.xz) - t.x, p.y); return length(q) - t.y; }
      vec3 breathe(vec3 p, float t, float seed) {
        p += vec3(
          sin(p.y * 5.0 + t * (1.13 + seed) + seed * 8.0),
          sin(p.z * 4.0 - t * (0.81 + seed) + seed * 3.0),
          sin(p.x * 6.0 + t * (0.67 + seed) + seed * 5.0)
        ) * .026;
        return p;
      }

      vec2 mapScene(vec3 p, float t) {
        vec3 q = p;
        q = breathe(q, t, .0);
        float azimuth = atan(q.z, q.x) + sin(q.y * 5.0 + t * .84) * .14;
        float elevation = atan(q.y, length(q.xz)) + sin(azimuth * 3.0 - t * .63) * .10;
        float radius = .575 + sin(azimuth * 4.0 + elevation * 3.0 + t * .67) * .026;
        float shell = abs(length(q) - radius) - .013;
        float longitude = (abs(sin(azimuth * 9.0 + sin(elevation * 4.0 + t) * .7)) - .105) * .052;
        float latitude = (abs(sin(elevation * 7.0 + sin(azimuth * 3.0 - t) * .55)) - .11) * .052;
        float core = max(shell, min(longitude, latitude));
        vec3 ringA = breathe(rotateY(rotateX(p, .72 + sin(t * .4) * .18), -.34 + sin(t * .37) * .10), t, .21);
        vec3 ringB = breathe(rotateY(rotateX(p, -1.03 + sin(t * .53) * .12), .68 + sin(t * .29) * .22), t, .49);
        vec3 ringC = breathe(rotateY(rotateX(p, .05 + sin(t * .31) * .15), 1.35 - sin(t * .33) * .20), t, .78);
        float a = torus(ringA, vec2(.80, .027));
        float b = torus(ringB, vec2(.72, .021));
        float c = torus(ringC, vec2(.88, .017));
        vec3 innerRing = breathe(rotateY(rotateX(p, .85 + sin(t * .72) * .16), t * .38), t, .94);
        float inner = torus(innerRing, vec2(.235, .016));
        float distance = core;
        float material = 1.0;
        if (a < distance) { distance = a; material = 2.0; }
        if (b < distance) { distance = b; material = 3.0; }
        if (c < distance) { distance = c; material = 4.0; }
        if (inner < distance) { distance = inner; material = 5.0; }
        return vec2(distance, material);
      }
      vec3 normalAt(vec3 p, float t) {
        vec2 e = vec2(.0022, 0.0);
        return normalize(vec3(
          mapScene(p + e.xyy, t).x - mapScene(p - e.xyy, t).x,
          mapScene(p + e.yxy, t).x - mapScene(p - e.yxy, t).x,
          mapScene(p + e.yyx, t).x - mapScene(p - e.yyx, t).x
        ));
      }
      vec2 march(vec3 ro, vec3 rd, float t) {
        float travel = 0.0;
        float material = 0.0;
        for (int i = 0; i < 60; i++) {
          vec2 hit = mapScene(ro + rd * travel, t);
          if (hit.x < .0015) { material = hit.y; break; }
          travel += hit.x * .72;
          if (travel > 4.4) break;
        }
        return vec2(travel, material);
      }
      float shadow(vec3 ro, vec3 rd, float t) {
        float strength = 1.0, travel = .025;
        for (int i = 0; i < 18; i++) {
          float h = mapScene(ro + rd * travel, t).x;
          strength = min(strength, 11.0 * h / travel);
          travel += clamp(h, .018, .12);
          if (h < .001 || travel > 2.0) break;
        }
        return clamp(strength, .22, 1.0);
      }
      void main() {
        vec2 uv = (gl_FragCoord.xy * 2.0 - u_resolution.xy) / min(u_resolution.x, u_resolution.y);
        uv.y *= -1.0;
        float energy = u_active;
        float t = u_time * .48;
        float cameraTurn = t * .15;
        vec3 ro = vec3(0.0, .035, 2.55);
        ro = rotateY(ro, cameraTurn);
        vec3 target = vec3(0.0, 0.0, 0.0);
        vec3 forward = normalize(target - ro);
        vec3 right = normalize(cross(vec3(0.0, 1.0, 0.0), forward));
        vec3 up = cross(forward, right);
        vec3 rd = normalize(forward * 2.25 + right * uv.x + up * uv.y);
        vec2 hit = march(ro, rd, t);
        if (hit.y < .5) {
          float glow = exp(-3.4 * max(0.0, length(uv) - .46));
          gl_FragColor = vec4(vec3(.10, .39, 1.0) * glow, glow * .055);
          return;
        }
        vec3 position = ro + rd * hit.x;
        vec3 normal = normalAt(position, t);
        vec3 key = normalize(vec3(-.55, .72, 1.0));
        float diffuse = max(0.0, dot(normal, key));
        float rim = pow(1.0 - max(0.0, dot(normal, -rd)), 3.1);
        float occlusion = shadow(position + normal * .008, key, t);
        vec3 color;
        if (hit.y < 1.5) {
          float latitude = atan(position.z, position.x) * 4.0 + position.y * 12.0 - t * 2.2;
          float liquid = sin(latitude + sin(position.y * 17.0 + t) * 1.7) * .5 + .5;
          float vein = pow(max(0.0, sin(latitude * 2.4 - t * 2.0)), 15.0);
          color = mix(vec3(.12, .035, .005), vec3(1.0, .42, .035), .46 + diffuse * .54);
          color += mix(vec3(1.0, .55, .10), vec3(.35, .72, 1.0), liquid) * (.20 + .28 * energy);
          color += vec3(1.0, .79, .27) * vein * (.35 + .45 * energy);
          color *= .84 + diffuse * .45;
          color *= occlusion;
        } else {
          float ringTone = hit.y == 2.0 ? 0.0 : hit.y == 3.0 ? .35 : hit.y == 4.0 ? .70 : .13;
          color = mix(vec3(.12, .72, 1.0), vec3(1.0, .39, .045), ringTone);
          color *= .55 + diffuse * .75;
          color += vec3(.64, .91, 1.0) * rim * .85;
          color *= 1.0 + energy * .42;
        }
        float highlight = pow(max(0.0, dot(reflect(rd, normal), key)), 28.0);
        color += vec3(1.0, .83, .43) * highlight * .95;
        color += vec3(.15, .53, 1.0) * rim * (.18 + energy * .30);
        float alpha = hit.y < 1.5 ? .86 : .92;
        gl_FragColor = vec4(color, alpha);
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
