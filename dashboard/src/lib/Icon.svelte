<script lang="ts">
  let {
    name,
    size = 18,
    stroke = 1.65,
  }: { name: string; size?: number; stroke?: number } = $props();
  const paths: Record<string, string> = {
    overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    agent:
      'M12 3v3 M9 3h6 M6 7h12a2 2 0 0 1 2 2v10H4V9a2 2 0 0 1 2-2Z M8 11v2 M16 11v2 M9 16h6 M1 11v5 M23 11v5',
    projects:
      'M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z M3 9h18',
    review: 'm8 12 3 3 5-6 M9 3H5v18h14V3h-4 M9 2h6v4H9z',
    vm: 'M4 3h16v7H4z M4 14h16v7H4z M7 6h.01 M7 17h.01 M15 6h2 M15 17h2',
    activity: 'M2 12h4l3-8 6 16 3-8h4',
    quota: 'M4 19V9 M10 19V4 M16 19v-7 M22 19H2',
    settings:
      'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z M10 2h4l1 3 3 1 3 2v4l-3 2-1 3-2 3h-4l-2-3-3-1-3-2v-4l3-2 1-3z',
    search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z m5 12 6 6',
    plus: 'M12 5v14 M5 12h14',
    arrow: 'M5 12h14 m-5-5 5 5-5 5',
    up: 'M12 19V5 m-6 6 6-6 6 6',
    chevron: 'm9 5 7 7-7 7',
    down: 'm6 9 6 6 6-6',
    close: 'm6 6 12 12 M6 18 18 6',
    check: 'm5 12 4 4L19 6',
    branch:
      'M6 3v12a4 4 0 0 0 4 4h3 M6 3a2 2 0 1 0 0 .01 M18 17a2 2 0 1 0 0 .01 M18 3v4a5 5 0 0 1-5 5H6 M18 3a2 2 0 1 0 0 .01',
    clock: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z M12 6v6l4 2',
    external: 'M14 3h7v7 M21 3 10 14 M10 3H3v18h18v-7',
    terminal: 'm4 6 6 6-6 6 M13 18h7',
    file: 'M14 2H4v20h16V8l-6-6Z M14 2v6h6 M8 13h8 M8 17h5',
    cpu: 'M6 6h12v12H6z M9 9h6v6H9z M9 2v4 M15 2v4 M9 18v4 M15 18v4 M2 9h4 M2 15h4 M18 9h4 M18 15h4',
    memory: 'M3 6h18v12H3z M7 10h3v4H7z M14 10h3v4h-3z M6 18v3 M10 18v3 M14 18v3 M18 18v3',
    globe: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z M2 12h20 M12 2c-6 6-6 14 0 20 6-6 6-14 0-20Z',
    shield: 'm12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4Z m-4 10 3 3 5-6',
    pause: 'M7 5v14 M17 5v14',
    play: 'm7 4 14 8-14 8V4Z',
    refresh: 'M20 8a9 9 0 0 0-16-2L2 8 M2 3v5h5 M4 16a9 9 0 0 0 16 2l2-2 M22 21v-5h-5',
    download: 'M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5',
    logout: 'M9 3H3v18h6 M9 12h12 m-5-5 5 5-5 5',
    menu: 'M3 6h18 M3 12h18 M3 18h18',
    spark: 'm12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2Z',
    stop: 'M5 5h14v14H5z',
    warning: 'm12 3 10 18H2L12 3Z M12 9v5 M12 17h.01',
    copy: 'M9 9h12v12H9z M5 15H3V3h12v2',
    link: 'm10 13 4-4 M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
  };
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width={stroke}
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"><path d={paths[name] ?? paths.file} /></svg
>
