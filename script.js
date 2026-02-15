const root = document.documentElement;
const toggle = document.getElementById('themeToggle');

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
