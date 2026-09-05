import './app.css';
import themeCss from './theme.css?inline';
import { mount } from 'svelte';
import App from './App.svelte';

function initializeTheme() {
  try {
    const stored = localStorage.getItem('werft_theme');
    const theme =
      stored === 'dark' || stored === 'light'
        ? stored
        : window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light';
    document.documentElement.dataset.theme = theme;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', theme === 'dark' ? '#10131d' : '#f7f9fc');
  } catch {
    // Storage may be unavailable in private or embedded browser contexts.
  }
}

const themeStyle = document.createElement('style');
themeStyle.textContent = themeCss;
document.head.append(themeStyle);

initializeTheme();

const target = document.getElementById('app');
if (!target) {
  throw new Error('#app element not found in index.html');
}

const app = mount(App, { target });

export default app;
