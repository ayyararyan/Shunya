const root = document.documentElement;
const toggle = document.getElementById('themeToggle');
const heroBrandImage = document.getElementById('heroBrandImage');

const storedTheme = localStorage.getItem('shunya-theme');
const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const initialDark = storedTheme ? storedTheme === 'dark' : prefersDark;

if (initialDark) {
  root.classList.add('dark');
}

if (toggle) {
  toggle.textContent = root.classList.contains('dark') ? '☀' : '☾';

  toggle.addEventListener('click', () => {
    const dark = root.classList.toggle('dark');
    localStorage.setItem('shunya-theme', dark ? 'dark' : 'light');
    toggle.textContent = dark ? '☀' : '☾';
  });
}

async function pickExistingBrandImage() {
  if (!heroBrandImage) return;

  const candidates = [
    './assets/shunya-brand.png',
    './assets/shunya-logo.png',
    './assets/shunya-dual-logo.png',
    './assets/shunya-brand.jpg',
    './assets/shunya-logo.jpg',
    './assets/shunya-logo.jpeg',
    './assets/shunya-logo.webp',
    './assets/shunya-logo.svg'
  ];

  for (const path of candidates) {
    try {
      const response = await fetch(path, { method: 'HEAD' });
      if (response.ok) {
        heroBrandImage.src = path;
        return;
      }
    } catch (_) {
      // Ignore and continue to next candidate.
    }
  }
}

pickExistingBrandImage();
