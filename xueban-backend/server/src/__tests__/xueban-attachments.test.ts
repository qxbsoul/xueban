import { describe, expect, it } from 'vitest';
import { buildChatAttachments, validateChatAttachments } from '../services/xueban-attachments';
import { toOpenAiMessages } from '../transport/api-clients/openai-compat-api-client';

const file = (name: string, mimetype: string, content: string, size?: number) => ({
  originalname: name, mimetype, buffer: Buffer.from(content), size: size ?? Buffer.byteLength(content),
});

describe('聊天附件与多模态契约', () => {
  it('TXT 按 UTF-8 解码，图片转为 data URL', () => {
    const result = validateChatAttachments([file('需求.txt', 'text/plain', '真实需求'), file('图.png', 'image/png', 'png-bytes')]);
    expect(result[0]).toMatchObject({ kind: 'text', text: '真实需求' });
    expect(result[1].data_url).toBe('data:image/png;base64,cG5nLWJ5dGVz');
  });

  it('同步校验只接受图片/视频/文本；文档交给异步解析', async () => {
    expect(() => validateChatAttachments([file('伪装.pdf', 'image/png', 'x')])).toThrow(/格式不支持/);
    expect(() => validateChatAttachments([file('文档.pdf', 'application/pdf', 'x')])).toThrow(/格式不支持/);
    const parsed = await buildChatAttachments(
      [file('讲义.pdf', 'application/pdf', 'pdf-bytes'), file('需求.txt', 'text/plain', '真实需求')],
      async () => 'PDF 解析出的学情内容',
    );
    expect(parsed[0]).toMatchObject({ kind: 'text', name: '讲义.pdf', text: 'PDF 解析出的学情内容' });
    expect(parsed[1]).toMatchObject({ kind: 'text', name: '需求.txt', text: '真实需求' });
  });

  it('文档解析失败会明确报错，不静默发送空附件', async () => {
    await expect(buildChatAttachments(
      [file('损坏.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'bad')],
      async () => { throw new Error('解析失败'); },
    )).rejects.toThrow(/解析失败/);
  });

  it('图片和视频使用 MiMo 官方 content parts', () => {
    const [message] = toOpenAiMessages([{ role: 'user', blocks: [
      { kind: 'image', data_url: 'data:image/png;base64,AA' },
      { kind: 'video', data_url: 'data:video/mp4;base64,BB' },
      { kind: 'text', text: '分析附件' },
    ] }]);
    expect(message).toMatchObject({ role: 'user', content: [
      { type: 'image_url', image_url: { url: 'data:image/png;base64,AA' } },
      { type: 'video_url', video_url: { url: 'data:video/mp4;base64,BB' }, fps: 2 },
      { type: 'text', text: '分析附件' },
    ] });
  });
});
