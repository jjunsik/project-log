// Runs before the application and stylesheet so the first paint uses the saved theme.
(() => {
  const storageKey = 'project-log.theme';
  const valid = value => ['system', 'light', 'dark'].includes(value);
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (valid(saved)) preference = saved;
  } catch { /* Storage restrictions do not prevent rendering. */ }
  const apply = () => {
    const theme = preference === 'system' ? (system.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  };
  window.projectLogTheme = {
    getPreference: () => preference,
    setPreference(value) {
      if (!valid(value)) return;
      preference = value;
      apply();
      try { window.localStorage.setItem(storageKey, value); }
      catch {
        // Discard an older selection if writing fails and removal is still permitted.
        try { window.localStorage.removeItem(storageKey); } catch { /* Keep the in-memory selection. */ }
      }
    },
    watchSystem() {
      system.addEventListener('change', apply);
      apply(); // Also covers an OS change between bootstrap and subscription.
      return () => system.removeEventListener('change', apply);
    },
  };
  apply();
})();
