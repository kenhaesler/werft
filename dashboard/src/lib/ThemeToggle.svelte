<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';

  type Theme = 'light' | 'dark';
  let theme: Theme = 'light';

  function setTheme(next: Theme, persist = true) {
    theme = next;
    document.documentElement.dataset.theme = next;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', next === 'dark' ? '#10131d' : '#f7f9fc');
    if (persist) {
      try {
        localStorage.setItem('werft_theme', next);
      } catch {
        // The current session still changes theme if storage is unavailable.
      }
    }
  }

  function toggleTheme() {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  }

  onMount(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem('werft_theme');
    } catch {
      // Use the system preference when storage is unavailable.
    }
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    setTheme(
      stored === 'dark' || stored === 'light' ? stored : prefersDark ? 'dark' : 'light',
      false,
    );
  });

  $: label = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
</script>

<button class="theme-toggle" type="button" aria-label={label} title={label} onclick={toggleTheme}>
  <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
</button>
