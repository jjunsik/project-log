import {describe, expect, it} from 'vitest';
import {uploadBatch} from './Materials';
import type {UploadItem} from './Materials';

// Synthetic File-like objects: batch policy only. Browser File/Drop paths use real E2E.
const files = (names: string[]): UploadItem[] => names.map(name => ({name, file: {name} as File}));
describe('independent sequential file uploads', () => {
  it('stops at the first failure and retains the earlier successes', async () => {
    const attempts: string[] = [], stored: string[] = [];
    const result = await uploadBatch(files(['A.txt', 'B.txt', 'C.exe', 'D.md', 'E.pdf']), async file => {
      attempts.push(file.name);
      if (file.name === 'C.exe') throw new Error('지원하지 않는 파일 형식입니다.');
      stored.push(file.name);
    });
    expect(attempts).toEqual(['A.txt', 'B.txt', 'C.exe']);
    expect(stored).toEqual(['A.txt', 'B.txt']);
    expect(result).toEqual({filename: 'C.exe', reason: '지원하지 않는 파일 형식입니다.', notUploaded: ['C.exe', 'D.md', 'E.pdf']});
  });
  it('rejects folders at their position in a batch without discarding earlier files', async () => {
    const stored: string[] = [];
    const result = await uploadBatch([...files(['A.md']), {name: 'folder', file: null}, ...files(['B.md'])], async file => {stored.push(file.name);});
    expect(stored).toEqual(['A.md']);
    expect(result?.notUploaded).toEqual(['folder', 'B.md']);
    expect(result?.reason).toContain('폴더 업로드는 지원하지 않습니다');
  });
  it('accepts all distinct files independently', async () => {
    const stored: string[] = [];
    expect(await uploadBatch(files(['A.txt', 'B.md']), async file => {stored.push(file.name);})).toBeNull();
    expect(stored).toEqual(['A.txt', 'B.md']);
  });
});
