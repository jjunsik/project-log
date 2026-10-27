// Separate dev HTML entry. No preview controls or fixtures are imported by main.tsx.
if(import.meta.env.DEV) void import('./dev/ExperiencePreview').then(module=>module.startPreview());
else document.getElementById('root')!.textContent='개발 전용 합성 미리보기입니다.';
