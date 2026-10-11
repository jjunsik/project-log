import {useEffect, useState} from 'react';

type ThemePreference = 'system' | 'light' | 'dark';
declare global {
  interface Window {
    projectLogTheme: {
      getPreference: () => ThemePreference;
      setPreference: (value: ThemePreference) => void;
      watchSystem: () => () => void;
    };
  }
}

export default function ThemeControl() {
  const [preference,setPreference] = useState(() => window.projectLogTheme.getPreference());
  useEffect(() => {
    if (preference === 'system') return window.projectLogTheme.watchSystem();
  }, [preference]);
  return <label className="theme-control"><span>테마</span><select aria-label="테마" value={preference} onChange={e => {
    const next = e.target.value as ThemePreference;
    window.projectLogTheme.setPreference(next);
    setPreference(next);
  }}><option value="system">시스템</option><option value="light">라이트</option><option value="dark">다크</option></select></label>;
}
